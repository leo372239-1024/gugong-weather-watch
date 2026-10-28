# -*- coding: utf-8 -*-
"""
故宫降雪预警 —— 核心引擎

设计目标：用「尽可能准确」的方式判断未来若干天故宫上空是否落雪。

技术路线（气象业务标准做法，非单源猜测）：
  1. 确定性多模式集成：ECMWF IFS025 / GFS / ICON / CMA-GRAPES（中国气象局）/ JMA / GEM
     —— 一次请求拿 6 家主流全球模式的日降雪量、天气现象码、气温极值
  2. 集合预报概率：ECMWF IFS 50 成员集合，统计「报出降雪的成员比例」= 真实降雪概率
     —— 这是判断「会不会下」最严谨的方式，远优于看单一数值预报
  3. 相态校验：北京冬季 0~3℃ 常出现雨夹雪/模式误报雪，用多模式日最低温中位数二次校验
  4. 交叉一致性：6 家模式报雪的"票数"与集合概率相互印证，给出置信度分级

输出：逐日降雪评估 + 抢票时间推算（参观日 - 7 天 20:00）+ 邮件通知。

运行环境：Python 3.9+（仅标准库，无第三方依赖，可直接用于腾讯云 CloudBase 云函数）
"""

import json
import os
import statistics
import ssl
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------

TZ_CN = timezone(timedelta(hours=8))  # 固定 UTC+8，不依赖系统 tzdata

DEFAULT_CONFIG = {
    # 目标地点：故宫博物院（紫禁城中心，太和殿一带）
    "site_name": "故宫博物院（紫禁城）",
    "latitude": 39.9163,
    "longitude": 116.3972,

    # 预报与告警窗口
    "scan_days": 15,          # 实际拉取天数（ECMWF/GFS 可到 15~16 天）
    "alert_days": 8,          # 「最近 8 天」主告警窗口（用户要求）
    "forecast_days_limit": 8,  # 8 天窗口内 = 可行动预警；8 天外 = 远期观察

    # 故宫放票规则（已核验官方 ticket.dpm.org.cn）
    "book_lead_days": 7,      # 参观日前 7 天
    "book_hour": 20,          # 20:00 开售
    "book_minute": 0,

    # 确定性模式列表（Open-Meteo 模式标识）
    "models": [
        "ecmwf_ifs025",       # 欧洲中心，全球中期预报最权威
        "gfs_seamless",       # 美国 NOAA
        "icon_seamless",      # 德国 DWD
        "cma_grapes_global",  # 中国气象局 GRAPES，本地化最好
        "jma_seamless",       # 日本气象厅
        "gem_seamless",       # 加拿大
    ],
    "ensemble_model": "ecmwf_ifs025",
    "ensemble_members": 50,

    # 判据阈值
    "snow_cm_threshold": 0.1,        # 日降雪量 >= 0.1cm 记为有雪
    "snow_wmo_codes": [71, 73, 75, 77, 85, 86],  # WMO 降雪类天气码
    "prob_high": 0.50,               # 集合概率 >= 50% → 高置信
    "prob_medium": 0.25,             # >= 25% → 中等
    "prob_low": 0.05,                # > 5%  → 弱信号
    "vote_high": 4,                  # >= 4/6 家模式报雪 → 高置信
    "vote_medium": 2,                # >= 2 家 → 中等
    "temp_rain_cutoff": 2.5,         # 报雪模式日最低温中位数 >= 2.5℃ → 降级（雨夹雪）
    "temp_rain_hard": 3.5,           # >= 3.5℃ → 物理上不可能落雪，直接剔除
    "temp_frozen": 0.0,              # 日最高温中位数 <= 0℃ 视为全天冰点，升级

    # 邮件
    "smtp_host": "smtp.example.com",
    "smtp_port": 465,
    "smtp_ssl": True,
    "smtp_user": "you@example.com",
    "smtp_password": "",             # 由环境变量 SMTP_PASSWORD 注入
    "mail_from_name": "故宫降雪预警",
    "mail_from": "you@example.com",
    "mail_to": ["you@example.com"],
    "send_when_no_snow": False,      # 无雪不发信
    "send_on_error": True,           # 抓取失败时发告警信（禁止静默失败）

    # 网络
    "http_timeout": 30,
    "http_retries": 3,
}


