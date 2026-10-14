"""文件桥接：把「另一个程序」产出的文件私聊推送给主人。

用途：任何在你本机跑的数据采集/量化/报表程序，只要把文件丢到约定的目录里，
本插件就会在 20 秒内把它推送到你的 QQ。两个程序之间零代码耦合，只靠文件通信。

约定目录（由 .env 的 MACRO_DIR 指定它的项目根目录）：

    <MACRO_DIR>/output/alerts/*.md    逐条私聊推送，推完归档到 sent/
    <MACRO_DIR>/output/reports/*.pdf  推送 PDF 附件；
                                      同名 .md 的前 12 行会作为摘要一起发出

**未配置 MACRO_DIR、或该目录不存在时，本插件整体静默禁用**，不影响其它功能。

现成的例子（含"股票每日推送""日报"）见 examples/ 目录 —— 生产者脚本不需要
import 本仓库的任何东西，按上面的目录约定写文件就行。
"""
import asyncio
import os
import shutil

from bot import events
from bot.router import Plugin

POLL_INTERVAL = 20  # 秒

# 下面四个路径在 setup() 里由 cfg.MACRO_DIR 展开（见 bot/config.py）
ALERT_DIR = ""
SENT_DIR = ""
REPORT_DIR = ""
REPORT_SENT_DIR = ""


def _resolve_dirs(macro_dir: str) -> bool:
    """把 MACRO_DIR 展开成四个子目录；目录不存在则返回 False（插件保持禁用）。"""
    global ALERT_DIR, SENT_DIR, REPORT_DIR, REPORT_SENT_DIR
    if not macro_dir or not os.path.isdir(macro_dir):
        return False
    ALERT_DIR = os.path.join(macro_dir, "output", "alerts")
    SENT_DIR = os.path.join(ALERT_DIR, "sent")
    REPORT_DIR = os.path.join(macro_dir, "output", "reports")
    REPORT_SENT_DIR = os.path.join(REPORT_DIR, "sent")
    return True


class MacroPlugin(Plugin):
    COMMANDS = ()

    def setup(self, router):
        super().setup(router)
        self._enabled = _resolve_dirs(getattr(self.cfg, "MACRO_DIR", ""))
        if not self._enabled:
            return  # 未配置就是禁用，连轮询任务都不起
        self._task = asyncio.create_task(self._poll_loop())

    # ---------- 轮询 ----------
    async def _poll_loop(self):
        while True:
            try:
                await self._dispatch_alerts()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # 单次失败不致命，下轮重试
            try:
                await self._dispatch_reports()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(POLL_INTERVAL)

    async def _dispatch_reports(self):
        """推送新出现的 PDF 报告（文本摘要 + 文件附件）。

        推送成功后写 .sent 标记（不移动文件）——文件留在原路径，
        这样 NapCat 异步上传时不会因为源文件已被挪走而失败。
        """
        owner = self.cfg.OWNER_QQ
        if not owner or not os.path.isdir(REPORT_DIR):
            return
        for name in sorted(os.listdir(REPORT_DIR)):
            if not name.endswith(".pdf"):
                continue
            path = os.path.join(REPORT_DIR, name)
            if not os.path.isfile(path):
                continue
            marker = path + ".sent"
            if os.path.exists(marker):
                continue
            try:
                summary = self._report_summary(name)
                segments = [
                    {"type": "text", "data": {"text": summary}},
                    {"type": "file", "data": {"file": "file:///" + path.replace("\\", "/")}},
                ]
                sent_ok = await self.client.send_action(
                    "send_private_msg", {"user_id": int(owner), "message": segments})
                if not sent_ok:
                    continue  # WS 未连接/发送异常，下轮重试，不写标记
                # 防僵尸连接：send_json 对半开连接可能假成功，
                # 让出一次事件循环，若连接已被心跳判死则本轮不算数。
                await asyncio.sleep(1)
                if self.client.ws is None:
                    continue
                with open(marker, "w", encoding="utf-8") as f:
                    f.write("sent")
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # 单次失败下轮重试

    @staticmethod
    def _report_summary(pdf_name):
        """优先用同名 .md 的前 12 行当摘要；没有就用固定文案。"""
        md_path = os.path.join(REPORT_DIR, pdf_name.replace(".pdf", ".md"))
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                lines = [l for l in f.read().splitlines() if l.strip()][:12]
            return "\n".join(lines) or "📄 报告"
        except OSError:
            return "📄 报告（详见附件）"

    async def _dispatch_alerts(self):
        """推送新出现的 .md 提醒，推完无论成败都挪到 sent/（避免重复轰炸）。"""
        owner = self.cfg.OWNER_QQ
        if not owner or not os.path.isdir(ALERT_DIR):
            return
        for name in sorted(os.listdir(ALERT_DIR)):
            if not name.endswith(".md"):
                continue
            path = os.path.join(ALERT_DIR, name)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read().strip()
            except OSError:
                continue
            if text:
                await self.client.send_action(
                    "send_private_msg", {"user_id": int(owner), "message": text}
                )
            os.makedirs(SENT_DIR, exist_ok=True)
            shutil.move(path, os.path.join(SENT_DIR, name))

    async def on_message(self, ev: events.MessageEvent) -> bool:
        return False
