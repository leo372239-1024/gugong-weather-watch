# -*- coding: utf-8 -*-
"""
自检脚本：用合成雪况验证判定逻辑、抢票推算与邮件渲染。

用法：
  python selftest.py            # 只渲染，不发信
  python selftest.py --send     # 渲染并真实发送一封测试邮件
"""

import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "functions", "gugong-snow-alert"))

import snow_alert  # noqa: E402

TZ = snow_alert.TZ_CN
TODAY = datetime.now(TZ).date()
DAYS = [(TODAY + timedelta(days=i)).isoformat() for i in range(15)]


def build_fixture():
    """构造 15 天合成预报：覆盖 高/中/低/相态降级/周一闭馆 五种场景。"""
    models = snow_alert.DEFAULT_CONFIG["models"]
    n = len(DAYS)

    def blank():
        return [0.0] * n

    snow_sum = {m: blank() for m in models}
    wcode = {m: [0] * n for m in models}
    pcp = {m: blank() for m in models}
    tmin = {m: [4.0] * n for m in models}
    tmax = {m: [11.0] * n for m in models}
    tmean = {m: [7.0] * n for m in models}

    # 场景 A（第 4 天）：6/6 模式报雪 2.0cm，最低 -3℃ → 高置信
    for m in models:
        snow_sum[m][4] = 2.0
        wcode[m][4] = 73
        pcp[m][4] = 2.0
        tmin[m][4] = -3.0
        tmax[m][4] = 1.0
        tmean[m][4] = -1.0

    # 场景 B（第 7 天）：3/6 模式报雪，其余无 → 中等置信
    for m in models[:3]:
        snow_sum[m][7] = 0.8
        wcode[m][7] = 71
        pcp[m][7] = 0.8
        tmin[m][7] = -1.5
        tmax[m][7] = 2.0

    # 场景 C（第 10 天）：仅 1 家模式报雪 → 弱信号
    snow_sum[models[0]][10] = 0.3
    wcode[models[0]][10] = 71
    pcp[models[0]][10] = 0.3
    tmin[models[0]][10] = 0.5
    tmax[models[0]][10] = 5.0

    # 场景 D（第 12 天）：模式报雪，但最低温 6℃ → 应被相态校验降级/剔除
    for m in models[:4]:
        snow_sum[m][12] = 1.5
        wcode[m][12] = 73
        pcp[m][12] = 3.0
        tmin[m][12] = 6.0
        tmax[m][12] = 12.0

    det = {"time": DAYS}
    for m in models:
        det["snowfall_sum_%s" % m] = snow_sum[m]
        det["weather_code_%s" % m] = wcode[m]
        det["precipitation_sum_%s" % m] = pcp[m]
        det["temperature_2m_min_%s" % m] = tmin[m]
        det["temperature_2m_max_%s" % m] = tmax[m]
        det["temperature_2m_mean_%s" % m] = tmean[m]

    # 集合成员：第 4 天 46/50 报雪；第 7 天 18/50；第 10 天 4/50；第 12 天 40/50（检验相态降级）
    profile = {4: 46, 7: 18, 10: 4, 12: 40}
    ens = {"time": DAYS, "snowfall_sum": [0.0] * n}
    for k in range(1, 51):
        arr = [0.0] * n
        for day_idx, count in profile.items():
            if k <= count:
                arr[day_idx] = {4: 2.2, 7: 0.9, 10: 0.3, 12: 1.6}[day_idx]
        ens["snowfall_sum_member%02d" % k] = arr

    return det, ens


def build_far_low_fixture():
    """只含 1 个「远期低置信」雪信号，用于验证远端弱信号抑制规则。"""
    models = snow_alert.DEFAULT_CONFIG["models"]
    n = len(DAYS)
    idx = 12                                   # 今天+12，位于 8 天窗口之外

    det = {"time": DAYS}
    for m in models:
        det["snowfall_sum_%s" % m] = [0.0] * n
        det["weather_code_%s" % m] = [0] * n
        det["precipitation_sum_%s" % m] = [0.0] * n
        det["temperature_2m_min_%s" % m] = [5.0] * n
        det["temperature_2m_max_%s" % m] = [12.0] * n
        det["temperature_2m_mean_%s" % m] = [8.0] * n

    det["snowfall_sum_%s" % models[0]][idx] = 0.3
    det["weather_code_%s" % models[0]][idx] = 71
    det["temperature_2m_min_%s" % models[0]][idx] = 0.5
    det["temperature_2m_max_%s" % models[0]][idx] = 4.0

    ens = {"time": DAYS, "snowfall_sum": [0.0] * n}
    for k in range(1, 51):
        arr = [0.0] * n
        if k <= 3:
            arr[idx] = 0.3
        ens["snowfall_sum_member%02d" % k] = arr
    return det, ens


