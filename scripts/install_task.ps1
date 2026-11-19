<#
.SYNOPSIS
    注册 / 卸载「战锤信息日报」的 Windows 定时任务。

    会创建一个计划任务 + 一个开机启动项：
      InfoTwitter-Daily   每日 12:00 运行采集与推送（计划任务）
      启动文件夹里的 InfoTwitter-Server.vbs  登录时静默拉起日报页服务
      （推送里的「完整版」链接依赖这个服务）

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
    powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -At 11:55
    powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$DailyTask = 'InfoTwitter-Daily',
    [string]$ServerTask = 'InfoTwitter-Server',
    [string]$At = '12:00',
    [switch]$NoServer,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $root '.venv\Scripts\python.exe'
$runner = Join-Path $root 'run.py'

if ($Uninstall) {
    foreach ($name in @($DailyTask, $ServerTask)) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "已删除定时任务：$name" -ForegroundColor Yellow
        }
    }
    $vbs = Join-Path ([Environment]::GetFolderPath('Startup')) 'InfoTwitter-Server.vbs'
    if (Test-Path $vbs) {
        Remove-Item $vbs -Force
        Write-Host "已移除开机自启：$vbs" -ForegroundColor Yellow
    }
    Get-Process pythonw -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like '*InfoTwitter*' } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    return
}

foreach ($p in @($python, $runner)) {
    if (-not (Test-Path $p)) { throw "缺少文件：$p" }
}

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

# ---------- 每日抓取与推送 ----------
# 用 pythonw 而不是 python：避免每天 12:00 在桌面弹一个黑框
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }
$action = New-ScheduledTaskAction -Execute $pythonw -Argument 'run.py run' -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew `
    -Hidden -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)

if (Get-ScheduledTask -TaskName $DailyTask -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $DailyTask -Confirm:$false
}

# 优先用 S4U（不依赖用户是否登录）；注册失败（权限/策略）再退回 Interactive
$s4u = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
$registered = $false
try {
    Register-ScheduledTask -TaskName $DailyTask -Action $action -Trigger $trigger `
        -Settings $settings -Principal $s4u -Description '每天采集战锤情报，校验排序后推送' -ErrorAction Stop | Out-Null
    $registered = $true
    Write-Host "已注册每日任务 $DailyTask（每日 $At，S4U：未登录也会执行）" -ForegroundColor Green
} catch {
    Write-Host "S4U 注册失败（$($_.Exception.Message)），退回 Interactive 模式" -ForegroundColor Yellow
}
if (-not $registered) {
    Register-ScheduledTask -TaskName $DailyTask -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Description '每天采集战锤情报，校验排序后推送' | Out-Null
    Write-Host "已注册每日任务 $DailyTask（每日 $At，Interactive：需要保持登录）" -ForegroundColor Green
}

# ---------- 日报页服务 ----------
# 注意：AtLogOn / AtStartup 触发器需要管理员权限（实测返回 Access denied），
# 因此改用「启动文件夹」放一个 vbs 静默拉起服务，无需提权。
if (-not $NoServer) {
    $startup = [Environment]::GetFolderPath('Startup')
    $vbs = Join-Path $startup 'InfoTwitter-Server.vbs'
    $pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path $pythonw)) { $pythonw = $python }

    $content = @"
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "$root"
sh.Run """$pythonw"" ""$runner"" serve", 0, False
"@
    [System.IO.File]::WriteAllText($vbs, $content, (New-Object System.Text.UTF8Encoding $false))
    Write-Host "已安装开机自启：$vbs" -ForegroundColor Green

    # 立刻启动一次，别等下次登录。
    # 必须先在端口上探一下：脚本重复执行时无条件 Start-Process 会起第二个进程，
    # 而 http.server 的 allow_reuse_address 在 Windows 上允许重复绑定同一个端口。
    $cfgFile = Join-Path $root 'config\config.yaml'
    $port = 8080
    if (Test-Path $cfgFile) {
        $m = Select-String -Path $cfgFile -Pattern '^\s*report_port:\s*(\d+)' | Select-Object -First 1
        if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
    }
    $listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($listening) {
        Write-Host "日报服务已在端口 $port 运行（PID $($listening[0].OwningProcess)），跳过启动" -ForegroundColor Yellow
    } else {
        Start-Process -FilePath $pythonw -ArgumentList $runner, 'serve' -WorkingDirectory $root -WindowStyle Hidden
        Start-Sleep -Seconds 3
    }
}

$ip = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
    Select-Object -First 1).IPAddress

$cfgFile2 = Join-Path $root 'config\config.yaml'
$port2 = 8080
if (Test-Path $cfgFile2) {
    $m2 = Select-String -Path $cfgFile2 -Pattern '^\s*report_port:\s*(\d+)' | Select-Object -First 1
    if ($m2) { $port2 = [int]$m2.Matches[0].Groups[1].Value }
}

Write-Host ""
Write-Host "下次抓取：$((Get-ScheduledTaskInfo -TaskName $DailyTask).NextRunTime)"
Write-Host "日报地址：http://$ip`:$port2/latest.html"
Write-Host ""
Write-Host "提示：笔记本需接电源或允许唤醒；首次运行前请确认 .env 已配置密钥。" -ForegroundColor Yellow
