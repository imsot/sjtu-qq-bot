# -*- coding: utf-8 -*-
"""qq-companion 看门狗:确保 NapCat + bot 保持运行,登录丢失时弹提醒。

由 Windows 计划任务 MacroKeep 每分钟调用一次。所有操作幂等(重复运行安全):
- NapCat 进程消失 → 用自带 node 重启(NapCat 自恢复机制可免扫码续登)
- NapCat 在但 3001 未监听(等扫码)→ 桌面 Toast + 自动打开二维码提醒主人
- 卡在登录界面太久(down_since>600s)→ 强制重启
- 【假活检测】NapCat 和 3001 都正常,但协议层已掉线(get_status.online=false,
  即"幽灵会话":API 能应答但实际收发全挂)→ 杀进程重启;
  若凭证真过期,重启后会自然落入"等扫码"分支弹码提醒主人
- bot(python main.py)不在 → 重启
运行环境:Python 3 标准库,零依赖(协议探测用手写裸 WebSocket 客户端)。
"""

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
from datetime import datetime

# ---------- 配置 ----------
# 本脚本是"运维层",需要知道 NapCat 装在哪、Python 在哪、机器人 QQ 是多少。
# 这些因机器而异,所以放在脚本同目录的 watchdog.json 里(已被 .gitignore 忽略,
# 模板见同目录 watchdog.json.example)。没有这个文件时按下面的规则自动推断:
#   · bot_dir  = 本脚本所在目录的上一级(即仓库根目录)
#   · python   = 当前跑本脚本的解释器
#   · napcat_dir 留空 = 本看门狗不再管理 NapCat,只负责让 bot 活着
#   · notify_ps1 留空 = 不弹桌面通知(只写日志)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(_SCRIPT_DIR)


