param(
    [Parameter(Mandatory = $true)][string]$Python,
    [string]$EnvFile,
    [int]$Port = 8000
)
$ErrorActionPreference = 'Stop'
if (-not $EnvFile) { $EnvFile = Join-Path $PSScriptRoot '..\.env' }
$EnvFile = (Resolve-Path -LiteralPath $EnvFile).Path
& $Python "$PSScriptRoot\media_preflight.py" --env-file $EnvFile
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Python -m uvicorn app.main:app --app-dir "$PSScriptRoot\.." --env-file $EnvFile --host 127.0.0.1 --port $Port --workers 1
exit $LASTEXITCODE
