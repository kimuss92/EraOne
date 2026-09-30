"""era_telegram.py — two-way Telegram chat with Era, from any network.

WHY LONG-POLLING, NOT A WEBHOOK OR THE LAN BRIDGE
Era's existing remote entry point (`/remote_voice`, Flask on 0.0.0.0:5005) only
works when your phone is on the same Wi-Fi as the laptop. A webhook would need a
public HTTPS URL — port forwarding, a static IP, or a tunnel.

Telegram's `getUpdates` needs none of that. Era's machine makes an OUTBOUND HTTPS
call to api.telegram.org that blocks until you send a message. Outbound works from
any network, behind any router, with no inbound port open. Era only has to be
running.

SECURITY — THIS IS A REMOTE COMMAND CHANNEL
Anyone can find a bot and message it. Era acts on the real machine (mouse, files,
shell, publishing to live social accounts), so an unauthenticated message here is a
remote-code-execution hole. Therefore:

  * Every update is dropped unless `message.chat.id` EXACTLY equals TELEGRAM_CHAT_ID.
  * The check is on the numeric chat id, not the username (usernames are mutable and
    spoofable in display; the id is not).
  * Unauthorized senders get NO reply — a reply confirms the bot is live and tells an
    attacker their probe was received.
  * We never echo the bot token, and never log message text of rejected senders.

Configure via env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (both already used by Era's
push notifications). Without them this module stays dormant and Era runs unchanged.
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
from pathlib import Path

import requests

_API = "https://api.telegram.org"

# Where the getUpdates cursor lives. Telegram re-delivers any update we never
# acknowledge, so persisting the offset stops Era re-running your last order every
# time she restarts. Not under memory/ — that path is guarded against writes.
_OFFSET_FILE = Path(__file__).with_name("runtime") / "telegram_offset.json"

# Telegram holds the request open this long waiting for a message, then answers
# with an empty list and we immediately ask again.
#
# WHY 25s AND NOT 50s — THIS IS THE LATENCY FIX
# A long poll is an idle TCP connection. Home routers, corporate firewalls and
# mobile carriers silently evict idle NAT entries (often around 30-60s) WITHOUT
# sending a RST: Telegram then writes your message into a socket that no longer
# reaches us, and we sit there seeing nothing until our own read timeout fires.
# With a 50s poll behind a 65s read timeout that meant a message could take over
# a minute to land — the "poll error ... Read timed out (read timeout=65)" line,
# followed by a slow command. Re-establishing the connection every 25s keeps it
# under the usual idle-eviction window, and caps the worst case if one dies.
_POLL_TIMEOUT_S = 25

# Split connect from read. One scalar timeout applies to BOTH, so the old 65s
# value also allowed a 65s stall just opening a socket. Connecting is fast or
# broken; only the read is legitimately slow (it's holding the long poll open).
_CONNECT_TIMEOUT_S = 10
_POLL_TIMEOUTS = (_CONNECT_TIMEOUT_S, _POLL_TIMEOUT_S + 10)
_SEND_TIMEOUTS = (_CONNECT_TIMEOUT_S, 20)
_UPLOAD_TIMEOUTS = (_CONNECT_TIMEOUT_S, 120)

# Telegram hard-caps a text message at 4096 chars.
_MAX_TEXT = 4000

# Outbound sends get their own session, kept separate from the poller's: the
# poll loop throws its session away whenever a connection goes stale (see
# poll_forever), and that must never yank the connection out from under a reply
# or an asset upload running on another thread.
_session = requests.Session()


# ── configuration ────────────────────────────────────────────────────────────


def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def _chat_id() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def enabled() -> bool:
    """True when both the bot token and the owner's chat id are configured."""
    return bool(_token() and _chat_id())


def _url(method: str) -> str:
    return f"{_API}/bot{_token()}/{method}"


# ── outbound ─────────────────────────────────────────────────────────────────


