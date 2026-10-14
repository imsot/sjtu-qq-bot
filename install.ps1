#Requires -Version 5.1
<#
  QQ 伴侣机器人 · 引导式安装

  用法：右键本文件 →「使用 PowerShell 运行」，或在终端里执行
        powershell -ExecutionPolicy Bypass -File install.ps1

  它会自动完成：装依赖 → 问你几个问题 → 写 .env 和各插件配置 →
  配好 NapCat 的 OneBot 连接 → 启动 NapCat 并等你扫码 → 启动机器人 → 体检。

  有且只有三步需要你亲自动手，这是物理上无法自动化的：
    1. 手机扫码登录 QQ（首次必须，之后就免扫码了）
    2. 填一个 LLM API Key（要去 DeepSeek/Anthropic 注册）
    3. 校园插件的凭证（用不到就不用管）

  脚本只写配置文件，不改系统设置；除了你选装的部分，不会碰别的目录。
#>

$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoDir

function Ok   ($t) { Write-Host "  [OK]   $t" -ForegroundColor Green }
function Warn ($t) { Write-Host "  [!]    $t" -ForegroundColor Yellow }
function Bad  ($t) { Write-Host "  [X]    $t" -ForegroundColor Red }
function Step ($t) { Write-Host ""; Write-Host "── $t " -NoNewline; Write-Host ("─" * [Math]::Max(0, 46 - $t.Length)) }

function Ask($prompt, $default) {
    if ($default) { $t = Read-Host "$prompt [$default]" } else { $t = Read-Host $prompt }
    if ([string]::IsNullOrWhiteSpace($t)) { return $default }
    return $t.Trim()
}

function Ask-Yes($prompt, $defaultYes) {
    $hint = if ($defaultYes) { "Y/n" } else { "y/N" }
    $t = Read-Host "$prompt [$hint]"
    if ([string]::IsNullOrWhiteSpace($t)) { return $defaultYes }
    return ($t.Trim().ToLower() -eq "y")
}

function Test-Port($host_, $port) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect($host_, $port)
        $c.Close()
        return $true
    } catch { return $false }
}

function Set-EnvValue([string[]]$lines, [string]$key, [string]$value) {
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match ("^\s*" + [regex]::Escape($key) + "\s*=")) {
            $lines[$i] = "$key=$value"
            return $lines
        }
    }
    return $lines + "$key=$value"
}

function Find-Python {
    $cands = @()
    if (Get-Command py -ErrorAction SilentlyContinue) { $cands += ,@("py", @("-3")) }
    if (Get-Command python -ErrorAction SilentlyContinue) { $cands += ,@("python", @()) }
    if (Get-Command python3 -ErrorAction SilentlyContinue) { $cands += ,@("python3", @()) }

    foreach ($c in $cands) {
        $exe = $c[0]; $pre = $c[1]
        try {
            $out = & $exe @pre "-c" "import sys; print(sys.version_info[0]*100+sys.version_info[1])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) {
                return @{ Exe = $exe; Pre = $pre; Ver = [int]$out }
            }
        } catch { }
    }
    return $null
}

# ============================================================
Write-Host ""
Write-Host "========================================================"
Write-Host " QQ 伴侣机器人 · 引导式安装"
Write-Host " 目录：$RepoDir"
Write-Host "========================================================"
Write-Host ""
Warn "使用第三方 QQ 协议登录有账号风险，请务必用小号。详见 DISCLAIMER.md"
Write-Host ""
if (-not (Ask-Yes "开始安装？" $true)) { Write-Host "已取消。"; exit 0 }

# ---------- 1. 环境 ----------
Step "1/7 检查环境"

if (Test-Path (Join-Path $RepoDir "main.py")) { Ok "在仓库根目录" }
else { Bad "这个目录里没有 main.py，请把 install.ps1 放在仓库根目录运行"; exit 1 }

