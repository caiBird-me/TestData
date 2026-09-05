# -*- coding: utf-8 -*-
"""东方财富行情数据层

接口均为公开接口，无需 token：
- 全市场快照(按涨幅排序翻页): push2.eastmoney.com/api/qt/clist
- 日K线: push2his.eastmoney.com/api/qt/stock/kline
- 板块行情: 同 clist 接口换 fs 参数
- 交易日历: 用沪深300日K的最后交易日推断
"""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# 统一北京时间（云端 Actions 服务器为 UTC）
CN_TZ = timezone(timedelta(hours=8))


def now_cn():
    return datetime.now(CN_TZ)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}

# clist 字段: f2最新价 f3涨跌幅 f8换手率 f10量比 f12代码 f14名称 f15最高 f16最低 f17今开
# f18昨收 f20总市值 f21流通市值 f22涨速 f62主力净流入 f100所属板块 f128领涨股
CLIST_FIELDS = "f2,f3,f8,f10,f12,f14,f15,f16,f17,f18,f20,f21,f62,f100,f128"

# 全部A股（沪深京）
FS_ASHARE = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
# 概念/行业板块
FS_BOARD = "m:90+t:2,m:90+t:1"

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
HISTORY_DIR = DATA_DIR / "history"

# 行情域名容错：push2 有时直接断连，失败时切换 push2delay（延迟15秒，对分析无影响）
# push2his 同样偶发断连，切换 push2his 前多做几次重试


def _get(url, params, retries=4, timeout=15):
    """带重试的 GET，返回 JSON 的 data 部分，失败返回 None"""
    # 域名容错：push2/push2his 在部分网络环境下会被直接断连，
    # push2delay（延迟15秒，对收盘后分析无影响）在各环境均稳定
    urls = [url]
    for host in ("push2.eastmoney.com", "push2his.eastmoney.com"):
        if host in url:
            urls.append(url.replace(host, "push2delay.eastmoney.com"))
    for i in range(retries):
        for u in urls:
            try:
                r = SESSION.get(u, params=params, timeout=timeout)
                r.raise_for_status()
                js = r.json()
                if js.get("rc") == 0 and js.get("data") is not None:
                    return js["data"]
                return js.get("data")  # rc!=0 可能是空数据（如非交易日）
            except (requests.RequestException, ValueError):
                continue
        time.sleep(2 * (i + 1))
    print(f"[datasource] 请求失败: {url}")
    return None


def fetch_top_gainers(pages=4, page_size=100, keep_raw=False):
    """拉取全市场按涨幅降序的前 N 页股票快照（含实时行情字段）。

    keep_raw=True 时额外返回原始响应（存档用，含全部原始字段——
    字段口径变化后历史数据仍可重算）。
    """
    stocks, raw_pages = [], []
    for pn in range(1, pages + 1):
        data = _get(
            "https://push2.eastmoney.com/api/qt/clist/get",
            {
                "pn": pn, "pz": page_size, "po": 1, "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2, "fid": "f3", "fs": FS_ASHARE,
                "fields": CLIST_FIELDS,
            },
        )
        if not data or not data.get("diff"):
            break
        stocks.extend(data["diff"])
        if keep_raw:
            raw_pages.append(data)
    normalized = [_normalize_stock(s) for s in stocks if _normalize_stock(s)]
    # 结构漂移检测：关键字段大面积缺失说明东财改了字段名——
    # 静默置0会让候选池悄悄消失，必须显式告警
    _check_field_drift(normalized, stocks)
    return (normalized, raw_pages) if keep_raw else normalized


def _check_field_drift(normalized, raw_stocks):
    """检测关键字段是否大面积缺失（数据源结构漂移的信号）"""
    if not normalized:
        return
    n = len(normalized)
    zero_turnover = sum(1 for s in normalized if s["turnover"] == 0.0)
    zero_inflow = sum(1 for s in normalized if s["main_inflow"] == 0.0)
    # 涨幅榜前400只的换手率不可能大面积为0；主力净流入恰好为0也罕见
    if zero_turnover > n * 0.5:
        print(f"[datasource] ⚠️ 数据漂移警告: {zero_turnover}/{n} 只换手率为0，"
              f"东财可能已修改f8字段口径，请人工核对！")
    if zero_inflow > n * 0.9:
        print(f"[datasource] ⚠️ 数据漂移警告: {zero_inflow}/{n} 只主力净流入为0，"
              f"东财可能已修改f62字段口径，请人工核对！")


