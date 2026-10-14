"""只做一件事:按看门狗的方式把 bot 拉起来。供 start_all.bat 调用。

为什么单独抽这个文件:如果在 .bat 里用

    Start-Process ... -WindowStyle Hidden    (不做任何重定向)

子进程的输出会进到那个隐藏控制台,`bot_out.log` / `bot_err.log` 里**什么都看不到**。
机器人看起来在正常干活,实际完全不可见——日志因此失去证据价值,
排查时会误以为"日志里没有 = 事情没发生"。

所以统一走 watchdog.start_bot():日志追加写、已有实例时拒绝重复启动、
探测失败时保守不动手。任何启动方式的日志都落在同一对文件里,排查不再有盲区。
"""
import sys

from watchdog import bot_instances, start_bot


def main() -> int:
    inst = bot_instances()
    if inst is None:
        # 探测失败时绝不据此启动,否则会累积重复实例(项目里踩过多次)
        print("进程探测失败，保守起见不启动（避免拉出重复实例）")
        return 2
    if inst:
        print(f"Bot already running (PID {inst[-1][0]}), skip")
        return 0
    if start_bot():
        print("Bot started")
        return 0
    print("启动失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())
