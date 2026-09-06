# -*- coding: utf-8 -*-
"""低频三策略回测：S1 ETF趋势跟随 / S2 行业ETF轮动 / S3 小市值轮动

用法（经 main.py 入口）:
  py src/main.py backtest etf 2019 2026        # S1+S2（约30只ETF，本地分钟级）
  py src/main.py backtest smallcap 2019 2026   # S3（全市场拉K，建议云端跑）

数据源（2026-09 实测选型）:
  ETF：腾讯 ifzq fqkline qfq 前复权日K（640根/段分页回溯，实测到2014）。
       ETF必须前复权——红利ETF(510880)有分红，原始价会系统性低估收益
       （与打板回测刻意用原始价的场景不同：打板要精确涨停价，ETF只看趋势）。
  小市值：新浪原始日K（复用 backtest.fetch_stock_bars_sina，流式抽取月末月首
       两个截面，内存与股票总数无关）+ 东财当前股本快照近似历史市值。

成交口径（与打板回测一致）：D日收盘出信号 → D+1开盘价×(1±滑点)成交，整百。

已知偏差（报告末尾披露，读数字前必读）:
  1. 幸存者偏差（S3 尤重）：universe 是当前存续股票，退市的（多为崩盘小票）
     不在样本——S3 收益显著偏乐观，只作方向参考，附宽基ETF基准对照
  2. 股本时变（S3）：送转/增发使"当前股本×历史价"与真实历史市值有偏差
  3. 无历史ST过滤（S3）：接口不给历史名称，用当前名称近似
  4. qfq价缩放：前复权价整体下移，整百取整与真实手数有差异（对收益率
     的相对影响远小于打板回测的 gap 假设）
  5. 停牌近似：买入顺延≤5自然日否则放弃；小票跌停开盘无法卖出时顺延到
     下一调仓期（偏乐观方向的近似，已披露）
  6. 行业ETF上市晚：512690 从2019-05、515790 从2020-11 起有数据，
     早期样本偏少，动量窗口不足的不参与排名
  7. 现金替代按0收益计（保守下界）
  8. 成本双场景：ETF策略同时按「万2.5最低5元」与「ETF免五万0.5」回测——
     3k本金月频轮动下最低佣金是致命拖累，免五与否可直接翻转结论
"""
import gc
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import datasource as ds
from backtest import (DATA_DIR, _CircuitBreaker, fetch_stock_bars_sina,
                      cached_codes_today, read_kline_cache, save_kline_cache)
from datasource import now_cn
from etf import (ETF_NAMES, ROTATION_UNIVERSE, TREND_UNIVERSE, etf_symbol,
                 is_first_trade_day_of_month, rotation_targets, trend_target)

TENCENT_FQ_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

# 默认成本场景（可被 config lowfreq.costs 覆盖）
DEFAULT_COSTS = {
    "etf_std": {"commission_rate": 0.00025, "commission_min": 5.0, "stamp_duty": 0.0},
    "etf_free": {"commission_rate": 0.0005, "commission_min": 0.0, "stamp_duty": 0.0},
    "stock_std": {"commission_rate": 0.00025, "commission_min": 5.0, "stamp_duty": 0.0005},
}


# ---------- 腾讯 qfq ETF 日K数据层 ----------

_thread_local = threading.local()


def _session():
    import requests
    if not hasattr(_thread_local, "s"):
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        _thread_local.s = s
    return _thread_local.s


def fetch_etf_segment(sym, end, count=640):
    """腾讯 qfq 单段日K。返回 [[date, open, close, high, low]] 或 None。

    注意返回字段序：date, open, close, high, low（第2列是开、第3列是收）。
    坑（实测）：带 qfq 参数时，有分红历史的ETF数据在 "qfqday" 键下，
    从未分红的ETF却在 "day" 键下（且不带 qfq 参数反而什么都不返回）——
    两个键都取。
    """
    try:
        r = _session().get(
            TENCENT_FQ_URL,
            params={"param": f"{sym},day,,{end},{count},qfq"},
            timeout=(10, 30),
        )
        d = (r.json().get("data") or {}).get(sym) or {}
        rows = d.get("qfqday") or d.get("day") or []
        return [[p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4])]
                for p in rows]
    except Exception:
        return None