def fetch_snapshot_by_codes(codes):
    """按代码列表拉取实时快照（早间竞价确认用）"""
    secs = []
    for c in codes:
        secid = code_to_secid(c)
        if secid:
            secs.append(secid)
    result = {}
    # 分批，每批50只
    for i in range(0, len(secs), 50):
        batch = ",".join(secs[i:i + 50])
        data = _get(
            "https://push2.eastmoney.com/api/qt/ulist.np/get",
            {
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2, "fields": CLIST_FIELDS,
                "secids": batch,
            },
        )
        if data and data.get("diff"):
            for s in data["diff"]:
                ns = _normalize_stock(s)
                if ns:
                    result[ns["code"]] = ns
    return result


def code_to_secid(code):
    """600xxx -> 1.600xxx (沪)  00xxxx/30xxxx -> 0.00xxxx (深)  8/4开头 -> 0.xxx (北)"""
    code = str(code).zfill(6)
    if code[0] == "6":
        return f"1.{code}"
    if code[0] in ("0", "3"):
        return f"0.{code}"
    if code[0] in ("8", "4", "9"):
        return f"0.{code}"
    return None


def _normalize_stock(s):
    """统一字段名，过滤停牌(价格为-)的股票"""
    try:
        if s.get("f2") in ("-", None) or s.get("f3") in ("-", None):
            return None
        return {
            "code": str(s["f12"]).zfill(6),
            "name": s.get("f14", ""),
            "price": _f(s.get("f2")),        # 最新/现价
            "pct": _f(s.get("f3")),          # 涨跌幅%
            "high": _f(s.get("f15")),
            "low": _f(s.get("f16")),
            "open": _f(s.get("f17")),
            "pre_close": _f(s.get("f18")),
            "turnover": _f(s.get("f8")),     # 换手率%
            "vol_ratio": _f(s.get("f10")),  # 量比
            "mktcap": _f(s.get("f21")),     # 流通市值(元)
            "main_inflow": _f(s.get("f62")),  # 主力净流入(元)
            "board": s.get("f100") or "",   # 所属板块
        }
    except (KeyError, TypeError):
        return None


def _f(v):
    if v in ("-", None, ""):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_kline(code, days=120, klt=101):
    """K线（前复权）。klt=101 日K / klt=1 分钟K。

    push2his 日K在部分网络环境会被断连；分钟K在 push2delay 上稳定。
    日K拉取失败时自动降级：拉全天分钟K聚合（days*241根，days<=30时可用）。
    """
    secid = code_to_secid(code)
    if not secid:
        return []
    data = _get(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        {
            "secid": secid,
            "fields1": "f1,f2,f3",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
            "klt": klt, "fqt": 1, "end": "20500101", "lmt": days,
        },
    )
    klines = []
    if data and data.get("klines"):
        for line in data["klines"]:
            p = line.split(",")
            # 日期,开,收,高,低,成交量,成交额
            klines.append({
                "date": p[0], "open": float(p[1]), "close": float(p[2]),
                "high": float(p[3]), "low": float(p[4]),
                "volume": float(p[5]), "amount": float(p[6]),
            })
        return klines

    # 日K失败 → 分钟K聚合降级（仅 klt=101 时）
    if klt == 101 and days <= 30:
        m = fetch_kline(code, days * 241, klt=1)
        if m:
            return _aggregate_daily(m)
    return []


def _aggregate_daily(minute_bars):
    """分钟K聚合成日K"""
    days = {}
    for bar in minute_bars:
        days.setdefault(bar["date"][:10], []).append(bar)
    return [
        {
            "date": d,
            "open": bars[0]["open"], "close": bars[-1]["close"],
            "high": max(b["high"] for b in bars), "low": min(b["low"] for b in bars),
            "volume": sum(b["volume"] for b in bars),
            "amount": sum(b["amount"] for b in bars),
        }
        for d, bars in sorted(days.items())
    ]


def fetch_first_minute(code, date_str=None):
    """某日第一根分钟K（09:31，即开盘后第一分钟的实际成交区间）。

    成交可行性建模用：竞价价只是虚拟撮合价，09:31 分钟K的 open/high/low 才是
    开盘后真实可成交的价格区间。返回 {open,high,low,close} 或 None。
    """
    k = fetch_kline(code, days=10, klt=1)
    if not k:
        return None
    target = (date_str or now_cn().strftime("%Y-%m-%d"))
    first = None
    for bar in k:
        if bar["date"].startswith(target):
            if first is None:  # 该日第一根（09:31）
                first = bar
    return first