def load_config():
    """默认配置 + 环境变量覆盖（密码等敏感项走环境变量，不落代码）。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝

    env_map = {
        "SMTP_HOST": ("smtp_host", str),
        "SMTP_PORT": ("smtp_port", int),
        "SMTP_USER": ("smtp_user", str),
        "SMTP_PASSWORD": ("smtp_password", str),
        "MAIL_FROM": ("mail_from", str),
        "MAIL_TO": ("mail_to", lambda v: [x.strip() for x in v.split(",") if x.strip()]),
        "ALERT_DAYS": ("alert_days", int),
        "SCAN_DAYS": ("scan_days", int),
        "SEND_WHEN_NO_SNOW": ("send_when_no_snow", lambda v: v.lower() in ("1", "true", "yes")),
        "SITE_NAME": ("site_name", str),
        "LATITUDE": ("latitude", float),
        "LONGITUDE": ("longitude", float),
    }
    for env_key, (cfg_key, caster) in env_map.items():
        raw = os.environ.get(env_key)
        if raw not in (None, ""):
            try:
                cfg[cfg_key] = caster(raw)
            except Exception:
                pass
    return cfg


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------

def log(msg):
    print("[gugong-snow] " + str(msg), flush=True)


def http_get_json(url, timeout=30, retries=3, backoff=(3, 8, 20)):
    """带重试的 GET。失败抛异常，绝不静默返回空数据。"""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "gugong-snow-alert/1.0 (+personal weather monitor)",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read().decode("utf-8")
            return json.loads(payload)
        except Exception as exc:                      # noqa: BLE001
            last_err = exc
            log("HTTP 请求失败 (第 %d/%d 次): %s | %s" % (attempt + 1, retries, url[:110], exc))
            if attempt < retries - 1:
                time.sleep(backoff[min(attempt, len(backoff) - 1)])
    raise RuntimeError("天气数据获取失败，已重试 %d 次：%s" % (retries, last_err))


def median_or_none(values):
    vals = [v for v in values if isinstance(v, (int, float))]
    return statistics.median(vals) if vals else None


def fmt_temp(v):
    return "—" if v is None else "%.1f℃" % v


# ----------------------------------------------------------------------------
# 数据抓取
# ----------------------------------------------------------------------------

def fetch_deterministic(cfg, target_date):
    """6 家确定性模式：日降雪量 / 天气码 / 降水 / 气温极值。"""
    params = {
        "latitude": cfg["latitude"],
        "longitude": cfg["longitude"],
        "daily": "weather_code,snowfall_sum,precipitation_sum,"
                 "temperature_2m_min,temperature_2m_max,temperature_2m_mean",
        "timezone": "Asia/Shanghai",
        "forecast_days": cfg["scan_days"],
        "models": ",".join(cfg["models"]),
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    data = http_get_json(url, timeout=cfg["http_timeout"], retries=cfg["http_retries"])
    daily = data.get("daily") or {}
    if not daily.get("time"):
        raise RuntimeError("确定性预报返回结构异常：缺少 daily.time")
    return daily


def fetch_ensemble(cfg):
    """ECMWF 50 成员集合预报：用于计算降雪概率。"""
    params = {
        "latitude": cfg["latitude"],
        "longitude": cfg["longitude"],
        "daily": "snowfall_sum",
        "timezone": "Asia/Shanghai",
        "forecast_days": cfg["scan_days"],
        "models": cfg["ensemble_model"],
    }
    url = "https://ensemble-api.open-meteo.com/v1/ensemble?" + urllib.parse.urlencode(params)
    data = http_get_json(url, timeout=cfg["http_timeout"], retries=cfg["http_retries"])
    daily = data.get("daily") or {}
    if not daily.get("time"):
        raise RuntimeError("集合预报返回结构异常：缺少 daily.time")
    return daily


# ----------------------------------------------------------------------------
# 分析
# ----------------------------------------------------------------------------

LEVEL_ORDER = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
LEVEL_LABEL = {"HIGH": "高", "MEDIUM": "中", "LOW": "低", "NONE": "无"}


def _upgrade(level):
    return {"NONE": "LOW", "LOW": "MEDIUM", "MEDIUM": "HIGH", "HIGH": "HIGH"}[level]


def _downgrade(level):
    return {"HIGH": "MEDIUM", "MEDIUM": "LOW", "LOW": "NONE", "NONE": "NONE"}[level]


def analyze(cfg, det_daily, ens_daily):
    """逐日评估降雪可能性，返回按日期排序的评估列表。"""
    dates = det_daily["time"]
    ens_index = {d: i for i, d in enumerate(ens_daily.get("time", []))}
    th_cm = cfg["snow_cm_threshold"]
    snow_codes = set(cfg["snow_wmo_codes"])
    models = cfg["models"]

    # 集合成员键名
    member_keys = [k for k in ens_daily.keys() if k.startswith("snowfall_sum_member")]

    results = []
    for i, d in enumerate(dates):
        per_model = {}
        for m in models:
            snow = (det_daily.get("snowfall_sum_%s" % m) or [None] * len(dates))[i]
            code = (det_daily.get("weather_code_%s" % m) or [None] * len(dates))[i]
            pcp = (det_daily.get("precipitation_sum_%s" % m) or [None] * len(dates))[i]
            tmin = (det_daily.get("temperature_2m_min_%s" % m) or [None] * len(dates))[i]
            tmax = (det_daily.get("temperature_2m_max_%s" % m) or [None] * len(dates))[i]
            per_model[m] = {"snow_cm": snow, "code": code, "pcp": pcp, "tmin": tmin, "tmax": tmax}

        # —— 模式投票：报出降雪的模式数
        votes = 0
        snow_amounts = []
        snow_model_set = set()
        for m, v in per_model.items():
            has_snow = False
            if isinstance(v["snow_cm"], (int, float)) and v["snow_cm"] >= th_cm:
                has_snow = True
                snow_amounts.append(v["snow_cm"])
            elif isinstance(v["code"], int) and v["code"] in snow_codes:
                has_snow = True
            if has_snow:
                votes += 1
                snow_model_set.add(m)

        # —— 气温：优先取「报雪模式」的气温中位数判断落地相态，无报雪模式时退回全模式。
        #    用全模式会稀释边缘信号（例如 1 家报雪但其余模式暖），导致误判。
        temp_pool = ([per_model[m] for m in snow_model_set]
                     if snow_model_set else list(per_model.values()))
        tmin_med = median_or_none([v["tmin"] for v in temp_pool])
        tmax_med = median_or_none([v["tmax"] for v in temp_pool])
        pcp_med = median_or_none([v["pcp"] for v in per_model.values()])

        # —— 集合概率
        prob = None
        n_members = 0
        if d in ens_index and member_keys:
            j = ens_index[d]
            member_vals = []
            for k in member_keys:
                arr = ens_daily.get(k) or []
                if j < len(arr) and isinstance(arr[j], (int, float)):
                    member_vals.append(arr[j])
            n_members = len(member_vals)
            if n_members:
                prob = sum(1 for x in member_vals if x >= th_cm) / float(n_members)

        # —— 综合定级
        level = "NONE"
        reasons = []
        if prob is not None and prob >= cfg["prob_high"]:
            level = "HIGH"
            reasons.append("集合概率 %.0f%%" % (prob * 100))
        elif prob is not None and prob >= cfg["prob_medium"]:
            level = "MEDIUM"
            reasons.append("集合概率 %.0f%%" % (prob * 100))
        elif prob is not None and prob > cfg["prob_low"]:
            level = "LOW"
            reasons.append("集合概率 %.0f%%" % (prob * 100))

        if votes >= cfg["vote_high"]:
            level = "HIGH" if LEVEL_ORDER[level] < 3 else level
            reasons.append("%d/%d 家模式报雪" % (votes, len(models)))
        elif votes >= cfg["vote_medium"]:
            if LEVEL_ORDER[level] < 2:
                level = "MEDIUM"
            reasons.append("%d/%d 家模式报雪" % (votes, len(models)))
        elif votes >= 1:
            if LEVEL_ORDER[level] < 1:
                level = "LOW"
            reasons.append("%d/%d 家模式报雪" % (votes, len(models)))

        # —— 相态修正（关键：避免北京 0~3℃ 的雨夹雪被误报成纯雪，也避免暖天误报雪）
        phase_note = ""
        if level != "NONE":
            if tmin_med is not None and tmin_med >= cfg["temp_rain_hard"]:
                # 最低温显著高于冰点，降水几乎不可能以雪的形式落地 —— 直接剔除
                level = "NONE"
                phase_note = "报雪模式的气温偏高（最低 %s），降水应为雨，已排除降雪" % fmt_temp(tmin_med)
            elif tmin_med is not None and tmin_med >= cfg["temp_rain_cutoff"]:
                level = _downgrade(_downgrade(level))
                phase_note = "近地面气温偏高（最低 %s），降水可能以雨为主，仅少量雨夹雪" % fmt_temp(tmin_med)
            elif tmax_med is not None and tmax_med <= cfg["temp_frozen"]:
                level = _upgrade(level)
                phase_note = "全天维持冰点以下（最高 %s），积雪易留存" % fmt_temp(tmax_med)
            elif tmin_med is not None and cfg["temp_frozen"] < tmin_med < cfg["temp_rain_cutoff"]:
                phase_note = "雨雪临界温度（最低 %s），可能为雨夹雪" % fmt_temp(tmin_med)

        if level == "NONE":
            continue

        # —— 降雪量估计：取报雪模式降雪量的中位数
        if snow_amounts:
            est = statistics.median(snow_amounts)
        elif isinstance(pcp_med, (int, float)) and pcp_med > 0:
            est = pcp_med * 0.7  # 粗估：降水按雪水比折算
        else:
            est = None
        # 算出 0 或负值等同于「量级无法估计」，避免出现「有雪但 0.0cm」的自相矛盾
        if est is not None and est <= 0:
            est = None

        results.append({
            "date": d,
            "level": level,
            "level_label": LEVEL_LABEL[level],
            "prob": prob,
            "members": n_members,
            "votes": votes,
            "model_count": len(models),
            "snow_est_cm": est,
            "tmin": tmin_med,
            "tmax": tmax_med,
            "pcp": pcp_med,
            "reasons": reasons,
            "phase_note": phase_note,
        })

    return results


def snow_grade(cm):
    """按降雪量给中文描述。cm 为雪深厘米数。"""
    if cm is None:
        return "量级待定"
    if cm < 1.0:
        return "小雪"
    if cm < 3.0:
        return "中雪"
    if cm < 5.0:
        return "大雪"
    return "暴雪"


def build_action(cfg, day):
    """推算抢票时间与状态。"""
    y, m, dd = (int(x) for x in day["date"].split("-"))
    visit = date(y, m, dd)
    book = visit - timedelta(days=cfg["book_lead_days"])
    today = datetime.now(TZ_CN).date()

    book_dt = datetime(book.year, book.month, book.day,
                       cfg["book_hour"], cfg["book_minute"], tzinfo=TZ_CN)
    now = datetime.now(TZ_CN)

    if book > today:
        days_left = (book - today).days
        status = "UPCOMING"
        status_text = "%d 天后（%s 周%s %02d:%02d）开抢" % (
            days_left, book.isoformat(), "一二三四五六日"[book.weekday()],
            cfg["book_hour"], cfg["book_minute"])
    elif book == today:
        if now < book_dt:
            status = "TONIGHT"
            status_text = "就在今晚 %02d:%02d 开抢" % (cfg["book_hour"], cfg["book_minute"])
        else:
            status = "TODAY_LATE"
            status_text = "今晚 %02d:%02d 已开售，尽快下单" % (cfg["book_hour"], cfg["book_minute"])
    else:
        status = "OPENED"
        status_text = "预售已开启，只能看余票或退票回流"

    # 周一闭馆（法定节假日除外）
    closed_monday = (visit.weekday() == 0)

    return {
        "visit_date": visit.isoformat(),
        "visit_weekday": "周" + "一二三四五六日"[visit.weekday()],
        "book_date": book.isoformat(),
        "book_weekday": "周" + "一二三四五六日"[book.weekday()],
        "book_at": "%s %02d:%02d" % (book.isoformat(), cfg["book_hour"], cfg["book_minute"]),
        "status": status,
        "status_text": status_text,
        "closed_monday": closed_monday,
    }


# ----------------------------------------------------------------------------
# 邮件渲染
# ----------------------------------------------------------------------------

LEVEL_COLOR = {"HIGH": "#c0392b", "MEDIUM": "#d35400", "LOW": "#7f8c8d"}
LEVEL_BG = {"HIGH": "#fdecea", "MEDIUM": "#fef5e7", "LOW": "#f4f6f7"}


def render_text(cfg, alerts, run_at):
    lines = []
    lines.append("故宫降雪预警 · %s" % run_at.strftime("%Y-%m-%d %H:%M"))
    lines.append("=" * 46)
    lines.append("")
    for a in alerts:
        if "action" not in a:
            a["action"] = build_action(cfg, a)
        act = a["action"]
        lines.append("【%s】%s  %s" % (a["level_label"], a["date"], act["visit_weekday"]))
        amt = ("降雪量约 %.1fcm" % a["snow_est_cm"]) if a["snow_est_cm"] is not None else "降雪量待定"
        lines.append("  预计：%s　%s　气温 %s / %s" % (
            snow_grade(a["snow_est_cm"]), amt,
            fmt_temp(a["tmin"]), fmt_temp(a["tmax"])))
        lines.append("  依据：%s" % "；".join(a["reasons"]))
        if a["phase_note"]:
            lines.append("  相态：%s" % a["phase_note"])
        lines.append("  抢票：%s %s" % (act["book_at"], act["status_text"]))
        if a.get("window") == "far":
            lines.append("  说明：远期预报（超出 8 天窗口），仅供参考，抢票日尚未来临")
        if act["closed_monday"]:
            lines.append("  注意：该日为周一，故宫闭馆（法定节假日除外）")
        lines.append("")
    lines.append("-" * 46)
    lines.append("放票规则：参观日前 7 天 20:00 开售，不售当日票，周一闭馆。")
    lines.append("官方渠道：故宫博物院小程序 / ticket.dpm.org.cn")
    lines.append("数据源：Open-Meteo（ECMWF / GFS / ICON / CMA-GRAPES / JMA / GEM + ECMWF 50 成员集合）")
    return "\n".join(lines)


def render_html(cfg, alerts, run_at):
    if not alerts:
        return """<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f5f6f7;">
