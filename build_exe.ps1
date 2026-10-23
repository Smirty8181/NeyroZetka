$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot
python -m pip install -r requirements.txt
python -m pip install pyinstaller

if (Test-Path "build") { Remove-Item "build" -Recurse -Force }
$configBackup = Join-Path $env:TEMP "NeyroZetka-config.backup.json"
if (Test-Path "dist\config.json") {
  Copy-Item "dist\config.json" $configBackup -Force
}
if (Test-Path "dist") { Remove-Item "dist" -Recurse -Force }
if (Test-Path "NeyroZetka.spec") { Remove-Item "NeyroZetka.spec" -Force }

python -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --windowed `
  --icon "neyrozetka.ico" `
  --name "NeyroZetka" `
  "app.py"

if (Test-Path $configBackup) {
  Copy-Item $configBackup "dist\config.json" -Force
  Remove-Item $configBackup -Force
  Write-Host "Restored existing dist\config.json (user settings preserved)." -ForegroundColor Green
} else {
  Copy-Item "config.json" "dist\config.json"
}

Write-Host ""
Write-Host "Build complete: $PSScriptRoot\dist\NeyroZetka.exe" -ForegroundColor Green