def fetch_etf_bars(code, start_date, end_date):
    """ETF前复权日K（分页回溯+按日去重，段间连续实测无重叠）。
    返回 [{date,open,close,high,low}] 升序，失败返回 None。"""
    sym = etf_symbol(code)
    by_date = {}
    end = end_date
    for _ in range(12):  # 640×12 ≈ 覆盖14年
        rows = None
        for attempt in range(2):
            rows = fetch_etf_segment(sym, end)
            if rows is not None:
                break
            time.sleep(1 + attempt)
        if not rows:
            break
        for row in rows:
            by_date[row[0]] = row
        if rows[0][0] <= start_date or len(rows) < 640:
            break
        end = rows[0][0]
    if not by_date:
        return None
    return [{"date": d, "open": o, "close": c, "high": h, "low": low}
            for d, o, c, h, low in (by_date[k] for k in sorted(by_date))]


def probe_etf_source():
    """源健康探测：3只代表性ETF全成功才可用（沿用 backtest.probe_source 模式）。"""
    end = now_cn().strftime("%Y-%m-%d")
    for code in ("510300", "159915", "512690"):
        rows = fetch_etf_segment(etf_symbol(code), end, count=10)
        if not rows:
            return False
    return True


def load_etf_bars(universe, start_date, end_date):
    """拉取 universe 全部ETF日K（当日磁盘缓存复用 backtest 的缓存目录）。
    返回 {code: bars}，仅含有数据的。"""
    bars_by_code = {}
    cached = cached_codes_today()
    todo = [c for c in universe if c not in cached]
    for code in [c for c in universe if c in cached]:
        bars = read_kline_cache(code)
        if bars:
            bars_by_code[code] = bars
    if todo:
        if not probe_etf_source():
            raise RuntimeError("腾讯qfq ETF数据源不可用，中止回测（快速失败）")
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(fetch_etf_bars, c, start_date, end_date): c
                    for c in todo}
            for fut in as_completed(futs):
                code = futs[fut]
                bars = fut.result()
                if bars:
                    save_kline_cache(code, bars)
                    bars_by_code[code] = bars
                else:
                    print(f"[lowfreq-bt] {code} {ETF_NAMES.get(code, '')} "
                          f"K线拉取失败/无数据，跳过", flush=True)
    return bars_by_code


# ---------- 模拟账本 ----------

class SimBook:
    """回测模拟账本：整百成交、买卖滑点、佣金/印花税（costs dict 注入）。

    买入按开盘价×(1+滑点)，卖出按开盘价×(1-滑点)——买卖双侧都付滑点，
    比打板回测只算买入滑点更保守（ETF点差真实存在）。
    """

    def __init__(self, capital, costs, slippage=0.001):
        self.cash = float(capital)
        self.initial = float(capital)
        self.costs = dict(costs)
        self.slippage = slippage
        self.positions = {}    # code -> {"shares", "buy_price"}
        self.trades = []       # 成交流水
        self.nav_curve = []    # [(date, value)]
        self.total_costs = 0.0

    def _buy_fee(self, amount):
        return max(amount * self.costs["commission_rate"],
                   self.costs["commission_min"])

    def _sell_fee(self, amount):
        return (max(amount * self.costs["commission_rate"],
                    self.costs["commission_min"])
                + amount * self.costs["stamp_duty"])

    def buy(self, code, price, date, budget):
        """按预算整百买入（现金不足自动减手数）。返回是否成交。"""
        fill = price * (1 + self.slippage)
        shares = int(budget / (fill * 100)) * 100
        while shares >= 100:
            amount = round(shares * fill, 2)
            fee = self._buy_fee(amount)
            if amount + fee <= self.cash:
                self.cash -= amount + fee
                self.total_costs += fee
                self.positions[code] = {"shares": shares, "buy_price": fill}
                self.trades.append({"date": date, "code": code, "action": "buy",
                                    "price": round(fill, 4), "shares": shares})
                return True
            shares -= 100
        return False

    def sell(self, code, price, date):
        """清仓卖出。返回毛盈亏（费用另计在 total_costs）。"""
        pos = self.positions.pop(code, None)
        if not pos:
            return None
        fill = price * (1 - self.slippage)
        amount = round(pos["shares"] * fill, 2)
        fee = self._sell_fee(amount)
        self.cash += amount - fee
        self.total_costs += fee
        gross = round(pos["shares"] * (fill - pos["buy_price"]), 2)
        self.trades.append({"date": date, "code": code, "action": "sell",
                            "price": round(fill, 4), "shares": pos["shares"],
                            "pnl": gross})
        return gross

    def value(self, prices):
        """prices: {code: 现价}，缺价用成本价（停牌兜底）。"""
        v = self.cash
        for code, p in self.positions.items():
            v += prices.get(code, p["buy_price"]) * p["shares"]
        return v