$pyInfo = Find-Python
if ($null -eq $pyInfo) {
    Bad "没找到 Python。装一个 3.10+ 再来：https://www.python.org/downloads/"
    exit 1
}
if ($pyInfo.Ver -lt 310) {
    Bad "Python 版本太低（需要 3.10+）"
    exit 1
}
$Python = $pyInfo.Exe
$PyPre = $pyInfo.Pre
$pyLabel = (@($Python) + $PyPre) -join " "
Ok "Python $([Math]::Floor($pyInfo.Ver / 100)).$($pyInfo.Ver % 100)　($pyLabel)"

# ---------- 2. 依赖 ----------
Step "2/7 安装依赖"
try {
    & $Python @PyPre "-m" "pip" "install" "-q" "-r" (Join-Path $RepoDir "requirements.txt")
    if ($LASTEXITCODE -eq 0) { Ok "依赖已安装" }
    else { Warn "pip 返回了非零退出码，等下体检时会告诉你缺什么" }
} catch {
    Warn "依赖安装失败：$_"
    Warn "可以稍后手动执行：$pyLabel -m pip install -r requirements.txt"
}

# ---------- 3. 基本配置 ----------
Step "3/7 填写基本信息"

$owner = Ask "你的 QQ 号（管理命令和私聊陪聊只有这个号能用）" ""
while (-not ($owner -match '^\d{5,12}$')) {
    Bad "看起来不是有效的 QQ 号"
    $owner = Ask "你的 QQ 号" ""
}

$botQQ = Ask "机器人的 QQ 号（建议用小号）" ""
while (-not ($botQQ -match '^\d{5,12}$')) {
    Bad "看起来不是有效的 QQ 号"
    $botQQ = Ask "机器人的 QQ 号" ""
}

Write-Host ""
Write-Host "  陪聊用哪个模型服务？"
Write-Host "    1) DeepSeek（推荐：国内直连、便宜）"
Write-Host "    2) Anthropic / Claude（需要能访问 Anthropic 的网络）"
$providerChoice = Ask "选择 1 或 2" "1"
if ($providerChoice -eq "2") {
    $provider = "anthropic"; $keyName = "ANTHROPIC_API_KEY"
    Write-Host "  去 https://console.anthropic.com 申请 Key"
} else {
    $provider = "deepseek"; $keyName = "DEEPSEEK_API_KEY"
    Write-Host "  去 https://platform.deepseek.com 申请 Key"
}
$apiKey = Ask "粘贴 API Key（直接回车 = 稍后自己填到 .env）" ""

$botName = Ask "机器人叫什么名字" "小助手"

# 写 .env
$envExample = Join-Path $RepoDir ".env.example"
if (-not (Test-Path $envExample)) { Bad "找不到 .env.example"; exit 1 }
$lines = [System.IO.File]::ReadAllLines($envExample, [System.Text.Encoding]::UTF8)

$lines = Set-EnvValue $lines "OWNER_QQ" $owner
$lines = Set-EnvValue $lines "BOT_QQ" $botQQ
$lines = Set-EnvValue $lines "LLM_PROVIDER" $provider
$lines = Set-EnvValue $lines "BOT_NAME" $botName
if ($apiKey) { $lines = Set-EnvValue $lines $keyName $apiKey }

$envPath = Join-Path $RepoDir ".env"
[System.IO.File]::WriteAllLines($envPath, $lines, (New-Object System.Text.UTF8Encoding $false))
Ok "已写入 .env"

# ---------- 4. 插件 ----------
Step "4/7 选择要启用的插件"

$pluginMenu = @(
    @{ File = "jwc.json";           Name = "教务处通知（无需凭证，但栏目是上海交大的）" },
    @{ File = "bilibili.json";      Name = "B站更新提醒（需要扫码登录一次，任何人可用）" },
    @{ File = "canvas.json";        Name = "Canvas 教务（需交大账号）" },
    @{ File = "aihaoke.json";       Name = "知途好课（需交大账号）" },
    @{ File = "sjtu_academic.json"; Name = "第二课堂（需交大账号，凭证 24 小时过期）" }
)

