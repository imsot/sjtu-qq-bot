"""一键体检：一屏看出机器人为什么不动了。

用法：
    python doctor.py

它只读不写——不改配置、不杀进程、不发消息。可以随时放心跑。

覆盖的检查：
    1. 运行环境（Python 版本 / 平台 / git）
    2. 仓库结构是否完整
    3. Python 依赖是否装齐
    4. .env 是否填对
    5. 各插件配置有没有"文件在但 JSON 写坏了"（这种最坑：会静默少监控）
    6. OneBot 连接：端口通不通 + 协议层是不是"假活"
    7. 有没有重复实例、同步端口有没有起来
    8. 各插件的 last_ok / last_error（**这是判断"静默失效"的关键**）
    9. 日志里最近的异常、数据库是否正常
"""
import importlib.util
import json
import platform
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent
SCRIPTS = REPO / "scripts"
PLUGINS = REPO / "plugins"

# 有独立配置文件的插件：(模块名, 配置文件名, 中文名, 是否需要凭证)
PLUGIN_CONFIGS = [
    ("bilibili", "bilibili.json", "B站更新", True),
    ("canvas", "canvas.json", "Canvas 教务", True),
    ("aihaoke", "aihaoke.json", "知途好课", True),
    ("sjtu_academic", "sjtu_academic.json", "第二课堂", True),
    ("jwc", "jwc.json", "教务处通知", False),
]

OK, WARN, BAD = "✅", "⚠️", "❌"


class Report:
    def __init__(self):
        self.lines = []
        self.bads = 0
        self.warns = 0
        self._n = 0

    def section(self, title):
        self._n += 1
        self.lines.append("")
        self.lines.append(f"── {title} " + "─" * max(0, 50 - len(title)))

    def ok(self, text, detail=""):
        self.lines.append(f"  {OK} {text}" + (f"　{detail}" if detail else ""))

    def warn(self, text, detail=""):
        self.warns += 1
        self.lines.append(f"  {WARN} {text}" + (f"　{detail}" if detail else ""))

    def bad(self, text, detail=""):
        self.bads += 1
        self.lines.append(f"  {BAD} {text}" + (f"　{detail}" if detail else ""))

    def note(self, text):
        """给建议/补充说明，不计数"""
        self.lines.append(f"     → {text}")

    def render(self) -> int:
        print("\n".join(self.lines))
        print()
        print("=" * 56)
        if self.bads:
            print(f"发现 {self.bads} 个问题、{self.warns} 条提醒 —— 先解决 ❌ 的那些。")
            return 1
        if self.warns:
            print(f"没发现致命问题，但有 {self.warns} 条提醒。")
            return 0
        print("一切正常 ✨")
        return 0


# ============================================================
# 1. 运行环境
# ============================================================

def check_env_runtime(rep: Report):
    rep.section("运行环境")
    v = sys.version_info
    if v >= (3, 10):
        rep.ok(f"Python {platform.python_version()}")
    else:
        rep.bad(f"Python {platform.python_version()} 太旧", "需要 3.10+（代码用了 X | None 语法）")
        rep.note("装个新版本：https://www.python.org/downloads/")

    rep.ok(f"平台 {platform.system()} {platform.release()}")

    if sys.platform != "win32":
        rep.warn("非 Windows", "scripts/*.bat 与看门狗是 Windows 专用的；main.py 本身跨平台")


# ============================================================
# 2. 仓库结构
# ============================================================

def check_layout(rep: Report):
    rep.section("仓库结构")
    need = ["main.py", "requirements.txt", "bot/router.py", "bot/config.py",
            "plugins/chat.py", "plugins/help.py"]
    missing = [p for p in need if not (REPO / p).exists()]
    if missing:
        rep.bad("缺少关键文件", ", ".join(missing))
        rep.note("是不是在错误的目录下运行？请在仓库根目录执行 python doctor.py")
    else:
        rep.ok("核心文件齐全")


# ============================================================
# 3. 依赖
# ============================================================

def check_deps(rep: Report):
    rep.section("Python 依赖")
    groups = [
        ("aiohttp", ["aiohttp"], "WebSocket 与本地同步接口", True),
        ("python-dotenv", ["dotenv"], "读取 .env", True),
        ("openai", ["openai"], "LLM_PROVIDER=deepseek 时需要", False),
        ("anthropic", ["anthropic"], "LLM_PROVIDER=anthropic 时需要", False),
    ]
    for label, mods, why, required in groups:
        found = any(importlib.util.find_spec(m) is not None for m in mods)
        if found:
            rep.ok(label)
        elif required:
            rep.bad(f"缺少 {label}", why)
        else:
            rep.warn(f"未安装 {label}", why + "（用不到就可以不管）")

    if any(importlib.util.find_spec(m) is None for m in ("aiohttp", "dotenv")):
        rep.note("装依赖：pip install -r requirements.txt")