# ---------- 模拟：S1 趋势跟随 / S2 轮动 ----------

def last_bar_at(bars, date):
    """<= date 的最后一根K线（净值定价用）。bars 升序。"""
    target = str(date)[:10]
    last = None
    for b in bars:
        if b["date"][:10] <= target:
            last = b
        else:
            break
    return last


def _nav(book, bars_by_code, date, held_codes):
    prices = {}
    for code in held_codes:
        b = last_bar_at(bars_by_code.get(code) or [], date)
        if b:
            prices[code] = b["close"]
    book.nav_curve.append((str(date)[:10], round(book.value(prices), 2)))


def simulate_trend(bars_by_code, dates, ma_window=20, ma_confirm=1, mom_window=20,
                   costs=None, capital=1000.0, slippage=0.001):
    """S1：每日收盘算信号，次日开盘切换（先卖后买，单票满仓）。"""
    book = SimBook(capital, costs or DEFAULT_COSTS["etf_free"], slippage)
    holding, prev_target = None, None
    for i, date in enumerate(dates):
        # 1) 执行昨收信号：今日开盘
        if i > 0 and prev_target != holding:
            if holding is not None:
                bars = bars_by_code.get(holding) or []
                b = _buyable(bars, date)
                if b is not None:
                    book.sell(holding, b["open"], date)
                    holding = None
            if holding is None and prev_target is not None:
                b = _buyable(bars_by_code.get(prev_target) or [], date)
                if b is not None and book.buy(prev_target, b["open"], date, book.cash):
                    holding = prev_target
        # 2) 今日收盘信号
        prev_target, _ = trend_target(bars_by_code, date, ma_window, ma_confirm,
                                       mom_window)
        # 3) 记净值
        _nav(book, bars_by_code, date, book.positions)
    return book


def simulate_rotation(bars_by_code, dates, rebalance_days=20, mom_window=20,
                      top_n=2, min_mom=0.0, costs=None, capital=1000.0,
                      slippage=0.001):
    """S2：每 rebalance_days 个交易日收盘排名，次日开盘调仓（先卖后买，等权）。"""
    book = SimBook(capital, costs or DEFAULT_COSTS["etf_free"], slippage)
    pending = None          # 昨收算出的目标（今晨执行）
    days_since = rebalance_days  # 首日即触发
    for i, date in enumerate(dates):
        # 1) 执行昨收目标：今晨开盘
        if i > 0 and pending is not None:
            targets = set(pending) - {"__CASH__"}
            # 先卖：不在目标里的持仓
            for code in list(book.positions):
                if code not in targets:
                    b = _buyable(bars_by_code.get(code) or [], date)
                    if b is not None:
                        book.sell(code, b["open"], date)
            # 等权买入新标的：槽位 = 总值 / top_n
            if targets:
                total = book.value({c: (last_bar_at(bars_by_code[c], date) or
                                        {"close": 0})["close"]
                                    for c in book.positions if c in bars_by_code})
                slot = total / top_n
                for code in pending:
                    if code != "__CASH__" and code not in book.positions:
                        b = _buyable(bars_by_code.get(code) or [], date)
                        if b is not None:
                            book.buy(code, b["open"], date, slot)
        pending = None
        # 2) 收盘：调仓到期则算新目标（明晨执行）
        days_since += 1
        if days_since >= rebalance_days:
            pending, _ = rotation_targets(bars_by_code, date, mom_window, top_n,
                                          min_mom)
            days_since = 0
        # 3) 记净值
        _nav(book, bars_by_code, date, book.positions)
    return book


def _buyable(bars, date, max_delay=5):
    """买入侧成交K线：停牌顺延≤5自然日（复用 etf.bar_on_or_after 语义）。"""
    from etf import bar_on_or_after
    return bar_on_or_after(bars, date, max_delay)


# ---------- 模拟：S3 小市值（流式月度截面） ----------

