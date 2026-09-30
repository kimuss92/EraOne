# Operations Guide

## Environment Setup

```powershell
# Create virtual environment (recommended)
python -m venv venv
.\venv\Scripts\Activate.ps1

# Install dependencies
python setup.py
# Or manually:
pip install -r requirements.txt
python -m playwright install
```

## Dependencies

All dependencies are listed in `requirements.txt`. Notable packages:

| Package | Purpose | Required |
|---------|---------|----------|
| google-genai | Gemini API | Yes |
| requests | HTTP | Yes |
| beautifulsoup4 | HTML parsing | For web search |
| playwright | Browser automation | For browser control |
| pyautogui | GUI automation | For desktop control |
| psutil | System info | For system monitor |
| pygetwindow | Window management | For window control |

### Optional Dependencies

These are non-critical. The Doctor reports them as WARN if missing:

- `pyaudio` — microphone/voice input
- `python_vlc` — local media playback

The Vosk model (`vosk-model-small-en-us-0.15`) is optional for offline speech recognition.

## API Key Configuration

Keys are stored in `config/api_keys.json` (automatically gitignored).

```json
{
    "gemini_api_key": "your-key-here",
    "os_system": "windows",
    "camera_index": 0,
    "SPOTIPY_CLIENT_ID": "your-spotify-client-id",
    "SPOTIPY_CLIENT_SECRET": "your-spotify-client-secret",
    "SPOTIPY_REDIRECT_URI": "http://127.0.0.1:8888/callback",
    "OPENAI_API_KEY": "your-openai-key-here"
}
```

- `gemini_api_key` is the only required key
- Spotify keys enable media control features
- OpenAI key is used as fallback only

## Running Era One

```powershell
python main.py
```

No Gemini API calls are made during tests or validation — only during actual `main.py` execution with tool dispatch.

## Running All Checks

```powershell
python tools/run_all_checks.py
```

Pipeline executes in order:

1. **Compile check** — `py_compile` on 16 core files
2. **Test suites** — 9 test suites covering validation, logging, memory, execution, verification, bridge, confirmation flow, doctor, smoke E2E
3. **Doctor** — health check reporting PASS/WARN/FAIL for all subsystems

### Interpreting Results

| Output | Meaning |
|--------|---------|
| `ALL_CHECKS_OK` + `READY` | All checks pass. System is operational. |
| `NOT READY` | One or more checks failed. Check the summary table for FAIL entries. |

## Interpreting Doctor Results

The Doctor outputs a health report with three status levels:

| Status | Meaning |
|--------|---------|
| `[PASS]` | Check passed |
| `[WARN]` | Non-critical issue — system will still operate |
| `[FAIL]` | Critical issue — must be resolved |

### Common WARN Items

- **Optional packages** — `pyaudio` and `python_vlc` are not installed. This is expected for text-only operation.
- **Vosk model** — offline speech model not found. Only needed for voice commands.
- **Backups count** — more than expected; this is informational.

If the Doctor shows only WARN items and the summary says `READY`, the system is operational.

## Memory & Backups

Era One uses a file-based memory system:

| File | Purpose |
|------|---------|
| `memory/long_term.json` | Long-term memory store |
| `memory/runtime_intent.json` | Runtime intent memory |
| `memory/feedback_memory.jsonl` | User feedback log |
| `memory/backups/*.jsonbak` | Auto-created backups via `safe_store.py` |

Backup behavior:

- `safe_write_json` creates a timestamped backup before overwriting
- `safe_append_jsonl` repairs corrupted `.jsonl` files automatically, backing up the corrupted version
- `cleanup_backups()` retains only the N most recent backups per memory file
- `recover_from_backup()` restores the most recent backup if the primary file is corrupted

## Logging

Logs are written to `logs/era.log` in JSONL format (one JSON object per line).

Rotation: 5 MB max file size, 5 backup files retained.

### Log Format

Each line contains:

| Field | Description |
|-------|-------------|
| `ts` | ISO 8601 timestamp |
| `level` | DEBUG / INFO / WARNING / ERROR / CRITICAL |
| `logger` | Logger name (era_one, era_one.memory, etc.) |
| `event` | Event type (tool.safety.classified, tool.execution.completed, etc.) |
| `tool` | Tool name |
| `message` | Human-readable message |

### Safety Features

- Sensitive keys (`password`, `token`, `api_key`, `secret`, `authorization`, `auth`, `credential`, `cookie`) are replaced with `***`
- Long parameter values are truncated to 120 characters
- Long `message_text` fields are truncated with a `message_text_length` field tracking the original length
- `attachments` fields are summarized as `<N attachment(s)>`
- File paths are relativized for privacy

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `No module named 'X'` | Run `python setup.py` or `pip install -r requirements.txt` |
| `playwright` errors | Run `python -m playwright install` |
| Doctor shows API key error | Check `config/api_keys.json` exists and has a valid key |
| Confirmation loop | The tool requires HIGH risk confirmation. Use `confirm_pending_confirmation` or `cancel_pending_confirmation`. |
| Port conflict (8888) | Spotify OAuth callback port is in use. Stop the conflicting process. |
| `ALL_CHECKS_OK` but Era One fails | Run `python tools/era_doctor.py` for detailed subsystem checks. |
