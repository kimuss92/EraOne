# Security rotation checklist

A previous local copy of this project contained literal API credentials. Treat those credentials as exposed.

Do this now:

1. Revoke/regenerate the Gemini API keys.
2. Revoke/regenerate the OpenAI API key.
3. Regenerate the Spotify client secret.
4. Regenerate the Telegram bot token via BotFather if it appeared in any previous copy.
5. Keep `config/api_keys.json` local only; it is ignored by `.gitignore`.
6. Prefer environment variables for secrets when possible.

Never commit these files:

- `config/api_keys.json`
- `.cache` / `*.cache`
- `logs/`
- `memory/long_term.json`
- `models/`