def extract_monthly(bars, sel_dates, exec_dates):
    """单只股票流式抽取月度截面（S3回测核心——只留小结果，K线用完即弃）。

    sel_dates / exec_dates: 等长列表（月初选股日 / 其后第一个日历交易日）。
    对每个月 k：取选股日收盘 close_sel，执行日取该股票在选股日之后的
    第一根K线（正常为次日；停牌则顺延，顺延过远由模拟侧放弃）。
    返回 {k: (close_sel, open_exec, low_exec, prev_close_exec, exec_date)}；
    该月停牌（选股日无K线）则不含 k。
    """
    out = {}
    n = len(bars)
    j = 0
    for k, sel in enumerate(sel_dates):
        while j < n and bars[j]["date"][:10] < sel:
            j += 1
        if j >= n:
            break
        if bars[j]["date"][:10] == sel and j + 1 < n:
            ex_bar = bars[j + 1]
            out[k] = (bars[j]["close"], ex_bar["open"], ex_bar["low"],
                      bars[j]["close"], ex_bar["date"][:10])
    return out


def select_monthly(monthly_by_code, shares_map, list_date_map, sel_dates,
                   k, top_n=5, min_list_days=365):
    """第 k 期选股：当月有收盘截面、上市满 min_list_days、当前股本已知的
    股票里，近似市值（当前股本×当月收盘）最小的 top_n。"""
    import datetime as dt

    def _d(v):
        s = str(v).replace("-", "")[:8]
        return dt.datetime.strptime(f"{s[:4]}-{s[4:6]}-{s[6:8]}", "%Y-%m-%d")

    try:
        d_sel = _d(sel_dates[k])
    except (ValueError, TypeError):
        return []
    rows = []
    for code, months in monthly_by_code.items():
        rec = months.get(k)
        if rec is None:
            continue
        shares = shares_map.get(code)
        if not shares:
            continue
        ld = (list_date_map or {}).get(code)
        if not ld:
            continue  # 上市日期缺失（数据漂移）保守剔除
        try:
            d_list = _d(ld)
        except (ValueError, TypeError):
            continue
        if (d_sel - d_list).days < min_list_days:
            continue
        rows.append((shares * rec[0], code))
    rows.sort()
    return [code for _, code in rows[:top_n]]


def simulate_smallcap(monthly_by_code, selected, sel_dates, top_n=5,
                      costs=None, capital=1000.0, slippage=0.003, max_delay=8):
    """S3 月度轮动模拟：每期卖出全部持仓（停牌/跌停开盘顺延到下期，
    乐观方向的近似，报告披露），再等权买入 selected[k]。

    monthly_by_code: {code: extract_monthly输出}
    selected: {k: [code]} 每期选股名单（select_monthly 预先算好）
    """
    import datetime as dt
    book = SimBook(capital, costs or DEFAULT_COSTS["stock_std"], slippage)
    n = len(sel_dates)
    for k in range(n):
        # 1) 卖出全部持仓（第 k 期执行日开盘价）
        for code in list(book.positions):
            rec = monthly_by_code.get(code, {}).get(k)
            if rec is None:
                continue  # 该月停牌：顺延持有
            _, open_exec, low_exec, prev_close, ex_date = rec
            # 跌停开盘卖不出：顺延到下期（左尾的乐观近似，报告披露）
            if (open_exec <= low_exec + 1e-9 and prev_close > 0
                    and open_exec / prev_close - 1 <= -0.095):
                continue
            # 执行日顺延过远（长期停牌后的首根K线）：下期再卖
            try:
                d0 = dt.datetime.strptime(str(sel_dates[k])[:10], "%Y-%m-%d")
                d1 = dt.datetime.strptime(ex_date[:10], "%Y-%m-%d")
                if (d1 - d0).days > max_delay:
                    continue
            except ValueError:
                pass
            book.sell(code, open_exec, ex_date[:10])
        # 2) 等权买入本期名单
        picks = selected.get(k) or []
        if picks:
            slot = book.cash / len(picks)
            for code in picks:
                rec = monthly_by_code.get(code, {}).get(k)
                if rec is None:
                    continue
                book.buy(code, rec[1], rec[4][:10], slot)
        # 3) 记净值（选股日收盘定价）
        prices = {}
        for code in book.positions:
            rec = monthly_by_code.get(code, {}).get(k)
            if rec is not None:
                prices[code] = rec[0]
        book.nav_curve.append((str(sel_dates[k])[:10],
                               round(book.value(prices), 2)))
    return book