# ============================================================
# 4. .env
# ============================================================

def parse_env(path: Path) -> dict:
    out = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def check_dotenv(rep: Report):
    rep.section("配置 .env")
    env_file = REPO / ".env"
    if not env_file.exists():
        rep.bad(".env 不存在")
        rep.note("复制一份模板：copy .env.example .env　然后填上 OWNER_QQ 和 API Key")
        return None

    env = parse_env(env_file)
    if not env:
        rep.bad(".env 存在但读不出任何配置项")
        return None

    owner = env.get("OWNER_QQ", "")
    if not owner.isdigit() or owner == "0":
        rep.bad(f"OWNER_QQ 没填对：{owner!r}")
        rep.note("填你自己的 QQ 号；管理命令和私聊陪聊只有这个号能用")
    elif owner == "100000000":
        rep.bad("OWNER_QQ 还是模板里的占位符 100000000")
    else:
        rep.ok(f"OWNER_QQ = {owner}")

    bot_qq = env.get("BOT_QQ", "0")
    if bot_qq in ("", "0"):
        rep.ok("BOT_QQ = 0（自动识别，没问题）")
    else:
        rep.ok(f"BOT_QQ = {bot_qq}")

    provider = (env.get("LLM_PROVIDER") or "deepseek").lower()
    if provider == "deepseek":
        key = env.get("DEEPSEEK_API_KEY", "")
        if key and key.startswith("sk-") and "填你的" not in key:
            rep.ok("DEEPSEEK_API_KEY 已填", f"({key[:7]}…)")
        else:
            rep.bad("DEEPSEEK_API_KEY 没填", "陪聊功能不可用")
            rep.note("去 https://platform.deepseek.com 申请")
    elif provider == "anthropic":
        if env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY"):
            rep.ok("Anthropic 凭证已填")
        else:
            rep.bad("LLM_PROVIDER=anthropic 但没填 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN")
    else:
        rep.warn(f"未知的 LLM_PROVIDER: {provider}", "可选 deepseek / anthropic")

    if (env.get("CHAT_MAX_TOKENS") or "2048").isdigit() and int(env.get("CHAT_MAX_TOKENS") or 2048) < 1024:
        rep.warn("CHAT_MAX_TOKENS < 1024", "推理模型会把 token 全用在思考上，导致空回复")

    return env


# ============================================================
# 5. 插件配置（重点：区分"没有"和"写坏了"）
# ============================================================

def check_plugin_configs(rep: Report, env: dict):
    rep.section("插件配置")
    enabled = []

    for mod, fname, label, needs_cred in PLUGIN_CONFIGS:
        path = PLUGINS / fname
        if not path.exists():
            rep.lines.append(f"  ·  {label}：未配置（功能停用）")
            continue
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            # 这条最要命：文件存在但解析失败，插件可能静默退回默认值 → 少监控还看不出来
            rep.bad(f"{label} 的 {fname} 解析失败", str(e)[:60])
            rep.note(f"通常是漏了逗号/多了逗号。用 JSON 校验器过一遍：{path}")
            continue

        if not isinstance(cfg, dict):
            rep.bad(f"{label} 的 {fname} 结构不对", "顶层应该是个对象 {}")
            continue

        if cfg.get("enabled") is False:
            rep.lines.append(f"  ·  {label}：已关闭（enabled=false）")
            continue

        if needs_cred:
            cred = cfg.get("token") or cfg.get("sessdata")
            if not cred:
                rep.warn(f"{label}：enabled=true 但没有凭证", f"{fname} 里的 token/sessdata 是空的")
                rep.note(f"看 {fname} 里的 _说明 / _怎么拿token 字段")
                continue

        rep.ok(f"{label}：已启用")
        enabled.append((mod, fname, label, cfg))

    # 第二课堂的 token 只有 24 小时，值得单独提一句
    for mod, fname, label, cfg in enabled:
        if mod == "sjtu_academic":
            tok = cfg.get("token") or ""
            exp = _jwt_exp(tok)
            if exp is None:
                rep.warn("第二课堂的 token 解不出有效期", "可能不是合法 JWT")
            else:
                left = exp - time.time()
                if left <= 0:
                    rep.bad("第二课堂的 token 已过期", "发 #二课token <新token> 换一个")
                elif left < 6 * 3600:
                    rep.warn(f"第二课堂的 token 还有 {left / 3600:.1f} 小时就过期")
                    rep.note("这个平台的 token 只有 24 小时，且不能刷新，需要手动换")
                else:
                    rep.ok(f"第二课堂的 token 还有 {left / 3600:.1f} 小时")

    return enabled


