# QQ 校园助手 🤖

> 一个挂在你自己 QQ 上的 AI 助手：**陪聊 + 定时提醒 + 群管理 + 校园信息聚合**。

它不只会在群里卖萌，更会替你**盯着那些你总是忘记看的地方**：

- 📋 教务处又发了什么通知
- 🎓 Canvas 上新了什么作业、什么时候截止
- 📚 知途好课的任务做完了没
- 🏫 第二课堂有什么活动开放报名了、你的收藏什么时候开抢
- 📺 关注的 UP 主更新了

所有新内容**主动私聊推给你**，不用你自己去一个个网站刷。

```
QQ 账号
   ↓
NapCat（QQ 协议端，OneBot v11 正向 WS）      ← 第三方组件，见免责声明
   ↓   ws://127.0.0.1:3001
本机器人（Python）
   ├── AI 陪聊（DeepSeek / Anthropic）
   ├── SQLite（聊天记忆 + 定时提醒）
   └── 插件系统（11 个插件，可自由增删）
```

---

## ⚠️ 先读这个

**请用小号运行机器人。** 第三方 QQ 协议登录违反腾讯用户协议，账号有可能被风控甚至封禁。
完整的风险说明见 [DISCLAIMER.md](DISCLAIMER.md)。

**校园监控插件仅供上海交通大学使用者。** Canvas / 知途好课 / 第二课堂三个插件需要你的
交大账号凭证；如果你不是交大用户，删掉 `plugins/` 下对应的 `.py` 即可，其余功能不受影响。
教务处插件读取公开网页，但栏目地址也是交大的。

---

## 特性

| 功能 | 说明 |
|---|---|
| 💬 **AI 陪聊** | 私聊直接聊（带记忆）、群里 @ 即回；人设、模型、上下文长度都可配 |
| 🧠 **serious 通道** | 消息以 `serious ` 开头时换一个不带人设的模型，用来问正经问题 |
| ⏰ **定时提醒** | `#提醒 明早8点 打卡`，支持"5分钟 / 2小时 / 3天后 / 今晚9点半"；存 SQLite，重启不丢 |
| 🛡 **群管理** | 禁言 / 解禁 / 移出 / 全体禁言、新人进群自动欢迎、关键词自动回复 |
| 🎲 **娱乐** | 掷骰子、翻硬币、今日运势 |
| 🎓 **Canvas 监控** | 公告 / 作业 / 课件 / 成绩，截止前 24h、2h 提醒，每晚 22:00 汇总明日待办 |
| 📚 **知途好课** | 新任务推送、未完成必做任务清单（`#好课检查`） |
| 🏫 **第二课堂** | 新活动开放报名即推；收藏的活动在**报名开始前 1 天 / 1 小时 / 10 分钟**精确提醒 |
| 📋 **教务处** | 7 个栏目新通知增量推送（无需凭证） |
| 📺 **B站更新** | 关注的 UP 主新投稿推送，自带扫码登录脚本 |
| 📤 **文件桥接** | 把你本机另一个程序产出的 `.md` / `.pdf` 推送到 QQ（零代码耦合） |
| 📖 **指令大全** | `#指令` 自动汇总所有插件声明的命令，新增插件不用改文档 |

### 几个值得一提的设计

- **凭证脱敏**：带 token 的命令（如 `#二课token`）在日志里只留前缀，正文替换成
  `<正文已隐去>`；发到群里会被先撤回再警告。插件只要声明 `redact_prefixes` 即可。
- **浏览器脚本自动续凭证**：第二课堂 / 知途好课在两个本地回环端口上各开一个小接口，
  配一个 Tampermonkey 用户脚本，你在浏览器里正常使用网站时 token 就自动同步过来了，
  不用每天手动复制粘贴。（三层防护：只绑 127.0.0.1 + 密钥 + 只放行本站来源）
- **配置缺失不崩**：任何插件没有配置文件时只是"功能停用"，不影响其它功能。
- **看门狗**：`scripts/watchdog.py` 定时检查进程和端口，NapCat 掉线/假活自动重启，
  失效时弹二维码提醒你扫码；bot 挂了自动拉起，并防止重复实例。
- **离线自测**：`selftest.py` 用一个假 OneBot 服务器跑通事件流转，不需要 NapCat，
  也不碰任何真实凭证。

---

## 快速开始

三种装法，挑一种：