# ---------- 统计 ----------

def _metrics(nav_curve):
    """净值曲线 → 累计/年化/最大回撤/分年收益。净值频率：ETF日频，S3月频。"""
    if len(nav_curve) < 2:
        return {"total": 0.0, "annual": 0.0, "mdd": 0.0, "years": {}}
    import datetime as dt
    total = nav_curve[-1][1] / nav_curve[0][1] - 1
    d0 = dt.datetime.strptime(nav_curve[0][0], "%Y-%m-%d")
    d1 = dt.datetime.strptime(nav_curve[-1][0], "%Y-%m-%d")
    days = max((d1 - d0).days, 1)
    annual = (1 + total) ** (365.25 / days) - 1 if total > -1 else -1.0
    peak, mdd = nav_curve[0][1], 0.0
    for _, v in nav_curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    years = {}
    base, cur_year, last_v = nav_curve[0][1], nav_curve[0][0][:4], nav_curve[0][1]
    for date, v in nav_curve[1:]:
        y = date[:4]
        if y != cur_year:
            years[cur_year] = round(last_v / base - 1, 4)
            base, cur_year = v, y
        last_v = v
    years[cur_year] = round(last_v / base - 1, 4)
    return {"total": round(total, 4), "annual": round(annual, 4),
            "mdd": round(mdd, 4), "years": years}


def _fmt_pct(v):
    return f"{v*100:+.1f}%" if v is not None else "-"


def _buyhold_nav(bars, dates, capital=1000.0):
    """基准：首日全仓买入持有（资金曲线对照）"""
    if not bars or not dates:
        return []
    first = last_bar_at(bars, dates[0])
    if not first:
        return []
    shares = int(capital / (first["close"] * 100)) * 100
    if shares < 100:
        return []  # 一手都买不起（qfq价缩放后偶发）
    nav = []
    for date in dates:
        b = last_bar_at(bars, date)
        if b:
            nav.append((str(date)[:10], round(shares * b["close"], 2)))
    return nav


# ---------- 主入口 ----------

