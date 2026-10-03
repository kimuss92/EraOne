$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Run-Step {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE" }
}

if (!(Test-Path ".venv\Scripts\python.exe")) {
    Run-Step "py" @("-3.12", "-m", "venv", ".venv")
}
$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
Run-Step $Python @("-c", "import sys; assert sys.version_info[:2] == (3,12), 'Rebuild .venv with Python 3.12'")
Run-Step $Python @("-m", "pip", "install", "--upgrade", "pip")
Run-Step $Python @("-m", "pip", "install", "-r", "requirements.windows.txt", "-c", "constraints.stability.txt")
Run-Step $Python @("-m", "playwright", "install", "chromium")
Run-Step $Python @("tools\era_manifest.py")
if (!(Test-Path "config\api_keys.json")) {
    Copy-Item "config\api_keys.example.json" "config\api_keys.json"
}
Write-Host "Setup complete. Configure GEMINI_API_KEY or config/api_keys.json, then run start_windows.cmd."
Write-Host "Optional offline voice: .venv\Scripts\python -m pip install -r requirements.local.txt -c constraints.stability.txt"
