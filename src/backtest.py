# -*- coding: utf-8 -*-
"""历史事件回测：验证"打板次日溢价"策略期望值

用法（建议在云端 Actions 跑，本地网络 push2his 不通）:
  py src/main.py backtest            # 2019年至今
  py src/main.py backtest 2023 2025  # 指定年份区间

数据与口径：
  - 全市场日K扫描自建涨停日历（涨幅≥涨停幅度-0.15% 且 收盘=最高）
  - 涨停判定用前复权价：相邻日涨跌幅口径不受除权影响，单日内 close==high 判定一致

事件模型（与实盘策略同构）：
  D日涨停(streak, 收盘价≤15元) → D+1日：
    gap过滤(-2%~+7%) → 开盘价×(1+0.3%滑点)买入，1500元仓位上限
  → D+2日（T+1解锁）：
    盘中破-5%止损 → 按止损价卖出
    收盘涨停 → 续持至D+3收盘
    否则收盘卖出
  成本：佣金万2.5(最低5元)双向 + 印花税万5卖出

已知偏差（必须在报告里披露，读结果时心里有数）：
  1. 幸存者偏差：universe是当前存续股票，2019年以来退市的（多为崩盘股）不在样本
  2. 无ST过滤：日K接口不给历史名称，ST股混在样本里（实盘会剔除）
  3. 无概念主线过滤：历史概念归属不可得，回测的是"全部涨停股"而非"主线内涨停股"
     ——实盘有主线约束，样本比回测更精，回测结果是期望的下界参考
  4. 一字板买不进：gap>7%过滤掉了大部分，但D+1开盘介于+2%~+7%的快速秒板
     实际可能排队不成交（回测按能成交计，略偏乐观）
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from datasource import code_to_secid, fetch_universe, now_cn

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
KLINE_CACHE_DIR = DATA_DIR / "backtest" / "klines"

# 涨停幅度判定阈值（按代码前缀分档，-0.15%容差吸收四舍五入）
_LIMIT_PCT = {"30": 19.85, "68": 19.85, "8": 29.7, "4": 29.7, "92": 29.7}
_DEFAULT_LIMIT = 9.85


def limit_threshold(code):
    """按代码前缀取涨停判定阈值（20cm/30cm板块）"""
    return _LIMIT_PCT.get(code[:2]) or _LIMIT_PCT.get(code[:1]) or _DEFAULT_LIMIT


_thread_local = threading.local()


def _session():
    import requests
    if not hasattr(_thread_local, "s"):
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        _thread_local.s = s
    return _thread_local.s


def _fetch_stock_kline(code, lmt=2200):
    """单只股票全量日K（前复权，含官方涨跌幅f59）。
    2200根约覆盖9年，足够2019年起回测。

    失败退避：东财对海外IP（GitHub Actions）限流时请求会超时，
    无退避的立即重试只会火上浇油——sleep后再试。
    """
    secid = code_to_secid(code)
    if not secid:
        return None
    # 限流下瞬时重试全灭：指数退避 + 4次尝试（实测海外IP连接层被拒时
    # 第4次尝试+8秒等待后成功率明显回升）
    backoffs = (1, 3, 8)
    for attempt in range(4):
        try:
            r = _session().get(
                "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                params={
                    "secid": secid, "fields1": "f1",
                    "fields2": "f51,f52,f53,f54,f55,f56,f59",
                    "klt": 101, "fqt": 1, "end": "20500101", "lmt": lmt,
                },
                timeout=(10, 20),  # 连接10s/读取20s，连接被拒时快速失败
            )
            js = r.json()
            data = js.get("data")
            if data and data.get("klines"):
                bars = []
                for line in data["klines"]:
                    p = line.split(",")
                    bars.append({
                        "date": p[0], "open": float(p[1]), "close": float(p[2]),
                        "high": float(p[3]), "low": float(p[4]),
                        "volume": float(p[5]), "pct": float(p[6]),
                    })
                return bars
        except Exception:
            if attempt < 2:
                time.sleep(backoffs[attempt])
    return None


def fetch_all_klines(universe, workers=6, cache=True, on_progress=None):
    """全市场日K拉取（带本地缓存）。

    缓存 data/backtest/klines/{code}.json：{fetched: 拉取日, bars: [...]}，
    当天已拉取的跳过（断点续传：5400只×几十KB，中断后重跑只补失败的部分）。
    注意：缓存不进git（仓库会膨胀到几百MB），云端每次dispatch全量拉取约10-20分钟。

    限流自适应：单只拉取失败（超时/被拒）说明东财在限流——
    该worker暂停2秒再放下一个请求，避免雪崩式失败。
    """
    if cache:
        KLINE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today = now_cn().strftime("%Y%m%d")
    result, failed = {}, []
    codes = [c for c, _ in universe]
    done = 0
    t0 = time.monotonic()

    def work(code):
        if cache:
            cf = KLINE_CACHE_DIR / f"{code}.json"
            if cf.exists():
                try:
                    js = json.loads(cf.read_text(encoding="utf-8"))
                    if js.get("fetched") == today and js.get("bars"):
                        return code, js["bars"]
                except ValueError:
                    pass
        bars = _fetch_stock_kline(code)
        if bars is None:
            time.sleep(2)  # 失败≈被限流信号，让路
        elif cache:
            cf = KLINE_CACHE_DIR / f"{code}.json"
            cf.write_text(json.dumps({"fetched": today, "bars": bars}), encoding="utf-8")
        return code, bars

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(work, c): c for c in codes}
        for fut in as_completed(futs):
            code, bars = fut.result()
            done += 1
            if bars:
                result[code] = bars
            else:
                failed.append(code)
            if on_progress and (done % 200 == 0 or done == len(codes)):
                on_progress(done, len(codes), len(failed), time.monotonic() - t0)
    return result, failed


def _is_limit_up(bar, code):
    """单日K涨停判定：涨幅达涨停幅度(-0.15%容差) 且 收盘=最高（收在板上）"""
    return bar["pct"] >= limit_threshold(code) and bar["close"] >= bar["high"] - 1e-9


def build_events(klines, start_year, end_year):
    """从全市场日K构建涨停事件流。

    事件 = (code, D日index) 满足D日涨停。附带连板数（向前连续涨停天数）。
    返回 {date: [(code, streak, close)]}，按日分组——控制单日事件数用。
    """
    events_by_date = {}
    for code, bars in klines.items():
        lu_flags = [_is_limit_up(b, code) for b in bars]
        for i, b in enumerate(bars):
            if not lu_flags[i]:
                continue
            year = int(b["date"][:4])
            if not (start_year <= year <= end_year):
                continue
            # 连板数：今日及向前连续涨停的天数（官方口径）
            streak = 1
            j = i - 1
            while j >= 0 and lu_flags[j]:
                streak += 1
                j -= 1
            events_by_date.setdefault(b["date"][:10], []).append(
                (code, streak, b["close"]))
    return events_by_date


def simulate_event(code, bars, i, costs, slippage=0.003, capital=1500.0,
                   max_gap=0.07, min_gap=-0.02, stop_pct=-0.05):
    """模拟单个事件：D日(bars[i])涨停 → D+1买入 → D+2卖出（含T+1/止损/续持规则）。

    返回 None（事件无效：停牌/超价/gap过滤）或
    {date, code, buy_price, sell_price, pnl_pct, days, reason}
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
            return _settle(d1["date"][:10], code, buy_price, stop_price, shares,
                           costs, days_held, "止损")
        if days_held < 3 and _is_limit_up(b, code):
            j += 1  # 收盘涨停，续持一天
            continue
        return _settle(d1["date"][:10], code, buy_price, b["close"], shares,
                       costs, days_held, "收盘卖出")
    # 数据尽头（续持后停牌/退市）：按最后处理的K线收盘价结算
    b = last_b or bars[-1]
    return _settle(d1["date"][:10], code, buy_price, b["close"], shares,
                   costs, max(days_held, 1), "数据尽头结算")


