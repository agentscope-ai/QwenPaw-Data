[CmdletBinding()]
param(
    [switch]$SqliteOnly,
    [switch]$Register
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$ComposeFile = Join-Path $ScriptDir "docker-compose.yml"
$InitDemo = Join-Path $ScriptDir "init_demo.py"
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "QwenPaw Data's root Python environment was not found at $Python. Run .\scripts\init_local.ps1 first."
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [string[]]$ArgumentList = @()
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($ArgumentList -join ' ')"
    }
}

Invoke-NativeCommand -FilePath $Python -ArgumentList @($InitDemo)
if ($SqliteOnly) {
    exit 0
}

$DockerCommand = Get-Command docker -CommandType Application -ErrorAction SilentlyContinue
if (-not $DockerCommand) {
    throw "Docker Desktop is required for the PostgreSQL demo. Use -SqliteOnly without Docker."
}
$Docker = $DockerCommand.Source
Invoke-NativeCommand -FilePath $Docker -ArgumentList @("info")
Invoke-NativeCommand -FilePath $Docker -ArgumentList @("compose", "version")
Invoke-NativeCommand -FilePath $Docker -ArgumentList @(
    "compose", "-f", $ComposeFile, "up", "-d", "--wait"
)

$PostgresPort = if ($env:QWENPAW_DATA_DEMO_POSTGRES_PORT) {
    [int]$env:QWENPAW_DATA_DEMO_POSTGRES_PORT
} else {
    55432
}
$PostgresDsn = "postgresql://qwenpaw_data:qwenpaw-data-demo@127.0.0.1:${PostgresPort}/qwenpaw_data_demo"
Invoke-NativeCommand -FilePath $Python -ArgumentList @(
    $InitDemo, "--postgres-dsn", $PostgresDsn
)

if ($Register) {
    Invoke-NativeCommand -FilePath $Python -ArgumentList @(
        $InitDemo,
        "--register",
        "--postgres-port", [string]$PostgresPort
    )
}

Write-Host "Demo ready. Repository root: $RepoRoot"