$enabledPlugins = @()
foreach ($p in $pluginMenu) {
    if (Ask-Yes "  启用「$($p.Name)」？" $false) {
        Copy-Item (Join-Path $RepoDir "plugins\$($p.File).example") (Join-Path $RepoDir "plugins\$($p.File)") -Force
        $enabledPlugins += $p.File
        Ok "  → 已生成 plugins\$($p.File)（凭证要你自己填，见文件里的 _说明）"
    }
}
if ($enabledPlugins.Count -eq 0) { Write-Host "  （都没选，随时可以复制 plugins\*.json.example 再启用）" }

# ---------- 5. NapCat ----------
Step "5/7 配置 NapCat（QQ 协议端）"

$wsPort = 3001
$napcatDir = ""

Write-Host "  NapCat 负责真正登录 QQ。如果你还没装："
Write-Host "    https://github.com/NapNeko/NapCatQQ/releases"
Write-Host "    下载 NapCat.Shell.Windows.OneKey.zip，解压后双击 NapCatInstaller.exe"
Write-Host ""
$napcatDir = Ask "NapCat.Shell 目录的完整路径（没有就回车跳过）" ""

if ($napcatDir) {
    if (-not (Test-Path $napcatDir)) {
        Bad "这个路径不存在：$napcatDir"
        $napcatDir = ""
    } elseif (-not (Test-Path (Join-Path $napcatDir "index.js"))) {
        Warn "这个目录里没有 index.js，可能不是 NapCat.Shell"
        if (-not (Ask-Yes "  仍然使用它？" $false)) { $napcatDir = "" }
    }
}

if ($napcatDir) {
    Ok "NapCat 目录：$napcatDir"

    # 直接写 OneBot 配置，省得去 WebUI 里点
    $cfgDir = Join-Path $napcatDir "napcat\config"
    if (-not (Test-Path $cfgDir)) {
        New-Item -ItemType Directory -Path $cfgDir -Force | Out-Null
        Write-Host "  （配置目录不存在，已创建——NapCat 首次启动后会沿用里面已有的配置）"
    }
    $obFile = Join-Path $cfgDir "onebot11_$botQQ.json"

    if (Test-Path $obFile) {
        Copy-Item $obFile "$obFile.bak_$(Get-Date -Format yyyyMMdd_HHmmss)" -Force
        Ok "已备份原有 onebot11 配置"
    }

    # 结构必须把网络配置包在 network 键下，否则 NapCat 会静默丢弃
    $obConfig = @{
        imageDownloadProxy   = ""
        enableLocalFile2Url  = $false
        musicSignUrl         = ""
        parseMultMsg         = $false
        network              = @{
            websocketClients = @()
            plugins          = @()
            websocketServers = @(@{
                enable              = $true
                token               = ""
                reportSelfMessage   = $false
                enableForcePushEvent = $true
                messagePostFormat   = "array"
                name                = "websocket-server"
                debug               = $false
                host                = "127.0.0.1"
                port                = $wsPort
                heartInterval       = 30000
            })
            httpSseServers   = @()
            httpClients      = @()
            httpServers      = @()
        }
        timeout              = @{
            uploadSpeedKBps   = 256
            downloadSpeedKBps = 256
            maxTimeout        = 1800000
            baseTimeout       = 10000
        }
    }
    $json = $obConfig | ConvertTo-Json -Depth 8
    [System.IO.File]::WriteAllText($obFile, $json, (New-Object System.Text.UTF8Encoding $false))
    Ok "已写入 OneBot 正向 WS 配置（127.0.0.1:$wsPort）——不用再去 WebUI 点"

    # 看门狗的配置也一并生成
    $wd = @{
        napcat_dir = $napcatDir
        bot_dir    = ""
        python     = ""
        bot_qq     = $botQQ
        ws_port    = $wsPort
        notify_ps1 = ""
    }
    [System.IO.File]::WriteAllText((Join-Path $RepoDir "scripts\watchdog.json"),
        ($wd | ConvertTo-Json -Depth 4), (New-Object System.Text.UTF8Encoding $false))
    Ok "已写入 scripts\watchdog.json"
} else {
    Warn "跳过 NapCat 配置。装好之后可以重新运行本脚本，或手动在 WebUI 里添加一个"
    Warn "「WebSocket 服务端（正向 WS）」、端口 $wsPort 的网络配置。"
}

