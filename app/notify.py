"""Best-effort Telegram outcome mirror (extracted from main.py)."""

import threading


def _tg_notify(text: str) -> None:
    """Best-effort mirror of a spoken CM outcome into the Telegram chat, so a job you
    ordered remotely reports back remotely. Module-level (not a method) because it
    needs no instance state. Never raises and never blocks: Telegram being down must
    not turn a successful content run into a failed one."""
    try:
        import era_telegram

        if era_telegram.enabled():
            threading.Thread(target=era_telegram.send_message, args=(text,), daemon=True).start()
    except Exception as e:
        print(f"[TG] notify skipped: {e}")
