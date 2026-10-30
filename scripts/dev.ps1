# Local development on Windows without Docker: start the portable Postgres 16 if it is not running, then the panel.
# Run from your own terminal (not from a tool shell: Postgres dies when that shell's session ends):
#   cd imoveis ; .\scripts\dev.ps1
# Stop the panel with Ctrl+C. Stop Postgres with:  ..\.pg16\pgsql\bin\pg_ctl -D ..\.pg16\data stop
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)   # imoveis/
$pg = Join-Path (Split-Path -Parent $root) ".pg16"

& "$pg\pgsql\bin\pg_ctl.exe" -D "$pg\data" status | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Postgres is not running; starting it (log: $pg\pg.log)"
    & "$pg\pgsql\bin\pg_ctl.exe" -D "$pg\data" -o "-p 5432" -l "$pg\pg.log" -w start
}
& "$pg\pgsql\bin\pg_isready.exe" -h 127.0.0.1 -p 5432
if ($LASTEXITCODE -ne 0) { throw "Postgres did not come up; read $pg\pg.log" }

Set-Location $root
& "$root\.venv\Scripts\python.exe" -m uvicorn app.web.main:app --reload --port 8000
