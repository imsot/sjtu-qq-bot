"""离线自测：不依赖 NapCat / Claude，用本地模拟服务器验证机器人事件流转。

用法：python selftest.py
会启动一个假的 OneBot 服务器（127.0.0.1:3001），依次给机器人发：
  1. 群消息 #掷骰子      → 期望回复骰子
  2. 群消息 "早上好"      → 期望关键词回复
  3. 主人私聊 #提醒 1分钟 喝水 → 期望创建提醒
  4. 进群通知            → 期望欢迎语（带 @）
然后检查机器人发出的所有 action 是否符合预期。
请确保此时没开真正的 NapCat。
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta

os.environ.setdefault("OWNER_QQ", "10002")  # 模拟测试用主人号
os.environ.setdefault("WELCOME_MSG", "欢迎测试新人")

import aiohttp
import aiohttp.web
from aiohttp import web

from bot.client import OneBotClient
from bot.config import Config
from bot.db import DB
from bot.router import Router
from plugins.admin import AdminPlugin, _parse_duration
# 两个插件都有同名函数 _remain，用别名区分，避免后者覆盖前者导致测错对象
from plugins.aihaoke import (TASK_TYPE, AihaokePlugin, _has_deadline, _parse_dt,
                             _remain as _ahk_remain)
from plugins.canvas import CanvasPlugin, _next_link, _parse_iso, _remain
from plugins.chat import ChatPlugin
from plugins.fun import FunPlugin
from plugins.remind import RemindPlugin, fmt_time, parse_remind
from plugins.sjtu_academic import (SjtuAcademicPlugin, _due_tiers, _extract_token,
                                   _fmt_range, _is_open, _jwt_exp,
                                   _parse_dt as _acad_parse_dt, _quota_text,
                                   _range as _acad_range, _remain as _acad_remain)

RECEIVED: list[dict] = []


def unit_checks() -> bool:
    """纯函数单元检查：提醒时间解析、禁言时长解析"""
    ok = True

    def check(name: str, cond: bool):
        nonlocal ok
        ok = ok and cond
        print(f"  {'✅' if cond else '❌'} {name}")

    import time as _t

    now = _t.time()
    t, c = parse_remind("5分钟 喝水")
    check("相对时间 5分钟", t is not None and 290 < t - now < 310 and c == "喝水")
    t, c = parse_remind("2小时 写作业")
    check("相对时间 2小时", t is not None and 7100 < t - now < 7300 and c == "写作业")
    t, c = parse_remind("3天后 还书")
    check("相对时间 3天", t is not None and 3 * 86400 - 60 < t - now < 3 * 86400 + 60 and c == "还书")
    t, c = parse_remind("明早8点 打卡")
    check("时刻 明早8点", t is not None and c == "打卡" and fmt_time(t).endswith("08:00"))
    t, c = parse_remind("今晚9点半 睡觉")
    check("时刻 今晚9点半", t is not None and c == "睡觉" and fmt_time(t).endswith("21:30"))
    t, c = parse_remind("今天8点 开会")  # 若 8 点已过应顺延到明天
    check("时刻 今天8点可解析", t is not None and c == "开会")
    t, c = parse_remind("随便写点什么")
    check("乱写返回 None", t is None and c is None)
    check("禁言时长 5分钟=300秒", _parse_duration("5分钟") == 300)
    check("禁言时长 1小时=3600秒", _parse_duration("1小时") == 3600)
    check("禁言时长 默认10分钟", _parse_duration("随便") == 600)
    return ok


def canvas_checks() -> bool:
    """Canvas 插件的纯函数检查：时间解析、分页链接、剩余时间、消息渲染。
    全部离线，不连 Canvas。"""
    ok = True

    def check(name: str, cond: bool):
        nonlocal ok
        ok = ok and cond
        print(f"  {'✅' if cond else '❌'} {name}")

    # ISO8601 时间解析（Canvas 返回 UTC，需转本地）
    dt = _parse_iso("2026-09-25T15:59:00Z")
    check("ISO 时间解析", dt is not None and dt.year == 2026 and dt.month == 9)
    check("空时间返回 None", _parse_iso(None) is None)
    check("缺字段返回 None", _parse_iso("") is None)
    check("乱码时间返回 None", _parse_iso("not-a-date") is None)

    # Link 头分页解析
    link = ('<https://oc.sjtu.edu.cn/api/v1/courses?page=2>; rel="next", '
            '<https://oc.sjtu.edu.cn/api/v1/courses?page=9>; rel="last"')
    check("Link 取 next", _next_link(link) == "https://oc.sjtu.edu.cn/api/v1/courses?page=2")
    check("Link 无 next 返回 None",
          _next_link('<https://x/y?page=9>; rel="last"') is None)
    check("Link 为空返回 None", _next_link(None) is None and _next_link("") is None)

    # 剩余时间粒度
    now = datetime.now().astimezone()
    check("剩余时间 天", "天" in _remain(now + timedelta(days=3)))
    check("剩余时间 小时", "小时" in _remain(now + timedelta(hours=5)))
    check("剩余时间 分钟", "分钟" in _remain(now + timedelta(minutes=30)))
    check("已截止", _remain(now - timedelta(minutes=1)) == "已截止")
    check("无截止时间", _remain(None) == "无截止时间")

    # 消息渲染
    p = CanvasPlugin()
    names = {1: "CS201 数据结构"}
    asg = p._render_asg({"name": "实验三", "due_at": None, "html_url": "http://x",
                         "_course": 1}, names)
    check("作业渲染含课程名", "CS201 数据结构" in asg and "实验三" in asg)
    ann = p._render_ann({"title": "调课通知", "posted_at": "2026-09-18T06:30:00Z",
                         "html_url": "http://y", "_course": 1}, names)
    check("公告渲染含标题", "调课通知" in ann and "CS201 数据结构" in ann)
    dl = p._render_deadline({"name": "实验三", "html_url": "http://x", "_course": 1},
                            names, now + timedelta(hours=2), "2 小时")
    check("截止提醒含剩余时间", "2 小时" in dl and "实验三" in dl)
    check("成绩去重键含分数",
          p._grade_key({"id": 7, "submission": {"score": 95}}) == "grade:7:95")
    return ok


def aihaoke_checks() -> bool:
    """知途好课插件的纯函数检查。全部离线，不连平台。"""
    ok = True

    def check(name: str, cond: bool):
        nonlocal ok
        ok = ok and cond
        print(f"  {'✅' if cond else '❌'} {name}")

    # 平台的时间字符串有两种格式，实测都出现过
    a = _parse_dt("2026-09-19 23:59")
    check("解析 无秒 格式", a is not None and a.hour == 23 and a.minute == 59)
    b = _parse_dt("2026-09-19 23:59:59")
    check("解析 带秒 格式", b is not None and b.second == 59)
    check("空值返回 None", _parse_dt(None) is None and _parse_dt("") is None)
    check("乱码返回 None", _parse_dt("不是时间") is None)

    # 哨兵日期：平台用 9999-12-31 表示"无截止时间"，绝不能当真
    sentinel = _parse_dt("9999-12-31 23:59:59")
    check("哨兵能解析出", sentinel is not None)
    check("哨兵被判定为无截止", not _has_deadline(sentinel))
    check("1900 起始时间也不当截止", not _has_deadline(_parse_dt("1900-01-01 00:00:00")))
    check("正常日期算有截止", _has_deadline(_parse_dt("2026-09-19 23:59")))
    check("None 算无截止", not _has_deadline(None))

    # 剩余时间粒度（naive 本地时间）
    now = datetime.now()
    check("剩余时间 天", "天" in _ahk_remain(now + timedelta(days=3)))
    check("剩余时间 小时", "小时" in _ahk_remain(now + timedelta(hours=5)))
    check("剩余时间 分钟", "分钟" in _ahk_remain(now + timedelta(minutes=30)))
    check("已截止", _ahk_remain(now - timedelta(minutes=1)) == "已截止")

    # 渲染
    p = AihaokePlugin()
    names = {28565: "高等数学 上"}
    t = {"taskId": 84486, "taskName": "9月18日作业", "taskType": 50,
         "endTime": "2026-09-19 23:59", "_course": 28565}
    r = p._render_task(t, names, _parse_dt(t["endTime"]))
    check("任务渲染含课程名", "高等数学 上" in r and "9月18日作业" in r)
    check("任务渲染含类型中文", "练习" in r)
    check("任务渲染含链接", "/student/course/28565/home" in r)
    dl = p._render_deadline(t, names, now + timedelta(hours=2), "2 小时")
    check("截止提醒含剩余时间", "2 小时" in dl and "9月18日作业" in dl)
    check("taskType 50 = 练习", TASK_TYPE.get(50) == "练习")
    return ok


def sjtu_academic_checks() -> bool:
    """第二课堂插件的纯函数检查。全部离线，不连平台。"""
    ok = True

    def check(name: str, cond: bool):
        nonlocal ok
        ok = ok and cond
        print(f"  {'✅' if cond else '❌'} {name}")

    # 平台时间是 'YYYY-MM-DD HH:mm:ss'（北京时间墙钟，无时区后缀）
    a = _acad_parse_dt("2026-09-27 10:00:00")
    check("解析 带秒 格式", a is not None and a.hour == 10 and a.second == 0)
    b = _acad_parse_dt("2026-09-27 10:00")
    check("解析 无秒 格式", b is not None and b.hour == 10 and b.minute == 0)
    check("空值返回 None", _acad_parse_dt(None) is None and _acad_parse_dt("") is None)
    check("乱码返回 None", _acad_parse_dt("不是时间") is None)

    # 时间字段是 2 元素数组，但要容错单值 / 空值
    s, e = _acad_range(["2026-09-20 08:00:00", "2026-09-30 23:59:59"])
    check("区间数组两头都解析", s is not None and e is not None)
    s2, e2 = _acad_range("2026-09-20 08:00:00")
    check("单值容错（只有开始）", s2 is not None and e2 is None)
    check("空值区间返回 (None,None)", _acad_range(None) == (None, None))
    check("区间渲染含波浪号", "~" in _fmt_range(["2026-09-20 08:00:00",
                                                "2026-09-30 23:59:59"]))

    # 测试数据一律相对当前时间构造，避免写死日期后测试随时间腐坏
    now = datetime.now()

    def stamp(delta) -> str:
        return (now + delta).strftime("%Y-%m-%d %H:%M:%S")

    base_act = {
        "id": 12345,
        "name": "2026 秋季志愿服务",
        "sponsor": "上海交通大学青年志愿者协会",
        "activity_time": [stamp(timedelta(days=1)), stamp(timedelta(days=1, hours=2))],
        "registration_time": [stamp(-timedelta(days=1)), stamp(timedelta(days=3))],
        "method": 3, "person_num": 50, "signed_up_num": 37,
    }
    check("报名中判定为可报名", _is_open(base_act, now)[0])

    not_started = dict(base_act, registration_time=[stamp(timedelta(days=5)),
                                                    stamp(timedelta(days=9))])
    check("报名未开始被拦下", _is_open(not_started, now) == (False, "报名未开始"))
    ended = dict(base_act, registration_time=[stamp(-timedelta(days=9)),
                                              stamp(-timedelta(days=3))])
    check("报名已结束被拦下", _is_open(ended, now) == (False, "报名已结束"))

    full = dict(base_act, person_num=50, signed_up_num=50)
    check("method=3 满员被拦下", _is_open(full, now) == (False, "名额已满"))
    # 关键回归：只有 method=3 才按名额判满；其它报名方式前端压根不显示人数比例
    full_other = dict(full, method=2)
    check("method≠3 不按名额判满", _is_open(full_other, now)[0])

    check("method=3 显示人数比", _quota_text(base_act) == "37/50 人")
    check("method=2 不显示人数比", _quota_text(dict(base_act, method=2)) == "")
    check("无名额上限不显示", _quota_text(dict(base_act, person_num=0)) == "")

    # 剩余时间粒度（naive 本地时间，与平台墙钟一致）
    check("剩余时间 天", "天" in _acad_remain(now + timedelta(days=3)))
    check("剩余时间 小时", "小时" in _acad_remain(now + timedelta(hours=5)))
    check("剩余时间 分钟", "分钟" in _acad_remain(now + timedelta(minutes=30)))
    check("已截止", _acad_remain(now - timedelta(minutes=1)) == "已截止")
    check("无截止时间", _acad_remain(None) == "无截止时间")

    # 渲染与去重键
    p = SjtuAcademicPlugin()
    base_url = "https://activity.sjtu.edu.cn"
    r = p._render(base_act, now, base_url)
    check("推送含活动名", "2026 秋季志愿服务" in r)
    check("推送含主办方", "青年志愿者协会" in r)
    check("推送含报名截止", "报名截止" in r and "还剩" in r)
    check("推送含详情链接", "/activity/detail/12345" in r)
    check("去重键用 id", p._key(base_act) == "act:12345")
    check("无 id 时去重键为 None", p._key({"name": "无 id"}) is None)

    # JWT 过期时间解析（该平台 token 只活 24h，靠这个提前提醒主人换）
    # payload 是预编码好的 {"exp":1790580352}，避免测试里再引 base64
    jwt = "header.eyJleHAiOjE3OTA1ODAzNTJ9.signature"
    exp = _jwt_exp(jwt)
    check("JWT 能解出过期时间", exp is not None and exp.year == 2026)
    check("非法 JWT 返回 None", _jwt_exp("not-a-jwt") is None)
    check("空 token 返回 None", _jwt_exp("") is None)
    check("无 exp 字段返回 None", _jwt_exp("a.eyJhIjoxfQ.c") is None)

    # #二课token 命令取 token（从剪贴板粘来常带引号或换行）
    check("提取 token", _extract_token("#二课token eyJabc.def.ghi")[0]
          == "eyJabc.def.ghi")
    check("提取带引号的 token",
          _extract_token('#二课token "eyJabc.def.ghi"')[0] == "eyJabc.def.ghi")
    check("提取带换行和空格的 token",
          _extract_token("#二课token\n  eyJabc.def.ghi  ")[0] == "eyJabc.def.ghi")
    check("没带 token 时报错", _extract_token("#二课token") == (None, "没带 token"))
    check("只有空白也报错", _extract_token("#二课token   ") == (None, "没带 token"))

    # 路由日志脱敏：凭证类命令的正文绝不能进 bot_out.log / bot_err.log
    r = Router(None, None, None)
    r.plugins = [p]
    check("凭证命令日志脱敏", "eyJabc" not in r._log_text("#二课token eyJabc.def.ghi"))
    check("脱敏后仍能看出是哪条命令",
          r._log_text("#二课token eyJabc.def.ghi").startswith("#二课token"))
    check("普通命令日志不受影响", r._log_text("#二课状态") == "#二课状态")
    check("超长普通消息仍截断到 40 字", len(r._log_text("啊" * 100)) == 40)

    # ---- 收藏活动的报名提醒 ----
    collected = [
        {"activity_id": 1, "name": "正常活动",
         "registration_time": [stamp(timedelta(days=2)), stamp(timedelta(days=5))]},
        # 实测平台真会返回这种(不开放报名的收藏活动),必须跳过,
        # 否则会拿 None 算出一个荒谬的提醒时刻
        {"activity_id": 2, "name": "没有报名时间",
         "registration_time": [None, None]},
        {"activity_id": 3, "name": "报名已开始",
         "registration_time": [stamp(-timedelta(days=1)), stamp(timedelta(days=3))]},
        {"activity_id": 4, "name": "报名已结束",
         "registration_time": [stamp(-timedelta(days=9)), stamp(-timedelta(days=3))]},
        # 个别接口只给 id 不给 activity_id,要兜住
        {"id": 5, "name": "只有 id",
         "registration_time": [stamp(timedelta(days=4)), stamp(timedelta(days=6))]},
    ]
    st: dict = {}
    watching, changed = p._sync_collected(st, collected, base_url)
    check("只盯报名尚未开始的收藏活动", watching == 2 and changed == 2)
    check("无报名时间的被跳过", "2" not in st["collected"])
    check("报名已开始的被跳过", "3" not in st["collected"])
    check("报名已结束的被跳过", "4" not in st["collected"])
    check("缺 activity_id 时用 id 兜底", "5" in st["collected"])
    check("记录里存了详情链接",
          st["collected"]["1"]["url"].endswith("/activity/detail/1"))

    # 报名时间没变要沿用已发标记(否则每轮重发);改期要重置
    # (否则改期后会沿用旧的已发标记,把提醒整个吞掉 —— 改期才是最该提醒的)
    st["collected"]["1"]["sent"] = {"d1": True}
    p._sync_collected(st, collected, base_url)
    check("报名时间未变时保留已发标记",
          st["collected"]["1"]["sent"].get("d1") is True)
    moved = [dict(a) for a in collected]
    moved[0] = dict(moved[0], registration_time=[stamp(timedelta(days=3)),
                                                stamp(timedelta(days=6))])
    p._sync_collected(st, moved, base_url)
    check("报名时间改期后重置已发标记", not st["collected"]["1"]["sent"])

    # 该发哪一层提醒
    def at(hours, sent=None):
        return {"reg_start": stamp(timedelta(hours=hours)), "sent": dict(sent or {})}

    check("离报名还有 2 天时都不发", _due_tiers(at(48), now)[1] is None)
    check("进入 1 天内发「提前1天」", _due_tiers(at(20), now)[1] == "d1")
    _rs, fire, mark = _due_tiers(at(0.5), now)
    check("进入 1 小时内只发最紧迫的「1小时」那一层",
          fire == "h1" and sorted(mark) == ["d1", "h1"])
    check("进入 10 分钟内发「10分钟」那一层", _due_tiers(at(5 / 60), now)[1] == "m10")
    check("断线重连跨过多层时,更粗的层一并标记作罢",
          sorted(_due_tiers(at(5 / 60), now)[2]) == ["d1", "h1", "m10"])
    _rs, fire, mark = _due_tiers(at(-5 / 60), now)
    check("报名已开始就不再补发", fire is None and mark)
    check("已发过的层不再重发",
          _due_tiers(at(0.5, {"d1": True, "h1": True}), now)[1] is None)
    check("没有报名开始时间则不提醒",
          _due_tiers({"reg_start": None, "sent": {}}, now) == (None, None, []))

    # 提醒计划文案
    txt = SjtuAcademicPlugin._reminders_text({"remind_collected": True}, st)
    check("提醒计划含活动名", "正常活动" in txt)
    check("提醒计划含报名时间", "报名开始" in txt)
    check("关闭收藏提醒时有说明",
          "关闭" in SjtuAcademicPlugin._reminders_text({"remind_collected": False}, {}))
    check("没有收藏活动时有说明",
          "没有" in SjtuAcademicPlugin._reminders_text({}, {}))

    # ---- #二课收藏：列出全部收藏及报名开始时间（不是只列会被提醒的）----
    col_all = [
        {"activity_id": 20, "name": "报名已开始的",
         "registration_time": [stamp(-timedelta(days=1)), stamp(timedelta(days=2))]},
        {"activity_id": 10, "name": "待提醒的",
         "registration_time": [stamp(timedelta(days=2)), stamp(timedelta(days=4))]},
        {"activity_id": 30, "name": "没有报名时间的",
         "registration_time": [None, None]},
    ]
    watched = {"10": {"aid": "10", "name": "待提醒的",
                      "reg_start": stamp(timedelta(days=2)), "sent": {"d1": True}}}
    # 报名窗口已经关掉的那个，应当被剔除（名字故意和提示文案不重合，
    # 否则"已剔除 N 个报名已截止的"这句话会让下面的 in 判断失真）
    closed_item = {"activity_id": 40, "name": "已关闭的活动",
                   "registration_time": [stamp(-timedelta(days=9)),
                                          stamp(-timedelta(days=3))]}
    txt = SjtuAcademicPlugin._render_collection(col_all + [closed_item],
                                               {"collected": watched}, base_url)
    check("收藏列表列出报名期内的活动",
          "待提醒的" in txt and "报名已开始的" in txt and "没有报名时间的" in txt)
    check("报名已截止的被剔除", "已关闭的活动" not in txt)
    check("剔除数量写在标题里（不是静默丢弃）", "已剔除 1 个" in txt)
    check("收藏列表标出无报名时间", "无报名时间" in txt)
    check("收藏列表标出报名已开始", "报名已开始（不再提前提醒）" in txt)
    check("收藏列表显示各层的已提醒状态", "已提醒:提前 1 天" in txt)
    check("收藏列表带详情链接", "/activity/detail/10" in txt)
    check("有报名时间的排在无报名时间的之前",
          txt.index("待提醒的") < txt.index("没有报名时间的"))
    check("一个收藏都没有时有说明",
          "还没有收藏" in SjtuAcademicPlugin._render_collection([], {}, base_url))
    check("全部已截止时给出提示而不是空列表",
          "没有还在报名期的收藏活动"
          in SjtuAcademicPlugin._render_collection([closed_item], {}, base_url))
    check("无报名时间的活动不会被当成已截止误删",
          "没有报名时间的" in SjtuAcademicPlugin._render_collection(
              [col_all[2]], {}, base_url))
    return ok


def group_text(text: str) -> dict:
    return {
        "post_type": "message", "message_type": "group",
        "self_id": 10001, "user_id": 10002, "group_id": 20001, "message_id": 1,
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
    }


def private_text(text: str) -> dict:
    return {
        "post_type": "message", "message_type": "private",
        "self_id": 10001, "user_id": 10002, "message_id": 2,
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
    }


def group_increase(user_id: int) -> dict:
    return {
        "post_type": "notice", "notice_type": "group_increase",
        "self_id": 10001, "user_id": user_id, "group_id": 20001,
    }


async def mock_server():
    async def handler(request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        events = [
            group_text("#掷骰子"),
            group_text("早上好"),
            private_text("#提醒 1分钟 喝水"),
            group_increase(10003),
        ]
        for ev in events:
            await ws.send_json(ev)
            await asyncio.sleep(0.5)  # 给机器人处理时间，保持事件顺序
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                RECEIVED.append(json.loads(msg.data))
        return ws

    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 3001)
    await site.start()
    return runner


async def main():
    logging.basicConfig(level=logging.WARNING)
    print("== 单元检查 ==")
    unit_ok = unit_checks()
    print("\n== Canvas 插件检查（离线）==")
    canvas_ok = canvas_checks()
    print("\n== 知途好课插件检查（离线）==")
    aihaoke_ok = aihaoke_checks()
    print("\n== 第二课堂插件检查（离线）==")
    acad_ok = sjtu_academic_checks()

    print("\n== 集成检查（模拟服务器）==")
    cfg = Config()
    db = DB("data/selftest.db")
    client = OneBotClient("ws://127.0.0.1:3001")
    router = Router(client, db, cfg)
    for plugin in (RemindPlugin(), AdminPlugin(), FunPlugin(), CanvasPlugin(),
                   AihaokePlugin(), SjtuAcademicPlugin(), ChatPlugin()):
        router.register(plugin)
    client.on_event = router.handle

    server = await mock_server()
    try:
        await asyncio.wait_for(client.run(), timeout=6)
    except asyncio.TimeoutError:
        pass
    await asyncio.sleep(1)
    await server.cleanup()

    msgs = [a["params"]["message"] for a in RECEIVED if a.get("action") == "send_group_msg"]
    pmsgs = [a["params"]["message"] for a in RECEIVED if a.get("action") == "send_private_msg"]

    print("群消息回复:", msgs)
    print("私聊回复:", pmsgs)

    checks = [
        ("骰子回复", any("掷出" in str(m) for m in msgs)),
        ("关键词回复", any("早安" in str(m) for m in msgs)),
        ("创建提醒", any("已记下" in str(m) for m in pmsgs)),
        ("欢迎语带@", any("欢迎测试新人" in str(m) and isinstance(m, list) for m in msgs)),
    ]
    ok = (all(v for _, v in checks) and unit_ok and canvas_ok and aihaoke_ok
          and acad_ok)
    for name, passed in checks:
        print(f"  {'✅' if passed else '❌'} {name}")
    print("\n✅ PASS：全部通过" if ok else "\n❌ FAIL：有未通过的项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
