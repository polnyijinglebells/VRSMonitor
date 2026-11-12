param(
    [string]$ServiceName = "VRSMonitor",
    [string]$PythonExe = "python",
    [switch]$SkipFirewall
)
$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$portableExe = Join-Path $projectDir "VRSMonitor.exe"
$configPath = Join-Path $projectDir "config.json"
if (-not (Test-Path $configPath)) {
    throw "Не найден config.json. Распакуйте архив полностью и повторите установку."
}
$config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
$listenPort = [int]$config.listen_port

if (Test-Path $portableExe) {
    $action = New-ScheduledTaskAction -Execute $portableExe -WorkingDirectory $projectDir
} else {
    if (-not (Test-Path (Join-Path $projectDir "app.py"))) {
        throw "Не найден VRSMonitor.exe или app.py. Распакуйте архив полностью."
    }
    $pythonPath = (Get-Command $PythonExe).Source
    $action = New-ScheduledTaskAction -Execute $pythonPath -Argument 'app.py' -WorkingDirectory $projectDir
}
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 100 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $ServiceName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Контроль приемников Virtual Radar Server" -Force
Start-ScheduledTask -TaskName $ServiceName

if (-not $SkipFirewall) {
    $firewallRule = "VRSMonitor-$listenPort"
    if (-not (Get-NetFirewallRule -Name $firewallRule -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name $firewallRule -DisplayName "VRS Monitor (TCP $listenPort)" `
            -Direction Inbound -Action Allow -Protocol TCP -LocalPort $listenPort `
            -RemoteAddress LocalSubnet | Out-Null
    }
    Write-Host "Брандмауэр: TCP-порт $listenPort разрешён для локальной сети."
}

Write-Host "Служба $ServiceName установлена и запущена."
Write-Host "Панель на сервере: http://127.0.0.1:$listenPort"
Write-Host "Панель в локальной сети: http://IP-СЕРВЕРА:$listenPort"
