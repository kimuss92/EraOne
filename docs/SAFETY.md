# Safety Guide

## Risk Level System

Every tool dispatch goes through `core/safety_guard.py` which returns a risk level:

| Risk Level | Confirmation Required | Example Tools |
|------------|----------------------|---------------|
| **LOW** | No | `open_app`, `web_search`, `shutdown_era`, `save_memory`, `enter_standby` |
| **MEDIUM** | Yes (notified) | `file_controller write/copy` (non-system paths) |
| **HIGH** | Yes (required) | `send_message`, `file_controller delete` (user paths), `computer_settings shutdown` |
| **BLOCKED** | Denied outright | `file_controller` operations on `C:\Windows`, `C:\Program Files`, `APPDATA` — protected system paths |

### Action-Based Risk

Some tools have dynamic risk based on the `action` parameter:

- `browser_control` — navigation/read actions are LOW; form-filling/clicking is MEDIUM
- `computer_settings` — system info is LOW; shutdown/restart is HIGH
- `file_controller` — reading is LOW; deletion is HIGH; system-path operations are BLOCKED
- `desktop_control` — wallpaper/personalization is LOW

## Confirmation Flow

When a HIGH risk tool is dispatched, the system:

1. Creates a `PendingConfirmation` with a unique ID and stores it (expires after 5 minutes)
2. Returns a `ToolResult` with `error_type="ConfirmationRequired"` and the `confirmation_id`
3. The user/caller must then call one of:

### `confirm_pending_confirmation`

```
Parameters: { "confirmation_id": "<id>" }
```

- Re-dispatches the originally stored tool with original parameters
- Returns the actual tool result
- Clears the pending confirmation

### `cancel_pending_confirmation`

```
Parameters: {}  (cancels any active pending)
```

- Clears the pending store
- Returns `"Pending action cancelled."`
- If no pending action exists, returns `"There is no pending action to cancel."`

Both confirmation management tools are classified as LOW risk and execute without further confirmation.

## Shutdown Guard

- `shutdown_era` is classified LOW (can execute without confirmation)
- A duplicate guard prevents shutdown from executing more than once per session
- Second and subsequent calls return `"Already in progress"`

## Path Protection

`core/safety_guard.py` defines protected paths that may not be modified:

| Platform | Protected Paths |
|----------|----------------|
| Windows | `C:\Windows`, `C:\Program Files`, `C:\Program Files (x86)`, `APPDATA`, `LOCALAPPDATA`, `PROGRAMDATA` |

The `file_controller` tool checks every operation against these paths and returns BLOCKED for any targeted path.

## Logger Safety

`core/logger.py` sanitizes log data before writing:

### Sensitive Key Masking

Values for any key containing these substrings are replaced with `***`:

- `password`, `token`, `api_key`, `key`, `secret`, `authorization`, `auth`, `credential`, `cookie`

### Long Value Truncation

- Parameter values over 120 characters are truncated with `...`
- `message_text` values are truncated with a `message_text_length` field preserving the original length

### Path Privacy

- Paths under the repository root are relativized to the project directory
- Paths outside the repository show only the filename

### Full-Message and Secret Protection

- The Gemini API key is never written to logs
- Full long message bodies are not included in logs (only truncated versions with length tracking)
- The E2E smoke test (`test_smoke_e2e.py`) explicitly verifies that no secrets or full message bodies leak into logs

## Smoke E2E Safety

`tools/test_smoke_e2e.py` validates safety behavior end-to-end:

- LOW risk: executes directly with no confirmation events
- HIGH risk: requires confirmation, then verifies accepted execution
- HIGH risk: cancellation clears pending state entirely
- Log safety: verifies JSONL validity, secret masking, and no full message bodies
