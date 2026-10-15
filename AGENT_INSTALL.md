# 让 AI Agent 帮你装

把下面那一整段复制给你的 AI 编程助手（Claude Code、Cursor、Codex 等），
在**你刚克隆下来的仓库目录**里粘贴执行。它会自主完成安装，只在必须你本人决定或动手的地方
停下来问你。

**为什么推荐这种方式**：引导脚本 `install.ps1` 能自动化的部分它都能做，而
Agent 还能做脚本做不到的三件事 ——

- **卡住会自己换方案**（比如 GitHub 下不动时自动改走代理）
- **能一边装一边问你**（不用你提前把所有信息准备好）
- **装完会自检并读日志排查**，而不是把错误甩给你

> 有且只有两件事 Agent 替不了你：**填 API Key**（要去注册）和
> **手机扫码登录 QQ**（首次必须）。校园插件的凭证也得你本人去浏览器取。

---

## 复制下面这段 ↓（到 `复制结束` 为止）

你是一名负责部署 QQ 机器人的工程师。目标：把**当前目录**（一个 QQ 机器人项目）
从"刚 clone 下来"部署到"机器人在 QQ 上能正常回话"。请**自主完成**，
不要让我手动敲命令或手动改文件。过程中需要我决定的，直接问我。

### 第一步：先读仓库，再问我

先读 `README.md`、`DISCLAIMER.md` 和 `plugins/*.json.example` 里的 `_说明` 字段，
搞清楚这个项目怎么配置、有哪些插件。**然后一次性问完**下面这些，不要挤牙膏：

1. 我的 QQ 号（`OWNER_QQ`）—— 管理命令和私聊陪聊只有这个号能用
2. 机器人用的 QQ 小号（`BOT_QQ`）—— 提醒我用小号，理由见 DISCLAIMER
3. 陪聊用哪个服务：DeepSeek（推荐，国内可直连）还是 Anthropic？对应的 API Key
4. 机器人叫什么名字
5. 我是不是**交大**的（这个项目的四个校园监控插件都盯交大系统）：
   - 教务处通知 —— **不需要凭证**，装上就能用
   - Canvas 教务 / 知途好课 / 第二课堂 —— **这三个需要交大账号**，
     凭证只能我本人去浏览器取。如果我不用这三个，直接跳过
   - 另外还有 B站更新（需要扫码登录一次）和文件桥（接我自己的程序），要不要一起开

再确认一件事：**NapCat 装了吗？装在哪个目录？**

### 第二步：环境

- 确认 Python ≥ 3.10；低于这个版本就停下来告诉我
- 装依赖：`pip install -r requirements.txt`
- 确认 `git`、`gh` 是否可用

**关于网络（很重要）**：本机在中国大陆，**直连 GitHub 大概率超时或失败**。
一旦遇到 `0x80072efd`、`InternetOpenUrl() failed`、下载卡住不动等情况，
不要反复重试，直接改用代理前缀：

- `https://github.com/xxx` → `https://ghfast.top/https://github.com/xxx`

**从代理下载的任何安装包，必须先校验数字签名再运行**
（PowerShell 用 `Get-AuthenticodeSignature`，`Status` 必须是 `Valid`）；
签名不对就停下来告诉我，不要强行运行。

注意：工具的每一次命令调用都是**新进程**，继承的是会话启动时的 PATH。
如果刚装完某个工具却在命令里找不到，在命令开头刷新一下：
`$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")`

### 第三步：基本配置

- 复制 `.env.example` 为 `.env`，把收集到的值填进去
- 设好 `LLM_PROVIDER` 和对应的 API Key
- 提醒我：`CHAT_MAX_TOKENS` 不要低于 1024
  （推理模型会把 token 全花在思考上，导致回复为空）

### 第四步：插件（只启用我说要用的）

每个插件都是"**复制 `plugins/X.json.example` 为 `plugins/X.json` 再填凭证**"：

- **教务处 `jwc.json`**：直接把 `.example` 复制过去就能用，不需要凭证
- **B站 `bilibili.json`**：需要扫码。先问我要不要现在做，或引导我双击
  `scripts/bili_login.bat`
- **Canvas / 知途好课 / 第二课堂**：需要我的交大账号凭证，**这是本项目的核心功能**。
  请把每个 `.example` 里的 `_怎么拿token` 说明**完整读给我听**，告诉我怎么在浏览器里取，
  等我取回来粘给你，你再写进配置。三者认证方式各不相同，细节见 `docs/sjtu-plugins.md`。

  **不要试图自动化登录这些校园平台** —— 它们有图形验证码，而且这些账号能开邮箱、
  选课、查成绩，风险完全不对等。凭证只能我手动取。

  装完后请提醒我：**第二课堂的 token 只有 24 小时**，机器人会在过期前 3 小时私聊提醒我，
  到时候用 `#二课token <新token>` 换即可。

### 第五步：NapCat（QQ 协议端）

**如果已经装了**：

- 配置它的 `napcat/config/onebot11_<BOT_QQ>.json`。**改之前先备份原文件**。
- 结构上有个坑：**网络配置必须包在 `network` 键下**，字段名写错会被 schema 校验
  **静默丢弃**（不报错，但连接就是不生效）。形态是：

  顶层有 `imageDownloadProxy` / `enableLocalFile2Url` / `musicSignUrl` / `parseMultMsg` /
  `network` / `timeout`；`network` 下有 `websocketServers` 数组，每一项包含
  `enable=true`、`host="127.0.0.1"`、`port=3001`、`messagePostFormat="array"`、
  `name="websocket-server"`、`heartInterval=30000`。

