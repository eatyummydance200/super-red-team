$ErrorActionPreference = 'Stop'
& "$PSScriptRoot/.venv/Scripts/python.exe" -m uvicorn app.main:app --app-dir "$PSScriptRoot/backend" --env-file "$PSScriptRoot/backend/.env" --host 127.0.0.1 --port 8000 --workers 1
exit $LASTEXITCODE
