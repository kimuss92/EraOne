# Optional capability dependencies, separate from Era's Node UI dependencies.
$ErrorActionPreference = 'Stop'
python -m pip install 'openpyxl>=3.1,<4' 'pypdf>=6,<7' 'reportlab>=4,<5' 'pefile>=2024.8.26' 'qrcode[pil]>=8,<9'
if ($LASTEXITCODE -ne 0) { throw 'Python capability dependency installation failed' }
$eraReaPrefix = Join-Path $env:USERPROFILE '.era/tools/rea'
npm install --prefix $eraReaPrefix --save-exact rea-agents@6.7.0
if ($LASTEXITCODE -ne 0) { throw 'REA installation failed' }
$eraApiAnythingPrefix = Join-Path $env:USERPROFILE '.era/tools/api-anything'
npm install --prefix $eraApiAnythingPrefix --save-exact --ignore-scripts api-anything@0.3.2
if ($LASTEXITCODE -ne 0) { throw 'api-anything installation failed' }
python (Join-Path $PSScriptRoot 'install_era_analysis.py')
if ($LASTEXITCODE -ne 0) { throw 'Portable analysis provider installation failed' }
Write-Output 'Capability dependencies and portable analysis providers installed.'
