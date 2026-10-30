# -*- coding: utf-8 -*-
"""
每日天气播报自检：验证「是否推送」判定、风力换算、AQI 折算、穿衣档位与新简报格式。

用法：
  python selftest_brief.py             # 全部校验（含真实数据链路）
  python selftest_brief.py --offline   # 跳过真实数据链路（本地网络异常时用）
  python selftest_brief.py --send      # 用真实天气推一条（需 BARK_KEY）
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "functions", "gugong-snow-alert"))

import daily_brief  # noqa: E402


def make_hourly(feel_min, feel_max, prob, code_am=3, code_pm=3,
                wind_lo=8.0, wind_hi=18.0, wind_deg=350):
    """生成 24 小时数据：07 点最冷、14 点最热；上午/下午天气码可分别指定。"""
    times, feels, temps, probs, winds, dirs, codes = [], [], [], [], [], [], []
    for h in range(24):
        times.append("2026-10-28T%02d:00" % h)
        if 7 <= h <= 21:
            ratio = max(0.0, 1.0 - abs(h - 14) / 7.0)
            f = feel_min + (feel_max - feel_min) * ratio
            f = max(feel_min, min(feel_max, f))
            w = wind_lo + (wind_hi - wind_lo) * ratio
        else:
            f = feel_min - 4
            w = wind_lo
        feels.append(round(f, 1))
        temps.append(round(f + 1.0, 1))
        probs.append(prob)
        winds.append(round(w, 1))
        dirs.append(wind_deg)
        codes.append(code_am if h < 13 else code_pm)
    return {"time": times, "apparent_temperature": feels, "temperature_2m": temps,
            "precipitation_probability": probs, "wind_speed_10m": winds,
            "wind_direction_10m": dirs, "weather_code": codes}


def make_air(pm25, pm10):
    times = ["2026-10-28T%02d:00" % h for h in range(24)]
    return {"time": times, "pm2_5": [pm25] * 24, "pm10": [pm10] * 24}


def make_today(code, tmin, tmax, feel_min=None, feel_max=None, prob=0,
               precip=0.0, snow=0.0, wind=10.0, gust=20.0, uv=3.0, hum=60,
               code_am=None, code_pm=None, wind_deg=350, wind_lo=6.0, wind_hi=None):
    fm = feel_min if feel_min is not None else tmin
    fx = feel_max if feel_max is not None else tmax
    cam = code if code_am is None else code_am
    cpm = code if code_pm is None else code_pm
    daily = {
        "time": ["2026-10-28"],
        "weather_code": [code],
        "precipitation_probability_max": [prob],
        "precipitation_sum": [precip],
        "snowfall_sum": [snow],
        "temperature_2m_min": [tmin],
        "temperature_2m_max": [tmax],
        "apparent_temperature_min": [tmin - 4],
        "apparent_temperature_max": [tmax],
        "wind_speed_10m_max": [wind],
        "wind_gusts_10m_max": [gust],
        "wind_direction_10m_dominant": [wind_deg],
        "uv_index_max": [uv],
        "relative_humidity_2m_mean": [hum],
        "sunrise": ["2026-10-28T06:38"],
        "sunset": ["2026-10-28T17:17"],
    }
    return {"daily": daily,
            "hourly": make_hourly(fm, fx, prob, cam, cpm,
                                  wind_lo=wind_lo,
                                  wind_hi=wind_hi if wind_hi is not None else wind,
                                  wind_deg=wind_deg)}


# (说明, today 参数, 空气质量(pm25,pm10), 期望推送, 期望穿衣档位关键字)
# 默认 BRIEF_ALWAYS_PUSH=true：晴雨都推，因此所有个例期望均为 True
CASES = [
    ("阴天 · 无降水", dict(code=3, tmin=10, tmax=18, prob=0),
     (35, 50), True, "春秋过渡"),
    ("小雨 · 概率80%", dict(code=61, tmin=8, tmax=13, prob=80, precip=4.2),
     (45, 80), True, "深秋"),
    ("大雪 · 低温", dict(code=75, tmin=-6, tmax=-1, feel_min=-13, feel_max=-5, prob=90, snow=5.0),
     (20, 30), True, "极寒"),
    ("晴天 · 概率低（每日必推）", dict(code=0, tmin=20, tmax=28, prob=5, uv=8),
     (30, 45), True, "夏季"),
    ("晴天 · 概率60%", dict(code=0, tmin=18, tmax=25, prob=60),
     (60, 100), True, "温暖舒适"),
    ("多云 · 低温", dict(code=2, tmin=4, tmax=16, feel_min=2, feel_max=14, prob=10),
     (90, 160), True, "严冬"),
    ("多云 · 大风", dict(code=2, tmin=15, tmax=22, prob=10, wind=35, gust=52, uv=7),
     (50, 80), True, "温暖舒适"),
    ("晴间少云 · 高温（每日必推）", dict(code=1, tmin=26, tmax=34, prob=0, uv=9),
     (25, 40), True, "夏季"),
    ("雾 · 能见度低", dict(code=45, tmin=5, tmax=11, prob=20),
     (70, 120), True, "深秋"),
    ("多云转晴", dict(code=2, tmin=14, tmax=21, prob=2, code_am=2, code_pm=0),
     (30, 50), True, "春秋过渡"),
]


def check_units():
    """风力换算、风向、AQI 折算、日期格式的单元校验。"""
    ok = True
    print("=" * 72)
    print("单元校验：蒲福风级 / 风向 / 中国 AQI / 日期格式")
    print("=" * 72)

    beaufort_cases = [(0, 0), (3, 1), (10, 2), (15, 3), (25, 4),
                      (35, 5), (45, 6), (55, 7), (120, 12)]
    for kmh, want in beaufort_cases:
        got = daily_brief.beaufort(kmh)
        res = got == want
        ok = ok and res
        print("  [%s] %3d km/h → %2s 级（期望 %s）" % ("PASS" if res else "FAIL", kmh, got, want))

    dir_cases = [(0, "北风"), (45, "东北风"), (90, "东风"), (180, "南风"),
                 (270, "西风"), (315, "西北风"), (350, "北风")]
    for deg, want in dir_cases:
        got = daily_brief.wind_dir_cn(deg)
        res = got == want
        ok = ok and res
        print("  [%s] %3d° → %s（期望 %s）" % ("PASS" if res else "FAIL", deg, got, want))

    aqi_cases = [
        ((35, 50), 50, "优"),
        ((75, 150), 100, "良"),
        ((115, 250), 150, "轻度污染"),
        ((150, 350), 200, "中度污染"),
        ((250, 420), 300, "重度污染"),
    ]
    for (pm25, pm10), want_aqi, want_level in aqi_cases:
        got = daily_brief.china_aqi(pm25, pm10)
        lvl = daily_brief.aqi_level(got)
        res = abs(got - want_aqi) <= 1 and lvl == want_level
        ok = ok and res
        print("  [%s] PM2.5=%3d PM10=%3d → AQI %s（%s），期望 %d（%s）"
              % ("PASS" if res else "FAIL", pm25, pm10, got, lvl, want_aqi, want_level))

    date_label = daily_brief.format_date_label("2026-10-28")
    res = date_label == "2026年10月28日（星期三）"
    ok = ok and res
    print("  [%s] 日期格式 → %s" % ("PASS" if res else "FAIL", date_label))
    return ok


def main():
    cfg = daily_brief.load_brief_config()
    ok = check_units()

    print("\n" + "=" * 72)
    print("个例校验：推送判定 / 穿衣档位 / 简报格式")
    print("=" * 72)

    for desc, kw, air_kw, want_push, want_scene in CASES:
        today = make_today(**kw)
        cross = {"code": kw["code"], "prob": kw.get("prob", 0)}
        air = make_air(*air_kw)
        brief = daily_brief.assess(cfg, today, cross, air)

        passed = brief["should_push"] == want_push
        if not passed:
            ok = False

        print("\n[%s] %s → %s，推送=%s" % ("PASS" if passed else "FAIL",
                                          desc, brief["phrase"], brief["should_push"]))
        if not want_push:
            print("     （按规则静默，不推送）")
            continue

        scene, top, bottom, shoes, socks = daily_brief.clothing_advice(brief)
        scene_ok = (want_scene or "") in scene
        if not scene_ok:
            ok = False
        title, body = daily_brief.render_push(cfg, brief)
        print("     穿衣档位：%s [%s]"
              % (scene, "PASS" if scene_ok else "FAIL 期望 " + str(want_scene)))
        print("     标题：%s" % title)
        for line in body.split("\n"):
            print("     正文│ %s" % line)

    # 早晚天气不同 → 「A转B」
    print("\n" + "-" * 72)
    today = make_today(code=2, tmin=14, tmax=21, prob=2, code_am=2, code_pm=0)
    brief = daily_brief.assess(cfg, today, {"code": 2, "prob": 2}, make_air(30, 50))
    trans_ok = brief["phrase"] == "多云转晴"
    if not trans_ok:
        ok = False
    print("[%s] 天气转变描述：%s（期望 多云转晴）"
          % ("PASS" if trans_ok else "FAIL", brief["phrase"]))

    # 降水概率窗口应覆盖到 24 点：构造仅 22、23 点高概率的情形
    print("\n" + "-" * 72)
    t_prob = make_today(code=3, tmin=10, tmax=18, prob=5)
    for i, ts in enumerate(t_prob["hourly"]["time"]):
        hour = int(ts[11:13])
        t_prob["hourly"]["precipitation_probability"][i] = 90 if hour in (22, 23) else 5
    b_prob = daily_brief.assess(cfg, t_prob, {"code": 3, "prob": 5}, make_air(30, 50))
    prob_ok = (b_prob["prob"] == 90) and (b_prob["prob_window"] == "07:00-24:00")
    if not prob_ok:
        ok = False
    print("[%s] 降水概率窗口 %s → %d%%（期望 90%%，取到 22-23 点峰值；若窗口只到 21 点则为 5%%）"
          % ("PASS" if prob_ok else "FAIL", b_prob["prob_window"], b_prob["prob"]))

    # 对照：22 点后的极低体感不应影响穿衣（穿衣只看 07:00-21:00）
    t_cloth = make_today(code=3, tmin=10, tmax=18, prob=5)
    for i, ts in enumerate(t_cloth["hourly"]["time"]):
        if int(ts[11:13]) >= 22:
            t_cloth["hourly"]["apparent_temperature"][i] = -20.0
    b_cloth = daily_brief.assess(cfg, t_cloth, {"code": 3, "prob": 5}, make_air(30, 50))
    scene_only, _, _, _, _ = daily_brief.clothing_advice(b_cloth)
    cloth_ok = scene_only == "春秋过渡"
    if not cloth_ok:
        ok = False
    print("[%s] 夜间低温不污染穿衣：档位=%s（期望 春秋过渡；若误用全天极值会变成极寒）"
          % ("PASS" if cloth_ok else "FAIL", scene_only))

    # 安静模式对照（BRIEF_ALWAYS_PUSH=false）：晴天低概率必须静默
    # 旧默认就是安静模式，2026-10-29/30 连续晴天导致连续两天零通知，故改为默认必推
    print("\n" + "-" * 72)
    sunny_today = make_today(code=0, tmin=20, tmax=28, prob=5)
    cfg_quiet = daily_brief.load_brief_config()
    cfg_quiet["always_push"] = False
    q_push = daily_brief.assess(cfg_quiet, sunny_today, {"code": 0, "prob": 5},
                                make_air(30, 45))["should_push"]
    d_push = daily_brief.assess(cfg, sunny_today, {"code": 0, "prob": 5},
                                make_air(30, 45))["should_push"]
    quiet_ok = (q_push is False) and (d_push is True)
    if not quiet_ok:
        ok = False
    print("[%s] 开关生效：默认每日必推=%s / 安静模式=%s（期望 True / False）"
          % ("PASS" if quiet_ok else "FAIL", d_push, q_push))

    # 真实数据链路
    if "--offline" in sys.argv:
        print("\n[SKIP] 真实数据链路（--offline）")
    else:
        print("\n" + "-" * 72)
        try:
            real = daily_brief.fetch_today(cfg)
            air = daily_brief.fetch_air_quality(cfg)
            cross = daily_brief.fetch_cross_check(cfg)
            rb = daily_brief.assess(cfg, real, cross, air)
            t, b = daily_brief.render_push(cfg, rb)
            print("[PASS] 真实数据链路　今日：%s　推送=%s" % (rb["phrase"], rb["should_push"]))
            print("     标题：%s" % t)
            for line in b.split("\n"):
                print("     正文│ %s" % line)
        except Exception as exc:                      # noqa: BLE001
            ok = False
            print("[FAIL] 真实数据链路：%s" % exc)

    if "--send" in sys.argv:
        print("\n" + "-" * 72)
        daily_brief.run_daily_brief(cfg=cfg, force=True)
        print("已用真实天气推送一条（强制模式）")

    print("\n" + "=" * 72)
    print("总体判定：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