def main():
    cfg = snow_alert.load_config()
    det, ens = build_fixture()

    days = snow_alert.analyze(cfg, det, ens)
    print("=" * 62)
    print("合成数据判定结果（今日 %s）" % TODAY.isoformat())
    print("=" * 62)

    ok = True
    got = {d["date"]: d for d in days}
    expect = {
        4: ("HIGH", "高置信/6模式/低温"),
        7: ("MEDIUM", "中等置信/3模式"),
        10: ("LOW", "弱信号/单模式"),
        12: ("NONE", "高温降级应剔除"),
    }
    for idx, (want, desc) in expect.items():
        d = DAYS[idx]
        actual = got[d]["level"] if d in got else "NONE"
        mark = "PASS" if actual == want else "FAIL"
        if actual != want:
            ok = False
        print("  第%-2d天 %s  期望 %-6s 实际 %-6s  [%s]  %s"
              % (idx + 1, d, want, actual, mark, desc))

    print()
    print("检出的降雪日明细：")
    for d in days:
        act = snow_alert.build_action(cfg, d)
        print("  %s %s | %s | 概率 %s | %d/%d 模式 | 抢票 %s | %s"
              % (d["date"], act["visit_weekday"], d["level_label"],
                 ("%.0f%%" % (d["prob"] * 100)) if d["prob"] is not None else "—",
                 d["votes"], d["model_count"],
                 act["book_at"], act["status"]))
        if d.get("phase_note"):
            print("        相态：%s" % d["phase_note"])
        if act["closed_monday"]:
            print("        提示：该日为周一，故宫闭馆")

    # 抢票推算校验（「今天+7」的期望值随当前时刻变化：20:00 前是待开抢，之后是已开售）
    print()
    print("抢票状态机校验：")
    now_cn = datetime.now(TZ)
    tonight_expect = "TONIGHT" if now_cn.hour < 20 else "TODAY_LATE"
    for offset, want in [(7, tonight_expect), (9, "UPCOMING"), (3, "OPENED")]:
        visit = TODAY + timedelta(days=offset)
        fake = {"date": visit.isoformat()}
        act = snow_alert.build_action(cfg, fake)
        mark = "PASS" if act["status"] == want else "FAIL"
        if act["status"] != want:
            ok = False
        print("  参观日 %s（今天+%d）→ %s [%s] %s"
              % (visit.isoformat(), offset, act["status"], mark, act["status_text"]))

    # 周一闭馆校验
    print()
    print("周一闭馆校验：")
    for offset in range(1, 15):
        visit = TODAY + timedelta(days=offset)
        if visit.weekday() == 0:
            act = snow_alert.build_action(cfg, {"date": visit.isoformat()})
            mark = "PASS" if act["closed_monday"] else "FAIL"
            if not act["closed_monday"]:
                ok = False
            print("  %s %s → closed_monday=%s [%s]"
                  % (visit.isoformat(), act["visit_weekday"], act["closed_monday"], mark))
            break

    # 渲染校验
    print()
    html = snow_alert.render_html(cfg, days, datetime.now(TZ)) if days else ""
    text = snow_alert.render_text(cfg, days, datetime.now(TZ)) if days else ""
    print("渲染：HTML %d 字符 / 文本 %d 字符" % (len(html), len(text)))

    # —— 端到端：用合成雪数据跑完整 run()，覆盖「有雪分支」的主题构建与渲染。
    #    该分支曾因 subject 引用错字段而在首次降雪时崩溃，必须常态化覆盖。
    print()
    print("端到端（合成雪况跑完整 run，dry-run 不发信）：")
    det_fx, ens_fx = build_fixture()
    orig_det, orig_ens = snow_alert.fetch_deterministic, snow_alert.fetch_ensemble
    snow_alert.fetch_deterministic = lambda _cfg, _d=None: det_fx
    snow_alert.fetch_ensemble = lambda _cfg: ens_fx
    try:
        e2e = snow_alert.run(cfg, dry_run=True)
    finally:
        snow_alert.fetch_deterministic, snow_alert.fetch_ensemble = orig_det, orig_ens

    e2e_checks = []
    e2e_checks.append(("返回 dry_run 状态", e2e.get("status") == "dry_run"))
    e2e_checks.append(("产出邮件主题", bool(e2e.get("subject"))))
    e2e_checks.append(("主题含降雪判定", "有雪" in (e2e.get("subject") or "")))
    e2e_checks.append(("至少一天告警", len(e2e.get("alerts") or []) >= 1))
    for name, res in e2e_checks:
        if not res:
            ok = False
        print("  [%s] %s" % ("PASS" if res else "FAIL", name))
    print("  主题：%s" % e2e.get("subject"))

    # —— 远期弱信号抑制：8 天窗口外若只有 LOW 信号，不应发信（否则会逐日重复骚扰）
    print()
    print("远期弱信号抑制（窗口外仅 LOW）：")
    det_far, ens_far = build_far_low_fixture()
    snow_alert.fetch_deterministic, snow_alert.fetch_ensemble = (
        lambda _cfg, _d=None: det_far, lambda _cfg: ens_far)
    try:
        far_res = snow_alert.run(cfg, dry_run=True)
    finally:
        snow_alert.fetch_deterministic, snow_alert.fetch_ensemble = orig_det, orig_ens

    sup_checks = [
        ("状态为 no_alert（不发信）", far_res.get("status") == "no_alert"),
        ("记录被抑制的远期信号", len(far_res.get("suppressed_far") or []) == 1),
    ]
    for name, res in sup_checks:
        if not res:
            ok = False
        print("  [%s] %s" % ("PASS" if res else "FAIL", name))
    print("  被抑制：%s" % (far_res.get("suppressed_far")))

    if "--send" in sys.argv and days:
        subject = "[自检] 故宫降雪预警 - 渲染与通道测试"
        snow_alert.send_mail(cfg, subject, html, text)
        print("测试邮件已发送")

    print()
    print("总体判定：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
