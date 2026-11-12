$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = Join-Path $root "config.json"
$config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json

Write-Host "=== Проверка VRS Monitor ===" -ForegroundColor Cyan
Write-Host "VRS: $($config.vrs_url)"
$headers = @{}
if ($config.vrs_username) {
    $credentials = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("$($config.vrs_username):$($config.vrs_password)"))
    $headers.Authorization = "Basic $credentials"
}
try {
    $vrs = Invoke-RestMethod "$($config.vrs_url.TrimEnd('/'))/ServerConfig.json" -TimeoutSec 8
    $count = @($vrs.Receivers).Count
    Write-Host "OK: VRS отвечает, найдено приемников: $count" -ForegroundColor Green
} catch {
    Write-Host "ОШИБКА: VRS не отвечает. Проверьте vrs_url в config.json" -ForegroundColor Red
    Write-Host $_.Exception.Message
}

try {
    $state = Invoke-RestMethod "$($config.vrs_url.TrimEnd('/'))/WebAdmin/Index/GetState" -Headers $headers -TimeoutSec 8
    $feeds = @($state.Response.Feeds)
    Write-Host "OK: VRS WebAdmin отвечает, состояний каналов: $($feeds.Count)" -ForegroundColor Green
    $feeds | Select-Object Name,ConnDesc,Msgs | Format-Table -AutoSize
} catch {
    Write-Host "ОШИБКА: состояния каналов VRS недоступны." -ForegroundColor Red
    Write-Host "Проверьте плагин WebAdmin, vrs_username и vrs_password в config.json."
    Write-Host $_.Exception.Message
}

$monitorUrl = "http://127.0.0.1:$($config.listen_port)"
try {
    $health = Invoke-RestMethod "$monitorUrl/health" -TimeoutSec 3
    Write-Host "OK: панель работает — $monitorUrl" -ForegroundColor Green
} catch {
    Write-Host "Панель пока не запущена. Запустите start.bat или VRSMonitor.exe" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Нажмите Enter для выхода"
Read-Host | Out-Null
