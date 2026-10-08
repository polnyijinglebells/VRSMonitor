$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = Join-Path $root "config.json"
$config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json

Write-Host "=== Проверка VRS Monitor ===" -ForegroundColor Cyan
Write-Host "VRS: $($config.vrs_url)"
try {
    $vrs = Invoke-RestMethod "$($config.vrs_url.TrimEnd('/'))/ServerConfig.json" -TimeoutSec 8
    $count = @($vrs.Receivers).Count
    Write-Host "OK: VRS отвечает, найдено приемников: $count" -ForegroundColor Green
} catch {
    Write-Host "ОШИБКА: VRS не отвечает. Проверьте vrs_url в config.json" -ForegroundColor Red
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