def is_today_trading_day():
    """判断今天是否交易日（morning 盘前用，日K此时还停留在昨天无法区分）。

    原理：上证指数行情时间戳 f86 在交易日竞价完成后（09:25）会更新为当日；
    非交易日返回的是上一交易日收盘时间。时间戳拉不到时保守视为交易日。
    """
    data = _get(
        "https://push2.eastmoney.com/api/qt/stock/get",
        {"secid": "1.000001", "fields": "f86",
         "ut": "bd1d9ddb04089700cf9c27f6f7426281", "invt": 2, "fltt": 2},
    )
    ts = int((data or {}).get("f86") or 0)
    if ts <= 0:
        return True
    quote_day = datetime.fromtimestamp(ts, CN_TZ).strftime("%Y%m%d")
    return quote_day == now_cn().strftime("%Y%m%d")


def get_kline_last_date(index_secid="1.000300"):
    """指数日K最后一根的日期（= 最近一个已收盘的交易日），morning 判断信号过期用"""
    data = _get(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        {"secid": index_secid, "fields1": "f1", "fields2": "f51,f53",
         "klt": 101, "fqt": 1, "end": "20500101", "lmt": 5},
    )
    if data and data.get("klines"):
        return data["klines"][-1].split(",")[0]
    return None


def fetch_concept_map(n_boards=40):
    """构建 股票代码→所属热门概念板块名列表 映射（题材聚类用，支持一票多属）。

    做法：拉今日涨幅前 n_boards 个概念板块（m:90 t:3），
    再逐板块拉成分股（按涨幅排序前100只，涨停股必在其中）。
    板块接口失败时返回空 dict，调用方回退到 f100 行业聚类。
    """
    boards = _get(
        "https://push2.eastmoney.com/api/qt/clist/get",
        {
            "pn": 1, "pz": n_boards, "po": 1, "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2, "invt": 2, "fid": "f3", "fs": "m:90+t:3",
            "fields": "f12,f14",
        },
    )
    if not boards or not boards.get("diff"):
        return {}

    concept_map = {}
    for b in boards["diff"]:
        bcode, bname = b.get("f12"), b.get("f14")
        if not bcode:
            continue
        members = _get(
            "https://push2.eastmoney.com/api/qt/clist/get",
            {
                "pn": 1, "pz": 100, "po": 1, "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2, "fid": "f3", "fs": f"b:{bcode}",
                "fields": "f12",
            },
        )
        if not members or not members.get("diff"):
            continue
        for m in members["diff"]:
            code = str(m.get("f12", "")).zfill(6)
            if code:
                concept_map.setdefault(code, []).append(bname)
    return concept_map


def calc_sentiment():
    """市场情绪：昨日涨停股今日的平均涨跌幅（打板体系的赚/亏钱效应核心指标）。

    返回 (平均涨幅%, 板块数)。昨日涨停名单从归档读取；无归档返回 (None, 0)。
    """
    prev = load_prev_limit_ups(1)
    if not prev:
        return None, 0
    last_date = max(prev.keys())
    codes = list(prev[last_date])
    if not codes:
        return None, 0
    snap = fetch_snapshot_by_codes(codes)
    pcts = [s["pct"] for s in snap.values() if s["pct"] is not None]
    if not pcts:
        return None, 0
    return round(sum(pcts) / len(pcts), 2), len(codes)


def fetch_boards():
    """行业+概念板块行情，返回按涨幅排序的板块列表"""
    data = _get(
        "https://push2.eastmoney.com/api/qt/clist/get",
        {
            "pn": 1, "pz": 100, "po": 1, "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2, "invt": 2, "fid": "f3", "fs": FS_BOARD,
            "fields": "f2,f3,f8,f12,f14,f104,f105,f128",
        },
    )
    boards = []
    if data and data.get("diff"):
        for b in data["diff"]:
            boards.append({
                "code": b.get("f12"), "name": b.get("f14", ""),
                "pct": _f(b.get("f3")),
                "up_count": b.get("f104", 0),    # 上涨家数
                "down_count": b.get("f105", 0),  # 下跌家数
                "leader": b.get("f128") or "",   # 领涨股
            })
    return boards


def get_last_trade_date():
    """用沪深300指数日K推断最近交易日，返回 YYYYMMDD 字符串"""
    data = _get(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        {
            "secid": "1.000300", "fields1": "f1", "fields2": "f51,f53",
            "klt": 101, "fqt": 1, "end": "20500101", "lmt": 5,
        },
    )
    if data and data.get("klines"):
        return data["klines"][-1].split(",")[0].replace("-", "")
    return now_cn().strftime("%Y%m%d")


# ---------- 每日归档（用于自算连板数） ----------

