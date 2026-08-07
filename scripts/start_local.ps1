$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$EntryPoint = Join-Path $ScriptDir "start_local.py"

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 $EntryPoint @args
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python $EntryPoint @args
} else {
    Write-Error "Python 3.11 or newer was not found on PATH."
}
exit $LASTEXITCODE
