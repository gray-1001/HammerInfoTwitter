<#
.SYNOPSIS
    注册「每周新品价格更新」的 Windows 定时任务。

    任务做的事（scripts\weekly_price_update.py）：
      抓目录 → 给新出现的商品补货号 → 补进商品目录 → 翻译 → 匹配写价格点

    与每日任务（InfoTwitter-Daily）完全独立：价格更新不阻塞日更，
    日更也不依赖它跑成功。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_weekly_task.ps1
    powershell -ExecutionPolicy Bypass -File scripts\install_weekly_task.ps1 -At 09:00 -DayOfWeek Monday
    powershell -ExecutionPolicy Bypass -File scripts\install_weekly_task.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'InfoTwitter-Price-Weekly',
    [string]$At = '09:00',
    [string]$DayOfWeek = 'Monday',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $root '.venv\Scripts\python.exe'
$script = Join-Path $root 'scripts\weekly_price_update.py'
$logDir = Join-Path $root 'logs'
$log = Join-Path $logDir 'price-weekly.log'

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "已删除定时任务：$TaskName" -ForegroundColor Yellow
    } else {
        Write-Host "没有找到任务：$TaskName" -ForegroundColor Yellow
    }
    return
}

foreach ($p in @($python, $script)) {
    if (-not (Test-Path $p)) { throw "缺少文件：$p" }
}
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

# 用 cmd 重定向输出：计划任务本身没法直接写日志文件
$action = New-ScheduledTaskAction -Execute 'cmd.exe' `
    -Argument "/c `"`"$python`" `"$script`" >> `"$log`" 2>&1`"" ` -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DayOfWeek -At $At
# 抓全站目录约 65 分钟，再补码/翻译/匹配，给 3 小时上限
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew `
    -Hidden -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$s4u = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
$done = $false
try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $s4u `
        -Description '每周抓取 Warhammer 新品价格，补进商品目录（模型/小说，不含涂料）' -ErrorAction Stop | Out-Null
    $done = $true
    Write-Host "已注册 $TaskName（每$DayOfWeek $At，S4U：未登录也会执行）" -ForegroundColor Green
} catch {
    Write-Host "S4U 注册失败（$($_.Exception.Message)），退回 Interactive" -ForegroundColor Yellow
}
if (-not $done) {
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description '每周抓取 Warhammer 新品价格，补进商品目录（模型/小说，不含涂料）' | Out-Null
    Write-Host "已注册 $TaskName（每$DayOfWeek $At，Interactive：需要保持登录）" -ForegroundColor Green
}

$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "下次运行：$($info.NextRunTime)"
Write-Host "日志文件：$log"