def _reset_send_session() -> None:
    """Throw away the outbound session's pooled sockets and start fresh.

    Era replies minutes apart, so the keep-alive connection sitting in the pool
    has usually been idle long enough for a router or carrier to have evicted it
    WITHOUT telling us (the same silent NAT eviction that plagues the long poll —
    see _POLL_TIMEOUT_S). We only discover it by writing into the dead socket and
    waiting out the read timeout, which is why a reply Era had already composed
    would die with "sendMessage failed: Read timed out". A dead socket is never
    worth retrying ON; it has to be replaced.
    """
    global _session
    old, _session = _session, requests.Session()
    try:
        old.close()
    except Exception:
        pass


def send_message(text: str, *, chat_id: str | None = None) -> bool:
    """Send plain text to the owner. Plain text, not MarkdownV2: captions and file
    paths are full of characters Markdown would choke on, and a formatting error
    that silently drops a Gate-2 review message is worse than unstyled text.

    Retried once on a connection fault, because the overwhelmingly likely cause is
    a stale pooled socket (above) and the second attempt — on a new connection —
    succeeds. NOTE the accepted risk: the Bot API has no idempotency key, so if the
    request DID reach Telegram and only the response was lost, the retry posts the
    message twice. A duplicate reply is a wart; a Gate-2 review that silently never
    arrives is a job you think is waiting on you and isn't. We take the wart.
    """
    if not enabled() or not text:
        return False
    body = text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 3] + "..."
    payload = {"chat_id": chat_id or _chat_id(), "text": body}

    for attempt in (1, 2):
        try:
            r = _session.post(_url("sendMessage"), json=payload, timeout=_SEND_TIMEOUTS)
            if r.status_code == 200:
                return True
            # A non-200 is Telegram REFUSING the message (bad chat id, too long,
            # blocked). The connection is fine and a retry would be refused
            # identically, so don't burn one.
            print(f"[TG] sendMessage -> HTTP {r.status_code}: {r.text[:120]}")
            return False
        except (requests.Timeout, requests.ConnectionError) as e:
            print(f"[TG] sendMessage {type(e).__name__}{' — retrying' if attempt == 1 else ''}")
            _reset_send_session()
            if attempt == 2:
                print(f"[TG] sendMessage failed after retry: {e}")
                return False
        except Exception as e:
            print(f"[TG] sendMessage failed: {e}")
            return False
    return False


def send_asset(path, caption: str = "") -> bool:
    """Push a produced asset (reel/carousel image) to the owner for Gate-2 review.

    Picks the right Telegram method from the file type so a video arrives playable
    and an image arrives viewable. Falls back to sendDocument, which accepts
    anything — better a file you can open than a silent failure on an unusual codec.
    """
    if not enabled():
        return False
    p = Path(path)
    if not p.is_file():
        print(f"[TG] send_asset: no such file: {p}")
        return False

    mime = (mimetypes.guess_type(p.name)[0] or "").lower()
    if mime.startswith("video/"):
        method, field = "sendVideo", "video"
    elif mime.startswith("image/"):
        method, field = "sendPhoto", "photo"
    else:
        method, field = "sendDocument", "document"

    data = {"chat_id": _chat_id()}
    if caption:
        # Telegram caps captions at 1024 chars.
        data["caption"] = caption[:1024]

    # Same one-shot retry as send_message, and for the same reason: a review asset
    # lost to a stale pooled socket is a draft you never get to approve. The file is
    # reopened per attempt — a half-consumed handle cannot be re-uploaded.
    for attempt in (1, 2):
        try:
            with p.open("rb") as fh:
                r = _session.post(
                    _url(method), data=data, files={field: (p.name, fh)}, timeout=_UPLOAD_TIMEOUTS
                )
            if r.status_code != 200:
                print(f"[TG] {method} -> HTTP {r.status_code}")
                return False
            return True
        except (requests.Timeout, requests.ConnectionError) as e:
            print(f"[TG] {method} {type(e).__name__}{' — retrying' if attempt == 1 else ''}")
            _reset_send_session()
            if attempt == 2:
                print(f"[TG] send_asset failed after retry: {e}")
                return False
        except Exception as e:
            print(f"[TG] send_asset failed: {e}")
            return False
    return False


