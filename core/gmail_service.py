# core/gmail_service.py
"""Gmail read and send operations for Era One.

Credentials, scopes and the API client handle live in `core.gmail_auth`; this
module is the mail itself — listing, fetching, MIME parsing, and sending.

Two properties worth keeping:

* **Header injection is impossible.** Recipients and subjects are validated
  before an outbound message is ever constructed, so a newline in an address
  can never open a Bcc header.
* **Nothing raises.** Every entry point returns a result dict whose `message`
  is safe to read aloud, including the failure paths.

Email *content* is untrusted, sender-controlled text. This module returns it
raw; `core.gmail_priority` wraps and neutralises it before it reaches the model.
"""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any

from core.gmail_auth import (
    GmailNotConnected,
    _require_service,
    auth_status,
    authorize,
    client_secret_path,
    dependencies_ready,
    drop_thread_service,
    gmail_state_dir,
    is_connected,
    revoke,
    token_path,
    user_email,
)
from core.utils import log

# Re-exported so existing importers of core.gmail_service keep working and
# callers have one obvious place to import the Gmail surface from.
__all__ = [
    "GmailNotConnected",
    "auth_status",
    "authorize",
    "client_secret_path",
    "dependencies_ready",
    "extract_address",
    "get_message",
    "gmail_state_dir",
    "is_connected",
    "list_messages",
    "load_json",
    "parse_message",
    "revoke",
    "save_json",
    "send_email",
    "token_path",
    "user_email",
    "validate_recipients",
]