<div style="max-width:640px;margin:0 auto;padding:24px 16px;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;">
  <div style="background:#fff;border-radius:12px;padding:24px;">
    <h1 style="margin:0 0 8px;font-size:19px;color:#1a1a1a;font-weight:700;">故宫降雪巡检 · 无降雪信号</h1>
    <p style="font-size:13px;color:#555;line-height:1.9;margin:0;">
      %s 巡检：未来 %d 天内，故宫上空未监测到降雪信号。<br>
      生成时间：%s（北京时间）
    </p>
  </div>
</div>
</body></html>""" % (cfg["site_name"], cfg["alert_days"], run_at.strftime("%Y-%m-%d %H:%M"))

    rows = []
    for a in alerts:
        if "action" not in a:
            a["action"] = build_action(cfg, a)
        act = a["action"]
        color = LEVEL_COLOR[a["level"]]
        bg = LEVEL_BG[a["level"]]
        prob_txt = "%.0f%%" % (a["prob"] * 100) if a["prob"] is not None else "—"
        amount = "%.1f cm" % a["snow_est_cm"] if a["snow_est_cm"] is not None else "—"
        monday = ('<div style="color:#c0392b;font-size:12px;margin-top:4px;">'
                  '⚠ 该日为周一，故宫闭馆（法定节假日除外），请确认是否值得抢票</div>'
                  ) if act["closed_monday"] else ""
        phase = ('<div style="color:#7f8c8d;font-size:12px;margin-top:4px;">%s</div>'
                 % a["phase_note"]) if a["phase_note"] else ""
        far_note = ('<div style="color:#9a6b00;font-size:12px;margin-top:4px;">'
                    'ⓘ 远期预报（超出 8 天可行动窗口），仅供参考，抢票日尚未来临</div>'
                    ) if a.get("window") == "far" else ""
        status_color = {"TONIGHT": "#c0392b", "TODAY_LATE": "#c0392b",
                        "UPCOMING": "#d35400", "OPENED": "#888888"}.get(act["status"], "#555555")
        rows.append("""
    <tr>
      <td style="padding:14px 12px;border-bottom:1px solid #eee;background:%s;">
        <div style="font-size:17px;font-weight:700;color:#1a1a1a;">%s</div>
        <div style="font-size:12px;color:#666;margin-top:2px;">%s</div>
      </td>
      <td style="padding:14px 12px;border-bottom:1px solid #eee;background:%s;">
        <span style="display:inline-block;padding:2px 10px;border-radius:11px;background:%s;color:#fff;font-size:12px;font-weight:600;">%s置信</span>
        <div style="margin-top:6px;font-size:14px;color:#1a1a1a;">%s · %s</div>
        <div style="font-size:12px;color:#666;margin-top:2px;">集合概率 %s（%d 成员）· %d/%d 家模式报雪</div>
        %s
      </td>
      <td style="padding:14px 12px;border-bottom:1px solid #eee;font-size:13px;color:#333;background:%s;">
        <div style="font-weight:600;color:%s;">%s</div>
        <div style="color:#666;margin-top:3px;">气温 %s / %s</div>
        <div style="margin-top:8px;padding-top:8px;border-top:1px dashed #e0e0e0;font-size:12px;color:#555;">
          🎫 <b>%s</b> %s<br>
          <span style="color:#888;">抢票时间：%s</span>
        </div>
        <div style="margin-top:6px;font-size:12px;font-weight:700;color:%s;">%s</div>
        %s
      </td>
        </tr>""" % (
            bg, a["date"], act["visit_weekday"],
            bg, color, a["level_label"],
            snow_grade(a["snow_est_cm"]), amount,
            prob_txt, a["members"], a["votes"], a["model_count"],
            phase,
            bg, color, snow_grade(a["snow_est_cm"]),
            fmt_temp(a["tmin"]), fmt_temp(a["tmax"]),
            act["book_date"], act["book_weekday"],
            act["book_at"],
            status_color, act["status_text"],
            monday + far_note,
        ))

    if all(a.get("window") == "far" for a in alerts):
        banner = "均为 8 天前的远期预报，仅供提前留意；抢票日尚未到来，无需现在动手"
        banner_color = "#8e7cc3"
    elif any(a["action"]["status"] == "TONIGHT" for a in alerts):
        banner = "今晚 20:00 就要开抢，别错过"
        banner_color = "#c0392b"
    elif any(a["action"]["status"] == "UPCOMING" for a in alerts):
        banner = "预约日尚未到来，日历上先记一笔"
        banner_color = "#d35400"
    else:
        banner = "预售窗口已开启，仅剩余票机会"
        banner_color = "#7f8c8d"

    return """<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f5f6f7;">