| 方式 | 适合谁 | 你需要做的 |
|---|---|---|
| **① 让 AI Agent 装**（推荐） | 有 Claude Code / Cursor 等工具 | 粘一段提示词，然后回答问题、扫一次码 |
| **② 引导式脚本** | 想自己看着装 | 跑一条命令，按提示输入，扫一次码 |
| **③ 手动** | 想完全掌控每一步 | 照着下面的步骤一步步来 |

> 不管哪种方式，**只有三件事必须你本人做**（物理上无法自动化）：
> **手机扫码登录 QQ**（首次）、**填一个 LLM API Key**（要注册）、
> **校园插件的凭证**（用不到就不用管）。

### ① 让 AI Agent 帮你装（推荐）

打开 [AGENT_INSTALL.md](AGENT_INSTALL.md)，把里面那段提示词整个复制给你的 AI 编程助手，
在仓库目录里执行。它会自主完成安装 —— 卡住会自己换方案（比如 GitHub 下不动时改走代理）、
一边装一边问你、装完还会跑体检并读日志排查。

### ② 引导式脚本

```powershell
# 右键 install.ps1 →「使用 PowerShell 运行」，或者：
powershell -File install.ps1

# 从网上下载的脚本可能被标记为「来自 Internet」而拒绝运行，解除即可：
Unblock-File .\install.ps1
```

它会自动装依赖、问你要几个信息、写好 `.env` 和插件配置、**直接写 NapCat 的 OneBot 配置**
（不用去 WebUI 里点）、启动并等你扫码，最后跑一遍体检。

---

## 手动安装

不想用上面两种方式的话，照这里一步步来。**下面这段描述的就是
[AGENT_INSTALL.md](AGENT_INSTALL.md) 里那位 Agent 会替你做的事。**

### 0. 环境要求

- **Windows 10/11**（运维脚本 `scripts/*.bat`、看门狗是 Windows 专用的；
  `main.py` 本身跨平台，Linux/macOS 用户可自行改用 systemd 等）
- **Python 3.10+**（开发环境为 3.14；代码用到 `X | None` 这类新语法）

### 1. 装依赖

```powershell
pip install -r requirements.txt
```

### 2. 装 NapCat（QQ 协议端）

1. 打开 <https://github.com/NapNeko/NapCatQQ/releases>，下载
   `NapCat.Shell.Windows.OneKey.zip`（一键包，**内置 QQ，不用另装**）。
2. 解压 → 双击 `NapCatInstaller.exe`，等它自动配置完成。
3. 进入生成的 `NapCat.XXXX.Shell` 目录，双击 `napcat.bat`。
4. 启动后日志里会打印一行 WebUI 地址，类似
   `http://127.0.0.1:6099/webui?token=xxxxxxxx`，浏览器打开它，
   **扫码登录你的机器人小号**。
5. WebUI → **网络配置** → **添加网络配置**：
   - 类型选 **WebSocket 服务端（正向 WS）**
   - 端口填 `3001`
   - 保存并启用
6. 让 NapCat 窗口保持运行（最小化即可）。

> 建议把 WebUI 的监听地址限制在 `127.0.0.1`，不要暴露到局域网/公网。

### 3. 配置机器人

```powershell
copy .env.example .env
```

编辑 `.env`，最少填这三项：

```ini
OWNER_QQ=你的QQ号
BOT_QQ=机器人QQ号
DEEPSEEK_API_KEY=sk-你的key
```

> `LLM_PROVIDER` 默认 `deepseek`（国内直连、便宜）。想用 Claude 就改成 `anthropic`
> 并填 `ANTHROPIC_API_KEY`，但需要能访问 Anthropic 的网络。

### 4. 启用你想用的监控插件

插件的配置都是"**复制模板再改**"：

```powershell
cd plugins
copy canvas.json.example          canvas.json          # Canvas 教务（需交大身份）
copy bilibili.json.example        bilibili.json        # B站更新
copy aihaoke.json.example         aihaoke.json         # 知途好课（需交大身份）
copy sjtu_academic.json.example   sjtu_academic.json   # 第二课堂（需交大身份）
copy jwc.json.example             jwc.json             # 教务处（无需凭证）
```

**不需要的插件，连 json 都不用建 —— 它会自己停用。**
每个模板文件里的 `_说明` / `_怎么拿token` 字段写清了凭证从哪来。

> 这些真实配置文件都已被 `.gitignore` 忽略，不会被误提交。

### 5. 启动

```powershell
python main.py
```

