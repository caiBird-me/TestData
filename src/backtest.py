# -*- coding: utf-8 -*-
"""历史事件回测：验证"打板次日溢价"策略期望值

用法（本地/云端均可）:
  py src/main.py backtest            # 2019年至今
  py src/main.py backtest 2023 2025  # 指定年份区间

数据源（2026-09 实测选型，按优先级探测）：
  主源 新浪 getKLineData：
  - 无登录、无WAF、单请求返回全历史日K（datalen=3000实测覆盖到2014年）
  - 价格准确性实测：与新浪/官方涨跌幅338个交易日交叉比对100%一致
  - 不覆盖北交所（腾讯/新浪一样）
  备源 腾讯 ifzq 原始日K：
  - 接口快，但持续大量请求会触发 waf.tencent.com 反爬封禁
    （实测约2400请求/10分钟即被封）——只能作为小规模降级路径
  已弃用 baostock：官方涨跌幅口径虽准（除权日无失真），但实测其客户端
  recv 无超时保护、服务端会对批量下载断流限流——查询会永久挂死且
  socket timeout 无法打破，5400只规模下等于12小时空转，可靠性不可接受
  已弃用 东方财富 push2his：对 GitHub Actions 的 Azure IP 段硬封锁
  （实测 0/20 请求成功），本地网络同样不通。

架构（流式，内存占用与股票总数无关）：
  每只股票：拉K线 → 检测涨停事件 → 立即模拟 → 只保留小结果对象 → 丢弃K线
  而非旧版"全市场K线驻留内存"（5900只×1800根×dict ≈ 数GB，会OOM/拖垮gc）

已知偏差（必须在报告里披露，读结果时心里有数）：
  1. 幸存者偏差：universe 是当前存续股票，退市的（多为崩盘股）不在样本
  2. 无ST过滤：接口不给历史名称，ST股混在样本里（实盘会剔除）
  3. 无概念主线过滤：回测的是"全部涨停股"而非"主线内涨停股"
     ——实盘有主线约束，样本比回测更精，回测结果是期望的下界参考
  4. 一字板买不进：gap>7%过滤掉了大部分，但D+1开盘介于+2%~+7%的快速秒板
     实际可能排队不成交（回测按能成交计，略偏乐观）
  5. 北交所剔除：无任何数据源覆盖北证K线（影响约4%股票池）
  6. 除权日涨停漏检：原始价口径（新浪/腾讯），现金分红使除权日涨幅被低估，
     少数"除权日涨停"检测不到——用启发式近似量化并在报告披露（见
     is_probably_exdiv_miss）
"""
import gc
import json
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from datasource import fetch_universe, now_cn

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
KLINE_CACHE_DIR = DATA_DIR / "backtest" / "klines"

# 涨停幅度判定阈值（按代码前缀分档，-0.15%容差吸收四舍五入）
_LIMIT_PCT = {"30": 19.85, "68": 19.85, "8": 29.7, "4": 29.7, "92": 29.7}
_DEFAULT_LIMIT = 9.85

SINA_KLINE_URL = ("https://money.finance.sina.com.cn/quotes_service/api/"
                 "json_v2.php/CN_MarketData.getKLineData")
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def limit_threshold(code):
    """按代码前缀取涨停判定阈值（20cm/30cm板块）"""
    return _LIMIT_PCT.get(code[:2]) or _LIMIT_PCT.get(code[:1]) or _DEFAULT_LIMIT


def is_bj_code(code):
    """北交所代码（4/8/9开头）——无数据源覆盖北证K线，回测剔除"""
    return code[0] in ("4", "8", "9")


# ---------- 源实现（均为原始价口径，K线格式统一） ----------

_thread_local = threading.local()


def _session():
    import requests
    if not hasattr(_thread_local, "s"):
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        _thread_local.s = s
    return _thread_local.s