<div style="max-width:760px;margin:0 auto;padding:20px 16px;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;">

  <div style="background:#fff;border-radius:12px;padding:22px 24px;box-shadow:0 1px 4px rgba(0,0,0,.06);">
    <div style="font-size:12px;color:#999;letter-spacing:.5px;">%s · 降雪监测</div>
    <h1 style="margin:8px 0 4px;font-size:22px;color:#1a1a1a;font-weight:700;">❄️ 故宫今冬降雪预警</h1>
    <div style="font-size:13px;color:#666;margin-top:6px;">
      未来 %d 天内共监测到 <b style="color:%s;">%d 个降雪日</b>，最早的出现在 <b>%s</b>。
    </div>
    <div style="margin-top:12px;padding:10px 14px;background:#fafbfc;border-left:3px solid %s;border-radius:0 6px 6px 0;font-size:13px;color:#444;">
      %s
    </div>
  </div>

  <div style="background:#fff;border-radius:12px;margin-top:14px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.06);">
    <table style="width:100%%;border-collapse:collapse;">
      <thead>
        <tr style="background:#f8f9fa;">
          <th style="text-align:left;padding:10px 12px;font-size:12px;color:#888;font-weight:600;">参观日</th>
          <th style="text-align:left;padding:10px 12px;font-size:12px;color:#888;font-weight:600;">降雪置信度</th>
          <th style="text-align:left;padding:10px 12px;font-size:12px;color:#888;font-weight:600;">抢票行动</th>
        </tr>
      </thead>
      <tbody>%s</tbody>
    </table>
  </div>

  <div style="background:#fff;border-radius:12px;margin-top:14px;padding:18px 24px;box-shadow:0 1px 4px rgba(0,0,0,.06);">
    <div style="font-size:14px;font-weight:700;color:#1a1a1a;margin-bottom:10px;">📋 抢票要点</div>
    <ul style="margin:0;padding-left:20px;font-size:13px;color:#444;line-height:1.9;">
      <li>故宫门票<b>参观日前 7 天 20:00</b> 准时开售，<b>不售当日票</b>。</li>
      <li>唯一官方渠道：<b>「故宫博物院」微信小程序</b> / <a href="https://ticket.dpm.org.cn/" style="color:#c0392b;">ticket.dpm.org.cn</a>。第三方代购均非官方。</li>
      <li>实行实名制，<b>提前在小程序里填好参观人信息</b>，开抢时只点提交。</li>
      <li><b>周一闭馆</b>（法定节假日除外），雪日撞上周一请先确认开放安排。</li>
      <li>门票分上午/下午时段，上午场最迟 12:00 检票，下午场最早 11:00 检票。</li>
      <li>未能成行请于参观前一日 24:00 前退票，不计爽约。</li>
    </ul>
  </div>

  <div style="background:#fff;border-radius:12px;margin-top:14px;padding:16px 24px;box-shadow:0 1px 4px rgba(0,0,0,.06);">
    <div style="font-size:12px;color:#888;line-height:1.8;">
      <b>数据说明</b><br>
      地点：%s（%.4f°N, %.4f°E）<br>
      数据源：Open-Meteo 确定性多模式集成（ECMWF IFS025 / GFS / ICON / CMA-GRAPES / JMA / GEM）+ ECMWF 50 成员集合预报<br>
      判定方法：集合成员报雪比例 = 降雪概率；多模式投票交叉验证；日最低温中位数做雨雪相态校验<br>
      生成时间：%s（北京时间）<br>
      <span style="color:#aaa;">远期（5 天以上）预报存在不确定性，置信度会随临近而修正。本邮件由自动化程序发出。</span>
    </div>
  </div>

  <div style="text-align:center;font-size:11px;color:#bbb;padding:18px 0;">
    故宫降雪预警 · 每日 07:00 自动巡检
  </div>