def _load_settings() -> dict:
    try:
        with open(os.path.join(_SCRIPT_DIR, "watchdog.json"), "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


_S = _load_settings()

NAPCAT_DIR = _S.get("napcat_dir", "") or ""
NAPCAT_NODE = os.path.join(NAPCAT_DIR, "node.exe") if NAPCAT_DIR else ""
QR_PATH = os.path.join(NAPCAT_DIR, "napcat", "cache", "qrcode.png") if NAPCAT_DIR else ""
BOT_DIR = _S.get("bot_dir") or _REPO_DIR
PYTHON = _S.get("python") or sys.executable
NOTIFY_PS1 = _S.get("notify_ps1", "") or ""
BOT_QQ = str(_S.get("bot_qq", "") or "")
WS_PORT = int(_S.get("ws_port", 3001))
STATE_FILE = os.path.join(BOT_DIR, ".watchdog_state.json")
ALERT_COOLDOWN = 300         # 秒,同状态提醒间隔(Toast)
STUCK_RESTART = 600          # 秒,扫码界面自动重启的最小间隔
QR_RESTART_CAP = 1800        # 秒:扫码状态持续超过此时长 → 停止自动重启。
                             #   快速登录只对"卡状态"(凭证仍有效)有效;重启若干次仍没人
                             #   扫码基本=凭证过期,继续重启只会刷屏(2026-08-26 实测)。
QR_WINDOW_INTERVAL = 1800    # 秒:扫码状态下重新弹二维码窗口的最小间隔。
                             #   每次 os.startfile 都会开一个新的照片查看器窗口,
                             #   弹太频会一屏全是码(2026-08-26 教训)。
GHOST_GRACE = 150            # 秒,NapCat 启动后宽限期内不判假活(可能还在登录收尾)
GHOST_RESTART_COOLDOWN = 600  # 秒,假活强制重启的最小间隔,防重启循环


def _ps(args, timeout=60):
    """跑 PowerShell 一行命令,返回 stdout。

    输出走临时文件而非管道(2026-08-26 教训):当 PowerShell 内 Start-Process
    拉起长驻子进程(NapCat/bot)时,子进程会继承捕获管道的写端;超时只能杀
    PowerShell 本身,subprocess 还在等管道 EOF → 看门狗永久卡死,计划任务
    因"不启动新实例"从此全部空转(实测挂了 12 小时)。改用文件+DEVNULL 后
    无任何句柄可继承,超时才能真正生效。
    """
    tmp = os.path.join(os.environ.get("TEMP") or BOT_DIR, "_wd_ps_out.txt")
    wrapped = "& { " + args + " } | Out-File -FilePath '" + tmp + "' -Encoding utf8"
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", wrapped],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return ""
    try:
        with open(tmp, "r", encoding="utf-8-sig") as f:
            return (f.read() or "").strip()
    except OSError:
        return ""


def port_up(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def napcat_running():
    out = _ps("$c = Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
              "Where-Object { $_.CommandLine -match 'napcat|index\\.js' }; "
              "if ($c) { 'UP' } else { 'DOWN' }")
    return "UP" in out


def napcat_age():
    """最老 NapCat node 进程的存活秒数;取不到返回 None。"""
    out = _ps("$p = Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
              "Where-Object { $_.CommandLine -match 'napcat|index\\.js' } | "
              "Sort-Object CreationDate | Select-Object -First 1 -ExpandProperty CreationDate; "
              "if ($p) { $p } else { '' }")
    if not out:
        return None
    try:
        # CIM 日期形如 20260826082253.123456+480,取前 14 位即可
        t = datetime.strptime(out[:14], "%Y%m%d%H%M%S")
        return max(0.0, time.time() - t.timestamp())
    except ValueError:
        return None


def napcat_pids():
    """所有 NapCat 相关 node 进程的 PID 列表。"""
    out = _ps("$c = Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
              "Where-Object { $_.CommandLine -match 'napcat|index\\.js' } | "
              "Select-Object -ExpandProperty ProcessId; "
              "if ($c) { ($c -join ' ') } else { '' }")
    return [p for p in out.split() if p.isdigit()]


def kill_napcat():
    """只杀 NapCat 相关的 node 进程(不动其他 node 程序)。"""
    kill_pids(napcat_pids())


def kill_pids(pids):
    """按 PID 列表强杀(只杀明确列出的进程)。"""
    pids = [str(p) for p in pids if str(p).isdigit()]
    if not pids:
        return
    args = ["taskkill", "/f"]
    for p in pids:
        args += ["/PID", p]
    try:
        subprocess.run(args, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def bot_instances():
    """所有 main.py 进程 [(pid, 创建时间戳)],按启动时间升序。

    探测失败返回 None —— 调用方必须把它当作"未知"并保守处理,绝不能据此
    拉起新实例(2026-09-18 教训:探测超时曾被当作"bot 不在",看门狗每天
    补拉一个,累积出 3 个实例同时在线,用户每条消息收到 3 份重复回复)。
    """
    # 匹配规则(两种写法都要认,否则会重复拉起):
    #   ① 绝对路径 `<本仓库>/main.py` —— 推荐所有启动方式都走这个;
    #   ② 相对路径 `python main.py`(工作目录正好在本仓库时)。
    # 必须排除 `python main.py watch` 这类别的程序:它同样带 main.py,却不是你,
    # 被误认后去重逻辑会去杀真 bot。
    main_py = os.path.join(BOT_DIR, "main.py")
    out = _ps("$c = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
              "Where-Object { $_.CommandLine -like '*" + main_py + "*' -or "
              "($_.CommandLine -like '*main.py*' -and $_.CommandLine -notlike '*watch*') } | "
              "ForEach-Object { \"$($_.ProcessId)|$($_.CreationDate.ToFileTime())\" }; "
              "if ($c) { $c -join ',' } else { 'NONE' }")
    if not out:
        return None
    if out.strip() == "NONE":
        return []
    res = []
    for item in out.split(","):
        pid_s, _, time_s = item.partition("|")
        # ToFileTime():1601 起的 100ns 刻度,与系统区域设置无关,直接比大小
        if not pid_s.strip().isdigit() or not time_s.strip().isdigit():
            return None      # 格式不对 → 视为探测失败,保守处理
        res.append((int(pid_s.strip()), int(time_s.strip())))
    res.sort(key=lambda x: x[1])
    return res


def start_napcat():
    """后台无窗口启动 NapCat。

    不能用 launcher-win10.bat(会弹 UAC);也不能用 WindowStyle Minimized
    (控制台仍会占任务栏,重启时还会被顶到前台——2026-08-26 用户反馈
    NapCat 窗口反复弹出的根因)。CREATE_NO_WINDOW 彻底不分配控制台。
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if not NAPCAT_DIR:
        return  # 未配置 napcat_dir,本看门狗不管 NapCat
    log_out = os.path.join(BOT_DIR, "napcat_out.log")
    log_err = os.path.join(BOT_DIR, "napcat_err.log")
    try:
        with open(log_out, "ab") as out, open(log_err, "ab") as err:
            subprocess.Popen(
                [NAPCAT_NODE, "index.js", "-q", BOT_QQ],
                cwd=NAPCAT_DIR,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                creationflags=flags,
                close_fds=True,
            )
    except OSError:
        pass


def hide_napcat_windows():
    """把已存在的 NapCat/node 控制台窗口藏起来,不杀进程。"""
    _ps(
        "Add-Type -Name WdHide -Namespace Wd -MemberDefinition "
        "'[DllImport(\"user32.dll\")] public static extern bool ShowWindow(IntPtr h,int n);' "
        "-ErrorAction SilentlyContinue; "
        "Get-Process node -ErrorAction SilentlyContinue | "
        "Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero } | "
        "ForEach-Object { [void][Wd.WdHide]::ShowWindow($_.MainWindowHandle, 0) }"
    )


def start_bot():
    """启动 bot;已有实例或探测失败时拒绝启动(防重复实例)。返回是否真的启动。

    用绝对脚本路径启动:命令行走 Popen 直接追加写日志(Start-Process 的重定向
    每次会截断,崩溃后的堆栈会被下一次启动覆盖掉,排查时什么都看不到)。
    """
    inst = bot_instances()
    if inst is None or len(inst) > 0:
        return False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        with open(os.path.join(BOT_DIR, "bot_out.log"), "ab") as out, \
                open(os.path.join(BOT_DIR, "bot_err.log"), "ab") as err:
            subprocess.Popen(
                [PYTHON, os.path.join(BOT_DIR, "main.py")],
                cwd=BOT_DIR,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                creationflags=flags,
                close_fds=True,
            )
        return True
    except OSError:
        return False


def _qr_age():
    if not QR_PATH:
        return None
    try:
        return time.time() - os.path.getmtime(QR_PATH)
    except OSError:
        return None


def toast(title, message):
    """桌面 Toast 通知,失败静默(通知不是关键路径)。"""
    if not NOTIFY_PS1:
        return  # 未配置 notify_ps1,只写日志不弹窗
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          NOTIFY_PS1, "-Title", title, "-Message", message],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        pass


def alert_login_needed(qr_age):
    """桌面 Toast + 打开二维码,提醒主人扫码。"""
    toast("🤖 机器人需要重新登录",
          f"QQ 机器人({BOT_QQ})登录失效。已打开二维码,请用手机 QQ 扫码登录。"
          f"二维码生成于 {max(0, int(qr_age))} 秒前")
    if not QR_PATH:
        return
    # 照片查看器对同名文件只显示缓存旧图(会让人扫到过期码),
    # 复制成带时间戳的新文件再打开,并清理旧副本。
    tmp = os.environ.get("TEMP") or os.path.dirname(QR_PATH)
    snap = os.path.join(tmp, f"qq_login_qr_{int(time.time())}.png")
    try:
        shutil.copyfile(QR_PATH, snap)
        for old in os.listdir(tmp):
            if old.startswith("qq_login_qr_") and old != os.path.basename(snap):
                try:
                    os.remove(os.path.join(tmp, old))
                except OSError:
                    pass
        os.startfile(snap)
    except OSError:
        try:
            os.startfile(QR_PATH)
        except OSError:
            pass


# ---------- 协议层探测 ----------
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("连接中断")
        buf += chunk
    return buf


def ws_get_status(port=WS_PORT, timeout=6):
    """裸 WebSocket 客户端调 OneBot get_status,返回 data 字典;任何异常返回 None。

    零依赖实现(标准库手写握手+帧编码),仅用于本机 127.0.0.1 可信环境。
    返回 None 表示探测失败(连不上/超时/响应异常),调用方应视为"未知"而不是
    掉线——宁可漏判也不能误杀正常的 NapCat。
    """
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError:
        return None
    try:
        s.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = s.recv(4096)
            if not chunk:
                return None
            head += chunk
        line, _, _rest = head.partition(b"\r\n\r\n")
        if b" 101 " not in line.split(b"\r\n")[0]:
            return None
        accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        if accept.encode() not in line:
            return None

        def send_masked(opcode, payload):
            if isinstance(payload, str):
                payload = payload.encode()
            mask = os.urandom(4)
            n = len(payload)
            hdr = bytes([0x80 | opcode])
            if n < 126:
                hdr += bytes([0x80 | n])
            elif n < 65536:
                hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
            else:
                hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
            s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

        def recv_frame():
            b1, b2 = _ws_recv_exact(s, 2)
            ln = b2 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", _ws_recv_exact(s, 2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", _ws_recv_exact(s, 8))[0]
            return b1 & 0x0F, _ws_recv_exact(s, ln)

        send_masked(1, json.dumps({"action": "get_status", "params": {}, "echo": "wd"}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            op, payload = recv_frame()
            if op == 9:          # ping → pong
                send_masked(10, payload)
                continue
            if op == 8:          # close
                return None
            if op == 1:          # text 帧(NapCat 应答都是小帧,不分片)
                try:
                    msg = json.loads(payload.decode())
                except ValueError:
                    return None
                if msg.get("echo") == "wd":
                    data = msg.get("data")
                    return data if isinstance(data, dict) else {}
        return None
    except Exception:
        # 兜底一切异常:探测失败只能视为"未知",绝不能让看门狗本身崩溃
        return None
    finally:
        try:
            s.close()
        except OSError:
            pass


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except OSError:
        pass


def main():
    now = time.time()
    st = load_state()
    manage_napcat = bool(NAPCAT_DIR)
    napcat = napcat_running() if manage_napcat else None
    up = port_up(WS_PORT) if manage_napcat else None
    inst = bot_instances()
    actions = []
    proto = "-"
    dup = 0
    if inst is None:
        bot = True          # 探测失败:保守当作在跑,绝不重复拉起
    else:
        bot = len(inst) > 0
        if len(inst) > 1:
            # 去重:只留最新实例,清掉历史遗留(多实例会让每条消息重复回多次)
            dup = len(inst) - 1
            kill_pids([p for p, _ in inst[:-1]])

    if not manage_napcat:
        # 0) 没配 napcat_dir:本看门狗只负责让 bot 活着,完全不碰 NapCat。
        if not bot and start_bot():
            actions.append("重启bot")
    elif not napcat:
        # 1) NapCat 进程消失:重启。NapCat 自恢复机制有机会免扫码续登。
        start_napcat()
        actions.append("重启NapCat")
        st["down_since"] = now
    elif not up:
        # 2) NapCat 在但 3001 没起:登录界面/等扫码。
        qr_since = st.get("qr_since") or now
        st["qr_since"] = qr_since
        qr_age = _qr_age()
        if qr_age is not None:
            in_episode = now - qr_since
            # 自动重启:只在扫码状态的前 QR_RESTART_CAP 秒内做,且每次间隔 STUCK_RESTART。
            # 重启能救"卡状态"(凭证仍有效,2026-08-25 上午),但超时仍没人扫码=凭证过期,
            # 重启纯属刷屏,必须停(2026-08-26 实测:凭证过期后重启 20+ 次毫无效果)。
            if in_episode < QR_RESTART_CAP and now - (st.get("down_since") or 0) > STUCK_RESTART:
                start_napcat()
                actions.append("扫码超时重启NapCat")
                st["down_since"] = now
            else:
                last_alert = st.get("last_alert") or 0
                if now - last_alert > ALERT_COOLDOWN and qr_age < 180:
                    # 二维码窗口按慢节奏重开(每次 os.startfile 都会新增一个窗口),
                    # 其余时间只发 Toast,避免一弹一大片。
                    last_qr_win = st.get("last_qr_win") or 0
                    if now - last_qr_win > QR_WINDOW_INTERVAL:
                        alert_login_needed(qr_age)
                        st["last_qr_win"] = now
                        actions.append("弹码")
                    else:
                        toast("🤖 机器人待扫码登录",
                              f"QQ 机器人({BOT_QQ})登录失效,请用手机 QQ 扫屏幕上的二维码。")
                        actions.append("提醒(码已弹过)")
                    st["last_alert"] = now
    else:
        # 3) 进程和端口都在 → 协议层假活检测。
        #    2026-08-25/26 实测:NapCat 外壳活着、3001 正常监听、get_login_info
        #    还能返回缓存账号,但 QQ 会话早已死亡(收发全挂)。只有
        #    get_status.online=false 能识别这种状态。
        st.pop("down_since", None)
        st.pop("qr_since", None)
        hide_napcat_windows()
        data = ws_get_status()
        if data is None:
            proto = "?"     # 探测失败,视为未知,宁可不动作也不误杀
        else:
            online = bool(data.get("online"))
            proto = "ON" if online else "OFF"
            if not online:
                age = napcat_age()
                grace_ok = age is None or age > GHOST_GRACE
                cd_ok = now - (st.get("ghost_restart_at") or 0) > GHOST_RESTART_COOLDOWN
                if grace_ok and cd_ok:
                    kill_napcat()
                    start_napcat()
                    st["ghost_restart_at"] = now
                    actions.append("假活重启NapCat")
                    toast("🤖 检测到QQ假活,看门狗已自动重启",
                          f"机器人({BOT_QQ})外壳正常但协议层已掉线,正在快速登录重连。"
                          "若稍后弹出二维码,说明凭证过期,请用手机 QQ 扫码。")
        # 留痕:排查"为什么没收到消息"时,状态文件里的 last_probe 是唯一时间线
        st["last_probe"] = {"t": now, "proto": proto}
        if not bot and start_bot():
            actions.append("重启bot")

    if dup:
        actions.append(f"清理{dup}个重复bot")

    save_state(st)
    def _up(v):
        return "n/a" if v is None else ("UP" if v else "DOWN")

    status = (f"napcat={_up(napcat)} ws={_up(up)} "
              f"proto={proto} bot={'UP' if bot else 'DOWN'}")
    if actions:
        print(f"[watchdog] {status} → {'/'.join(actions)}")
    else:
        print(f"[watchdog] {status} 正常,无需动作")


if __name__ == "__main__":
    main()
