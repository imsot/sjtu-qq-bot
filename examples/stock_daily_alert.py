"""股票每日推送示例：盯一组标的，涨跌幅超过阈值就写成一条 QQ 提醒。

这是"文件桥"最典型的用法 —— 你不需要碰机器人的任何代码，
只要把结果写成 Markdown 丢到 alerts/ 目录，机器人就会私聊推给你。

用法：
    python examples/stock_daily_alert.py                    # 默认看自选列表，±3% 才推
    python examples/stock_daily_alert.py -t 5               # 阈值改成 ±5%
    python examples/stock_daily_alert.py -a                 # 无论涨跌都推一份收盘小结
    python examples/stock_daily_alert.py --dry-run          # 只打印，不写文件
    python examples/stock_daily_alert.py sh600519 sz300750  # 只看这两只

数据源：腾讯行情 http://qt.gtimg.cn （公开、免登录、无需 API Key）。
它是非官方接口，字段可能变动，请自行评估使用风险，并保持克制的请求频率。

配合计划任务（每交易日收盘后跑一次）：
    schtasks /create /tn "StockDailyAlert" /sc weekly /d MON,TUE,WED,THU,FRI /st 15:05 ^
      /tr "python C:\\path\\to\\examples\\stock_daily_alert.py"
"""
import argparse
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# 自选标的：腾讯行情的代码格式是 交易所前缀 + 代码
#   sh = 上交所（含指数，如 sh000001 上证指数）
#   sz = 深交所（如 sz399001 深证成指）
DEFAULT_WATCHLIST = ["sh000001", "sz399001", "sh600519", "sz300750"]
DEFAULT_THRESHOLD = 3.0          # 涨跌幅绝对值超过这个数（%）才推送
QUOTE_URL = "https://qt.gtimg.cn/q="
TIMEOUT = 10


# ---------- 配置 ----------

def macro_dir():
    """优先读环境变量；没有就从仓库的 .env 里捞一行。"""
    v = os.environ.get("MACRO_DIR")
    if v:
        return Path(v)
    try:
        for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("MACRO_DIR="):
                return Path(line.split("=", 1)[1].strip())
    except OSError:
        pass
    return None


# ---------- 抓取 ----------

def fetch_quotes(codes):
    """批量抓行情。返回 [(code, name, price, prev_close, change_pct), ...]。

    腾讯返回的是 GBK 编码的一行行文本，形如：
        v_sh600519="1~贵州茅台~600519~1700.00~1690.00~...";
    字段以 ~ 分隔：[1]=名称、[3]=当前价、[4]=昨收。
    涨跌幅我们自己用 当前价/昨收 算，不依赖后面那些易变的字段下标。
    """
    url = QUOTE_URL + ",".join(codes)
    req = urllib.request.Request(url, headers={
        # 不带 UA 时部分节点会直接断开
        "User-Agent": "Mozilla/5.0 (compatible; qq-companion file-bridge example)",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
    text = raw.decode("gbk", errors="ignore")

    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("v_") or '="' not in line:
            continue
        body = line.split('="', 1)[1].rstrip('";')
        f = body.split("~")
        if len(f) < 5:
            continue
        try:
            price = float(f[3])
            prev = float(f[4])
        except ValueError:
            continue          # 停牌/无数据时这些字段可能是空串
        code = line[2:].split("=", 1)[0]
        change = (price - prev) / prev * 100 if prev else 0.0
        out.append((code, f[1], price, prev, change))
    return out


# ---------- 渲染 ----------

def fmt_row(name, price, change):
    arrow = "🔺" if change > 0 else ("🔻" if change < 0 else "➖")
    return f"{arrow} **{name}**  {price:.2f}  ({change:+.2f}%)"


def render_alerts(quotes, threshold, mode):
    """mode: 'alert' = 只列超阈值的；'summary' = 全部列一遍。"""
    hits = [q for q in quotes if abs(q[4]) >= threshold]
    lines = []

    if mode == "alert":
        lines.append(f"⚠️ **行情异动** · {datetime.now():%m-%d %H:%M}")
        lines.append("")
        lines.append(f"以下标的涨跌幅超过 ±{threshold:g}%：")
        lines.append("")
        for code, name, price, _prev, chg in sorted(hits, key=lambda q: -abs(q[4])):
            lines.append(fmt_row(name, price, chg))
    else:
        lines.append(f"📊 **收盘小结** · {datetime.now():%m-%d %H:%M}")
        lines.append("")
        for code, name, price, _prev, chg in quotes:
            lines.append(fmt_row(name, price, chg))
        if hits:
            lines.append("")
            lines.append(f"其中 {len(hits)} 个超过 ±{threshold:g}%")

    return "\n".join(lines)


# ---------- 主流程 ----------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把行情异动写成 QQ 提醒（文件桥示例）")
    ap.add_argument("codes", nargs="*", default=None, help="标的代码，留空用内置自选")
    ap.add_argument("-t", "--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help=f"涨跌幅阈值%%，默认 {DEFAULT_THRESHOLD:g}")
    ap.add_argument("-a", "--always", action="store_true", help="无论是否超阈值都推一份小结")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = ap.parse_args(argv)

    codes = args.codes or DEFAULT_WATCHLIST

    try:
        quotes = fetch_quotes(codes)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        print(f"❌ 抓行情失败：{e}")
        return 2

    if not quotes:
        print("❌ 没解析到任何行情（接口格式可能变了，或代码写错了）")
        return 2

    hits = [q for q in quotes if abs(q[4]) >= args.threshold]
    if not hits and not args.always:
        print(f"✅ 抓了 {len(quotes)} 个标的，无一个超过 ±{args.threshold:g}%，不推送。")
        return 0

    text = render_alerts(quotes, args.threshold, "summary" if (args.always and not hits) else "alert")

    if args.dry_run:
        print("（--dry-run，不写文件）\n")
        print(text)
        return 0

    base = macro_dir()
    if not base:
        print("❌ 没找到 MACRO_DIR，无法写入 alerts/。")
        print("   请在 .env 里加一行：MACRO_DIR=C:/my-alerts")
        return 1

    alerts = base / "output" / "alerts"
    alerts.mkdir(parents=True, exist_ok=True)
    path = alerts / f"stock_{datetime.now():%Y%m%d_%H%M%S}.md"
    path.write_text(text, encoding="utf-8")

    print(f"✅ 已写入 {path}")
    print(f"   {len(hits)} 个标的超阈值；机器人 20 秒内推送，然后归档到 sent/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
