# Reprise du projet MARK-XXXIX

## Ce que ce ZIP nettoyé change

- Suppression du fichier `.cache` qui contenait des tokens runtime.
- Suppression des logs et caches Python / pytest.
- Ajout de `.gitignore`.
- Ajout de `config/api_keys.example.json`.
- Ajout de `requirements.windows.txt` avec les dépendances manquantes détectées.
- Remplacement du token Telegram codé en dur par des variables d’environnement.
- Rendre `vosk` optionnel au démarrage : si le package n’est pas installé, le wake word est désactivé au lieu de casser le lancement.

## Installation Windows recommandée

```powershell
cd Mark-XXXIX-clean
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.windows.txt
python -m playwright install
```

## Configuration

Copier le fichier exemple :

```powershell
copy config\api_keys.example.json config\api_keys.json
notepad config\api_keys.json
```

Mettre au minimum `gemini_api_key`.

Pour Telegram, utiliser des variables d’environnement au lieu de les écrire dans le code :

```powershell
setx TELEGRAM_BOT_TOKEN "VOTRE_TOKEN"
setx TELEGRAM_CHAT_ID "VOTRE_CHAT_ID"
```

Puis ouvrir un nouveau terminal.

## Vérification

```powershell
python tools\era_doctor.py
python tools\run_all_checks.py
python main.py
```

## Note importante

Ce projet est une application Python Windows AI Assistant, pas un projet Django/ERP. Il n’y a pas de `manage.py`, `package.json`, `Dockerfile` ou `docker-compose.yml` dans le ZIP fourni.