看到 `WS 已连接 ✅` 就成功了。用 `OWNER_QQ` 那个号私聊机器人发 `#指令`，会收到完整的命令清单。

### 6. （可选）开机自启 + 看门狗

```powershell
cd scripts
copy watchdog.json.example watchdog.json
```

编辑 `scripts/watchdog.json`，填上 NapCat 装在哪、机器人 QQ 号等。然后：

- **一次性启动**：双击 `scripts/start_all.bat`
- **常驻看门狗**：把 `scripts/watchdog.bat` 挂到 Windows 计划任务（每 1~5 分钟一次），
  它会自动重启挂掉的进程，并在登录失效时弹二维码提醒你。

---

## 指令

发 `#指令` 给机器人会自动列出**你当前启用**的全部命令（未启用的插件不会出现）。
常用的一部分：

**所有人可用**

| 命令 | 说明 |
|---|---|
| `#指令` | 命令大全（别名 `#帮助` / `#help`） |
| `#掷骰子` / `#翻硬币` / `#今日运势` | 娱乐 |
| 私聊直接发消息 / 群里 `@机器人` | 和它聊天 |

**仅主人（`OWNER_QQ`）**

| 命令 | 说明 |
|---|---|
| `serious <问题>` | 严肃模式，不带人设（**不带 `#`**） |
| `#提醒 5分钟 喝水` / `#我的提醒` / `#取消提醒 3` | 定时提醒 |
| `#禁言 @某人 5分钟` / `#解禁` / `#移出` / `#全体禁言` / `#全体解禁` | 群管理 |
| `#Canvas状态` / `#Canvas检查` | Canvas 监控 |
| `#好课状态` / `#好课检查` / `#好课课程` / `#好课同步` | 知途好课 |
| `#二课状态` / `#二课检查` / `#二课收藏` / `#二课提醒` / `#二课类型` / `#二课token` / `#二课同步` | 第二课堂 |
| `#教务状态` / `#教务检查` / `#教务栏目` | 教务处通知 |
| `#B站状态` / `#B站检查` | B站更新 |

> 约定：**只有 `serious` 不带 `#`**，其余命令都带 `#`。

### 文件桥：把你自己的程序接到 QQ 上

`plugins/macro.py` 让**任何本机程序**都能往你 QQ 推消息，两个程序之间零耦合，
只靠文件通信。在 `.env` 里设好 `MACRO_DIR` 之后：

| 丢进这个目录 | 效果 |
|---|---|
| `<MACRO_DIR>/output/alerts/*.md` | 内容逐条私聊给你，推完归档到 `sent/` |
| `<MACRO_DIR>/output/reports/*.pdf` | 作为附件私聊给你；同名 `.md` 的前 12 行当摘要 |

`examples/` 里有三个能直接跑的示例：

- **`stock_daily_alert.py`** —— 股票每日推送：盯一组标的，涨跌幅超阈值就告警
  （用腾讯公开行情接口，免登录免 Key）
- **`daily_report.py`** —— 生成日报（`.md` + 可选 `.pdf`），演示"摘要 + 附件"
- **`hello_alert.py`** —— 最小用法，20 秒看到效果

```powershell
# 先看看效果（不写文件）
python examples\stock_daily_alert.py --dry-run

# 真推一条试试
python examples\stock_daily_alert.py -t 0.01
```

配上计划任务就是每天定时推送了，详见 [examples/README.md](examples/README.md)。

### 关键词自动回复

编辑 `keywords.json`：

```json
{ "早上好": "早安呀～今天也要元气满满！☀️" }
```

群里提到关键词就会自动回复。

---

## 目录结构