</div>
</body></html>""" % (
        cfg["site_name"], cfg["alert_days"],
        banner_color, len(alerts), alerts[0]["date"],
        banner_color, banner,
        "".join(rows),
        cfg["site_name"], cfg["latitude"], cfg["longitude"],
        run_at.strftime("%Y-%m-%d %H:%M"),
    )


def render_error_html(cfg, err, run_at):
    return """<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,'PingFang SC',sans-serif;padding:24px;background:#f5f6f7;">
<div style="max-width:640px;margin:0 auto;background:#fff;border-radius:12px;padding:24px;border-top:4px solid #c0392b;">
  <h2 style="margin:0 0 10px;font-size:19px;color:#c0392b;">⚠️ 故宫降雪预警：本次巡检失败</h2>
  <p style="font-size:13px;color:#444;line-height:1.8;">
    定时任务在 <b>%s</b> 执行时未能完成天气数据抓取，因此<b>今天没有降雪结论</b>——这不代表无雪。
  </p>
  <div style="background:#fdecea;border-radius:8px;padding:12px 14px;font-size:12px;color:#922b21;font-family:monospace;word-break:break-all;line-height:1.6;">
    %s
  </div>
  <p style="font-size:13px;color:#444;line-height:1.8;margin-top:14px;">
    建议：打开腾讯云 CloudBase 控制台查看该云函数日志，确认是否为上游接口故障或已超额。
  </p>