def run_etf_backtest(cfg, start_year=None, end_year=None):
    """S1+S2 回测：拉ETF日K → 参数网格模拟（双成本）→ 报告。"""
    st = (cfg.get("lowfreq") or {})
    costs_cfg = {**DEFAULT_COSTS, **(st.get("costs") or {})}
    end_year = end_year or now_cn().year
    start_year = start_year or 2019
    fetch_start = f"{start_year - 1}-01-01"   # 动量/均线预热：多拉一年
    fetch_end = now_cn().strftime("%Y-%m-%d")

    # YAML里 510300 等未加引号会解析成 int——统一归一化成6位字符串
    def _codes(lst):
        return [str(c).zfill(6) for c in (lst or [])]

    trend_u = _codes((st.get("trend") or {}).get("universe")) or TREND_UNIVERSE
    rotation_u = _codes((st.get("rotation") or {}).get("universe")) or ROTATION_UNIVERSE
    universe = sorted(set(trend_u) | set(rotation_u))
    print(f"[lowfreq-bt] 拉取 {len(universe)} 只ETF日K（腾讯qfq前复权）...", flush=True)
    bars_by_code = load_etf_bars(universe, fetch_start, fetch_end)
    if "510300" not in bars_by_code:
        raise RuntimeError("510300（主日历）K线拉取失败，中止")
    # ⚠️ 信号池隔离：S1 只在自己的宽基/黄金池里挑，S2 只在行业池里轮动。
    # bars_by_code 是两池并集（一次拉取），直接喂给 simulate 会让
    # trend_target/rotation_targets 遍历全部28只——S1 变相成了行业轮动。
    trend_bars = {c: bars_by_code[c] for c in trend_u if c in bars_by_code}
    rot_bars = {c: bars_by_code[c] for c in rotation_u if c in bars_by_code}
    dates_all = [b["date"][:10] for b in bars_by_code["510300"]]
    dates = [d for d in dates_all if f"{start_year}-01-01" <= d <= fetch_end]
    print(f"[lowfreq-bt] {len(dates)} 个交易日（{dates[0]} ~ {dates[-1]}）", flush=True)

    cap = 1000.0  # 与 books.trend/rotation 虚拟账本同额，结果可直接对照
    sl = (st.get("trend") or {}).get("slippage", 0.001)

    # S1 参数网格（双成本）
    trend_grid = []
    for ma in (20, 60):
        for mom in (20, 60, 120):
            row = {"ma": ma, "mom": mom}
            for cname in ("etf_free", "etf_std"):
                book = simulate_trend(trend_bars, dates, ma_window=ma,
                                      mom_window=mom, costs=costs_cfg[cname],
                                      capital=cap, slippage=sl)
                row[cname] = _metrics(book.nav_curve)
                row[cname]["n_trades"] = len(book.trades)
                row[cname]["costs"] = round(book.total_costs, 1)
            trend_grid.append(row)

    # S2 参数网格（双成本）
    rot_grid = []
    for reb in (5, 20, 60):
        for mom in (20, 60):
            row = {"reb": reb, "mom": mom}
            for cname in ("etf_free", "etf_std"):
                book = simulate_rotation(rot_bars, dates, rebalance_days=reb,
                                         mom_window=mom, top_n=2,
                                         costs=costs_cfg[cname], capital=cap,
                                         slippage=sl)
                row[cname] = _metrics(book.nav_curve)
                row[cname]["n_trades"] = len(book.trades)
                row[cname]["costs"] = round(book.total_costs, 1)
            rot_grid.append(row)

    # 默认参数明细（config口径，供与虚拟盘对照）
    d_ma = (st.get("trend") or {}).get("ma_window", 20)
    d_mom = (st.get("trend") or {}).get("mom_window", 20)
    d_reb = (st.get("rotation") or {}).get("rebalance_days", 20)
    d_rmom = (st.get("rotation") or {}).get("mom_window", 20)
    detail = {}
    for cname in ("etf_free", "etf_std"):
        bt_ = simulate_trend(trend_bars, dates, ma_window=d_ma,
                             mom_window=d_mom, costs=costs_cfg[cname],
                             capital=cap, slippage=sl)
        br_ = simulate_rotation(rot_bars, dates, rebalance_days=d_reb,
                                mom_window=d_rmom, top_n=2,
                                costs=costs_cfg[cname], capital=cap, slippage=sl)
        detail[cname] = {"trend": _metrics(bt_.nav_curve),
                         "rotation": _metrics(br_.nav_curve)}

    bench = _metrics(_buyhold_nav(bars_by_code["510300"], dates, cap))

    md = _report_etf(start_year, end_year, trend_grid, rot_grid, detail, bench)
    summary = {
        "generated": now_cn().strftime("%Y-%m-%d %H:%M"),
        "range": f"{start_year}-{end_year}",
        "trend_default": detail["etf_free"]["trend"],
        "rotation_default": detail["etf_free"]["rotation"],
        "trend_std": detail["etf_std"]["trend"],
        "rotation_std": detail["etf_std"]["rotation"],
        "benchmark_510300": bench,
    }
    return md, summary


