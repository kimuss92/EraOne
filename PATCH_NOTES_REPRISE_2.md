# Reprise patch notes — security/code quality pass

Applied fixes:

- Re-enabled path safety in `core/utils.py`.
- Removed duplicated `classify_vision_risk` in `core/safety_guard.py`.
- Made MEDIUM actions require confirmation.
- Made `ConfirmationManager` deny unconfirmed MEDIUM/HIGH actions.
- Removed temporary `/remote_voice` debug prints from `main.py`.
- Removed duplicate `open_app` import.
- Stopped `actions/spotify_control.py` from auto-installing packages at import time.
- Documented Spotify registry routing.
- Corrected `setup.py` project name from MARK XXV to MARK XXXIX.
- Updated `requirements.txt` with missing runtime dependencies.
- Updated Vosk model check to look under ignored `models/`.
- Added `SECURITY_ROTATION.md`.

Remaining known work:

- Run full checks on a real Windows machine.
- Fix known Netflix Vision fallback failure.
- Reduce silent `except: pass` blocks progressively.
- Decide whether Spotify control should be unified under one handler.