_EMAIL_RE = re.compile(r"^[^@\s,;<>\"]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_DISPLAY_ADDR_RE = re.compile(r"^(?P<name>[^<>]{0,120})<(?P<addr>[^<>]+)>$")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _header_map(payload: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in payload.get("headers", []) or []:
        name = (h.get("name") or "").lower()
        if name and name not in out:
            out[name] = h.get("value") or ""
    return out


def _b64url_decode(data: str) -> str:
    if not data:
        return ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _strip_html(html: str) -> str:
    """Crude but dependency-free HTML to text.

    Script, style and head bodies are dropped entirely rather than flattened:
    they are the most common carrier for text a sender never intended a human
    to read, which is exactly where an injection attempt hides.
    """
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p\s*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _walk_parts(payload: dict) -> tuple[str, str, list[str]]:
    """Return (plain_text, html_text, attachment_filenames)."""
    plain_chunks: list[str] = []
    html_chunks: list[str] = []
    attachments: list[str] = []

    def walk(part: dict, depth: int = 0) -> None:
        if depth > 12:  # malformed or hostile deep nesting
            return
        mime = (part.get("mimeType") or "").lower()
        body = part.get("body") or {}
        filename = part.get("filename") or ""
        if filename:
            attachments.append(filename)
        data = body.get("data")
        if data:
            decoded = _b64url_decode(data)
            if mime == "text/plain":
                plain_chunks.append(decoded)
            elif mime == "text/html":
                html_chunks.append(decoded)
        for sub in part.get("parts", []) or []:
            walk(sub, depth + 1)

    walk(payload)
    return "\n".join(plain_chunks).strip(), "\n".join(html_chunks).strip(), attachments


def extract_address(header_value: str) -> str:
    """'Jane Doe <jane@x.com>' -> 'jane@x.com'. Lowercased, best effort."""
    v = (header_value or "").strip()
    if not v:
        return ""
    m = _DISPLAY_ADDR_RE.match(v)
    if m:
        v = m.group("addr").strip()
    return v.strip("<> ").strip().lower()


def parse_message(msg: dict, include_body: bool = True) -> dict:
    """Normalise a Gmail API message resource into a flat dict.

    The `body` field is raw untrusted sender text — pass it through
    `core.gmail_priority.render_untrusted_block` before it reaches the model.
    """
    payload = msg.get("payload") or {}
    headers = _header_map(payload)
    label_ids = list(msg.get("labelIds", []) or [])
    parsed: dict[str, Any] = {
        "id": msg.get("id", ""),
        "thread_id": msg.get("threadId", ""),
        "label_ids": label_ids,
        "snippet": (msg.get("snippet") or "").strip(),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "subject": headers.get("subject", "(no subject)"),
        "date": headers.get("date", ""),
        "list_unsubscribe": headers.get("list-unsubscribe", ""),
        "internal_date": msg.get("internalDate", ""),
        "unread": "UNREAD" in label_ids,
        "attachments": [],
        "body": "",
        "body_format": "none",
    }
    parsed["from_address"] = extract_address(parsed["from"])
    if include_body:
        plain, html, attachments = _walk_parts(payload)
        parsed["attachments"] = attachments
        if plain:
            parsed["body"] = plain
            parsed["body_format"] = "text"
        elif html:
            parsed["body"] = _strip_html(html)
            parsed["body_format"] = "html"
    return parsed


def _api_error_text(e: Exception) -> str:
    """Human-readable API failure, with no token or header material in it."""
    status = getattr(getattr(e, "resp", None), "status", None)
    try:
        status = int(status) if status is not None else None
    except Exception:
        status = None
    if status == 401:
        return "Gmail rejected the stored credentials. Re-authorize Gmail access."
    if status == 403:
        return (
            "Gmail refused the request — the granted scope may not cover it, "
            "or the quota is exhausted."
        )
    if status == 429:
        return "Gmail is rate-limiting requests. Try again shortly."
    if status is not None and 500 <= status < 600:
        return "Gmail is having a server-side problem. Try again shortly."
    return f"Gmail request failed ({type(e).__name__})."


def _is_transient(e: Exception) -> bool:
    """A connection-level fault (TLS error, reset, timeout), not an answer from Gmail.

    HttpError carries `resp` and is not an OSError; ssl.SSLError, ConnectionError
    and TimeoutError all are.
    """
    return isinstance(e, OSError) and getattr(e, "resp", None) is None


def _is_retryable_status(e: Exception) -> bool:
    """Gmail answered, but with a 'try again' status (rate limit / server fault)."""
    status = getattr(getattr(e, "resp", None), "status", None)
    try:
        status = int(status) if status is not None else None
    except Exception:
        return False
    return status in (429, 500, 502, 503, 504)


def _execute_read(build_request) -> Any:
    """Run one READ-ONLY request, retrying once after a transient fault.

    A connection fault gets a fresh client (the old httplib2 connection is
    poisoned); a 429/5xx gets a short pause on the same one. Never used for
    sends: a repeated send could deliver twice."""
    try:
        return build_request(_require_service()).execute()
    except Exception as e:
        if _is_transient(e):
            log("GMAIL", f"transient {type(e).__name__}; retrying once on a fresh connection")
            drop_thread_service()
        elif _is_retryable_status(e):
            log("GMAIL", "Gmail asked us to back off; retrying once")
            time.sleep(1.5)
        else:
            raise
        return build_request(_require_service()).execute()


def list_messages(
    query: str = "",
    max_results: int = 10,
    label_ids: list[str] | None = None,
    include_body: bool = False,
) -> dict:
    """List messages, newest first.

    `label_ids=None` (the default) means the inbox. An explicit empty list
    means NO label filter — the whole mailbox, archived and sent mail
    included, the way Gmail's own search box works. (It used to be
    `label_ids or ["INBOX"]`, which turned "search everything" back into
    "search the inbox" and made archived mail unfindable.)

    `include_body=False` (the default) fetches metadata only — cheaper, and it
    keeps untrusted body text out of context until something actually needs it.
    """
    max_results = max(1, min(int(max_results or 10), 50))
    try:
        _require_service()
    except GmailNotConnected as e:
        return {"ok": False, "message": str(e), "messages": []}

    list_kwargs: dict[str, Any] = {"userId": "me", "q": query or "", "maxResults": max_results}
    labels = ["INBOX"] if label_ids is None else list(label_ids)
    if labels:
        list_kwargs["labelIds"] = labels

    try:
        listing = _execute_read(lambda svc: svc.users().messages().list(**list_kwargs))
    except Exception as e:
        return {"ok": False, "message": _api_error_text(e), "messages": []}

    ids = [m["id"] for m in listing.get("messages", []) or []]
    fmt = "full" if include_body else "metadata"
    kwargs: dict[str, Any] = {"format": fmt}
    if not include_body:
        kwargs["metadataHeaders"] = ["From", "To", "Cc", "Subject", "Date", "List-Unsubscribe"]

    messages = []
    for mid in ids:
        try:
            raw = _execute_read(
                lambda svc, mid=mid: svc.users().messages().get(userId="me", id=mid, **kwargs)
            )
            messages.append(parse_message(raw, include_body=include_body))
        except Exception as e:
            log("GMAIL", f"skipped message {mid}: {type(e).__name__}")
            continue

    return {
        "ok": True,
        "message": f"{len(messages)} message(s).",
        "messages": messages,
        "estimated_total": listing.get("resultSizeEstimate", len(messages)),
    }


def get_message(message_id: str, include_body: bool = True) -> dict:
    mid = (message_id or "").strip()
    if not mid:
        return {"ok": False, "message": "No message id given.", "email": None}
    try:
        _require_service()
    except GmailNotConnected as e:
        return {"ok": False, "message": str(e), "email": None}
    try:
        raw = _execute_read(
            lambda svc: svc.users().messages().get(userId="me", id=mid, format="full")
        )
    except Exception as e:
        return {"ok": False, "message": _api_error_text(e), "email": None}
    return {"ok": True, "message": "ok", "email": parse_message(raw, include_body=include_body)}


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def validate_recipients(raw: str | list[str]) -> tuple[list[str], str]:
    """(addresses, error). Rejects anything that isn't a bare valid address.

    Display-name forms are accepted but reduced to the address, so no
    sender-controlled text survives into a header.
    """
    if isinstance(raw, str):
        candidates = [p for p in re.split(r"[,;]", raw) if p.strip()]
    else:
        candidates = list(raw or [])
    out: list[str] = []
    for c in candidates:
        c = str(c).strip()
        if not c:
            continue
        if "\n" in c or "\r" in c or "\x00" in c:
            return [], "A recipient contained a line break, which is not allowed."
        addr = extract_address(c)
        if not _EMAIL_RE.match(addr):
            return [], f"'{c[:60]}' is not a valid email address."
        out.append(addr)
    if not out:
        return [], "No recipient address was given."
    if len(out) > 25:
        return [], "Too many recipients in one message (limit 25)."
    return out, ""


def _clean_subject(subject: str) -> tuple[str, str]:
    s = (subject or "").strip()
    if "\n" in s or "\r" in s or "\x00" in s:
        return "", "The subject contained a line break, which is not allowed."
    if len(s) > 300:
        return "", "The subject is too long (limit 300 characters)."
    return s, ""


def send_email(
    to: str | list[str],
    subject: str,
    body: str,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    reply_to_message_id: str = "",
) -> dict:
    """Send a plain-text message as the authenticated user.

    This function performs no authorization of its own — the caller
    (`actions/gmail_control.py`) only reaches it after Era's HIGH-risk
    confirmation gate has been cleared by the user.
    """
    to_addrs, err = validate_recipients(to)
    if err:
        return {"ok": False, "message": err}
    cc_addrs: list[str] = []
    bcc_addrs: list[str] = []
    if cc:
        cc_addrs, err = validate_recipients(cc)
        if err:
            return {"ok": False, "message": f"CC: {err}"}
    if bcc:
        bcc_addrs, err = validate_recipients(bcc)
        if err:
            return {"ok": False, "message": f"BCC: {err}"}

    clean_subject, err = _clean_subject(subject)
    if err:
        return {"ok": False, "message": err}

    body_text = (body or "").replace("\x00", "")
    if not body_text.strip():
        return {"ok": False, "message": "The message body is empty."}
    if len(body_text) > 100_000:
        return {"ok": False, "message": "The message body is too long."}

    try:
        svc = _require_service()
    except GmailNotConnected as e:
        return {"ok": False, "message": str(e)}

    try:
        from email.message import EmailMessage

        mime = EmailMessage()
        mime["To"] = ", ".join(to_addrs)
        if cc_addrs:
            mime["Cc"] = ", ".join(cc_addrs)
        if bcc_addrs:
            mime["Bcc"] = ", ".join(bcc_addrs)
        mime["Subject"] = clean_subject
        mime.set_content(body_text)

        payload: dict[str, Any] = {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")}
        if reply_to_message_id:
            # Keep the reply inside the original conversation.
            try:
                original = (
                    svc.users()
                    .messages()
                    .get(userId="me", id=reply_to_message_id, format="metadata")
                    .execute()
                )
                payload["threadId"] = original.get("threadId", "")
            except Exception:
                pass

        sent = svc.users().messages().send(userId="me", body=payload).execute()
    except Exception as e:
        return {"ok": False, "message": _api_error_text(e)}

    return {
        "ok": True,
        "message": f"Email sent to {', '.join(to_addrs)}.",
        "id": sent.get("id", ""),
        "thread_id": sent.get("threadId", ""),
        "to": to_addrs,
        "subject": clean_subject,
    }


# ---------------------------------------------------------------------------
# Small shared JSON helpers (used by the priority and watcher stores)
# ---------------------------------------------------------------------------


def load_json(path: Path, default: Any) -> Any:
    """Tolerant JSON read. A corrupt store degrades to the default."""
    try:
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data: Any) -> bool:
    """Atomic-ish JSON write. Returns success; never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception as e:
        log("GMAIL", f"could not write {path.name}: {type(e).__name__}")
        return False
