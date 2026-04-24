[CmdletBinding()]
param(
    [string]$ApiHost = "http://127.0.0.1:50000",
    [Parameter(Mandatory = $true)][string]$Token,
    [Parameter(Mandatory = $true)][string]$DirId,
    [int]$WorkspaceId = 0,
    [string]$ListenAddress = "0.0.0.0",
    [bool]$Headless = $true,
    [bool]$ForceOpen = $true,
    [switch]$SkipLocalVerify,
    [switch]$Cleanup,
    [int]$Port = 0,
    [string]$FirewallRulePrefix = "Roxy CDP",
    [switch]$NoFirewallRule
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-IsAdmin {
    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($currentIdentity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function New-QueryString {
    param([hashtable]$Params)
    if (-not $Params -or $Params.Count -eq 0) {
        return ""
    }
    return (($Params.GetEnumerator() | ForEach-Object {
        "{0}={1}" -f [uri]::EscapeDataString([string]$_.Key), [uri]::EscapeDataString([string]$_.Value)
    }) -join "&")
}

function Invoke-RoxyJson {
    param(
        [ValidateSet("GET", "POST")] [string]$Method,
        [string]$Path,
        [hashtable]$Query = @{},
        [object]$Body = $null
    )

    $headers = @{
        token  = $Token
        Accept = "application/json"
    }

    $base = $ApiHost.TrimEnd("/")
    $uri = "$base$Path"
    $queryString = New-QueryString -Params $Query
    if ($queryString) {
        $uri = "$uri?$queryString"
    }

    if ($Method -eq "GET") {
        return Invoke-RestMethod -Method Get -Uri $uri -Headers $headers
    }

    $headers["Content-Type"] = "application/json"
    $jsonBody = $null
    if ($null -ne $Body) {
        $jsonBody = ($Body | ConvertTo-Json -Depth 10 -Compress)
    }

    return Invoke-RestMethod -Method Post -Uri $uri -Headers $headers -Body $jsonBody
}

function Resolve-WorkspaceIdByDirId {
    param([string]$TargetDirId)

    $workspaceResp = Invoke-RoxyJson -Method GET -Path "/browser/workspace" -Query @{
        page_index = 1
        page_size  = 100
    }

    $rows = @($workspaceResp.data.rows)
    if (-not $rows -or $rows.Count -eq 0) {
        throw "Roxy /browser/workspace 返回为空，无法解析 workspaceId。"
    }

    foreach ($workspace in $rows) {
        $wid = [int]$workspace.id

        $listResp = Invoke-RoxyJson -Method GET -Path "/browser/list_v3" -Query @{
            workspaceId = $wid
            page_index  = 1
            page_size   = 100
        }

        foreach ($row in @($listResp.data.rows)) {
            $serialized = $row | ConvertTo-Json -Depth 20 -Compress
            if ($serialized -match [regex]::Escape($TargetDirId)) {
                return $wid
            }
        }
    }

    throw "未在任何 workspace 中找到 dirId=$TargetDirId"
}

function Remove-PortProxy {
    param(
        [string]$BindAddress,
        [int]$BindPort,
        [string]$RulePrefix
    )

    & netsh interface portproxy delete v4tov4 listenaddress=$BindAddress listenport=$BindPort | Out-Null

    $ruleName = "$RulePrefix $BindPort"
    try {
        $rule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction Stop
        if ($rule) {
            Remove-NetFirewallRule -DisplayName $ruleName | Out-Null
        }
    }
    catch {
    }
}

function Add-PortProxy {
    param(
        [string]$BindAddress,
        [int]$BindPort,
        [string]$ConnectAddress,
        [int]$ConnectPort,
        [string]$RulePrefix,
        [switch]$SkipFirewall
    )

    & netsh interface portproxy delete v4tov4 listenaddress=$BindAddress listenport=$BindPort | Out-Null
    $null = & netsh interface portproxy add v4tov4 listenaddress=$BindAddress listenport=$BindPort connectaddress=$ConnectAddress connectport=$ConnectPort protocol=tcp

    if (-not $SkipFirewall) {
        $ruleName = "$RulePrefix $BindPort"
        try {
            $existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction Stop
            if ($existing) {
                Remove-NetFirewallRule -DisplayName $ruleName | Out-Null
            }
        }
        catch {
        }

        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $BindPort | Out-Null
    }
}

function Get-PreferredIPv4 {
    $ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPAddress -ne "127.0.0.1" -and
            $_.IPAddress -notlike "169.254.*" -and
            $_.PrefixOrigin -ne "WellKnown"
        } |
        Sort-Object -Property InterfaceMetric |
        Select-Object -ExpandProperty IPAddress

    return @($ips)
}

function Test-HttpEndpoint {
    param(
        [string]$Url,
        [int]$TimeoutSec = 6
    )

    try {
        $resp = Invoke-WebRequest -UseBasicParsing -Uri $Url -Method Get -TimeoutSec $TimeoutSec
        return [ordered]@{
            ok          = $true
            status_code = [int]$resp.StatusCode
            url         = $Url
        }
    }
    catch {
        $statusCode = $null
        try {
            if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
                $statusCode = [int]$_.Exception.Response.StatusCode
            }
        }
        catch {
        }

        return [ordered]@{
            ok          = $false
            status_code = $statusCode
            url         = $Url
            error       = $_.Exception.Message
        }
    }
}

if (-not (Test-IsAdmin)) {
    throw "请以管理员身份运行此脚本（需要写入 netsh portproxy 和防火墙规则）。"
}

if ($Cleanup) {
    if ($Port -le 0) {
        throw "使用 -Cleanup 时必须同时提供 -Port <端口>"
    }

    Remove-PortProxy -BindAddress $ListenAddress -BindPort $Port -RulePrefix $FirewallRulePrefix
    Write-Host ("[OK] 已清理端口转发 listen={0}:{1}" -f $ListenAddress, $Port)
    exit 0
}

if ($WorkspaceId -le 0) {
    $WorkspaceId = Resolve-WorkspaceIdByDirId -TargetDirId $DirId
}

$openResp = Invoke-RoxyJson -Method POST -Path "/browser/open" -Body @{
    workspaceId = $WorkspaceId
    dirId       = $DirId
    forceOpen   = $ForceOpen
    headless    = $Headless
}

if ($openResp.code -ne 0) {
    throw ("Roxy /browser/open 失败: " + ($openResp | ConvertTo-Json -Depth 20))
}

$data = $openResp.data
$wsUri = [uri]$data.ws
$remotePort = [int]$wsUri.Port

if ($remotePort -le 0) {
    throw "未能从 ws 地址中解析出端口: $($data.ws)"
}

Add-PortProxy -BindAddress $ListenAddress -BindPort $remotePort -ConnectAddress "127.0.0.1" -ConnectPort $remotePort -RulePrefix $FirewallRulePrefix -SkipFirewall:$NoFirewallRule

$ips = Get-PreferredIPv4
$externalWs = @()

foreach ($ip in $ips) {
    $externalWs += ($data.ws -replace '^ws://127\.0\.0\.1:', ('ws://' + $ip + ':'))
}

$localVerify = $null
if (-not $SkipLocalVerify) {
    $localVerify = Test-HttpEndpoint -Url ("http://127.0.0.1:{0}/json/version" -f $remotePort)
}

$payload = [ordered]@{
    success        = $true
    api_host       = $ApiHost
    workspace_id   = $WorkspaceId
    dir_id         = $DirId
    headless       = $Headless
    force_open     = $ForceOpen
    internal_ws    = $data.ws
    internal_http  = $data.http
    forwarded_port = $remotePort
    listen_address = $ListenAddress
    external_ws    = $externalWs
    local_verify   = $localVerify
    pid            = $data.pid
    window_name    = $data.windowName
}

Write-Host "[OK] Roxy 窗口已打开并完成端口转发"
Write-Host ($payload | ConvertTo-Json -Depth 10)
Write-Host ""
if ($null -ne $localVerify) {
    if ($localVerify.ok) {
        Write-Host ("[OK] 本机验证成功: {0} status={1}" -f $localVerify.url, $localVerify.status_code)
    }
    else {
        Write-Host ("[WARN] 本机验证失败: {0}" -f (($localVerify | ConvertTo-Json -Compress)))
    }
    Write-Host ""
}
Write-Host "测试命令："
Write-Host "  curl http://127.0.0.1:$remotePort/json/version"

foreach ($ip in $ips) {
    Write-Host ("  curl http://{0}:{1}/json/version" -f $ip, $remotePort)
}

Write-Host ""
Write-Host "Ubuntu 验证："
foreach ($ip in $ips) {
    Write-Host ("  curl -v http://{0}:{1}/json/version" -f $ip, $remotePort)
    Write-Host ("  python - <<'PY'" )
    Write-Host ("import json, urllib.request")
    Write-Host (("print(json.load(urllib.request.urlopen('http://{0}:{1}/json/version', timeout=8)))" -f $ip, $remotePort))
    Write-Host ("PY")
}

Write-Host ""
Write-Host "清理命令："
Write-Host ("  powershell -ExecutionPolicy Bypass -File .\scripts\roxy_open_and_forward_cdp.ps1 -Cleanup -Port {0}" -f $remotePort)