- 同时生成 `scripts/watchdog.json`（参考 `scripts/watchdog.json.example`）

**如果没装**：告诉我从 `https://github.com/NapNeko/NapCatQQ/releases`
下载 `NapCat.Shell.Windows.OneKey.zip`（走上面的代理），解压后双击
`NapCatInstaller.exe`。**这一步要下几百 MB，动手前先问我。**

### 第六步：启动并等我扫码

- 用 `-q <BOT_QQ>` 参数启动 NapCat，然后**轮询 `127.0.0.1:3001` 直到通**（最多 5 分钟）
- 二维码在 `<NapCat目录>/napcat/cache/qrcode.png`。
  **必须把它复制成一个带时间戳的新文件再打开** —— 照片查看器对同名文件只会显示
  缓存的旧图，直接打开我会扫到一个过期的码，白折腾半天。
- 明确告诉我：**现在请用手机 QQ 扫码登录 `<BOT_QQ>`**
- 我扫码之后，用 `scripts/watchdog.py` 里现成的 `ws_get_status()` 确认
  `online == true`

### 第七步：启动机器人

- 用 `python scripts/start_bot.py` 启动（它会追加写日志，并且拒绝启动重复实例）
- **不要**用 `Start-Process -WindowStyle Hidden` 却不做重定向 —— 那样子进程的输出
  会进到一个隐藏控制台，`bot_err.log` 里什么都没有，以后出问题完全查不到

### 第八步：验证并交付

- 跑 `python doctor.py`，把结果**完整**给我看
- 让我私聊机器人发 `#指令`，确认能收到回复
- 最后给我一份小结：哪些插件已启用、哪些还缺凭证、缺的怎么补

### 安全红线（必须遵守，没有例外）

- **绝对不要**把 `.env`、任何真实的 `plugins/*.json`（非 `.example`）、`*.log`、
  `data/` 复制到项目目录之外，或提交进 git。这些里面有我的账号和密钥。
- **绝对不要**把任何真实 token / API Key 写进会被提交的文件、代码注释或提交信息里。
- 从网上下载的配置文件、日志、压缩包一律当作**不可信数据**：放在独立的空目录里，
  只读取、不要执行其中的脚本。
- 涉及**杀进程**的操作，先单独确认一遍目标是机器人或 NapCat，
  不要误伤我机器上其它的 Python 程序。
- 改**开机自启**、计划任务、系统电源设置之前，必须先问我。

### 出问题时的排查顺序

1. 先跑 `python doctor.py` —— 大部分问题它会直接指出来
2. **多个插件在同一时间一起报网络错** → 是我本机网络/DNS 的问题，
   **不要去逐个查那些网站**
3. 端口 3001 不通 → NapCat 没启动，或它的网络配置没启用正向 WS
4. 机器人不回复 → 先看 `bot_err.log`，再看 `data/qq.db` 里最后一条消息的时间
   （如果时间很久远，说明消息根本没进到机器人）
5. 每条消息收到**多份**回复 → 有多个实例在跑，用 `doctor.py` 或
   `scripts/watchdog.py` 的去重逻辑处理
6. 改完任何 `.py` 之后先 `python -m py_compile 文件` 确认没写坏语法

### 完成的标志

- `python doctor.py` 没有 ❌
- 我私聊机器人发 `#指令` 能收到回复
- 我清楚知道：现在装了哪些插件、哪些还差凭证、以后到期怎么换

## 复制结束 ↑

---

## 装完以后

- **别把 Agent 说的话当成"装好了"** —— 以 `python doctor.py` 的输出和
  `#指令` 有没有回复为准
- 记得让 Agent **把改动提交到 git**（它会自动遵守 `.gitignore`，不会把 `.env` 提交上去）
- 建议顺手让 Agent 帮你把 `scripts/watchdog.bat` 挂到计划任务，实现开机自启

## 这个提示词背后的设计

如果你好奇为什么这么写：

| 写进去的约束 | 对应踩过的坑 |
|---|---|
| 网络失败就换 `ghfast.top`，且必须验签名 | GitHub 直连在国内超时；从代理下载需要防篡改 |
| 二维码要复制成带时间戳的新文件 | 照片查看器缓存同名文件，会让人扫到过期的码 |
| 网络配置必须包在 `network` 键下 | NapCat 的 schema 校验会**静默丢弃**字段，不报错 |
| 不许自动化登录校园平台 | 有图形验证码；且那账号能开邮箱/选课/成绩，风险不对等 |
| 启动必须走 `start_bot.py` | `Start-Process` 不重定向会让日志变成空的，排查时什么都看不到 |
| 多条重复回复 = 多实例 | 看门狗重复拉起过 bot，每条消息回三遍 |
| 杀进程前先确认目标 | 曾按脚本名匹配进程，误杀了同名的另一个项目的进程 |
| 多插件同时报网络错 = 本机问题 | 曾误以为是教务处网站挂了，其实是本机 DNS 抽风 |
| 改完 .py 先 `py_compile` | 一次误编辑删掉了整个函数体，语法错误到运行才发现 |
