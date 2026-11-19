<#
.SYNOPSIS
    把仓库推送到 GitHub，令牌只从环境变量读取，不落盘、不写进 .git/config。

.DESCRIPTION
    安全约定：
      - 令牌通过 GH_TOKEN 环境变量（进程级或用户级）提供；
      - 用一次性 credential helper 传给 git，**不会**写进 .git/config 或远程 URL；
      - 脚本本身不含任何密钥。

    推送前请确认仓库里没有 .env / 数据库 / cookie / 浏览器登录态。
    快速自检：
        git ls-files | Select-String -Pattern '\.env$|cookies|browser_profile|\.db$'

.EXAMPLE
    # 一次性设置（新开的终端才读得到）
    setx GH_TOKEN "ghp_xxxxxxxxxxxxxxxx"

    # 然后
    powershell -ExecutionPolicy Bypass -File scripts\push_to_github.ps1
#>
[CmdletBinding()]
param(
    [string]$Remote = 'https://github.com/gray-1001/HammerInfoTwitter.git',
    [string]$Branch = 'main'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

# 1. 取令牌：先看当前进程，再看用户级（setx 写的那份）
$token = $env:GH_TOKEN
if (-not $token) {
    $token = [Environment]::GetEnvironmentVariable('GH_TOKEN', 'User')
}
if (-not $token) {
    throw "未找到 GH_TOKEN。请先在本机执行： setx GH_TOKEN `"你的PAT`"  （然后确认 PAT 具备 repo 权限）"
}

# 2. 推送前自检：跟踪文件里不能有敏感路径
$tracked = git -c core.quotepath=false ls-files
$bad = $tracked | Where-Object { $_ -match '(^|/)(\.env$|cookies|browser_profile|infotwitter\.db|logs/|reports/)' }
if ($bad) {
    throw "仓库里仍有敏感文件，拒绝推送：`n$($bad -join "`n")"
}

# 3. 确保远端存在
$existing = git remote get-url origin 2>$null
if (-not $existing) {
    git remote add origin $Remote
} elseif ($existing -ne $Remote) {
    git remote set-url origin $Remote
}

# 4. 代理：GitHub 在部分网络下必须走本机代理，否则 push 会被重置连接
#    优先用 HTTPS_PROXY 环境变量，其次读项目 config/config.yaml 的 proxy
$proxy = $env:HTTPS_PROXY
if (-not $proxy) {
    $cfgFile = Join-Path $root 'config\config.yaml'
    if (Test-Path $cfgFile) {
        $m = Select-String -Path $cfgFile -Pattern '^\s*proxy:\s*"?([^"\s#]+)"?' | Select-Object -First 1
        if ($m) { $proxy = $m.Matches[0].Groups[1].Value }
    }
}
$proxyArgs = @()
if ($proxy) {
    $proxyArgs = @('-c', "http.proxy=$proxy", '-c', "https.proxy=$proxy")
    Write-Host "使用代理：$proxy"
} else {
    Write-Host "未检测到代理配置，直连 GitHub" -ForegroundColor Yellow
}

# 5. 用一次性 helper 传令牌（不出现在命令行回显/配置文件里）
$env:GIT_TOKEN = $token
try {
    $gitArgs = @()
    if ($proxy) {
        $gitArgs += @('-c', "http.proxy=$proxy", '-c', "https.proxy=$proxy")
    }
    # 单引号包裹，保证 $GIT_TOKEN 交给 git 的 sh 去展开，而不是被 PowerShell 提前替换
    $gitArgs += @('-c', 'credential.helper=!f() { echo username=x-access-token; echo password=$GIT_TOKEN; }; f')
    $gitArgs += @('push', '-u', 'origin', $Branch)

    & git @gitArgs
    if ($LASTEXITCODE -ne 0) { throw "推送失败（退出码 $LASTEXITCODE）" }
} finally {
    Remove-Item Env:\GIT_TOKEN -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "推送完成：$Remote（分支 $Branch）" -ForegroundColor Green
git log -1 --pretty='  最新提交：%h %s（%an）'
