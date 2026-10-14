# 文件推送（文件桥）示例

`plugins/macro.py` 让你把**本机另一个程序**产出的东西推送到 QQ，两个程序之间
零代码耦合 —— 只靠**文件**通信。你可以拿它接自己的量化脚本、爬虫、备份任务、
监控告警……任何能写出文件的程序都行。

## 约定

在 `.env` 里把 `MACRO_DIR` 指到你的"生产者项目"根目录，然后：

| 丢进这个目录 | 会发生什么 |
|---|---|
| `<MACRO_DIR>/output/alerts/*.md` | 文件内容**逐条私聊**给你，推完自动挪到 `alerts/sent/` |
| `<MACRO_DIR>/output/reports/*.pdf` | 作为**文件附件**私聊给你；若存在**同名 `.md`**，它的前 12 行会作为摘要一起发出 |

机器人每 **20 秒**扫一次这两个目录。目录不存在就整体禁用，不会报错。

```ini
# .env
MACRO_DIR=C:/my-alerts
```

## 三个示例

| 文件 | 演示什么 |
|---|---|
| [`hello_alert.py`](hello_alert.py) | 最小用法：写一条 Markdown 提醒 |
| [`stock_daily_alert.py`](stock_daily_alert.py) | **股票每日推送**：盘中/收盘盯几个票，涨跌幅超阈值就告警 |
| [`daily_report.py`](daily_report.py) | 生成一份日报（`.md` + 可选 `.pdf`），演示"摘要 + 附件" |

三个都只用标准库（`daily_report.py` 的 PDF 部分需要可选的 `reportlab`），
可以直接读源码改成你自己的。

## 配合 Windows 计划任务定时跑

```powershell
# 每个交易日 15:05 推一次收盘提醒
schtasks /create /tn "StockDailyAlert" /sc weekly /d MON,TUE,WED,THU,FRI /st 15:05 ^
  /tr "python C:\path\to\qq-companion-oss\examples\stock_daily_alert.py"
```

Linux 上就是一行 crontab：

```cron
5 15 * * 1-5 python3 /path/to/examples/stock_daily_alert.py
```

## 自己写生产者

不需要 import 这个仓库的任何东西，只要按上面的目录约定写文件即可：

```python
from pathlib import Path
import os

alerts = Path(os.environ.get("MACRO_DIR", ".")) / "output" / "alerts"
alerts.mkdir(parents=True, exist_ok=True)
(alerts / "anything.md").write_text("**你好**，这条会推到我 QQ 上", encoding="utf-8")
```

> **小提示**：`.md` 内容会原样发到 QQ，所以用 `**加粗**`、`- 列表` 这类 QQ 认得的
> 写法，别用表格和图片语法（QQ 不渲染）。`#` 开头的行虽然 QQ 不渲染成标题，
> 但读起来仍然清楚，可以用。

## 为什么要"推完就挪走"

`alerts/*.md` 推送成功后会被**移动到** `alerts/sent/`，所以：

- 同一个文件不会被推第二次；
- 你的生产者程序可以放心地反复重写同一个文件名；
- 排查"到底推没推"时，看 `sent/` 里有没有那个文件就行。

`reports/*.pdf` 不同 —— 它**留在原地**，只在旁边写一个 `<名字>.pdf.sent` 标记。
因为 NapCat 上传附件是异步的，文件被挪走会导致上传失败。