def _settle(date, code, buy_price, sell_price, shares, costs, days, reason):
    amount = buy_price * shares
    buy_cost = max(amount * costs["commission_rate"], costs["commission_min"])
    sell_amount = sell_price * shares
    sell_cost = max(sell_amount * costs["commission_rate"], costs["commission_min"]) \
        + sell_amount * costs["stamp_duty"]
    pnl = sell_amount - amount - buy_cost - sell_cost
    return {
        "date": date, "code": code, "shares": shares,
        "buy_price": buy_price, "sell_price": sell_price,
        "pnl": round(pnl, 2), "pnl_pct": round(pnl / amount * 100, 2),
        "days": days, "reason": reason,
    }


def run_backtest(cfg, start_year=None, end_year=None):
    """主入口：拉数据 → 建事件 → 模拟 → 统计报告（markdown）"""
    from strategy import STREAK_SCORE

    st = cfg.get("backtest", {})
    costs = cfg.get("trading_costs") or {
        "commission_rate": 0.00025, "commission_min": 5.0, "stamp_duty": 0.0005}
    end_year = end_year or now_cn().year
    start_year = start_year or 2019

    print(f"[backtest] 拉取全市场代码清单 ...")
    universe = fetch_universe()
    print(f"[backtest] {len(universe)} 只股票，拉取日K（{start_year}-{end_year}，并发"
          f"{st.get('workers', 6)}）...")

    def progress(done, total, failed, elapsed):
        rate = done / elapsed if elapsed else 0
        eta = int((total - done) / rate) if rate else 0
        print(f"[backtest] K线 {done}/{total}（失败{failed}）"
              f" {rate:.0f}只/秒，预计还需{eta//60}分{eta%60}秒")

    klines, failed = fetch_all_klines(universe, workers=st.get("workers", 6),
                                      on_progress=progress)
    print(f"[backtest] K线就绪 {len(klines)} 只（失败 {len(failed)}）")

    events_by_date = build_events(klines, start_year, end_year)
    total_days = len(events_by_date)
    n_events = sum(len(v) for v in events_by_date.values())
    print(f"[backtest] {total_days}个交易日，{n_events}个涨停事件")

    # 逐日模拟：每日最多2个事件（模拟 max_stocks），按连板打分曲线排序
    # （2-3板优先——历史无封板质量数据，用连板数近似选股排序）
    max_per_day = st.get("max_events_per_day", 2)
    results = []
    for date in sorted(events_by_date):
        day_events = events_by_date[date]
        day_events.sort(key=lambda e: STREAK_SCORE.get(e[1], 8 if e[1] >= 5 else 15),
                        reverse=True)
        for code, streak, close in day_events[:max_per_day]:
            bars = klines[code]
            # 找到当日index
            try:
                i = next(idx for idx, b in enumerate(bars) if b["date"][:10] == date)
            except StopIteration:
                continue
            if i + 2 >= len(bars):
                continue  # 数据不足（刚上市/数据尽头）
            r = simulate_event(code, bars, i, costs)
            if r:
                r["streak"] = streak
                results.append(r)

    return _report(results, start_year, end_year, n_events, len(universe), failed)


def _report(results, start_year, end_year, n_events, n_universe, failed):
    """生成回测统计报告"""
    lines = [f"## 🧪 打板事件回测 {start_year}-{end_year}", ""]

    if not results:
        lines.append("无有效事件——请确认K线数据拉取成功（本地网络不通时请在云端跑）")
        return "\n".join(lines), results

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
    lines.append(f"- K线拉取失败 {len(failed)} 只（占比高时结果不可信，需重跑）")
    lines.append("_数据来源: 东方财富日K自建涨停日历 | 历史模拟不预示未来表现_")
    return "\n".join(lines), results
