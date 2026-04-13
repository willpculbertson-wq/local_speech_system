# restart.ps1 — Kill any running dictation process and restart it
# Run from the project root in an elevated (dictation) conda env

$scriptPath = Join-Path $PSScriptRoot "src\main.py"

# Kill any existing instance
Get-Process python -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*main.py*" } |
    Stop-Process -Force -ErrorAction SilentlyContinue

Write-Host "Restarting dictation system..." -ForegroundColor Cyan
python $scriptPath