def run_smallcap_backtest(cfg, start_year=None, end_year=None):
    """S3 回测：全市场月度截面 → 每期选股 → 模拟 → 报告。"""
    st_all = (cfg.get("lowfreq") or {})
    st = st_all.get("smallcap") or {}
    costs = {**DEFAULT_COSTS["stock_std"],
             **(st_all.get("costs") or {}).get("stock_std", {})}
    top_n = st.get("top_n", 5)
    min_list_days = st.get("min_list_days", 365)
    min_price = st.get("min_price", 2.0)
    slippage = st.get("slippage", 0.003)
    capital = 1000.0
    end_year = end_year or now_cn().year
    start_year = start_year or 2019
    fetch_start = f"{start_year - 1}-12-01"

    # 1) 股本快照（当前流通股本 × 历史价 ≈ 历史市值）
    print("[lowfreq-bt] 拉取全市场股本快照 ...", flush=True)
    caps = ds.fetch_market_caps()
    if not caps:
        raise RuntimeError("股本快照拉取失败，中止")
    # 无历史ST名称：用当前名称近似过滤（披露）；上市日期缺失的剔除
    universe = [c for c in caps
                if "ST" not in (c["name"] or "")
                and (c["name"] or "?")[0] != "N"
                and c.get("list_date") and c["price"] >= min_price]
    shares_map = {c["code"]: c["shares"] for c in universe}
    list_date_map = {c["code"]: c["list_date"] for c in universe}
    print(f"[lowfreq-bt] 过滤后 {len(universe)}/{len(caps)} 只进入样本", flush=True)

    # 2) 交易日历（浦发银行：1999年上市从未长期停牌）
    cal_bars = fetch_stock_bars_sina("600000", fetch_start,
                                     now_cn().strftime("%Y-%m-%d"))
    if not cal_bars:
        raise RuntimeError("交易日历（600000）拉取失败，中止")
    cal = [b["date"][:10] for b in cal_bars
           if f"{start_year}-01-01" <= b["date"][:10] <= now_cn().strftime("%Y-%m-%d")]
    sel_dates, exec_dates = [], []
    for i in range(1, len(cal)):
        if is_first_trade_day_of_month(cal[i - 1], cal[i]) and i + 1 < len(cal):
            sel_dates.append(cal[i])
            exec_dates.append(cal[i + 1])
    print(f"[lowfreq-bt] {len(sel_dates)} 个月度截面"
          f"（{sel_dates[0]} ~ {sel_dates[-1]}）", flush=True)

    # 3) 全市场K线 → 流式抽取月度截面（内存与股票总数无关）
    monthly_by_code = {}
    cached = cached_codes_today()
    # 注意：todo 必须含已缓存代码——月度截面只在下方 as_completed 循环里抽取，
    # 把缓存命中者排除在外会导致它们整段从样本中消失。
    todo = [c["code"] for c in universe]
    breaker = _CircuitBreaker()
    n_done = n_fail = 0
    t0 = time.monotonic()

    def work(code):
        bars = read_kline_cache(code) if code in cached else None
        if bars is None:
            bars = fetch_stock_bars_sina(code, fetch_start,
                                        now_cn().strftime("%Y-%m-%d"))
            if bars is not None:
                save_kline_cache(code, bars)
        return code, bars

    with ThreadPoolExecutor(
            max_workers=cfg.get("backtest", {}).get("workers", 6)) as ex:
        futs = {ex.submit(work, c): c for c in todo}
        for fut in as_completed(futs):
            code, bars = fut.result()
            n_done += 1
            if bars is None:
                breaker.record(False)
                n_fail += 1
            else:
                breaker.record(True)
                monthly_by_code[code] = extract_monthly(bars, sel_dates, exec_dates)
                del bars
            if breaker.tripped():
                raise RuntimeError(f"数据源熔断：{breaker.tripped()}，中止回测")
            if n_done % 500 == 0 or n_done == len(todo):
                el = time.monotonic() - t0
                rate = n_done / el if el else 0
                eta = int((len(todo) - n_done) / rate) if rate else 0
                print(f"[lowfreq-bt] K线 {n_done}/{len(todo)} {rate:.1f}只/秒，"
                      f"预计还需{eta//60}分{eta%60}秒", flush=True)
            if n_done % 2000 == 0:
                gc.collect()

    # 4) 每期选股（月度截面现算近似市值排名）
    selected = {}
    for k in range(len(sel_dates)):
        selected[k] = select_monthly(monthly_by_code, shares_map, list_date_map,
                                      sel_dates, k, top_n=top_n,
                                      min_list_days=min_list_days)

    # 5) 模拟
    book = simulate_smallcap(monthly_by_code, selected, sel_dates, top_n=top_n,
                             costs=costs, capital=capital, slippage=slippage)
    m = _metrics(book.nav_curve)

    md = _report_smallcap(start_year, end_year, m, book, len(sel_dates),
                          n_fail, len(universe), top_n)
    summary = {
        "generated": now_cn().strftime("%Y-%m-%d %H:%M"),
        "range": f"{start_year}-{end_year}",
        "metrics": m,
        "n_trades": len(book.trades),
        "total_costs": round(book.total_costs, 2),
        "n_failed": n_fail,
    }
    return md, summary