# ── inbound (long-poll) ──────────────────────────────────────────────────────


def _load_offset() -> int:
    try:
        return int(json.loads(_OFFSET_FILE.read_text(encoding="utf-8"))["offset"])
    except Exception:
        return 0


def _save_offset(offset: int) -> None:
    try:
        _OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _OFFSET_FILE.write_text(json.dumps({"offset": offset}), encoding="utf-8")
    except Exception as e:
        print(f"[TG] could not persist offset: {e}")


def _authorized(update: dict) -> tuple[bool, dict]:
    """(is_owner, message). The ONLY gate between the public internet and a command
    that runs on this machine. Compares the numeric chat id as a string, exactly.

    Returns the whole message rather than just its text, because a voice note
    carries its payload in `voice`/`audio`/`video_note` and an update with no
    `text` is no longer the same thing as an update with nothing in it.
    """
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    sender_id = str(chat.get("id", "")).strip()
    if not sender_id or sender_id != _chat_id():
        return False, {}
    return True, msg


# ── voice notes ──────────────────────────────────────────────────────────────
# Telegram's own cap on what a bot may download. Anything larger cannot be
# fetched at all, so refusing early gives the sender a real reason instead of an
# opaque API error.
_MAX_VOICE_BYTES = 20 * 1024 * 1024

# Where a downloaded note lands before transcription. Under runtime/ for the same
# reason as the offset cursor: memory/ is write-guarded.
_VOICE_DIR = Path(__file__).with_name("runtime") / "telegram_voice"


def _voice_payload(msg: dict) -> dict | None:
    """The audio attachment of a message, whichever field carries it.

    `voice` is the held-to-record note (Opus), `audio` a shared music/voice file,
    `video_note` the round video message — all three are the user talking, and a
    command should not depend on which button they happened to press. `document`
    is deliberately excluded: a forwarded file is data to look at, not an order
    to obey, and treating it as one would let any forwarded recording issue
    commands on this machine.
    """
    for field in ("voice", "audio", "video_note"):
        payload = msg.get(field)
        if isinstance(payload, dict) and payload.get("file_id"):
            return payload
    return None


def download_file(file_id: str, dest_dir: Path | None = None) -> Path:
    """Fetch a Telegram attachment to disk and return the local path.

    Two calls: getFile resolves the id to a server-side path, then the file
    endpoint serves the bytes. Raises RuntimeError with a readable message —
    every caller has a chat to report the failure into.
    """
    r = _session.get(_url("getFile"), params={"file_id": file_id}, timeout=_SEND_TIMEOUTS)
    payload = r.json() if r.status_code == 200 else {}
    if not payload.get("ok"):
        raise RuntimeError(
            f"getFile failed: {payload.get('description') or f'HTTP {r.status_code}'}"
        )

    result = payload.get("result") or {}
    remote = result.get("file_path") or ""
    if not remote:
        raise RuntimeError("Telegram returned no file path for that recording.")
    size = int(result.get("file_size") or 0)
    if size > _MAX_VOICE_BYTES:
        raise RuntimeError(f"That recording is too large to fetch ({size // (1024 * 1024)} MB).")

    out_dir = dest_dir or _VOICE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    # Keep the server-side extension: it is what tells the decoder the container.
    dest = out_dir / f"{int(time.time())}_{Path(remote).name}"

    # NOTE the token is in this URL by design (it is how the file endpoint
    # authenticates) — which is exactly why the URL is never printed or logged.
    with _session.get(
        f"{_API}/file/bot{_token()}/{remote}", stream=True, timeout=_UPLOAD_TIMEOUTS
    ) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"Download failed with HTTP {resp.status_code}.")
        written = 0
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(65536):
                written += len(chunk)
                if written > _MAX_VOICE_BYTES:
                    fh.close()
                    dest.unlink(missing_ok=True)
                    raise RuntimeError("That recording is too large to fetch.")
                fh.write(chunk)
    return dest


