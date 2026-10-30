# -*- coding: utf-8 -*-
"""
每日天气播报 —— 当天天气简报 + 穿衣建议，通过 Bark 推送到 iPhone

为什么用 Bark 而不是短信：
  国内没有合规且永久免费的 SMS 通道（运营商按条计费，个人主体申请签名需
  APP/网站备案资质）。Bark 是开源 iOS 推送服务，App 免费无内购，公共服务器
  不限量，通知经 Apple APNs 直达锁屏，体验最接近短信且零成本。

触发条件（每天 07:00 由同一个云函数调用）：
  · 默认「每日必推」：无论晴雨，每天早上都推一条（BRIEF_ALWAYS_PUSH 默认 true）
  · 设为 BRIEF_ALWAYS_PUSH=false 才恢复旧的「安静模式」：
      晴天且降水概率 < 50% → 静默不推
    该模式在连续晴天的季节会导致连续多日收不到任何通知，容易被误判为故障，
    因此不再作为默认行为。

推送格式：
  🌤️ 今日天气简报
  【多云转晴  下雨概率2%】
  1.气温范围：14℃ ~ 21℃
  2.风力风向：偏北风4~5级，阵风6级
  3.空气质量：58（良）
  4.穿衣建议：深秋/初冬-高领打底+针织马甲+毛呢大衣-休闲裤-乐福鞋+中厚棉袜
  5.日期：2026年10月28日（星期三）

数据源：
  · 气象：Open-Meteo（best_match 日级 + 小时级）
  · 空气质量：Open-Meteo Air Quality（PM2.5/PM10），按中国 HJ 633-2012 折算 AQI
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date as _date

import snow_alert

TZ_CN = snow_alert.TZ_CN

# ----------------------------------------------------------------------------
# WMO 天气现象码（Open-Meteo 使用 WMO 4677 子集）
# ----------------------------------------------------------------------------

WMO_TEXT = {
    0: "晴", 1: "晴间少云", 2: "多云", 3: "阴",
    45: "有雾", 48: "雾凇",
    51: "轻微毛毛雨", 53: "毛毛雨", 55: "浓毛毛雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷暴伴冰雹",
}

RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
SNOW_CODES = {71, 73, 75, 77, 85, 86}
SUNNY_CODES = {0, 1}

CATEGORY_LABEL = {
    "sunny": "晴", "cloudy": "多云", "overcast": "阴",
    "fog": "雾", "rain": "雨", "snow": "雪", "other": "其他", "unknown": "未知",
}

CATEGORY_EMOJI = {
    "sunny": "☀️", "cloudy": "⛅", "overcast": "☁️",
    "fog": "🌫️", "rain": "🌧️", "snow": "🌨️",
    "other": "🌤️", "unknown": "🌡️",
}

# 穿衣建议表（按白天体感最低温判定，档位自上而下不重叠）
# (最低温阈值, 季节/场景, 上衣组合, 下衣, 鞋子, 袜子)
CLOTHING_TABLE = [
    (-999, "极寒",
     "加厚发热内衣 + 羊毛衫/抓绒衣 + 长款加厚羽绒服/派克大衣",
     "防风裤/加绒裤",
     "高帮雪地靴或厚底防水登山鞋",
     "加厚羊毛袜（务必包裹脚踝）"),
    (-10, "严寒",
     "加厚发热内衣 + 羊毛衫/抓绒衣 + 厚羽绒服/长款棉服",
     "常规长裤（户外建议配防风裤）",
     "加绒短靴或高帮马丁靴",
     "中厚羊毛袜"),
    (0, "严冬",
     "发热内衣/美利奴打底 + 厚毛衣/羽绒马甲 + 防风大衣/羽绒服/冲锋衣",
     "防风裤/休闲长裤",
     "皮质短靴或加绒休闲鞋",
     "厚棉袜或薄羊毛袜"),
    (5, "深秋/初冬",
     "高领打底/长袖T恤 + 针织马甲/薄卫衣 + 毛呢大衣/短风衣",
     "休闲裤/牛仔裤",
     "乐福鞋、切尔西靴或帆布鞋",
     "中厚棉袜"),
    (10, "春秋过渡",
     "长袖T恤/衬衫 + 薄针织开衫/卫衣 + 薄风衣/夹克",
     "运动裤/牛仔裤",
     "运动鞋、板鞋或单皮鞋",
     "标准厚度棉袜"),
    (15, "温暖舒适",
     "短袖T恤/衬衫 + 薄外套（防晒衣/开衫，可选）",
     "透气长裤/短裤",
     "网面运动鞋、凉鞋或休闲鞋",
     "薄棉袜或隐形袜"),
    (20, "夏季",
     "短袖T恤（注重透气与防晒）",
     "短裤/轻薄长裤",
     "透气运动鞋、拖鞋或凉鞋",
     "薄袜或不穿袜"),
]

WEEKDAY_CN = "一二三四五六日"

# 蒲福风级上界（km/h），索引即风级
BEAUFORT_UPPER_KMH = [1, 5, 11, 19, 28, 38, 49, 61, 74, 88, 102, 117]

WIND_DIRS_CN = ["北风", "东北风", "东风", "东南风", "南风", "西南风", "西风", "西北风"]


def weather_text(code):
    return WMO_TEXT.get(code, "未知天气(%s)" % code)


def weather_category(code):
    """把 WMO 码归为 6 类，用于判断是否晴天与图标选择。"""
    if code is None:
        return "unknown"
    if code in SUNNY_CODES:
        return "sunny"
    if code == 2:
        return "cloudy"
    if code == 3:
        return "overcast"
    if code in (45, 48):
        return "fog"
    if code in SNOW_CODES:
        return "snow"
    if code in RAIN_CODES:
        return "rain"
    return "other"


# ----------------------------------------------------------------------------
# 气象量换算
# ----------------------------------------------------------------------------

def beaufort(kmh):
    """km/h → 蒲福风级。"""
    if kmh is None:
        return None
    for level, upper in enumerate(BEAUFORT_UPPER_KMH):
        if kmh <= upper:
            return level
    return 12


def wind_dir_cn(deg):
    """风向角度 → 八方位中文。"""
    if deg is None:
        return None
    try:
        idx = int((float(deg) + 22.5) // 45) % 8
    except (TypeError, ValueError):
        return None
    return WIND_DIRS_CN[idx]


def china_aqi(pm25, pm10):
    """
    按《环境空气质量指数(AQI)技术规定》HJ 633-2012 折算 AQI。

    Open-Meteo 只提供美标(us_aqi)与欧标(european_aqi)，与中国 AQI 分档不同，
    因此取 PM2.5 / PM10 日均浓度按国标分段线性插值，二者取大者即为 AQI。
    """
    def iaqi(conc, breakpoints):
        for c_lo, c_hi, i_lo, i_hi in breakpoints:
            if c_lo <= conc <= c_hi:
                return (i_hi - i_lo) / float(c_hi - c_lo) * (conc - c_lo) + i_lo
        return 500.0

    PM25_BP = [(0, 35, 0, 50), (35, 75, 50, 100), (75, 115, 100, 150),
               (115, 150, 150, 200), (150, 250, 200, 300),
               (250, 350, 300, 400), (350, 500, 400, 500)]
    PM10_BP = [(0, 50, 0, 50), (50, 150, 50, 100), (150, 250, 100, 150),
               (250, 350, 150, 200), (350, 420, 200, 300),
               (420, 500, 300, 400), (500, 600, 400, 500)]

    vals = []
    if isinstance(pm25, (int, float)):
        vals.append(iaqi(pm25, PM25_BP))
    if isinstance(pm10, (int, float)):
        vals.append(iaqi(pm10, PM10_BP))
    if not vals:
        return None
    return int(round(max(vals)))


def aqi_level(aqi):
    if aqi is None:
        return None
    if aqi <= 50:
        return "优"
    if aqi <= 100:
        return "良"
    if aqi <= 150:
        return "轻度污染"
    if aqi <= 200:
        return "中度污染"
    if aqi <= 300:
        return "重度污染"
    return "严重污染"


def format_date_label(iso):
    """2026-10-28 → 2026年10月28日（星期三）"""
    try:
        y, m, d = (int(x) for x in iso.split("-")[:3])
        wd = WEEKDAY_CN[_date(y, m, d).weekday()]
        return "%d年%d月%d日（星期%s）" % (y, m, d, wd)
    except (ValueError, TypeError, IndexError, AttributeError):
        return iso or ""


# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------

DEFAULT_BRIEF_CONFIG = {
    # 与降雪预警共用同一坐标：故宫博物院（紫禁城中心，太和殿一带）
    "site_name": "故宫博物院",
    "latitude": 39.9163,
    "longitude": 116.3972,

    # Bark 推送
    "bark_key": "",                                  # 环境变量 BARK_KEY 注入
    "bark_server": "https://api.day.app",            # 自建可替换为自有域名
    "bark_group": "每日天气",
    "bark_level": "timeSensitive",                   # 可突破专注模式，接近短信强度
    "bark_timeout": 20,
    "bark_retries": 3,

    # 触发判定
    "rain_prob_alert": 50,                           # 晴天但降水概率 >= 此值也推送
    "always_push": True,                             # 每日必推（晴也推）；False 恢复「晴天静默」
    "enable": True,                                  # 总开关
}


def load_brief_config():
    cfg = json.loads(json.dumps(DEFAULT_BRIEF_CONFIG))
    env_map = {
        "BARK_KEY": ("bark_key", str),
        "BARK_SERVER": ("bark_server", str),
        "BARK_GROUP": ("bark_group", str),
        "BARK_LEVEL": ("bark_level", str),
        "BRIEF_ENABLE": ("enable", lambda v: v.lower() in ("1", "true", "yes")),
        "BRIEF_ALWAYS_PUSH": ("always_push", lambda v: v.lower() in ("1", "true", "yes")),
        "BRIEF_RAIN_PROB_ALERT": ("rain_prob_alert", int),
        "LATITUDE": ("latitude", float),
        "LONGITUDE": ("longitude", float),
        "BRIEF_SITE_NAME": ("site_name", str),
    }
    for env_key, (cfg_key, caster) in env_map.items():
        raw = os.environ.get(env_key)
        if raw not in (None, ""):
            try:
                cfg[cfg_key] = caster(raw)
            except Exception:                         # noqa: BLE001
                pass
    return cfg


# ----------------------------------------------------------------------------
# 数据抓取
# ----------------------------------------------------------------------------

DAILY_FIELDS = (
    "weather_code,precipitation_probability_max,precipitation_sum,snowfall_sum,"
    "temperature_2m_max,temperature_2m_min,apparent_temperature_max,apparent_temperature_min,"
    "wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant,"
    "uv_index_max,relative_humidity_2m_mean,sunrise,sunset"
)

HOURLY_FIELDS = (
    "weather_code,apparent_temperature,temperature_2m,precipitation_probability,"
    "precipitation,wind_speed_10m,wind_direction_10m"
)


def fetch_today(cfg):
    """
    当天预报（Open-Meteo best_match 自动融合最优模式）。

    日级给气温极值与主导风向；小时级用于取「白天活动时段」的体感、风速与天气变化，
    避免凌晨最低温把穿衣建议压得过重。
    """
    params = {
        "latitude": cfg["latitude"],
        "longitude": cfg["longitude"],
        "daily": DAILY_FIELDS,
        "hourly": HOURLY_FIELDS,
        "timezone": "Asia/Shanghai",
        "forecast_days": 1,
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    data = snow_alert.http_get_json(url, timeout=30, retries=3)
    daily = data.get("daily") or {}
    if not daily.get("time"):
        raise RuntimeError("当天预报返回结构异常：缺少 daily.time")
    return {"daily": daily, "hourly": data.get("hourly") or {}}


def fetch_air_quality(cfg):
    """空气质量：取 PM2.5 / PM10，失败不阻断主流程（降级为暂无数据）。"""
    try:
        params = {
            "latitude": cfg["latitude"],
            "longitude": cfg["longitude"],
            "hourly": "pm2_5,pm10",
            "timezone": "Asia/Shanghai",
            "forecast_days": 1,
        }
        url = ("https://air-quality-api.open-meteo.com/v1/air-quality?"
               + urllib.parse.urlencode(params))
        data = snow_alert.http_get_json(url, timeout=25, retries=2)
        return data.get("hourly") or {}
    except Exception as exc:                          # noqa: BLE001
        snow_alert.log("空气质量接口不可用（降级处理）：%s" % exc)
        return {}


def fetch_cross_check(cfg):
    """用 ECMWF 单独拉一次天气码，用于交叉校验模式是否分歧。失败不阻断主流程。"""
    try:
        params = {
            "latitude": cfg["latitude"],
            "longitude": cfg["longitude"],
            "daily": "weather_code,precipitation_probability_max",
            "timezone": "Asia/Shanghai",
            "forecast_days": 1,
            "models": "ecmwf_ifs025",
        }
        url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
        data = snow_alert.http_get_json(url, timeout=25, retries=2)
        daily = data.get("daily") or {}
        codes = daily.get("weather_code_ecmwf_ifs025") or daily.get("weather_code") or []
        probs = (daily.get("precipitation_probability_max_ecmwf_ifs025")
                 or daily.get("precipitation_probability_max") or [])
        return {"code": codes[0] if codes else None,
                "prob": probs[0] if probs else None}
    except Exception as exc:                          # noqa: BLE001
        snow_alert.log("交叉校验源不可用（不影响主流程）：%s" % exc)
        return {"code": None, "prob": None}


# ----------------------------------------------------------------------------
# 分析
# ----------------------------------------------------------------------------

def _first(arr):
    return arr[0] if arr else None


def _hourly_at(hourly, key, hour):
    """取指定整点的值。"""
    times = hourly.get("time") or []
    vals = hourly.get(key) or []
    for i, ts in enumerate(times):
        try:
            if int(ts[11:13]) == hour and i < len(vals):
                return vals[i]
        except (ValueError, IndexError):
            continue
    return None


def daytime_stats(hourly, h_start=7, h_end=21, prob_h_end=23):
    """
    白天活动时段统计。

    两个窗口刻意不同：
      · 体感/气温/风速 取 07:00-21:00 —— 用于穿衣建议，覆盖人主要在外的时间
      · 降水概率 取 07:00-24:00 —— 按需求覆盖到当天结束（23 点数据代表 23-24 时段）
    若都用夜间低温算穿衣，会把建议压得过厚。
    """
    times = hourly.get("time") or []

    def collect(key, lo, hi):
        vals = hourly.get(key) or []
        out = []
        for i, ts in enumerate(times):
            try:
                hour = int(ts[11:13])
            except (ValueError, IndexError):
                continue
            if lo <= hour <= hi and i < len(vals) and isinstance(vals[i], (int, float)):
                out.append(vals[i])
        return out

    feels = collect("apparent_temperature", h_start, h_end)
    temps = collect("temperature_2m", h_start, h_end)
    winds = collect("wind_speed_10m", h_start, h_end)
    probs = collect("precipitation_probability", h_start, prob_h_end)

    return {
        "feel_min": min(feels) if feels else None,
        "feel_max": max(feels) if feels else None,
        "temp_min": min(temps) if temps else None,
        "temp_max": max(temps) if temps else None,
        "wind_min": min(winds) if winds else None,
        "wind_max": max(winds) if winds else None,
        "prob_max": max(probs) if probs else None,
        "hours": len(feels),
        "window": "%02d:00-%02d:00" % (h_start, h_end),
        "prob_window": "%02d:00-24:00" % h_start,
    }


def day_weather_phrase(hourly, daily_code):
    """
    生成「多云转晴」这类描述：分别取上午 10 点与下午 17 点的天气现象，
    类别不同时用「A转B」，相同则沿用日级主导码。
    """
    am = _hourly_at(hourly, "weather_code", 10)
    pm = _hourly_at(hourly, "weather_code", 17)
    if am is None or pm is None:
        return weather_text(daily_code)
    if weather_category(am) != weather_category(pm):
        return "%s转%s" % (weather_text(am), weather_text(pm))
    return weather_text(daily_code)


def assess(cfg, today, cross, air=None):
    """组装当天天气结论。today = {"daily": {...}, "hourly": {...}}"""
    daily = (today or {}).get("daily") or {}
    hourly = (today or {}).get("hourly") or {}
    air = air or {}
    date_iso = _first(daily.get("time")) or ""

    day = daytime_stats(hourly)

    code = _first(daily.get("weather_code"))
    prob_daily = _first(daily.get("precipitation_probability_max"))
    prob = (day["prob_max"] if day["prob_max"] is not None
            else (0 if prob_daily is None else prob_daily))

    cat = weather_category(code)
    is_sunny = cat == "sunny"

    # —— 交叉校验
    cross_code = cross.get("code")
    divergence = False
    cross_text = None
    if cross_code is not None:
        cross_text = weather_text(cross_code)
        divergence = weather_category(cross_code) != cat

    # —— 触发判定
    # 默认每日必推。2026-10-29 / 10-30 实测：连续晴天 + 旧「晴天静默」规则，
    # 导致连续两天早上手机没有任何通知，用户误判为服务故障。
    # 提醒类服务的价值在「稳定出现」，不在于「省一条通知」。
    always_push = bool(cfg.get("always_push", True))
    if not is_sunny:
        should_push = True
        reason = "非晴天：%s" % weather_text(code)
    elif prob >= cfg["rain_prob_alert"]:
        should_push = True
        reason = "预报偏晴但降水概率 %d%%" % prob
    elif always_push:
        should_push = True
        reason = "每日播报（晴，降水概率 %d%%）" % prob
    else:
        should_push = False
        reason = ("安静模式：晴且降水概率 %d%% < %d%%，跳过"
                  % (prob, cfg["rain_prob_alert"]))

    # —— 气温：白天窗口优先，缺失回退全天极值
    t_min = day["temp_min"] if day["temp_min"] is not None else _first(daily.get("temperature_2m_min"))
    t_max = day["temp_max"] if day["temp_max"] is not None else _first(daily.get("temperature_2m_max"))
    feel_min = day["feel_min"] if day["feel_min"] is not None else _first(daily.get("apparent_temperature_min"))
    feel_max = day["feel_max"] if day["feel_max"] is not None else _first(daily.get("apparent_temperature_max"))

    # —— 风力风向
    wind_dir_deg = _first(daily.get("wind_direction_10m_dominant"))
    if wind_dir_deg is None:
        wind_dir_deg = _hourly_at(hourly, "wind_direction_10m", 12)
    wind_lo = day["wind_min"] if day["wind_min"] is not None else _first(daily.get("wind_speed_10m_max"))
    wind_hi = day["wind_max"] if day["wind_max"] is not None else _first(daily.get("wind_speed_10m_max"))
    gust_hi = _first(daily.get("wind_gusts_10m_max"))

    # —— 空气质量（白天时段均值，按国标折算）
    pm25 = _mean_in_window(air, "pm2_5")
    pm10 = _mean_in_window(air, "pm10")
    aqi = china_aqi(pm25, pm10)

    # —— 图标跟随「下午」天气：文案写「晴转多云」时图标应是多云而非阴
    pm_code = _hourly_at(hourly, "weather_code", 17)
    icon_cat = weather_category(pm_code) if pm_code is not None else cat

    return {
        "date": date_iso,
        "date_label": format_date_label(date_iso),
        "code": code,
        "text": weather_text(code),
        "phrase": day_weather_phrase(hourly, code),
        "category": cat,
        "category_label": CATEGORY_LABEL.get(cat, "未知"),
        "emoji": CATEGORY_EMOJI.get(icon_cat, "🌤️"),
        "is_sunny": is_sunny,
        "should_push": should_push,
        "reason": reason,
        "prob": prob,
        "prob_daily_max": prob_daily,
        "prob_window": day["prob_window"],
        "day_window": day["window"],
        "day_hours": day["hours"],
        "precip_mm": _first(daily.get("precipitation_sum")),
        "snow_cm": _first(daily.get("snowfall_sum")),
        "t_min": t_min, "t_max": t_max,
        "feel_min": feel_min, "feel_max": feel_max,
        "wind_dir": wind_dir_cn(wind_dir_deg),
        "wind_dir_deg": wind_dir_deg,
        "wind_lo": wind_lo, "wind_hi": wind_hi,
        "wind_level_lo": beaufort(wind_lo),
        "wind_level_hi": beaufort(wind_hi),
        "gust_level": beaufort(gust_hi),
        "aqi": aqi,
        "aqi_level": aqi_level(aqi),
        "pm25": pm25, "pm10": pm10,
        "uv": _first(daily.get("uv_index_max")),
        "humidity": _first(daily.get("relative_humidity_2m_mean")),
        "sunrise": (_first(daily.get("sunrise")) or "")[-5:],
        "sunset": (_first(daily.get("sunset")) or "")[-5:],
        "cross_text": cross_text,
        "divergence": divergence,
    }


def _mean_in_window(air, key, h_start=7, h_end=21):
    """取白天时段某污染物的均值。"""
    times = air.get("time") or []
    vals = air.get(key) or []
    picked = []
    for i, ts in enumerate(times):
        try:
            hour = int(ts[11:13])
        except (ValueError, IndexError):
            continue
        if h_start <= hour <= h_end and i < len(vals) and isinstance(vals[i], (int, float)):
            picked.append(vals[i])
    return (sum(picked) / len(picked)) if picked else None


# ----------------------------------------------------------------------------
# 穿衣建议
# ----------------------------------------------------------------------------

def clothing_advice(brief):
    """按白天体感最低温匹配穿衣档位，返回 (季节/场景, 上衣, 下衣, 鞋子, 袜子)。"""
    t = brief["feel_min"]
    if t is None:
        t = brief["t_min"]
    tier = CLOTHING_TABLE[0]
    if t is not None:
        for row in CLOTHING_TABLE:
            if t >= row[0]:
                tier = row
    return tier[1], tier[2], tier[3], tier[4], tier[5]


def wind_phrase(brief):
    """组装「偏北风4~5级，阵风6级」。"""
    direction = brief["wind_dir"] or "风向不明"
    lo, hi = brief["wind_level_lo"], brief["wind_level_hi"]
    gust = brief["gust_level"]

    if lo is None and hi is None:
        force = "风力不明"
    elif lo == hi or hi is None:
        force = "%d级" % (lo if lo is not None else hi)
    else:
        force = "%d~%d级" % (min(lo, hi), max(lo, hi))

    text = "%s%s" % (direction, force)
    if gust is not None and hi is not None and gust > hi:
        text += "，阵风%d级" % gust
    return text


# ----------------------------------------------------------------------------
# 渲染
# ----------------------------------------------------------------------------

def _fmt_temp(v):
    return "—" if v is None else "%d" % round(v)


def render_push(cfg, brief):
    """生成 Bark 标题与正文，严格按约定的简报格式。"""
    title = "%s 今日天气简报" % brief["emoji"]

    # 【多云转晴  下雨概率2%】
    head = brief["phrase"]
    # 雪天说「降雪概率」更准确
    if brief["category"] == "snow":
        prob_label = "降雪概率"
    elif brief["category"] in ("rain", "fog"):
        prob_label = "降水概率"
    else:
        prob_label = "下雨概率"
    line_head = "【%s  %s%d%%】" % (head, prob_label, brief["prob"])

    lines = [line_head]

    # 1. 气温范围
    lines.append("1.气温范围：%s℃ ~ %s℃" % (_fmt_temp(brief["t_min"]), _fmt_temp(brief["t_max"])))

    # 2. 风力风向
    lines.append("2.风力风向：%s" % wind_phrase(brief))

    # 3. 空气质量
    if brief["aqi"] is not None:
        lines.append("3.空气质量：%d（%s）" % (brief["aqi"], brief["aqi_level"]))
    else:
        lines.append("3.空气质量：暂无数据")

    # 4. 穿衣建议（分项列出，便于直接照着穿）
    scene, top, bottom, shoes, socks = clothing_advice(brief)
    lines.append("4.穿衣建议：")
    lines.append("①季节：%s" % scene)
    lines.append("②上衣建议：%s" % top)
    lines.append("③下衣建议：%s" % bottom)
    lines.append("④鞋子建议：%s" % shoes)
    lines.append("⑤袜子建议：%s" % socks)

    # 5. 日期
    lines.append("5.日期：%s" % brief["date_label"])

    # 模式分歧提示只在真有分歧时附加，避免噪音
    if brief["divergence"] and brief["cross_text"]:
        lines.append("⚠️ 模式分歧：ECMWF 报「%s」" % brief["cross_text"])

    return title, "\n".join(lines)


# ----------------------------------------------------------------------------
# Bark 推送
# ----------------------------------------------------------------------------

def _http_post_json(url, payload, timeout=20):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": "gugong-weather-brief/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Bark 用 4xx 表达业务错误（如 key 无效），必须解析响应体才能知道原因
        body = exc.read().decode("utf-8", "ignore")
        try:
            return json.loads(body)
        except ValueError:
            raise RuntimeError("HTTP %s：%s" % (exc.code, body[:200]))


def send_bark(cfg, title, body, dry_run=False):
    """推送一条通知。优先 API v2（POST /push），失败回退 v1（GET /key/title/body）。"""
    server = cfg["bark_server"].rstrip("/")
    retries = cfg["bark_retries"]

    # dry-run 不需要密钥，便于无凭证时验证整条数据链路
    if dry_run:
        snow_alert.log("[dry-run] 跳过 Bark 推送：%s | %s"
                       % (title, body.replace("\n", " / ")))
        return True

    key = cfg.get("bark_key")
    if not key:
        raise RuntimeError("BARK_KEY 未配置，无法推送")

    payload = {
        "device_key": key,
        "title": title,
        "body": body,
        "group": cfg["bark_group"],
        "level": cfg["bark_level"],
        "isArchive": 1,
    }

    last_err = None
    for attempt in range(retries):
        # 主通道：API v2
        try:
            resp = _http_post_json(server + "/push", payload, timeout=cfg["bark_timeout"])
            if resp.get("code") == 200:
                snow_alert.log("Bark 推送成功（v2）")
                return True
            last_err = RuntimeError("Bark v2 返回 code=%s msg=%s"
                                    % (resp.get("code"), resp.get("message")))
            snow_alert.log(str(last_err))
        except Exception as exc:                      # noqa: BLE001
            last_err = exc
            snow_alert.log("Bark v2 推送异常（第 %d/%d 次）：%s" % (attempt + 1, retries, exc))

        # 回退：API v1（GET，参数走 query，避免部分节点不支持 /push）
        try:
            q = urllib.parse.urlencode({"group": cfg["bark_group"], "level": cfg["bark_level"]})
            v1 = "%s/%s/%s/%s?%s" % (server, urllib.parse.quote(key, safe=""),
                                     urllib.parse.quote(title, safe=""),
                                     urllib.parse.quote(body, safe=""), q)
            req = urllib.request.Request(v1, headers={"User-Agent": "gugong-weather-brief/1.0"})
            with urllib.request.urlopen(req, timeout=cfg["bark_timeout"]) as resp:
                r = json.loads(resp.read().decode("utf-8"))
            if r.get("code") == 200:
                snow_alert.log("Bark 推送成功（v1 回退）")
                return True
            last_err = RuntimeError("Bark v1 返回 code=%s msg=%s"
                                    % (r.get("code"), r.get("message")))
        except Exception as exc:                      # noqa: BLE001
            last_err = exc
            snow_alert.log("Bark v1 推送异常（第 %d/%d 次）：%s" % (attempt + 1, retries, exc))

        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))

    raise RuntimeError("Bark 推送失败，已重试 %d 次：%s" % (retries, last_err))


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------

def run_daily_brief(cfg=None, dry_run=False, force=False):
    """
    force=True 时忽略「晴天不推送」的过滤，用于通道自检。
    """
    cfg = cfg or load_brief_config()
    if not cfg["enable"]:
        snow_alert.log("天气播报已关闭（BRIEF_ENABLE=false），跳过")
        return {"status": "disabled"}

    today = fetch_today(cfg)
    air = fetch_air_quality(cfg)
    cross = fetch_cross_check(cfg)
    brief = assess(cfg, today, cross, air)

    snow_alert.log("当天天气：%s（%s）降水概率 %d%% 气温 %s~%s℃ 体感 %s~%s℃ AQI %s"
                   % (brief["phrase"], brief["code"], brief["prob"],
                      brief["t_min"], brief["t_max"],
                      brief["feel_min"], brief["feel_max"], brief["aqi"]))

    if not brief["should_push"] and not force:
        snow_alert.log("跳过推送 —— %s" % brief["reason"])
        return {"status": "sunny_skip", "text": brief["text"],
                "prob": brief["prob"], "reason": brief["reason"]}

    title, body = render_push(cfg, brief)
    send_bark(cfg, title, body, dry_run=dry_run)

    return {
        "status": "pushed" if not dry_run else "dry_run",
        "title": title,
        "body": body,
        "reason": brief["reason"],
        "category": brief["category"],
        "prob": brief["prob"],
        "temp": [brief["t_min"], brief["t_max"]],
        "feel": [brief["feel_min"], brief["feel_max"]],
        "aqi": brief["aqi"],
        "wind": wind_phrase(brief),
        "divergence": brief["divergence"],
        "forced": force,
    }
