[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][int]$Port,
    [int]$TargetPort = 0,
    [string]$TargetAddress = "127.0.0.1",
    [string]$ListenAddress = "0.0.0.0",
    [switch]$Cleanup,
    [string]$FirewallRulePrefix = "Local Port Forward",
    [switch]$NoFirewallRule,
    [switch]$SkipVerify
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-IsAdmin {
    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($currentIdentity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
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
        if ($null -ne $rule) {
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
            if ($null -ne $existing) {
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

function Test-TcpEndpoint {
    param(
        [string]$EndpointHost,
        [int]$PortToTest,
        [int]$TimeoutMs = 3000
    )

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect($EndpointHost, $PortToTest, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) {
            throw "timeout"
        }
        $client.EndConnect($iar) | Out-Null
        return [ordered]@{
            ok   = $true
            host = $EndpointHost
            port = $PortToTest
        }
    }
    catch {
        return [ordered]@{
            ok    = $false
            host  = $EndpointHost
            port  = $PortToTest
            error = $_.Exception.Message
        }
    }
    finally {
        $client.Close()
    }
}

if (-not (Test-IsAdmin)) {
    throw "请以管理员身份运行此脚本（需要写入 netsh portproxy 和防火墙规则）。"
}

if ($TargetPort -le 0) {
    $TargetPort = $Port
}

if ($Cleanup) {
    Remove-PortProxy -BindAddress $ListenAddress -BindPort $Port -RulePrefix $FirewallRulePrefix
    Write-Host ("[OK] 已清理端口转发 listen={0}:{1}" -f $ListenAddress, $Port)
    exit 0
}

$verify = $null
if (-not $SkipVerify) {
    $verify = Test-TcpEndpoint -EndpointHost $TargetAddress -PortToTest $TargetPort
    if (-not $verify.ok) {
        throw ("目标端口不可达: " + ($verify | ConvertTo-Json -Compress))
    }
}

Add-PortProxy -BindAddress $ListenAddress -BindPort $Port -ConnectAddress $TargetAddress -ConnectPort $TargetPort -RulePrefix $FirewallRulePrefix -SkipFirewall:$NoFirewallRule

$ips = Get-PreferredIPv4
$externalTcp = @()
foreach ($ip in $ips) {
    $externalTcp += ("{0}:{1}" -f $ip, $Port)
}

$firewallRuleValue = "-"
if (-not $NoFirewallRule) {
    $firewallRuleValue = "$FirewallRulePrefix $Port"
}

$payload = [ordered]@{
    success        = $true
    listen_address = $ListenAddress
    listen_port    = $Port
    target_address = $TargetAddress
    target_port    = $TargetPort
    firewall_rule  = $firewallRuleValue
    local_verify   = $verify
    external_hosts = $ips
    external_tcp   = $externalTcp
}

Write-Host "[OK] 端口转发已生效"
Write-Host ($payload | ConvertTo-Json -Depth 10)
Write-Host ""
Write-Host "Windows 本机验证："
Write-Host ("  Test-NetConnection -ComputerName 127.0.0.1 -Port {0}" -f $Port)
Write-Host ("  curl http://127.0.0.1:{0}" -f $Port)
Write-Host ""
Write-Host "Ubuntu 验证："
foreach ($ip in $ips) {
    Write-Host ("  nc -vz {0} {1}" -f $ip, $Port)
    Write-Host ("  curl -v http://{0}:{1}" -f $ip, $Port)
}
Write-Host ""
Write-Host "清理命令："
Write-Host ("  powershell -ExecutionPolicy Bypass -File .\scripts\forward-local-port.ps1 -Cleanup -Port {0}" -f $Port)