def _jwt_exp(token: str):
    """只解 payload 读 exp，不验签（读自己的凭证，安全）。"""
    import base64
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return float(exp) if exp else None
    except Exception:
        return None


# ============================================================
# 6. OneBot 连接
# ============================================================

def ws_target(env: dict):
    url = (env or {}).get("WS_URL") or "ws://127.0.0.1:3001"
    p = urlparse(url)
    host = p.hostname or "127.0.0.1"
    port = p.port or 3001
    return url, host, port


def check_connection(rep: Report, env: dict):
    rep.section("OneBot 连接")
    url, host, port = ws_target(env)

    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect((host, port))
        rep.ok(f"{url} 端口可达")
        tcp_ok = True
    except OSError as e:
        rep.bad(f"{url} 连不上", str(e)[:50])
        rep.note("NapCat 没启动，或它的「网络配置」里没建这个端口的正向 WS")
        tcp_ok = False
    finally:
        s.close()

    if not tcp_ok:
        return

    # 协议层探测：端口通 ≠ 真的能用，可能是"幽灵会话"
    wd = _load_watchdog()
    if wd is None:
        rep.warn("无法载入协议探测模块", "跳过假活检测")
        return

    data = wd.ws_get_status(port=port, timeout=6)
    if data is None:
        rep.warn("协议探测失败（超时/异常应答）", "当作未知，不一定是坏的")
        return

    if data.get("online"):
        rep.ok("协议层在线（online=true）")
        if data.get("good") is False:
            rep.warn("NapCat 报告状态不佳", f"good={data.get('good')}")
    else:
        rep.bad("协议层掉线（online=false）—— 这是典型的『幽灵会话』")
        rep.note("QQ 显示在线但收发全挂。重启 NapCat 通常能免扫码恢复；")
        rep.note("若重启后仍要扫码，说明凭证过期，需要用手机 QQ 扫一次。")


def _load_watchdog():
    """复用 scripts/watchdog.py 里那段零依赖的裸 WebSocket 探测。"""
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import watchdog
        return watchdog
    except Exception:
        return None


# ============================================================
# 7. 进程 / 端口
# ============================================================

def _bot_processes():
    """列出跑着 main.py 的进程 [(pid, cmdline)]。取不到返回 None。"""
    try:
        if sys.platform == "win32":
            ps = ("Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
                  "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=25)
            procs = []
            for line in (out.stdout or "").splitlines():
                pid, _, cmd = line.partition("|")
                if "main.py" in cmd and "watch" not in cmd and pid.strip().isdigit():
                    procs.append((int(pid.strip()), cmd.strip()))
            return procs
        else:
            out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True,
                                 text=True, timeout=15)
            procs = []
            for line in (out.stdout or "").splitlines()[1:]:
                parts = line.strip().split(None, 1)
                if len(parts) == 2 and "main.py" in parts[1] and "watch" not in parts[1]:
                    procs.append((int(parts[0]), parts[1]))
            return procs
    except Exception:
        return None


def check_processes(rep: Report, env: dict, enabled_plugins):
    rep.section("进程与端口")

    procs = _bot_processes()
    if procs is None:
        rep.warn("拿不到进程列表", "跳过重复实例检测")
    elif len(procs) == 0:
        rep.bad("没有发现正在运行的机器人进程")
        rep.note("启动它：python main.py")
    elif len(procs) == 1:
        rep.ok(f"机器人进程 1 个（PID {procs[0][0]}）")
    else:
        rep.bad(f"发现 {len(procs)} 个机器人实例！", "每条消息会被回复多次")
        for pid, _ in procs:
            rep.note(f"PID {pid}")
        rep.note("只留一个：taskkill /PID <多余的> /F；或让 scripts/watchdog.py 自动去重")

    # 浏览器同步端口（好课/二课的用户脚本靠它推 token）
    for mod, fname, label, cfg in enabled_plugins:
        if not cfg.get("sync_enabled"):
            continue
        port = int(cfg.get("sync_port") or 0)
        if not port:
            continue
        if _port_open("127.0.0.1", port):
            rep.ok(f"{label} 的同步接口在 {port} 监听中")
        else:
            rep.warn(f"{label} 的同步接口没起来（端口 {port}）")
            rep.note("浏览器用户脚本会静默失败。重启机器人；若提示端口被占用就换一个")


def _port_open(host, port, timeout=1.5) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ============================================================
# 8. 插件运行状态（判断"静默失效"的关键）
# ============================================================

def _plugin_state_files():
    out = []
    for p in sorted(PLUGINS.glob(".*_state.json")):
        name = p.name.strip(".").replace("_state.json", "")
        out.append((name, p))
    return out