def fetch_stock_bars_sina(code, start_date, end_date):
    """新浪原始日K：单请求全历史。返回统一K线格式或 None。

    datalen 按 (今年-起始年)×年交易日数+余量估算，上限3000（实测可返回
    2014年起的完整历史）。
    """
    datalen = min(int((int(end_date[:4]) - int(start_date[:4]) + 2) * 245), 3000)
    sym = ("sh" if code[0] == "6" else "sz") + code
    try:
        r = _session().get(
            SINA_KLINE_URL,
            params={"symbol": sym, "scale": "240", "ma": "no",
                    "datalen": str(datalen)},
            timeout=(10, 30))
        rows = r.json() or []
        if not rows:
            return None
        raw = [[d["day"][:10], float(d["open"]), float(d["close"]),
                float(d["high"]), float(d["low"])] for d in rows]
        return bars_with_pct(raw)
    except Exception:
        return None


def _tencent_kline_segment(sym, end, count=640):
    """腾讯单段日K（原始价）。返回 [date, open, close, high, low] 列表，失败 None。"""
    try:
        r = _session().get(
            TENCENT_KLINE_URL,
            params={"param": f"{sym},day,,{end},{count},"},
            timeout=(10, 30),
        )
        d = (r.json().get("data") or {}).get(sym) or {}
        rows = d.get("day") or []
        return [[p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4])]
                for p in rows]
    except Exception:
        return None


def fetch_stock_bars_tencent(code, start_date, end_date, max_bars=640):
    """腾讯原始日K（分页回溯+去重）。备源路径，低并发防WAF封禁。"""
    sym = ("sh" if code[0] == "6" else "sz") + code
    by_date = {}
    end = end_date
    for _ in range(10):
        rows = None
        for attempt in range(2):
            rows = _tencent_kline_segment(sym, end)
            if rows is not None:
                break
            time.sleep(1 + attempt)
        if not rows:
            break
        for row in rows:
            by_date[row[0]] = row
        if len(rows) < max_bars or rows[0][0] <= start_date:
            break
        end = rows[0][0]
    if not by_date:
        return None
    return bars_with_pct([by_date[d] for d in sorted(by_date)])


def probe_source(fetch_fn):
    """源健康探测：用3只存在的大盘股试拉，全部成功才算可用。"""
    for c in ("600000", "000001", "300750"):
        bars = fetch_fn(c, f"{now_cn().year}-01-01",
                        now_cn().strftime("%Y-%m-%d"))
        if not bars:
            return False
    return True


# ---------- 事件检测与模拟（纯函数） ----------

def bars_with_pct(raw_bars):
    """原始K线 [date,o,c,h,l] → 附加相邻日涨跌幅（首日无前收为0）。

    注意是原始价口径：除权日涨幅被低估（见模块docstring偏差6）。
    """
    out = []
    prev_close = None
    for d, o, c, h, low in raw_bars:
        pct = round((c / prev_close - 1) * 100, 4) if prev_close else 0.0
        out.append({"date": d, "open": o, "close": c, "high": h, "low": low,
                    "pct": pct})
        prev_close = c
    return out


def _is_limit_up(bar, code):
    """单日K涨停判定：涨幅达涨停幅度(-0.15%容差) 且 收盘=最高（收在板上）"""
    return bar["pct"] >= limit_threshold(code) and bar["close"] >= bar["high"] - 1e-9


def is_probably_exdiv_miss(bar, code):
    """疑似除权日涨停漏检（近似口径，用于报告偏差量化）：
    收盘=最高（封板形态）但原始价涨幅落在 [涨停-3%, 涨停-0.15%) 区间。
    实际是"差一点封板"的也会被算进来，因此是上界估计。
    """
    thr = limit_threshold(code)
    return (bar["close"] >= bar["high"] - 1e-9
            and thr - 3.0 <= bar["pct"] < thr - 0.15)


_COSTS = {"commission_rate": 0.00025, "commission_min": 5.0, "stamp_duty": 0.0005}