def transcribe_voice_message(msg: dict) -> str:
    """Turn a voice message into the text of the order it carries.

    Returns "" when the note held no intelligible speech. Raises RuntimeError
    with a user-readable reason on any failure — this runs on the poller thread,
    where a swallowed error is a command that vanished without a word.
    """
    payload = _voice_payload(msg)
    if payload is None:
        raise RuntimeError("There's no audio in that message.")
    if int(payload.get("file_size") or 0) > _MAX_VOICE_BYTES:
        raise RuntimeError("That recording is too large for me to fetch.")

    path = download_file(payload["file_id"])
    try:
        from core.transcribe import TranscriptionError, transcribe_file

        try:
            return transcribe_file(path)
        except TranscriptionError as e:
            raise RuntimeError(str(e)) from e
    finally:
        # The audio has served its purpose the moment it is text. Keeping it
        # would accumulate recordings of the owner on disk for no benefit.
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


def preflight() -> dict:
    """Identify the bot and check nothing else is stealing its updates.

    Telegram delivers a bot's updates to EITHER a webhook OR getUpdates, never both:
    while a webhook is registered, every getUpdates call returns 409 and Era receives
    nothing. A webhook we did not set also means a third party is reading every
    message sent to this bot — a token-compromise signal, not just a config error.
    So we say so loudly at startup instead of leaving a bare 409 in the log.

    Best-effort and never raises; returns {} when the check itself fails.
    """
    if not enabled():
        return {}
    info = {}
    try:
        me = _session.get(_url("getMe"), timeout=15).json()
        if me.get("ok"):
            info["username"] = me["result"].get("username", "")
            print(f"[TG] bot @{info['username']} authenticated.")
        else:
            # 401 = the token is wrong or revoked. Flag it so the caller can stop
            # rather than retry something that cannot succeed.
            info["auth_failed"] = me.get("error_code") == 401
            print(f"[TG] getMe rejected the token: {me.get('description')}")
            return info

        hook = _session.get(_url("getWebhookInfo"), timeout=15).json()
        url = (hook.get("result") or {}).get("url") or ""
        info["webhook_url"] = url
        if url:
            print(
                "[TG] *** A WEBHOOK IS REGISTERED ON THIS BOT ***\n"
                f"[TG]     {url}\n"
                "[TG] Telegram will send your messages THERE, not to Era, and every\n"
                "[TG] getUpdates call will fail with 409. If you did not set that URL,\n"
                "[TG] someone else holds this bot token: revoke it in @BotFather now.\n"
                "[TG] To hand this bot to Era, call deleteWebhook first."
            )
    except Exception as e:
        print(f"[TG] preflight check failed (continuing anyway): {e}")
    return info


def _handle_voice_message(msg: dict, on_command) -> None:
    """Transcribe one voice note and run it as a command. Never raises.

    Echoes the transcript back before acting, because a voice command has a
    failure mode a typed one does not: being HEARD WRONG. Seeing "Heard: close
    brave" is what lets the user tell a mistranscription apart from Era refusing
    the order — without it, a wrong action looks like a bug in the action.
    """
    try:
        text = transcribe_voice_message(msg)
    except Exception as e:
        print(f"[TG] voice note failed: {e}")
        send_message(f"I couldn't make out that voice note — {e}")
        return

    if not text:
        send_message("I couldn't hear any speech in that one, sir.")
        return

    print(f"[TG] voice command from owner: {text[:80]!r}")
    send_message(f"Heard: {text}")
    try:
        on_command(text)
    except Exception as e:
        print(f"[TG] on_command raised (voice): {e}")
        send_message(f"That failed on my side: {e}")