def save_daily_archive(stocks, raw_pages=None):
    """把当日涨停/强势股归档到 data/history/YYYYMMDD.json

    raw_pages: fetch_top_gainers(keep_raw=True) 的原始响应。
    原始数据原样存档（一天几十KB）——归一化口径变了之后，
    历史数据仍可基于原始归档重算，这是"口径改了还能重算"的唯一依据。
    """
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    date_str = now_cn().strftime("%Y%m%d")
    limit_up = [s for s in stocks if is_limit_up(s)]
    archive = {
        "date": date_str,
        "limit_up": [
            {"code": s["code"], "name": s["name"], "pct": s["pct"],
             "price": s["price"], "board": s["board"], "turnover": s["turnover"]}
            for s in limit_up
        ],
        "top": [
            {"code": s["code"], "name": s["name"], "pct": s["pct"],
             "board": s["board"], "turnover": s["turnover"], "vol_ratio": s["vol_ratio"],
             "main_inflow": s["main_inflow"], "price": s["price"],
             "high": s["high"], "pre_close": s["pre_close"]}
            for s in stocks[:100]
        ],
    }
    path = HISTORY_DIR / f"{date_str}.json"
    path.write_text(json.dumps(archive, ensure_ascii=False, indent=1), encoding="utf-8")
    # 原始数据归档（独立文件，永不参与读取逻辑，只为将来重算保留）
    if raw_pages:
        raw_path = HISTORY_DIR / f"{date_str}_raw.json"
        raw_path.write_text(json.dumps(raw_pages, ensure_ascii=False), encoding="utf-8")
    return limit_up


def load_prev_limit_ups(n_days=10):
    """读取今天之前最近 n 个交易日的涨停归档，返回 {date: {code}}

    n_days 语义 = 排除今天后的归档天数（今天的不算——晚间运行时当日涨停已在内存里）
    """
    result = {}
    if not HISTORY_DIR.exists():
        return result
    today = now_cn().strftime("%Y%m%d")
    for f in sorted(HISTORY_DIR.glob("*.json"), reverse=True):
        if f.stem.endswith("_raw"):
            continue  # 原始数据归档（列表结构），不参与连板计算
        if len(result) >= n_days:
            break
        try:
            js = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(js, dict) or js.get("date") == today:
                continue
            result[js["date"]] = {s["code"] for s in js.get("limit_up", [])}
        except (ValueError, KeyError):
            continue
    return result


def calc_streak_codes(today_limit_ups, prev_archives):
    """计算连板数: 今天涨停且昨日也涨停 -> 2板及以上"""
    dates = sorted(prev_archives.keys(), reverse=True)
    streak = {}
    for s in today_limit_ups:
        code = s["code"]
        n = 1
        # 沿日期往前数连续涨停
        for d in dates:
            if code in prev_archives[d]:
                n += 1
            else:
                break
        streak[code] = n
    return streak


def verify_streaks(today_limit_ups, streak_map):
    """用日K线校验连板数，防止归档缺失虚增（如某天Actions挂了没归档）。

    只校验归档算出 ≥2 板的票（通常几只，请求量小）。
    K线拉取失败时保留归档结果（宁可多看一眼，不能没数据）。
    """
    verified = dict(streak_map)
    for s in today_limit_ups:
        code = s["code"]
        if streak_map.get(code, 1) < 2:
            continue
        k = fetch_kline(code, 30)
        if not k or len(k) < 2:
            continue  # K线不可用，信任归档
        # 从昨天（倒数第二根）往前数连续涨停日，今天本身已确认涨停
        n = 1
        i = len(k) - 2
        while i >= 1:
            prev_close = k[i - 1]["close"]
            bar = k[i]
            if prev_close <= 0:
                break
            pct = (bar["close"] - prev_close) / prev_close * 100
            fake = {"code": code}
            if pct >= limit_up_pct(fake) - 0.2 and bar["close"] >= bar["high"] - 0.01:
                n += 1
                i -= 1
            else:
                break
        if n != streak_map[code]:
            print(f"[datasource] 连板校验修正 {s['name']}({code}): "
                  f"归档{streak_map[code]}板 -> K线{n}板")
        verified[code] = n
    return verified


def limit_up_pct(stock):
    """涨停幅度：主板10%，创业板/科创板20%，北交所30%（ST按5%简化不处理，通过名称过滤ST）"""
    code = stock["code"]
    if code[0] in ("8", "4", "9"):
        return 30.0
    if code[0] == "3" or code[:3] == "688":
        return 20.0
    return 10.0


def is_limit_up(stock):
    """涨幅达到涨停幅度且收盘价=最高价（收盘未打开）"""
    if stock["pct"] <= 0:
        return False
    return stock["pct"] >= limit_up_pct(stock) - 0.2 and stock["price"] >= stock["high"] - 0.01
