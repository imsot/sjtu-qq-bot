# -*- coding: utf-8 -*-
"""B站扫码登录:弹出二维码 → 手机B站APP扫码确认 → 自动把 SESSDATA 写入插件配置。

用法:python bili_login.py   (或双击 bili_login.bat)
成功后自动用动态流接口验证凭证有效性,再写入 plugins/bilibili.json。
"""
import io
import json
import os
import re
import struct
import sys
import time
import urllib.parse
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)  # 仓库根目录（本脚本放在 scripts/ 下）
CONFIG = os.path.join(REPO, "plugins", "bilibili.json")
GEN_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={}"
FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all?page=1&timezone_bias=0"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Referer": "https://www.bilibili.com/",
}


def get_json(url, extra_headers=None):
    h = dict(HEADERS)
    if extra_headers:
        h.update(extra_headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _png_chunk(typ, data):
    return (struct.pack(">I", len(data)) + typ + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))


def fetch_qr_image(login_url):
    """优先从在线 QR 服务直接取现成 PNG 图片(免装组件);失败则本地渲染兜底。返回文件路径。"""
    api = ("https://api.qrserver.com/v1/create-qr-code/?size=360x360&margin=16&data="
           + urllib.parse.quote(login_url, safe=""))
    try:
        req = urllib.request.Request(api, headers={"User-Agent": HEADERS["User-Agent"]})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
        if data[:4] == b"\x89PNG" and len(data) > 500:
            tmp = os.environ.get("TEMP", HERE)
            path = os.path.join(tmp, f"bili_login_qr_{int(time.time())}.png")
            with open(path, "wb") as f:
                f.write(data)
            return path
    except Exception:
        pass
    return make_qr_png(login_url)  # 在线失败 → 本地渲染(需 qrcode 组件)


def make_qr_png(login_url):
    """渲染二维码为 PNG 图片(纯标准库编码,无需 Pillow)并用照片查看器打开。"""
    import qrcode
    qr = qrcode.QRCode()  # 默认 border=4(标准静区),扫描成功率最高
    qr.add_data(login_url)
    qr.make(fit=True)
    matrix = qr.get_matrix()  # bool 二维列表,True=黑
    scale = 12  # 每模块放大像素数,便于手机扫描
    width = len(matrix[0]) * scale
    height = len(matrix) * scale

    # 灰度扫描线:0=黑 255=白;每条扫描线前有 1 字节过滤器(0=无)
    scan = bytearray()
    for row in matrix:
        line = bytearray(b"\xff" * width)
        for x, cell in enumerate(row):
            if cell:
                start = x * scale
                line[start:start + scale] = b"\x00" * scale
        for _ in range(scale):
            scan.append(0)
            scan.extend(line)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n"
           + _png_chunk(b"IHDR", ihdr)
           + _png_chunk(b"IDAT", zlib.compress(bytes(scan), 9))
           + _png_chunk(b"IEND", b""))

    tmp = os.environ.get("TEMP", HERE)
    # 时间戳文件名:避免照片查看器显示同名缓存旧图
    path = os.path.join(tmp, f"bili_login_qr_{int(time.time())}.png")
    for old in os.listdir(tmp):
        if old.startswith("bili_login_qr_") and old != os.path.basename(path):
            try:
                os.remove(os.path.join(tmp, old))
            except OSError:
                pass
    with open(path, "wb") as f:
        f.write(png)
    return path


def make_qr_file(login_url):
    """兜底:PNG 失败时渲染 ASCII 二维码到记事本。"""
    import qrcode
    qr = qrcode.QRCode(border=2)
    qr.add_data(login_url)
    qr.make(fit=True)
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    txt = os.path.join(os.environ.get("TEMP", HERE), "bili_login_qr.txt")
    with open(txt, "w", encoding="utf-8") as f:
        f.write("请用手机B站APP 扫一扫,并在手机上点【确认登录】\n"
                "(二维码约3分钟有效;若已过期请重新运行本脚本)\n\n")
        f.write(buf.getvalue())
        f.write("\n保持本窗口开着,等脚本提示成功即可关闭。\n")
    os.startfile(txt)


