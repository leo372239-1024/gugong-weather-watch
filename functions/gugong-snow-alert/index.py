# -*- coding: utf-8 -*-
"""
腾讯云 CloudBase 云函数入口 —— 故宫天气双任务

部署信息：
  运行时  Python 3.10
  入口    index.main
  触发器  定时器 0 0 7 * * * *（每天 07:00:00，北京时间）

每个 07:00 依次执行两个相互独立的任务（任一失败不影响另一个）：
  任务 A  降雪巡查   —— 未来 8 天有雪 → 发邮件（抢票提醒）
  任务 B  天气播报   —— 每天必推 → Bark 推送到 iPhone（天气+概率+穿衣）

定位统一为「故宫博物院」：39.9163N, 116.3972E

环境变量：
  SMTP_PASSWORD   邮件通道（任务 A 必需）
  SMTP_HOST / SMTP_PORT / SMTP_USER / MAIL_FROM / MAIL_TO
  BARK_KEY        Bark 推送（任务 B 必需）
  BARK_SERVER     可选，默认 https://api.day.app，自建可替换
  BARK_GROUP / BARK_LEVEL / BRIEF_ENABLE / BRIEF_ALWAYS_PUSH / BRIEF_RAIN_PROB_ALERT  可选

运维调用方式（手动触发时传 event）：
  {}                    正常巡检（两个任务都跑）
  {"dry_run": true}     两个任务都只分析、不发送
  {"snow_only": true}   只跑降雪巡查
  {"brief_only": true}  只跑天气播报
  {"test_mail": true}   邮件通道自检
  {"test_bark": true}   推送通道自检（忽略「晴天不推」规则，强制推一条）
"""

import json
import traceback
from datetime import datetime

import daily_brief
import snow_alert


def _task_mail_selftest():
    cfg = snow_alert.load_config()
    run_at = datetime.now(snow_alert.TZ_CN)
    stamp = run_at.strftime("%Y-%m-%d %H:%M:%S")
    html = snow_alert.render_html(cfg, [], run_at)
    note = ('<p style="font-size:13px;color:#c0392b;margin:10px 0 0;">'
            '这是一封通道自检邮件（%s），用于确认云函数到邮箱的投递链路正常。</p>' % stamp)
    html = html.replace("</h1>", "</h1>" + note, 1)
    text = ("故宫降雪预警通道自检：云函数已成功连通 %s:%s 并投递本邮件。（%s）"
            % (cfg["smtp_host"], cfg["smtp_port"], stamp))
    snow_alert.send_mail(cfg, "[自检] 故宫降雪预警 · 云端邮件通道正常 " + stamp, html, text)
    print("[handler] 邮件通道自检已发送")
    return {"status": "test_mail_sent", "at": stamp}


def main(event, context):
    event = event if isinstance(event, dict) else {}
    dry_run = bool(event.get("dry_run"))

    # —— 单通道自检（互不干扰，便于定位是哪条链路出问题）
    if event.get("test_mail"):
        return {"ok": True, "result": _task_mail_selftest()}

    if event.get("test_bark"):
        cfg = daily_brief.load_brief_config()
        r = daily_brief.run_daily_brief(cfg, dry_run=dry_run, force=True)
        return {"ok": True, "result": {"test_bark": r}}

    brief_only = bool(event.get("brief_only"))
    snow_only = bool(event.get("snow_only"))

    out = {}
    errors = []

    # —— 任务 A：降雪巡查 → 邮件
    if not brief_only:
        try:
            out["snow"] = snow_alert.run(dry_run=dry_run)
        except Exception as exc:                      # noqa: BLE001
            traceback.print_exc()
            errors.append("降雪巡查失败：%s" % exc)

    # —— 任务 B：当天天气 → Bark
    if not snow_only:
        try:
            out["brief"] = daily_brief.run_daily_brief(dry_run=dry_run)
        except Exception as exc:                      # noqa: BLE001
            traceback.print_exc()
            errors.append("天气播报失败：%s" % exc)

    print("[handler] 执行结果：" + json.dumps(out, ensure_ascii=False))

    if errors:
        # 抛异常让云函数记一次失败，便于在控制台配置告警；成功的那部分已写入日志
        raise RuntimeError("；".join(errors))

    return {"ok": True, "result": out}


# 兼容两种入口写法：index.main / index.main_handler
main_handler = main


if __name__ == "__main__":
    # 本地调试：python index.py
    print(main({}, None))
