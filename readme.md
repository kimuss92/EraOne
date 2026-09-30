# Era One — Windows AI Assistant

A Python-based Windows assistant with extensible tool dispatch, safety confirmation flow, structured logging, and memory persistence. Designed for local execution on Windows with optional Spotify/browser/media integrations.

## Quick Start

```powershell
# 1. Setup
python setup.py

# 2. Configure API keys
# Edit config/api_keys.json (see config/api_keys.example.json)

# 3. Validate
python tools/run_all_checks.py

# 4. Run
python main.py
```

## Prerequisites

Tested on Windows with the current local Python environment. Use Python 3.10+ unless project dependencies require otherwise.

## API Key Setup

Edit `config/api_keys.json` (already gitignored):

```json
{
    "gemini_api_key": "your-gemini-key-here",
    "os_system": "windows",
    "SPOTIPY_CLIENT_ID": "your-spotify-client-id",
    "SPOTIPY_CLIENT_SECRET": "your-spotify-client-secret",
    "SPOTIPY_REDIRECT_URI": "http://127.0.0.1:8888/callback",
    "OPENAI_API_KEY": "your-openai-key-here"
}
```

Only `gemini_api_key` is required. Spotify and OpenAI keys are optional.

## Validate Health

```powershell
python tools/run_all_checks.py
```

Output: `ALL_CHECKS_OK` / `READY` means all systems operational.

## Documentation

- [Operations Guide](docs/OPERATIONS.md) — environment, dependencies, memory, logs, troubleshooting
- [Safety Guide](docs/SAFETY.md) — risk levels, confirmation flow, logger safety, path protection

## Safety

Era One uses a multi-level safety system:

- **LOW** risk actions execute immediately
- **HIGH** risk actions (e.g., sending messages) require explicit confirmation
- **BLOCKED** actions are denied outright (e.g., deleting system files)
- All sensitive data (passwords, tokens, API keys) is masked in logs
- [Full safety documentation](docs/SAFETY.md)

## License

Private project — by karimbenattig.