def _settle(date, code, buy_price, sell_price, shares, days, reason):
    amount = buy_price * shares
    buy_cost = max(amount * _COSTS["commission_rate"], _COSTS["commission_min"])
    sell_amount = sell_price * shares
    sell_cost = max(sell_amount * _COSTS["commission_rate"], _COSTS["commission_min"]) \
        + sell_amount * _COSTS["stamp_duty"]
    pnl = sell_amount - amount - buy_cost - sell_cost
    return {
        "date": date, "code": code, "shares": shares,
        "buy_price": buy_price, "sell_price": sell_price,
        "pnl": round(pnl, 2), "pnl_pct": round(pnl / amount * 100, 2),
        "days": days, "reason": reason,
    }


def simulate_event(code, bars, i, slippage=0.003, capital=1500.0,
                   max_gap=0.07, min_gap=-0.02, stop_pct=-0.05):
    """模拟单个事件：D日(bars[i])涨停 → D+1买入 → D+2卖出（含T+1/止损/续持规则）。

    返回 None（事件无效：数据不足/gap过滤/一手买不起）或 _settle 结果。
    成本用模块级 _COSTS（run_backtest 启动时按 config 校准一次）。
    """
    d0, d1 = bars[i], bars[i + 1]
    gap = d1["open"] / d0["close"] - 1
    if gap > max_gap or gap < min_gap:
        return None  # 高开>7%不追（含一字板买不进）/ 低开<-2%剔除

    buy_price = round(d1["open"] * (1 + slippage), 2)
    shares = int(capital / buy_price // 100) * 100
    if shares <= 0:
        return None  # 一手都买不起（>15元）

    # T+1：买入日D+1不可卖，最早D+2。最多持有3个交易日，收盘涨停则续持
    j = i + 2
    days_held = 0
    last_b = None
    while j < len(bars) and days_held < 3:
        b = bars[j]
        last_b = b
        days_held += 1
        stop_price = round(buy_price * (1 + stop_pct), 2)
        if b["low"] <= stop_price:
            # 跳空低开时真实成交价是开盘价而非止损价（短线票隔夜跳空常见），
            # 一律按止损价结算会在最差的交易上系统性高估收益
            sell_price = min(stop_price, b["open"])
            return _settle(d1["date"][:10], code, buy_price, sell_price, shares,
                           days_held, "止损")
        if days_held < 3 and _is_limit_up(b, code):
            j += 1  # 收盘涨停，续持一天
            continue
        return _settle(d1["date"][:10], code, buy_price, b["close"], shares,
                       days_held, "收盘卖出")
    # 数据尽头（续持后停牌/退市）：按最后处理的K线收盘价结算
    b = last_b or bars[-1]
    return _settle(d1["date"][:10], code, buy_price, b["close"], shares,
                   max(days_held, 1), "数据尽头结算")


def scan_stock_events(code, bars, start_year, end_year):
    """单只股票的事件扫描：检测涨停事件并立即模拟（流式核心——bars 用完即弃）。

    返回 (事件结果列表, 疑似除权漏检数, 检测到的涨停事件总数)。
    事件结果即 simulate_event 的输出（含 streak 字段），不再依赖原始K线
    ——调用方只需收集小结果对象。
    """
    results = []
    n_detected = 0
    n_exdiv_miss = 0
    lu_flags = [_is_limit_up(b, code) for b in bars]
    for i, b in enumerate(bars):
        if is_probably_exdiv_miss(b, code):
            n_exdiv_miss += 1
        if not lu_flags[i]:
            continue
        year = int(b["date"][:4])
        if not (start_year <= year <= end_year):
            continue
        n_detected += 1
        if i + 1 >= len(bars):
            continue  # 末根K线涨停：无D+1买入价，无法模拟（事件仍计入池）
        streak = 1
        j = i - 1
        while j >= 0 and lu_flags[j]:
            streak += 1
            j -= 1
        r = simulate_event(code, bars, i)
        if r:
            r["streak"] = streak
            results.append(r)
    return results, n_exdiv_miss, n_detected


class _CircuitBreaker:
    """失败率熔断：滚动窗口内失败率超阈值即跳闸，杜绝"限流下磨十小时"。

    12h空转的教训：源被封时退避重试只会把失败重试堆积成小时级空转——
    熔断让程序在可解释的位置快速失败。线程安全。
    """

    def __init__(self, window=200, threshold=0.5):
        self.window = window
        self.threshold = threshold
        self.recent = deque(maxlen=window)
        self.lock = threading.Lock()
        self.tripped_reason = None

    def record(self, ok):
        with self.lock:
            self.recent.append(ok)
            if len(self.recent) >= 50:
                fail_rate = 1 - sum(self.recent) / len(self.recent)
                if fail_rate > self.threshold:
                    self.tripped_reason = (
                        f"最近{len(self.recent)}次拉取失败率{fail_rate*100:.0f}%"
                        f"（超过{self.threshold*100:.0f}%熔断线）")

    def tripped(self):
        return self.tripped_reason


# ---------- K线磁盘缓存（当日有效，断点续传） ----------

def _save_kline_cache(code, bars):
    try:
        KLINE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (KLINE_CACHE_DIR / f"{code}.json").write_text(json.dumps(
            {"fetched": now_cn().strftime("%Y%m%d"), "bars": bars}), encoding="utf-8")
    except OSError:
        pass  # 缓存写失败不影响回测本身


def _read_kline_cache(code):
    """读取单只股票的当日缓存，失败返回 None"""
    try:
        js = json.loads((KLINE_CACHE_DIR / f"{code}.json").read_text(encoding="utf-8"))
        if js.get("fetched") == now_cn().strftime("%Y%m%d") and js.get("bars"):
            return js["bars"]
    except (ValueError, OSError):
        pass
    return None


def _cached_codes_today():
    """当日有效缓存的code集合（只建索引不读内容，内存安全）。

    非当日/损坏的缓存文件直接删除。
    """
    if not KLINE_CACHE_DIR.exists():
        return set()
    today = now_cn().strftime("%Y%m%d")
    codes = set()
    removed = 0
    for f in KLINE_CACHE_DIR.glob("*.json"):
        code = f.stem
        if not code.isdigit() or len(code) != 6:
            continue
        try:
            js = json.loads(f.read_text(encoding="utf-8"))
            if js.get("fetched") == today and js.get("bars"):
                codes.add(code)
            else:
                f.unlink()
                removed += 1
        except (ValueError, OSError):
            f.unlink()
            removed += 1
    if codes:
        print(f"[backtest] 当日K线缓存命中 {len(codes)} 只"
              f"（清理过期{removed}个）", flush=True)
    return codes


def run_backtest(cfg, start_year=None, end_year=None):
    """主入口：选源探测 → 拉数据 → 流式扫事件 → 模拟 → 统计报告（markdown）"""
    from strategy import STREAK_SCORE

    st = cfg.get("backtest", {})
    workers = st.get("workers", 6)
    if cfg.get("trading_costs"):
        _COSTS.update({k: cfg["trading_costs"][k]
                       for k in _COSTS if k in cfg["trading_costs"]})
    end_year = end_year or now_cn().year
    start_year = start_year or 2019
    # 事件回看需要 start_year 前的K线算连板数（连板最长约2周，留1年余量足够）
    fetch_start = f"{start_year - 1}-12-01"
    fetch_end = now_cn().strftime("%Y-%m-%d")

    print("[backtest] 拉取全市场代码清单 ...", flush=True)
    universe = fetch_universe()
    if not universe:
        raise RuntimeError("代码清单拉取失败（东财clist不可达），中止回测")
    codes_all = [c for c, _ in universe]
    bj_count = sum(1 for c in codes_all if is_bj_code(c))
    codes = [c for c in codes_all if not is_bj_code(c)]
    print(f"[backtest] {len(codes_all)}只股票（剔除北交所{bj_count}只——"
          f"无数据源覆盖北证K线，见报告偏差说明）", flush=True)

    # ---------- 选源探测：新浪主 → 腾讯备 → 全灭即报错（快速失败） ----------
    if probe_source(fetch_stock_bars_sina):
        fetch_one, source = fetch_stock_bars_sina, "sina"
        print("[backtest] 数据源：新浪日K（原始价口径）", flush=True)
    elif probe_source(fetch_stock_bars_tencent):
        fetch_one, source = fetch_stock_bars_tencent, "tencent"
        print("[backtest] 数据源：腾讯日K（备源，原始价口径——"
              "大规模拉取可能触发反爬）", flush=True)
    else:
        raise RuntimeError("新浪与腾讯K线源均不可用，中止回测（快速失败，"
                           "不空转重试）")

    cached_codes = _cached_codes_today()
    todo = [c for c in codes if c not in cached_codes]
    breaker = _CircuitBreaker()
    results = []
    n_detected = 0
    n_exdiv_miss = 0
    failed_codes = []
    done = 0
    t0 = time.monotonic()

    def work(code):
        """线程worker：缓存优先 → 拉K线 → 缓存落盘。"""
        bars = None
        if code in cached_codes:
            bars = _read_kline_cache(code)
        if bars is None:
            bars = fetch_one(code, fetch_start, fetch_end)
            if bars is not None:
                _save_kline_cache(code, bars)
        return code, bars

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(work, c): c for c in todo}
        for fut in as_completed(futs):
            code, bars = fut.result()
            done += 1
            if bars is None:
                breaker.record(False)
                failed_codes.append(code)
            else:
                breaker.record(True)
                r_list, exdiv, detected = scan_stock_events(
                    code, bars, start_year, end_year)
                n_detected += detected
                n_exdiv_miss += exdiv
                results.extend(r_list)
                del bars
            trip = breaker.tripped()
            if trip:
                raise RuntimeError(f"数据源熔断：{trip}，中止回测")
            if done % 200 == 0 or done == len(todo):
                elapsed = time.monotonic() - t0
                rate = done / elapsed if elapsed else 0
                eta = int((len(todo) - done) / rate) if rate else 0
                print(f"[backtest] K线 {done}/{len(todo)}"
                      f" {rate:.1f}只/秒，预计还需{eta//60}分{eta%60}秒", flush=True)
            if done % 1000 == 0:
                gc.collect()

    # 缓存命中的部分也要扫事件（当日重跑场景）
    for code in codes:
        if code in cached_codes:
            bars = _read_kline_cache(code)
            if bars:
                r_list, exdiv, detected = scan_stock_events(
                    code, bars, start_year, end_year)
                n_detected += detected
                n_exdiv_miss += exdiv
                results.extend(r_list)
            else:
                failed_codes.append(code)

    print(f"[backtest] 完成：检测{n_detected}个涨停事件（另有{n_exdiv_miss}个"
          f"疑似除权漏检），成交{len(results)}笔，失败{len(failed_codes)}只", flush=True)

    # 每日选股：按连板打分曲线取前 max_events_per_day 个
    # （历史无封板质量数据，用连板数近似实盘选股排序）
    max_per_day = st.get("max_events_per_day", 2)
    by_day = {}
    for r in results:
        by_day.setdefault(r["date"], []).append(r)
    selected = []
    for date in sorted(by_day):
        day = sorted(by_day[date],
                     key=lambda r: STREAK_SCORE.get(r["streak"],
                                                    8 if r["streak"] >= 5 else 15),
                     reverse=True)
        selected.extend(day[:max_per_day])

    return _report(selected, start_year, end_year, n_detected, n_exdiv_miss,
                   len(universe), failed_codes, bj_count), selected


