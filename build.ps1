$ErrorActionPreference = 'Continue'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Create .venv and install requirements first.' }
& $python build.py
if ($LASTEXITCODE -ne 0) { throw 'Executable build failed.' }