def _harvest_via_ticket(ticket_url):
    """访问 crossDomain 票据 URL,从响应的 Set-Cookie 头收割 SESSDATA(第三代接口)。"""
    import http.cookiejar
    vals = []
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(ticket_url, headers=HEADERS)
    try:
        opener.open(req, timeout=15).read()
    except Exception:
        pass  # 即使页面报错,Set-Cookie 可能已随响应下发
    for c in jar:
        if c.name == "SESSDATA" and c.value and c.value not in vals:
            vals.append(c.value)
    if not vals:  # 兜底:不走 cookie jar,直接翻响应头
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                for h in (r.headers.get_all("Set-Cookie") or []):
                    m = re.match(r"\s*SESSDATA=([^;]+)", h)
                    if m and m.group(1) not in vals:
                        vals.append(m.group(1))
        except Exception:
            pass
    return vals


def extract_sessdatas(poll_data):
    """从轮询成功响应提取 SESSDATA 候选(逐一验证后择优)。
    覆盖三代接口:cookies 数组 / 回调URL参数 / crossDomain 票据(Set-Cookie)。"""
    out = []
    for c in poll_data.get("cookies") or []:
        if isinstance(c, dict) and c.get("name") == "SESSDATA" and c.get("value"):
            out.append(c["value"])
    back_url = poll_data.get("url") or ""
    parsed = urllib.parse.parse_qs(urllib.parse.urlsplit(back_url).query).get("SESSDATA", [""])[0]
    m = re.search(r"[?&]SESSDATA=([^&]+)", back_url)
    raw = m.group(1) if m else ""
    for v in (parsed, raw):
        if v and v not in out:
            out.append(v)
    if not out and back_url:
        out = _harvest_via_ticket(back_url)
    return out


def verify(sessdata):
    """真实调用动态流接口验证凭证。返回 (ok, code, msg, video_count)。"""
    try:
        obj = get_json(FEED_URL, {"Cookie": f"SESSDATA={sessdata}"})
    except Exception as e:
        return False, 0, f"网络错误 {e.__class__.__name__}", 0
    items = ((obj.get("data") or {}).get("items")) if obj.get("code") == 0 else None
    vids = 0
    if items:
        vids = sum(1 for it in items
                   if (it.get("modules") or {}).get("major_type") == "MAJOR_TYPE_ARCHIVE")
    return obj.get("code") == 0, obj.get("code"), (obj.get("message") or "")[:60], vids


def main():
    gen = get_json(GEN_URL)
    if gen.get("code") != 0:
        print("❌ 生成二维码失败:", gen.get("message"))
        sys.exit(1)
    key = gen["data"]["qrcode_key"]
    login_url = gen["data"]["url"]
    try:
        qr_path = fetch_qr_image(login_url)
        os.startfile(qr_path)
        print("✅ 二维码图片已在照片应用中打开,请用手机B站APP扫码并确认(3分钟内)")
    except Exception as e:
        print(f"⚠️ 图片二维码失败({e}),改用记事本 ASCII 码…")
        make_qr_file(login_url)
        print("✅ 二维码已在记事本中弹出,请用手机B站APP扫码并确认(3分钟内)")
    print("   等待中…(每2秒轮询一次,Ctrl+C 可取消)")

    deadline = time.time() + 185
    scanned_hint = False
    while time.time() < deadline:
        obj = get_json(POLL_URL.format(key))
        d = obj.get("data") or {}
        code = d.get("code")
        if code == 0:
            cands = extract_sessdatas(d)
            if not cands:
                print("❌ 登录成功但未能提取 SESSDATA")
                print("   调试信息: data 字段 =", sorted(d.keys()))
                print("   url 前80字符 =", (d.get("url") or "")[:80])
                print("   请把以上调试信息提交到项目 Issues")
                sys.exit(1)
            chosen = None
            for cand in cands:
                ok, c, msg, vids = verify(cand)
                print(f"   验证口径 len={len(cand)} → code={c} {msg}")
                if ok:
                    chosen = cand
                    print(f"   ✅ 凭证有效,首屏视频动态 {vids} 条")
                    break
            if not chosen:
                print("❌ 两种提取口径都验证失败,请把上面输出提交到项目 Issues")
                sys.exit(1)
            try:
                with open(CONFIG, encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
            cfg["sessdata"] = chosen
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            print("🎉 已写入 plugins/bilibili.json。重启机器人后生效:")
            print("   任务管理器结束 python main.py,看门狗1分钟内会自动拉起;")
            print("   或手动重启 python main.py。")
            sys.exit(0)
        elif code == 86090 and not scanned_hint:
            print("📱 已扫码,请在手机上点【确认登录】…")
            scanned_hint = True
        elif code == 86038:
            print("⌛ 二维码已过期,请重新运行本脚本")
            sys.exit(1)
        time.sleep(2)
    print("⌛ 超时未完成扫码")


if __name__ == "__main__":
    main()