def _report(results, start_year, end_year, n_events, n_exdiv_miss, n_universe,
            failed_codes, bj_count):
    """生成回测统计报告"""
    lines = [f"## 🧪 打板事件回测 {start_year}-{end_year}", ""]

    if not results:
        lines.append("无有效事件——请确认K线数据拉取成功")
        return "\n".join(lines)

    def stats_of(rs):
        if not rs:
            return None
        wins = [r for r in rs if r["pnl"] > 0]
        total_pnl = sum(r["pnl"] for r in rs)
        return {
            "n": len(rs), "win_rate": round(len(wins) / len(rs) * 100, 1),
            "avg_pct": round(sum(r["pnl_pct"] for r in rs) / len(rs), 2),
            "total_pnl": round(total_pnl, 0),
            "worst": min(r["pnl_pct"] for r in rs),
            "best": max(r["pnl_pct"] for r in rs),
        }

    overall = stats_of(results)
    lines.append(f"**总体**：{overall['n']}笔成交事件 | 胜率 **{overall['win_rate']}%** | "
                 f"单笔期望 **{overall['avg_pct']:+.2f}%** | "
                 f"单笔最差 {overall['worst']:+.1f}% / 最好 {overall['best']:+.1f}%")
    lines.append("")

    # 分年统计
    lines.append("**分年表现**（1500元/笔仓位，含佣金印花税与0.3%滑点）")
    lines.append("| 年份 | 事件 | 胜率 | 单笔期望 | 累计盈亏(1500元仓) |")
    lines.append("|---|---|---|---|---|")
    for year in range(start_year, end_year + 1):
        rs = [r for r in results if r["date"][:4] == str(year)]
        s = stats_of(rs)
        if not s:
            continue
        lines.append(f"| {year} | {s['n']} | {s['win_rate']}% | {s['avg_pct']:+.2f}% "
                     f"| {s['total_pnl']:+.0f}元 |")
    lines.append("")

    # 分连板数
    lines.append("**按连板数分层**（打分曲线的实证检验：2-3板是否真的最优）")
    lines.append("| 连板 | 事件 | 胜率 | 单笔期望 |")
    lines.append("|---|---|---|---|")
    for lo, hi, label in [(1, 1, "首板"), (2, 3, "2-3板"), (4, 4, "4板"), (5, 99, "5板+")]:
        rs = [r for r in results if lo <= r["streak"] <= hi]
        s = stats_of(rs)
        if s:
            lines.append(f"| {label} | {s['n']} | {s['win_rate']}% | {s['avg_pct']:+.2f}% |")
    lines.append("")

    # 最大连亏
    max_lose, cur = 0, 0
    for r in sorted(results, key=lambda r: r["date"]):
        cur = cur + 1 if r["pnl"] <= 0 else 0
        max_lose = max(max_lose, cur)
    lines.append(f"**最大连亏**：{max_lose} 笔（连亏熔断设3笔，回测连亏超过3的"
                 f"每一段都是实盘会被熔断截断的时段）")
    lines.append("")

    lines.append("**⚠️ 口径与偏差（读数字前必读）**")
    lines.append(f"- 涨停事件池 {n_events} 个，成交 {overall['n']} 笔"
                 f"（gap过滤-2%~+7%、价格≤15元、每日最多2笔）")
    lines.append("- 幸存者偏差：仅含当前存续股票（退市股多为崩盘股，结果略偏乐观）")
    lines.append("- 无ST/主线概念过滤：回测全样本，实盘更精——期望值是下界参考")
    lines.append(f"- 除权日涨停漏检约 {n_exdiv_miss} 个（原始价口径上界估计，"
                 f"实际影响远小于此数）")
    lines.append(f"- 北交所{bj_count}只剔除：无免费数据源覆盖北证K线")
    fail_note = f"- K线拉取失败 {len(failed_codes)} 只（占比高时结果不可信，需重跑）"
    if len(failed_codes) > n_universe * 0.05:
        fail_note += " ⚠️ 失败率超5%，结果可信度低！"
    lines.append(fail_note)
    lines.append("_数据来源: 新浪/腾讯财经日K自建涨停日历 | 历史模拟不预示未来表现_")
    return "\n".join(lines)