# ---------- 6. 启动 ----------
Step "6/7 启动 NapCat 和机器人"

if ($napcatDir -and -not (Test-Port "127.0.0.1" $wsPort)) {
    Write-Host "  正在启动 NapCat…"
    $node = Join-Path $napcatDir "node.exe"
    if (Test-Path $node) {
        Start-Process -FilePath $node -ArgumentList "index.js", "-q", $botQQ `
            -WorkingDirectory $napcatDir -WindowStyle Minimized
        Ok "已启动（用 -q $botQQ 快速登录；首次会需要扫码）"
    } else {
        Warn "没找到 $node，请手动双击 napcat.bat 启动"
    }

    Write-Host ""
    Write-Host "  ⏳ 等待 NapCat 就绪（最多 5 分钟）…"
    Write-Host "     如果弹出了二维码，请用手机 QQ 扫码登录【$botQQ】"
    Write-Host ""

    $qr = Join-Path $napcatDir "napcat\cache\qrcode.png"
    $qrShown = $false
    $deadline = (Get-Date).AddSeconds(300)
    while ((Get-Date) -lt $deadline) {
        if (Test-Port "127.0.0.1" $wsPort) { break }
        # 二维码出来了就弹一次。必须复制成带时间戳的新文件：
        # 照片查看器对同名文件只显示缓存的旧图，会让人扫到过期码。
        if (-not $qrShown -and (Test-Path $qr)) {
            try {
                $snap = Join-Path $env:TEMP "qq_login_qr_$(Get-Date -Format yyyyMMddHHmmss).png"
                Copy-Item $qr $snap -Force
                Invoke-Item $snap
                $qrShown = $true
                Write-Host "  📱 二维码已弹出，请扫码"
            } catch { }
        }
        Start-Sleep -Seconds 3
    }

    if (Test-Port "127.0.0.1" $wsPort) {
        Ok "NapCat 就绪，$wsPort 已监听"
    } else {
        Warn "等了 5 分钟还没就绪。可能是还没扫码，或者安装有问题。"
        Warn "扫码登录后机器人会自动连上；也可以稍后运行 python doctor.py 看卡在哪。"
    }
} elseif ($napcatDir) {
    Ok "NapCat 已在运行"
}

# 启动机器人
Write-Host ""
$alreadyRunning = $false
$procs = @(Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*$RepoDir*main.py*" })
if ($procs.Count -gt 0) { $alreadyRunning = $true; Ok "机器人已在运行（PID $($procs[0].ProcessId)）" }

if (-not $alreadyRunning) {
    $startBot = Join-Path $RepoDir "scripts\start_bot.py"
    if (Test-Path $startBot) {
        # 走 start_bot.py：日志追加写、拒绝重复实例
        & $Python @PyPre $startBot
    } else {
        Start-Process -FilePath $Python -ArgumentList (@($PyPre) + @("main.py")) -WorkingDirectory $RepoDir
    }
    Start-Sleep -Seconds 4
    Ok "机器人已启动"
}

# ---------- 7. 体检 ----------
Step "7/7 体检"
Write-Host ""
try { & $Python @PyPre (Join-Path $RepoDir "doctor.py") }
catch { Warn "体检脚本出错：$_" }

# ---------- 收尾 ----------
Write-Host ""
Write-Host "========================================================"
Write-Host " 完成"
Write-Host "========================================================"
Write-Host ""
Write-Host " 下一步："
Write-Host "   1. 用你的 QQ（$owner）私聊机器人，发  #指令  看看它认不认你"
Write-Host "   2. 校园插件的凭证还没填——打开 plugins\ 下对应的 .json，"
Write-Host "      按照里面的 _说明 填好，然后发 #xx检查 立刻生效"
Write-Host "   3. 想开机自启：把 scripts\watchdog.bat 挂到「任务计划程序」，"
Write-Host "      触发频率设成每 1~5 分钟一次"
Write-Host ""
Write-Host " 出问题了随时运行：$pyLabel doctor.py"
Write-Host ""
