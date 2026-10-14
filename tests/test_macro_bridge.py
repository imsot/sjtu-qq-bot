"""文件桥（plugins/macro.py）的离线回归测试。

不联网、不碰真实配置、不需要 NapCat：用一个假 client 和一个临时目录，
把"推完归档""不重复推""WS 断开时不能写标记"这些容易写错的边界都覆盖住。

用法：
    python tests/test_macro_bridge.py
"""
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from plugins import macro  # noqa: E402

results = []


def check(name, ok):
    results.append((name, ok))
    print(("  [OK] " if ok else "  [X]  ") + name)


class FakeClient:
    def __init__(self):
        self.sent = []
        self.ws = object()          # 非 None → 通过"防僵尸连接"检查

    async def send_action(self, action, params):
        self.sent.append((action, params))
        return True


class FakeCfg:
    OWNER_QQ = 10001


def make_plugin(root):
    p = macro.MacroPlugin()
    p.cfg = FakeCfg()
    p.client = FakeClient()
    p._enabled = macro._resolve_dirs(str(root))
    return p


def main():
    tmp = Path(tempfile.mkdtemp(prefix="bridge_"))
    try:
        print("--- 禁用路径 ---")
        check("MACRO_DIR 为空 → 禁用", macro._resolve_dirs("") is False)
        check("目录不存在 → 禁用", macro._resolve_dirs(str(tmp / "nope")) is False)

        alerts = tmp / "output" / "alerts"
        reports = tmp / "output" / "reports"
        alerts.mkdir(parents=True)
        reports.mkdir(parents=True)

        print("--- 启用路径 ---")
        check("目录存在 → 启用", macro._resolve_dirs(str(tmp)) is True)
        check("ALERT_DIR 展开正确", Path(macro.ALERT_DIR) == alerts)
        check("REPORT_DIR 展开正确", Path(macro.REPORT_DIR) == reports)

        # ---- alerts：推完必须挪到 sent/ ----
        (alerts / "a.md").write_text("hello 世界", encoding="utf-8")
        (alerts / "b.txt").write_text("不该被推", encoding="utf-8")   # 非 md，应忽略
        p = make_plugin(tmp)
        asyncio.run(p._dispatch_alerts())
        check("alerts 未推送的 .md 被移走", not (alerts / "a.md").exists())
        check("归档到 sent/", (alerts / "sent" / "a.md").exists())
        check("非 .md 文件不动", (alerts / "b.txt").exists())
        check("发了一条私聊给主人", len(p.client.sent) == 1
              and p.client.sent[0][0] == "send_private_msg"
              and p.client.sent[0][1]["user_id"] == 10001)
        check("正文正确", p.client.sent[0][1]["message"] == "hello 世界")

        # 空文件不该发消息，但仍应归档（否则每轮都会重复扫到它）
        (alerts / "empty.md").write_text("   \n", encoding="utf-8")
        p.client.sent.clear()
        asyncio.run(p._dispatch_alerts())
        check("空 .md 不发消息", len(p.client.sent) == 0)
        check("空 .md 仍被归档", (alerts / "sent" / "empty.md").exists())

        # ---- reports：pdf 留在原地，只写 .sent 标记 ----
        (reports / "r.pdf").write_bytes(b"%PDF-1.4 fake")
        (reports / "r.md").write_text("第一行摘要\n第二行\n", encoding="utf-8")
        p.client.sent.clear()
        asyncio.run(p._dispatch_reports())
        check("pdf 留在原地", (reports / "r.pdf").exists())
        check("写了 .sent 标记", (reports / "r.pdf.sent").exists())
        check("推了一条带附件的消息", len(p.client.sent) == 1)
        segs = p.client.sent[0][1]["message"]
        check("消息含 text 段", any(s["type"] == "text" for s in segs))
        check("消息含 file 段", any(s["type"] == "file" for s in segs))
        check("摘要取自同名 .md",
              "第一行摘要" in [s["data"]["text"] for s in segs if s["type"] == "text"][0])

        # ---- 幂等：已经推过的不该重复推 ----
        p.client.sent.clear()
        asyncio.run(p._dispatch_reports())
        check("已推过的 pdf 不再重复推", len(p.client.sent) == 0)

        # ---- 没有同名 .md 时用兜底文案 ----
        (reports / "solo.pdf").write_bytes(b"%PDF-1.4 fake")
        p.client.sent.clear()
        asyncio.run(p._dispatch_reports())
        segs = p.client.sent[0][1]["message"]
        txt = [s["data"]["text"] for s in segs if s["type"] == "text"][0]
        check("缺 .md 时用兜底文案", "附件" in txt)

        # ---- WS 断开时绝不能写标记（否则这条报告就永远丢了）----
        (reports / "z.pdf").write_bytes(b"%PDF-1.4 fake")
        p.client.ws = None
        p.client.sent.clear()
        asyncio.run(p._dispatch_reports())
        check("WS 断开时不写 .sent（下轮重试）", not (reports / "z.pdf.sent").exists())

        # ---- 没配 OWNER_QQ 时应静默跳过，而不是发到 user_id=0 ----
        p.client.ws = object()
        p.cfg.OWNER_QQ = 0
        p.client.sent.clear()
        (alerts / "c.md").write_text("x", encoding="utf-8")
        asyncio.run(p._dispatch_alerts())
        check("OWNER_QQ 未配置时不推送", len(p.client.sent) == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bad = sum(1 for _, ok in results if not ok)
    print()
    print(f"RESULT: {len(results) - bad}/{len(results)} passed"
          + ("" if bad == 0 else f"  ({bad} FAILED)"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