def _report_etf(start_year, end_year, trend_grid, rot_grid, detail, bench):
    lines = [f"## 🧪 低频策略回测（ETF）{start_year}-{end_year}", "",
             "1000元/账本口径（与虚拟盘分账同额）。信号D日收盘 → D+1开盘成交，"
             "含佣金与±0.1%滑点。年化 / 最大回撤。", ""]
    lines.append("**S1 指数ETF趋势跟随**（参数网格，双成本场景）")
    lines.append("| MA | 动量 | ETF免五 | 万2.5最低5元 |")
    lines.append("|---|---|---|---|")
    for r in trend_grid:
        f, s = r["etf_free"], r["etf_std"]
        lines.append(f"| MA{r['ma']} | {r['mom']}日 | "
                     f"{_fmt_pct(f['annual'])} / {_fmt_pct(f['mdd'])} | "
                     f"{_fmt_pct(s['annual'])} / {_fmt_pct(s['mdd'])} |")
    lines.append("")
    lines.append("**S2 行业ETF轮动**（持有前2，参数网格，双成本场景）")
    lines.append("| 调仓 | 动量 | ETF免五 | 万2.5最低5元 |")
    lines.append("|---|---|---|---|")
    for r in rot_grid:
        f, s = r["etf_free"], r["etf_std"]
        lines.append(f"| {r['reb']}日 | {r['mom']}日 | "
                     f"{_fmt_pct(f['annual'])} / {_fmt_pct(f['mdd'])} | "
                     f"{_fmt_pct(s['annual'])} / {_fmt_pct(s['mdd'])} |")
    lines.append("")
    lines.append(f"**基准对照**：510300 买入持有 年化 {_fmt_pct(bench['annual'])}"
                 f" / 最大回撤 {_fmt_pct(bench['mdd'])}")
    lines.append("")
    lines.append("**默认参数（config口径，虚拟盘对照基准）分年收益**")
    for strat, label in (("trend", "S1趋势"), ("rotation", "S2轮动")):
        d = detail["etf_free"][strat]
        yrs = " | ".join(f"{y}:{_fmt_pct(v)}" for y, v in sorted(d["years"].items()))
        lines.append(f"- {label}: 年化 {_fmt_pct(d['annual'])}，回撤 "
                     f"{_fmt_pct(d['mdd'])}，{yrs}")
    lines.append("")
    lines.append("**⚠️ 偏差与口径**：qfq前复权价（整百取整与真实手数略有差异）；"
                 "行业ETF上市晚致早期样本少；现金按0收益（保守）；成本双场景——"
                 "免五与否对3k本金月频轮动是可行性分水岭，实盘跟单前先确认佣金条件。")
    return "\n".join(lines)


def _report_smallcap(start_year, end_year, m, book, n_months, n_fail,
                     n_universe, top_n):
    lines = [f"## 🧪 低频策略回测（小市值）{start_year}-{end_year}", ""]
    lines.append(f"**总体**：月度轮动持股{top_n}只 | 1000元账本 | "
                 f"年化 **{_fmt_pct(m['annual'])}** | 最大回撤 **{_fmt_pct(m['mdd'])}** | "
                 f"成交 {len(book.trades)} 笔 | 总成本 {book.total_costs:.0f}元")
    lines.append("")
    lines.append("**分年收益**")
    lines.append("| 年份 | 收益 |")
    lines.append("|---|---|")
    for y, v in sorted(m["years"].items()):
        lines.append(f"| {y} | {_fmt_pct(v)} |")
    lines.append("")
    fail_note = f"K线拉取失败 {n_fail} 只"
    if n_fail > n_universe * 0.05:
        fail_note += " ⚠️ 失败率超5%，结果可信度低！"
    lines.append(fail_note)
    lines.append("")
    lines.append("**⚠️ 偏差（读数字前必读——S3只作方向参考）**")
    lines.append("- 幸存者偏差尤重：退市股（多为崩盘小票）不在样本，收益显著偏乐观"
                 "——该偏差对小市值比对任何其他策略都大")
    lines.append("- 历史市值用「当前股本×历史价」近似（送转/增发失真）")
    lines.append("- 无历史ST名称过滤（用当前名称近似）；跌停开盘卖不出顺延到"
                 "下期的乐观近似")
    lines.append("- 净值曲线为月度粒度（月频策略的回撤被低估）")
    lines.append("_历史模拟不预示未来表现_")
    return "\n".join(lines)


def run_lowfreq_backtest(cfg, mode, start_year=None, end_year=None):
    """统一入口：mode = 'etf' | 'smallcap'。返回 (markdown, summary)。"""
    if mode == "etf":
        return run_etf_backtest(cfg, start_year, end_year)
    if mode == "smallcap":
        return run_smallcap_backtest(cfg, start_year, end_year)
    raise ValueError(f"未知回测模式: {mode}")