```
qq-companion/
├── main.py                 入口：装配 Config / DB / Client / Router 与插件
├── bot/                    核心框架
│   ├── client.py           OneBot v11 WebSocket 客户端（自动重连）
│   ├── events.py           事件模型与解析
│   ├── router.py           插件路由、权限、冷却、日志脱敏
│   ├── config.py           .env 配置
│   └── db.py               SQLite（聊天记忆 + 提醒）
├── plugins/                插件（删掉 .py 即可停用该功能）
│   ├── chat.py             陪聊（兜底，注册在最后）
│   ├── remind.py           定时提醒
│   ├── admin.py            群管理 + 关键词回复
│   ├── fun.py              娱乐
│   ├── help.py             指令大全
│   ├── bilibili.py         B站更新（通用）
│   ├── macro.py            文件桥接（通用，需配 MACRO_DIR）
│   ├── canvas.py           SJTU Canvas 教务
│   ├── aihaoke.py          SJTU 知途好课
│   ├── sjtu_academic.py    SJTU 第二课堂
│   ├── jwc.py              SJTU 教务处通知
│   └── *.json.example      各插件配置模板
├── scripts/                运维脚本（Windows）
│   ├── watchdog.py         看门狗：保活 + 假活检测 + 二维码提醒
│   ├── start_bot.py        只负责按看门狗的方式拉起 bot
│   ├── start_all.bat       一键启动
│   ├── watchdog.bat        计划任务入口
│   ├── bili_login.py/.bat  B站扫码登录
│   └── fix_sleep.ps1       关掉睡眠，避免夜间掉线
├── examples/               文件桥示例（股票每日推送 / 日报 / 最小示例）
├── tests/                  离线回归测试
├── doctor.py               体检脚本：一屏看出哪里坏了
├── install.ps1             引导式安装
├── AGENT_INSTALL.md        给 AI Agent 的安装提示词
├── selftest.py             端到端自测（假 OneBot 服务器，不依赖 NapCat）
└── docs/                   补充文档
```

---

## 写一个自己的插件

继承 `bot.router.Plugin`，实现 `on_message`，在 `COMMANDS` 里声明指令即可 ——
`#指令` 会自动收录，不用改任何其它文件。

```python
from bot import events
from bot.router import Plugin


class EchoPlugin(Plugin):
    # (触发词, 参数说明, 是否仅主人, 作用)
    COMMANDS = (
        ("#echo", "<内容>", False, "把你说的话原样发回来"),
    )

    async def on_message(self, ev: events.MessageEvent) -> bool:
        text = events.text_of(ev.segments).strip()
        if not text.startswith("#echo"):
            return False
        await self.router.reply(ev, text[5:].strip() or "（空）")
        return True
```

然后在 `main.py` 的 `build_bot()` 里注册（**注册顺序 = 分发优先级，陪聊插件放最后兜底**）。
如果插件涉及凭证命令，记得声明 `redact_prefixes = ("#echo",)` 以免 token 落进日志。

---

## 测试

```powershell
python tests\test_macro_bridge.py   # 文件桥的离线回归测试（22 项，不联网、不碰真实配置）
python selftest.py                  # 端到端自测（会起一个假 OneBot 服务器，需先停掉 NapCat）
```

> `selftest.py` 要占用 3001 端口，所以跑之前得先把真的 NapCat 停掉。

## 出问题了？先跑体检

```powershell
python doctor.py
```

它**只读不写**——不改配置、不杀进程、不发消息，随时可以放心跑。会一次性告诉你：

- Python / 依赖 / `.env` 有没有问题
- **插件配置有没有「文件在但 JSON 写坏了」**（这种最坑：插件会少监控还不告诉你）
- OneBot 端口通不通、**协议层是不是"假活"**（QQ 显示在线但收发全挂）
- **有没有重复实例**（多开会让你收到重复回复）
- 每个插件的 `last_ok` / `last_error`、以及**凭证续期连续失败次数**
- 如果多个插件在同一时刻一起报错，它会直接告诉你「这是本机网络的问题，别去查网站」

## 常见问题

| 问题 | 排查 |
|---|---|
| 一直"正在连接" | NapCat 没启动，或 WebUI 里没启用 3001 正向 WS |
| 机器人不回复群消息 | 群里先 `@` 它；确认机器人已入群 |
| 管理命令没反应 | 确认发命令的是 `OWNER_QQ`，且机器人是群管理员 |
| 私聊不回复 | `.env` 的 `OWNER_QQ` 是不是**你自己**的号 |
| 某个插件没推送 | 先发 `#xx状态` 看状态；多数插件会把 `last_ok` / `last_error` 显示出来 |
| 多个插件同时报网络错 | 大概率是你本机网络/DNS 的问题，不是网站挂了 |
| 电脑睡眠后掉线 | 跑一下 `scripts/fix_sleep.ps1`（管理员权限）关掉睡眠 |

---

## 许可与致谢

- 本项目采用 [MIT 许可证](LICENSE)。
- QQ 协议端由 [NapCat](https://github.com/NapNeko/NapCatQQ) 提供，
  通信遵循 [OneBot v11](https://github.com/botuniverse/onebot-11) 标准。
- 使用前请务必阅读 [DISCLAIMER.md](DISCLAIMER.md)。**用小号，别商用，凭证不外传。**
