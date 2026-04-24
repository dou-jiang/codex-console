param(
    [Parameter(Position=0)]
    [ValidateSet('start', 'stop', 'restart', 'status', 'logs')]
    [string]$Action = 'start',
    [int]$Port = 8000,
    [string]$BindHost = '0.0.0.0',
    [switch]$DebugMode,
    [switch]$SkipUpdate,
    [switch]$SkipBuild,
    [switch]$OpenBrowser,
    [switch]$Guard,
    [switch]$UseConda,
    [string]$CondaEnv = '',
    [string]$HealthUrl = '',
    [double]$HealthInterval = 15,
    [double]$HealthTimeout = 5,
    [int]$HealthFailThreshold = 3,
    [int]$Lines = 80,
    [string]$Remote = '',
    [string]$Branch = '',
    [string]$PythonExe = 'python'
)

$ErrorActionPreference = 'Stop'
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Driver = Join-Path $ScriptRoot 'manage_webui.py'

if (-not (Test-Path -LiteralPath $Driver)) {
    throw "未找到驱动脚本: $Driver"
}

$argsList = @($Driver, $Action, '--port', "$Port", '--host', $BindHost, '--python', $PythonExe)
if ($DebugMode) { $argsList += '--debug' }
if ($SkipUpdate) { $argsList += '--skip-update' }
if ($SkipBuild) { $argsList += '--skip-build' }
if ($OpenBrowser) { $argsList += '--open-browser' }
if ($Guard) { $argsList += '--guard' }
if ($UseConda) { $argsList += '--use-conda' }
if ($CondaEnv) { $argsList += @('--conda-env', $CondaEnv) }
if ($HealthUrl) { $argsList += @('--health-url', $HealthUrl) }
if ($HealthInterval -ne 15) { $argsList += @('--health-interval', "$HealthInterval") }
if ($HealthTimeout -ne 5) { $argsList += @('--health-timeout', "$HealthTimeout") }
if ($HealthFailThreshold -ne 3) { $argsList += @('--health-fail-threshold', "$HealthFailThreshold") }
if ($Lines -ne 80) { $argsList += @('--lines', "$Lines") }
if ($Remote) { $argsList += @('--remote', $Remote) }
if ($Branch) { $argsList += @('--branch', $Branch) }

& $PythonExe @argsList
exit $LASTEXITCODE
