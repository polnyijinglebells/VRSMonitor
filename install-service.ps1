param(
    [string]$ServiceName = "VRSMonitor",
    [string]$PythonExe = "python"
)
$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$portableExe = Join-Path $projectDir "VRSMonitor.exe"
if (Test-Path $portableExe) {
    $action = New-ScheduledTaskAction -Execute $portableExe -WorkingDirectory $projectDir
} else {
    $pythonPath = (Get-Command $PythonExe).Source
    $action = New-ScheduledTaskAction -Execute $pythonPath -Argument 'app.py' -WorkingDirectory $projectDir
}
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 100 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $ServiceName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Контроль приемников Virtual Radar Server" -Force
Start-ScheduledTask -TaskName $ServiceName
Write-Host "Служба $ServiceName установлена и запущена."
