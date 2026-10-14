"""最小示例：往 alerts/ 丢一条 Markdown，机器人 20 秒内私聊推给你。

用法：
    1. 在 .env 里设置 MACRO_DIR（指向你这个"生产者项目"的根目录）
    2. python examples/hello_alert.py
    3. 20 秒内 QQ 上应该收到这条消息；再去 <MACRO_DIR>/output/alerts/sent/ 看它已被归档

不需要装任何依赖，也不需要 import 这个仓库的任何东西。
"""
import os
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def macro_dir() -> Path:
    """优先读环境变量；没有就从仓库的 .env 里捞一行（免得还要手动 set）。"""
    v = os.environ.get("MACRO_DIR")
    if v:
        return Path(v)
    env_file = REPO / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("MACRO_DIR="):
                return Path(line.split("=", 1)[1].strip())
    except OSError:
        pass
    return None


def main() -> int:
    base = macro_dir()
    if not base:
        print("❌ 没找到 MACRO_DIR。")
        print("   请先在 .env 里加一行（或用环境变量）：")
        print("       MACRO_DIR=C:/my-alerts")
        return 1

    alerts = base / "output" / "alerts"
    alerts.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    # 文件名带上时间戳，避免和之前的提醒撞名
    path = alerts / f"hello_{now:%Y%m%d_%H%M%S}.md"
    path.write_text(
        f"👋 **文件桥测试**\n\n"
        f"这条消息来自 `hello_alert.py`。\n"
        f"时间：{now:%Y-%m-%d %H:%M:%S}\n"
        f"机器人会在 20 秒内把它推给我，然后归档到 `sent/`。",
        encoding="utf-8",
    )

    print(f"✅ 已写入 {path}")
    print(f"   机器人最迟 20 秒后推送；然后这个文件会被挪到 {(alerts / 'sent')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