</div>
</body></html>""" % (run_at.strftime("%Y-%m-%d %H:%M"), str(err)[:800])


# ----------------------------------------------------------------------------
# 邮件发送
# ----------------------------------------------------------------------------

def send_mail(cfg, subject, html, text=""):
    if not cfg.get("smtp_password"):
        raise RuntimeError("SMTP_PASSWORD 未配置，无法发送邮件")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(cfg["mail_from_name"], "utf-8")), cfg["mail_from"]))
    msg["To"] = ", ".join(cfg["mail_to"])
    msg["Date"] = formatdate(localtime=True)
    # 先纯文本后 HTML，客户端按能力择优显示
    msg.attach(MIMEText(text or "", "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    last_err = None
    for attempt in range(3):
        try:
            context = ssl.create_default_context()
            if cfg["smtp_ssl"]:
                server = smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"],
                                          context=context, timeout=30)
            else:
                server = smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30)
                server.starttls(context=context)
            try:
                server.login(cfg["smtp_user"], cfg["smtp_password"])
                server.sendmail(cfg["mail_from"], cfg["mail_to"], msg.as_string())
            finally:
                try:
                    server.quit()
                except Exception:                      # noqa: BLE001
                    pass
            log("邮件已发送 -> %s" % ", ".join(cfg["mail_to"]))
            return True
        except Exception as exc:                       # noqa: BLE001
            last_err = exc
            log("邮件发送失败 (第 %d/3 次): %s" % (attempt + 1, exc))
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("邮件发送失败：%s" % last_err)


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------

def run(cfg=None, dry_run=False):
    cfg = cfg or load_config()
    run_at = datetime.now(TZ_CN)
    log("=== 故宫降雪巡检开始 %s ===" % run_at.strftime("%Y-%m-%d %H:%M:%S %Z"))

    try:
        det = fetch_deterministic(cfg, run_at.date())
        log("确定性预报：%d 天 × %d 模式" % (len(det.get("time", [])), len(cfg["models"])))
        ens = fetch_ensemble(cfg)
        member_count = len([k for k in ens.keys() if k.startswith("snowfall_sum_member")])
        log("集合预报：%d 天 × %d 成员" % (len(ens.get("time", [])), member_count))

        days = analyze(cfg, det, ens)
        log("未来 %d 天内检出降雪日：%d 个" % (cfg["scan_days"], len(days)))

        # 窗口分层：8 天内 = 可行动；8 天外 = 远期观察（只作趋势参考）
        cutoff = (run_at.date() + timedelta(days=cfg["alert_days"])).isoformat()
        in_window = [d for d in days if d["date"] <= cutoff]
        far = [d for d in days if d["date"] > cutoff]
        for d in in_window:
            d["window"] = "near"
        for d in far:
            d["window"] = "far"

        for d in days:
            d["action"] = build_action(cfg, d)

        # 8 天内无雪时，远期（>8 天）的弱信号不足以构成行动依据，且会逐日重复发送，
        # 因此远端只放行 MEDIUM 及以上；LOW 仅记录、不告警。
        if in_window:
            alerts = in_window
        else:
            alerts = [d for d in far
                      if LEVEL_ORDER.get(d["level"], 0) >= LEVEL_ORDER["MEDIUM"]]

        if not alerts:
            suppressed = [d for d in far if d not in alerts]
            if suppressed:
                log("8 天内无雪；远期存在 %d 个弱信号（%s），按规则不告警"
                    % (len(suppressed),
                       "，".join("%s %s 概率%s" % (d["date"], d["level_label"],
                                                  ("%.0f%%" % (d["prob"] * 100)) if d["prob"] is not None else "—")
                                for d in suppressed)))
            else:
                log("未来 %d 天无降雪信号" % cfg["scan_days"])
            if cfg["send_when_no_snow"]:
                subject = "[故宫降雪预警] %s 未来 %d 天无降雪信号" % (run_at.strftime("%m-%d"), cfg["alert_days"])
                html = render_html(cfg, [], run_at)
                text = "%s 巡检：未来 %d 天无降雪信号。生成于 %s" % (
                    cfg["site_name"], cfg["alert_days"], run_at.strftime("%Y-%m-%d %H:%M"))
                if not dry_run:
                    send_mail(cfg, subject, html, text)
            return {"status": "no_alert",
                    "suppressed_far": [{"date": d["date"], "level": d["level"], "prob": d["prob"]}
                                       for d in suppressed]}

        # 主题
        nearest = alerts[0]
        act = nearest["action"]
        if act["status"] == "TONIGHT":
            head = "今晚20:00开抢"
        elif act["status"] == "TODAY_LATE":
            head = "今晚已开售"
        elif act["status"] == "UPCOMING":
            head = act["status_text"].split("（")[0] + "开抢"
        else:
            head = "预售已开启"

        subject = "[故宫降雪] %s %s有雪（%s置信）· %s" % (
            nearest["date"][5:], act["visit_weekday"],
            nearest["level_label"], head)
        if len(alerts) > 1:
            subject += " 等%d天" % len(alerts)

        html = render_html(cfg, alerts, run_at)
        text = render_text(cfg, alerts, run_at)

        if dry_run:
            log("dry-run 模式，不发送邮件")
            log(text)
        else:
            send_mail(cfg, subject, html, text)

        return {
            "status": "alert_sent" if not dry_run else "dry_run",
            "subject": subject,
            "alerts": [
                {
                    "date": a["date"],
                    "level": a["level"],
                    "prob": a["prob"],
                    "votes": a["votes"],
                    "book_at": a["action"]["book_at"],
                    "status": a["action"]["status"],
                }
                for a in alerts
            ],
            "far_outlook": [{"date": d["date"], "level": d["level"], "prob": d["prob"]} for d in far],
        }

    except Exception as exc:                           # noqa: BLE001
        log("巡检失败：%s" % exc)
        if cfg["send_on_error"] and not dry_run:
            try:
                send_mail(cfg,
                          "[故宫降雪预警] ⚠️ %s 巡检失败" % run_at.strftime("%m-%d"),
                          render_error_html(cfg, exc, run_at), "")
            except Exception as mail_exc:              # noqa: BLE001
                log("告警邮件也发送失败：%s" % mail_exc)
        raise


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    result = run(dry_run=dry)
    print(json.dumps(result, ensure_ascii=False, indent=2))