def check_plugin_states(rep: Report):
    rep.section("插件运行状态")
    states = _plugin_state_files()
    if not states:
        rep.lines.append("  ·  还没有任何插件状态文件（插件可能还没跑过一轮）")
        return []

    errors = []
    for name, path in states:
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rep.warn(f"{name} 的状态文件读不出来", path.name)
            continue
        if not isinstance(st, dict):
            continue

        last_ok = st.get("last_ok")
        last_err = st.get("last_error")
        err_at = st.get("last_error_at") or st.get("last_refresh_try")

        if last_err:
            rep.warn(f"{name}：{str(last_err)[:60]}")
            if err_at:
                rep.note(f"最后一次出错：{_ago(err_at)}")
                errors.append((name, err_at))
        elif last_ok:
            rep.ok(f"{name}：上次成功 {_ago(last_ok)}")

        # 好课插件专有：主动续期连续失败（会静默失败，只能从这里看出来）
        streak = st.get("refresh_fail_streak")
        if streak:
            rep.warn(f"{name}：凭证续期已连续失败 {streak} 次")
            rep.note("续期坏了不等于马上不能用，但请尽快换一个新 token")

    # 多个插件在同一时间窗报错 → 大概率是本机网络/DNS，不是站点挂了
    if len(errors) >= 2:
        times = [t for _, t in errors if isinstance(t, (int, float))]
        if len(times) >= 2 and (max(times) - min(times)) < 300:
            rep.lines.append("")
            rep.warn(f"{len(errors)} 个插件在同一时间窗内都报错")
            rep.note("这几乎肯定是本机网络/DNS 的问题，不要挨个去查网站")
            rep.note("等网络恢复后插件会自行补上；可发 #xx状态 看是否已恢复")
    return errors


def _ago(ts) -> str:
    try:
        delta = time.time() - float(ts)
    except (TypeError, ValueError):
        return str(ts)
    if delta < 0:
        return "刚刚"
    for unit, n in (("天", 86400), ("小时", 3600), ("分钟", 60)):
        if delta >= n:
            return f"{delta / n:.0f} {unit}前"
    return f"{delta:.0f} 秒前"


# ============================================================
# 9. 日志与数据
# ============================================================

def check_logs(rep: Report):
    rep.section("日志与数据")

    log = REPO / "bot_err.log"
    if not log.exists():
        rep.lines.append("  ·  bot_err.log 还不存在（机器人可能没启动过）")
    else:
        try:
            lines = log.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            lines = []
        tail = lines[-300:]
        tbs = [l for l in tail if "Traceback" in l or "ERROR" in l or "出错" in l]
        if tbs:
            rep.warn(f"最近 300 行日志里有 {len(tbs)} 条异常记录")
            for l in tbs[-3:]:
                rep.note(l[:90])
        else:
            rep.ok(f"最近 300 行日志没有异常（文件共 {len(lines)} 行）")

        # 日志时间戳没日期的话，跨天之后就完全没法判断某行是哪天写的
        if lines and not any(len(l) >= 8 and l[:2].isdigit() and l[2] == "-" for l in tail[:50]):
            rep.warn("日志时间戳看起来没有日期", "排查跨天问题时会很痛苦")

    db_path = REPO / "data" / "qq.db"
    if not db_path.exists():
        rep.lines.append("  ·  数据库还没建（机器人首次跑起来时会自动建）")
        return
    try:
        conn = sqlite3.connect(str(db_path))
        n_chat = conn.execute("SELECT COUNT(*) FROM chat_history").fetchone()[0]
        row = conn.execute("SELECT MAX(ts) FROM chat_history").fetchone()[0]
        n_remind = conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
        conn.close()
        rep.ok(f"数据库正常：{n_chat} 条聊天记录、{n_remind} 条提醒")
        if row:
            rep.note(f"最后一条消息 {_ago(row)}")
            if time.time() - row > 86400:
                rep.note("超过一天没有新消息 —— 若你最近发过消息，说明消息根本没到机器人这里")
    except sqlite3.Error as e:
        rep.bad("数据库打不开或被占用", str(e)[:60])


# ============================================================

def main() -> int:
    rep = Report()
    print("=" * 56)
    print(" QQ 伴侣机器人 · 体检报告")
    print(f" {datetime.now():%Y-%m-%d %H:%M:%S}  |  {REPO}")
    print("=" * 56)

    check_env_runtime(rep)
    check_layout(rep)
    check_deps(rep)
    env = check_dotenv(rep)
    enabled = check_plugin_configs(rep, env) or []
    if env:
        check_connection(rep, env)
        check_processes(rep, env, enabled)
    check_plugin_states(rep)
    check_logs(rep)

    return rep.render()


if __name__ == "__main__":
    sys.exit(main())
