"""日报示例：生成一份报告，走 reports/ 通道（摘要 + 附件）推送到 QQ。

和 alerts/ 的区别：
    alerts/   → 适合**短消息**，推完文件被挪走
    reports/  → 适合**带附件的报告**，推的是 PDF 附件
                如果同时存在同名的 .md，它的**前 12 行**会作为摘要正文一起发出

用法：
    python examples/daily_report.py                     # 生成今天的日报
    python examples/daily_report.py --name weekly       # 换个文件名
    python examples/daily_report.py --no-pdf            # 只生成 .md（不写 pdf）
    python examples/daily_report.py --dry-run           # 只打印，不写文件

PDF 需要可选的 reportlab（`pip install reportlab`）。没装也不影响：
.md 照样生成，只是不会推送附件——`reports/` 只认 *.pdf。
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

# 同目录的示例模块（运行时脚本所在目录会在 sys.path 上）
from stock_daily_alert import fetch_quotes, macro_dir

DEFAULT_WATCHLIST = ["sh000001", "sz399001", "sh600519", "sz300750"]


# ---------- 生成 Markdown ----------

def build_markdown(quotes) -> str:
    now = datetime.now()
    lines = [
        f"# 日报 {now:%Y-%m-%d}",
        "",
        f"生成时间：{now:%Y-%m-%d %H:%M:%S}",
        "",
        "## 行情快照",
        "",
    ]
    for _code, name, price, prev, chg in quotes:
        arrow = "🔺" if chg > 0 else ("🔻" if chg < 0 else "➖")
        lines.append(f"- {arrow} {name}：{price:.2f}（{chg:+.2f}%）　昨收 {prev:.2f}")

    gainers = [q for q in quotes if q[4] > 0]
    lines += [
        "",
        "## 小结",
        "",
        f"- 共 {len(quotes)} 个标的，上涨 {len(gainers)} 个、下跌 {len(quotes) - len(gainers)} 个",
        f"- 平均涨跌幅 {sum(q[4] for q in quotes) / len(quotes):+.2f}%",
        "",
        "---",
        "",
        "_由 daily_report.py 生成，经文件桥推送到 QQ。_",
    ]
    return "\n".join(lines)


# ---------- 生成 PDF（可选） ----------

def build_pdf(md_text: str, out_path: Path) -> bool:
    """用 reportlab 生成一份中文 PDF。装不上就返回 False。"""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfgen import canvas
    except ImportError:
        return False

    # reportlab 自带 Adobe 中文 CID 字体，不需要额外的字体文件
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    except Exception:
        return False

    width, height = A4
    c = canvas.Canvas(str(out_path), pagesize=A4)
    c.setFont("STSong-Light", 11)
    y = height - 60
    for raw in md_text.splitlines():
        # 只做最朴素的排版：去掉 Markdown 记号，长得像标题就放大一点
        line = raw.lstrip("# ").strip() if raw.startswith("#") else raw
        size = 15 if raw.startswith("# ") else (12 if raw.startswith("##") else 11)
        c.setFont("STSong-Light", size)
        if y < 60:
            c.showPage()
            y = height - 60
            c.setFont("STSong-Light", size)
        c.drawString(50, y, line[:60])
        y -= size + 7
    c.save()
    return True


# ---------- 主流程 ----------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成日报并经文件桥推送到 QQ")
    ap.add_argument("--name", default=None, help="报告文件名（默认 daily_YYYY-MM-DD）")
    ap.add_argument("--no-pdf", action="store_true", help="只生成 .md")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = ap.parse_args(argv)

    try:
        quotes = fetch_quotes(DEFAULT_WATCHLIST)
    except Exception as e:
        print(f"❌ 抓行情失败：{e}")
        return 2
    if not quotes:
        print("❌ 没解析到任何行情")
        return 2

    md = build_markdown(quotes)
    name = args.name or f"daily_{datetime.now():%Y-%m-%d}"

    if args.dry_run:
        print("（--dry-run，不写文件）\n")
        print(md)
        return 0

    base = macro_dir()
    if not base:
        print("❌ 没找到 MACRO_DIR。请在 .env 里加：MACRO_DIR=C:/my-alerts")
        return 1

    reports = base / "output" / "reports"
    reports.mkdir(parents=True, exist_ok=True)

    # 1) 摘要用 .md —— 名字必须和 .pdf 一致，机器人靠这个配对
    md_path = reports / f"{name}.md"
    md_path.write_text(md, encoding="utf-8")
    print(f"✅ 摘要  {md_path}")

    # 2) 附件用 .pdf —— reports/ 只认 pdf，没有 pdf 就不会推送
    if args.no_pdf:
        print("   （--no-pdf，跳过 PDF：注意机器人只会推送 *.pdf，没有附件就不会推）")
        return 0

    pdf_path = reports / f"{name}.pdf"
    if build_pdf(md, pdf_path):
        print(f"✅ 附件  {pdf_path}")
        print("   机器人 20 秒内推送（正文 = .md 的前 12 行，附件 = 这个 pdf）")
    else:
        print("⚠️  没装 reportlab，跳过 PDF。")
        print("   装一下就能推附件：pip install reportlab")
        print("   （或者用你自己顺手的工具生成同名 .pdf 丢进 reports/ 也一样）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