def poll_forever(on_command, *, stop_event: threading.Event | None = None) -> None:
    """Block forever, handing each authorized message text to `on_command`.

    Runs on its own daemon thread. Never raises: a network blip must not kill Era's
    only remote channel, so every error backs off and retries.
    """
    if not enabled():
        print("[TG] disabled: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to enable.")
        return

    # A bad token can never fix itself by waiting, so don't even open the loop —
    # otherwise Era spends the whole session retrying a revoked token every 60s.
    info = preflight()
    if info.get("auth_failed"):
        print(
            "[TG] bridge STOPPED: this token is not valid.\n"
            "[TG] Most likely Era was launched from a shell started BEFORE you ran "
            "setx, so she inherited the old value — a running process keeps the "
            "environment it was born with. Close that shell, open a new one, and "
            "restart Era. Check with: python era_telegram.py doctor"
        )
        return

    offset = _load_offset()
    print(f"[TG] listening (long-poll) from offset {offset}; locked to chat {_chat_id()}")
    backoff = 1.0

    # The poller's own session (see _session above). Recycled — not reused — after
    # any connection-level failure: the pooled keep-alive socket that just timed out
    # is exactly the dead one we must not poll on again, and urllib3 would happily
    # hand it back to us.
    poll_session = requests.Session()
    stale_polls = 0

    while not (stop_event and stop_event.is_set()):
        try:
            try:
                r = poll_session.get(
                    _url("getUpdates"),
                    params={"offset": offset, "timeout": _POLL_TIMEOUT_S},
                    timeout=_POLL_TIMEOUTS,
                )
            except (requests.Timeout, requests.ConnectionError) as e:
                # NOT an error worth backing off from. A long poll sitting on a
                # connection the network quietly dropped is the normal failure mode
                # of this design (see _POLL_TIMEOUT_S), and the only cure is to
                # reconnect AT ONCE — every second spent sleeping here is a second a
                # message you already sent is stuck at Telegram, undelivered. The old
                # code ran these through the exponential backoff below, so a flaky
                # link escalated to a 60s blind spot and orders landed a minute late.
                #
                # Nothing is lost by retrying immediately: `offset` is unchanged, so
                # Telegram re-serves whatever it queued while we were disconnected.
                stale_polls += 1
                if stale_polls in (1, 5) or stale_polls % 25 == 0:
                    print(f"[TG] poll connection dropped ({type(e).__name__}); reconnecting")
                poll_session.close()
                poll_session = requests.Session()
                continue
            stale_polls = 0
            if r.status_code == 401:
                # Revoked mid-session (or rotated). No amount of retrying helps.
                print(
                    "[TG] bridge STOPPED: token rejected (401). Restart Era with a "
                    "valid TELEGRAM_BOT_TOKEN."
                )
                return
            if r.status_code == 409:
                # A webhook, or a second Era, owns this bot's updates. Retrying in a
                # tight loop would just fight it, so back off loudly — and re-run the
                # preflight so the log names the culprit instead of a bare "409".
                print("[TG] 409 conflict — something else owns this bot's updates.")
                preflight()
                time.sleep(30)
                continue
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            payload = r.json()
            if not payload.get("ok"):
                raise RuntimeError(payload.get("description", "not ok"))
            backoff = 1.0

            updates = payload.get("result", [])
            for update in updates:
                # Advance the cursor for EVERY update, authorized or not. An update we
                # refuse to act on must still be acknowledged, or Telegram redelivers
                # it forever and a stranger's message wedges the queue permanently.
                offset = max(offset, int(update.get("update_id", 0)) + 1)

                ok, msg = _authorized(update)
                if not ok:
                    # Deliberately silent: no reply, no message text logged.
                    print("[TG] dropped update from unauthorized chat")
                    continue

                text = (msg.get("text") or "").strip()

                if not text and _voice_payload(msg) is not None:
                    # A voice note becomes TEXT here and then travels the ordinary
                    # typed path, so standby, the wake word, the safety gate, CM
                    # routing and confirmations all behave identically to a message
                    # you typed. Transcription is a network round-trip; run it on a
                    # worker so a slow one cannot stall the poll loop and delay
                    # every command behind it.
                    threading.Thread(
                        target=_handle_voice_message,
                        args=(dict(msg), on_command),
                        name="era-telegram-voice",
                        daemon=True,
                    ).start()
                    continue

                if not text:
                    send_message("I can act on text and voice notes — that message had neither.")
                    continue

                print(f"[TG] command from owner: {text[:80]!r}")
                try:
                    on_command(text)
                except Exception as e:
                    print(f"[TG] on_command raised: {e}")
                    send_message(f"That failed on my side: {e}")

            # Only on a real delivery. An idle poll changes nothing, and rewriting the
            # cursor file every cycle just to store the same number is pure disk churn.
            if updates:
                _save_offset(offset)

        except Exception as e:
            # Reached only by genuine API-level faults now (bad HTTP status, ok:false) —
            # connection drops are handled above without any sleep. Something the server
            # is refusing SHOULD be backed off from; a socket that died should not.
            print(f"[TG] poll error ({e}); retrying in {backoff:.0f}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


def start(on_command) -> threading.Thread | None:
    """Spawn the poller on a daemon thread. Returns None when not configured."""
    if not enabled():
        return None
    t = threading.Thread(target=poll_forever, args=(on_command,), name="era-telegram", daemon=True)
    t.start()
    return t


# ── CLI ──────────────────────────────────────────────────────────────────────
# The community-management agent runs as a SEPARATE headless process, so it cannot
# call the functions above in-process. This CLI is how it pushes a finished draft to
# you for Gate-2 review:
#
#   python era_telegram.py asset "C:\...\reel_1.mp4" --caption "reel 1 — Nero Stellar"
#   python era_telegram.py send "Week 28 plan is ready for review."
#
# It inherits TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from Era's environment. Prints
# JSON so the caller can tell success from a silent no-op, and exits non-zero on
# failure so a broken push can't be mistaken for a delivered review.


def _main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="era_telegram", description="Send to Era's owner on Telegram."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_send = sub.add_parser("send", help="Send a text message.")
    p_send.add_argument("text")

    p_asset = sub.add_parser("asset", help="Send a file (reel/image) for review.")
    p_asset.add_argument("path")
    p_asset.add_argument("--caption", default="")

    sub.add_parser("doctor", help="Check the token, and whether a webhook is stealing updates.")

    args = ap.parse_args(argv)

    if not enabled():
        print(json.dumps({"ok": False, "error": "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set"}))
        return 1

    if args.cmd == "doctor":
        info = preflight()
        # Voice notes need two things the text path does not: a decoder, and a
        # transcription key. Both fail at USE time, i.e. when you are away from
        # the machine and least able to investigate — so report them here.
        try:
            from core.transcribe import ffmpeg_path
            from core.utils import get_transcription_api_key

            info["ffmpeg"] = bool(ffmpeg_path())
            try:
                info["transcription_key"] = bool(get_transcription_api_key())
            except Exception:
                info["transcription_key"] = False
        except Exception as e:
            info["voice_check_error"] = str(e)[:120]
        info["voice_ready"] = bool(info.get("ffmpeg") and info.get("transcription_key"))
        healthy = bool(info.get("username")) and not info.get("webhook_url")
        print(json.dumps({"ok": healthy, **info}))
        return 0 if healthy else 1

    if args.cmd == "send":
        ok = send_message(args.text)
    else:
        ok = send_asset(args.path, args.caption)

    print(json.dumps({"ok": bool(ok)}))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
