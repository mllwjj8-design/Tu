# 一键把 site 目录推到 GitHub Pages
#
# 用法（在 site 目录下打开 PowerShell）：
#   .\deploy.ps1 -Repo https://github.com/<用户名>/<仓库名>.git
#
# 前提：仓库已在 GitHub 网页上创建好（不要勾选任何初始化文件），
#      首次 push 会弹窗让你登录授权。
#
# 推送后若页面 404：仓库 Settings -> Pages -> Deploy from a branch / main / (root)

param(
    [Parameter(Mandatory = $true)]
    [string]$Repo,

    [string]$Branch = "main"
)

# 注意：不要设 $ErrorActionPreference = "Stop"，
# git 往 stderr 写 warning（如 LF/CRLF）会被 PowerShell 当成终止错误。
# 这里统一按 $LASTEXITCODE 判断成败。

function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$GitArgs)
    $out = & git @GitArgs 2>&1
    if ($out) { $out | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray } }
    if ($LASTEXITCODE -ne 0) {
        throw ("git " + ($GitArgs -join " ") + " 失败（退出码 " + $LASTEXITCODE + "）")
    }
}

if (-not (Test-Path ".git")) {
    Write-Host "初始化本地仓库..." -ForegroundColor Cyan
    Invoke-Git init -b $Branch
}

$name = (& git config --get user.name 2>$null)
if (-not $name) {
    & git config user.name "wj" 2>$null
    & git config user.email "wj@users.noreply.github.com" 2>$null
    Write-Host "已设置本仓库提交身份 wj <wj@users.noreply.github.com>（仅本仓库，可用 git config 修改）" -ForegroundColor Yellow
}

Invoke-Git add -A

$staged = (& git diff --cached --name-only 2>$null)
if (-not $staged) {
    Write-Host "没有需要提交的改动。" -ForegroundColor Yellow
}
else {
    Write-Host "本次提交文件：" -ForegroundColor Cyan
    $staged | ForEach-Object { Write-Host "  $_" }
    Invoke-Git commit -m "update gallery"
}

$remotes = @(& git remote 2>$null)
if ($remotes -notcontains "origin") {
    Invoke-Git remote add origin $Repo
}
else {
    Invoke-Git remote set-url origin $Repo
}

Write-Host "推送到 $Repo ..." -ForegroundColor Cyan
Invoke-Git push -u origin $Branch

Write-Host ""
Write-Host "推送完成。首次发布请到仓库 Settings -> Pages：" -ForegroundColor Green
Write-Host "  Source  = Deploy from a branch"
Write-Host "  Branch  = $Branch"
Write-Host "  Directory = /(root)"
Write-Host "等 1~2 分钟后访问 https://<用户名>.github.io/<仓库名>/"
