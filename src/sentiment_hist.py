# -*- coding: utf-8 -*-
"""历史情绪序列构建：全市场个股日K → 每日晋级率/涨停股均涨幅。

背景：情绪温度计（晋级率/涨停均涨幅）的 data/history/ 归档为空（2026-09
才上线），S2 情绪闸门回测只能自建历史序列。

口径（意图对齐 datasource 情绪温度计的回测版，但存在已知断裂，见下）:
  晋级率(d)     = |ZT(d-1) ∩ ZT(d)| / |ZT(d-1)|   ZT(d)=d日涨停股集合
  涨停均涨幅(d) = mean(pct(d, code)) for code in ZT(d-1)   单位：%
两者只用 d 日收盘前信息——闸门在 D 日收盘读数、D+1 日开盘执行，无未来函数。

涨停判定复用打板回测口径（backtest._is_limit_up，原始价+收盘=最高）。
已知断裂（回测序列与实盘温度计不是同一分布，阈值不可直接互搬）:
  - 原始价下除权日涨停漏检（方向未定：分子分母同时受影响）
  - ST股5%涨停达不到9.85%判定，全程不计入
  - 创业板2020-08-24注册制前为10%涨停，19.85%判定漏掉该时段——序列在
    2020-08-24存在结构性断点
  - 新股上市首日44%涨幅不计入
  - 序列由当前存续个股构建（幸存者偏差，聚合统计层面影响小）

输出 data/backtest/sentiment_history.json:
  {"generated": ..., "range": "2019-2026",
   "series": {date: {"promo": 0.18, "avg_gain": 1.2, "n_zt": 87}}}
  promo 为比例(0~1)；avg_gain 为百分比(%)；缺前日数据时为 null。

内存策略：拉取阶段全部个股K线驻留内存（~5000只全量），聚合阶段单线程
无锁遍历一遍后释放，只留每日涨停代码集合与次日涨幅列表。
"""
import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import datasource as ds
from backtest import (_CircuitBreaker, _is_limit_up, fetch_stock_bars_sina,
                      is_bj_code)
from datasource import now_cn

DATA_PATH = Path("data/backtest/sentiment_history.json")


def build_sentiment_history(start_year=2019, workers=6, force=False):
    """拉全市场个股日K，构建每日情绪序列并落盘。已存在且覆盖目标区间
    的序列直接复用（force=True 强制重建）。返回 series dict。"""
    existing = load_sentiment_history()
    if existing and not force:
        have = sorted(existing)
        # 新鲜度检查：末端距今 >12 天（覆盖春节/国庆长假）视为过期。静默复用
        # 过期序列会让闸门在近期"永远不触发"（缺失日 make_gate 返回 False），
        # 闸门ON 悄悄退化成残缺的 OFF——必须重建，宁可多花几分钟。
        try:
            stale = (now_cn().date() - date.fromisoformat(have[-1])).days
        except ValueError:   # 序列键损坏（非日期格式）→ 视为过期，落重建
            stale = 99
        if have[0] <= f"{start_year}-01-15" and stale <= 12:
            print(f"[sentiment] 复用已有序列 {have[0]} ~ {have[-1]} "
                  f"({len(have)} 天)", flush=True)
            return existing
        if have[0] <= f"{start_year}-01-15":
            print(f"[sentiment] ⚠️ 已有序列止于 {have[-1]}（距今 {stale} 天），"
                  f"过期，重建", flush=True)

    fetch_start = f"{start_year - 1}-12-01"   # 12月数据只为预热pct，不进序列
    fetch_end = now_cn().strftime("%Y-%m-%d")

    # 交易日历：浦发银行1999年上市从未长期停牌（与smallcap回测同口径）
    cal_bars = fetch_stock_bars_sina("600000", fetch_start, fetch_end)
    if not cal_bars:
        raise RuntimeError("交易日历（600000）拉取失败，中止")
    cal = [b["date"][:10] for b in cal_bars if b["date"][:10] >= f"{start_year}-01-01"]
    cal_set = set(cal)

    universe = [(c, s) for c, s in ds.fetch_universe() if not is_bj_code(c)]
    if not universe:
        raise RuntimeError("全市场代码清单拉取失败，中止")
    print(f"[sentiment] {len(universe)} 只个股 → 情绪序列 {cal[0]} ~ {cal[-1]}",
          flush=True)

    # 累加器：每日涨停代码集合 + 昨日涨停股的今日涨幅
    zt_by_date = defaultdict(set)
    next_pcts = defaultdict(list)

    def work(code):
        bars = fetch_stock_bars_sina(code, fetch_start, fetch_end)
        return code, bars

    breaker = _CircuitBreaker()
    n_done = n_fail = 0
    t0 = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(work, c): c for c, _ in universe}
        for fut in as_completed(futs):
            code, bars = fut.result()
            n_done += 1
            if bars is None:
                breaker.record(False)
                n_fail += 1
            else:
                breaker.record(True)
                results.append((code, bars))
            if breaker.tripped():
                raise RuntimeError(f"数据源熔断：{breaker.tripped()}，中止")
            if n_done % 500 == 0 or n_done == len(universe):
                el = time.monotonic() - t0
                rate = n_done / el if el else 0
                eta = int((len(universe) - n_done) / rate) if rate else 0
                print(f"[sentiment] K线 {n_done}/{len(universe)} "
                      f"{rate:.1f}只/秒，预计还需{eta//60}分{eta%60}秒",
                      flush=True)

    # 单线程聚合（避免竞态；5000只全量K已在内存，聚合很快）
    for code, bars in results:
        for i in range(1, len(bars)):
            b = bars[i]
            d = b["date"][:10]
            if d not in cal_set:
                continue
            if _is_limit_up(b, code):
                zt_by_date[d].add(code)
                # 次日涨幅贡献到 next_pcts[次日]
                if i + 1 < len(bars):
                    nd = bars[i + 1]["date"][:10]
                    if nd in cal_set:
                        next_pcts[nd].append(bars[i + 1]["pct"])
    del results

    # 序列化：promo(d) = |ZT(d-1)∩ZT(d)|/|ZT(d-1)|，avg_gain(d) = 昨涨停今日均涨幅
    series = {}
    prev_zt = None
    for d in cal:
        zt = zt_by_date.get(d) or set()
        promo = None
        if prev_zt:
            promo = round(len(prev_zt & zt) / len(prev_zt), 4)
        gains = next_pcts.get(d) or []
        avg_gain = round(sum(gains) / len(gains), 2) if gains else None
        series[d] = {"promo": promo, "avg_gain": avg_gain, "n_zt": len(zt)}
        prev_zt = zt          # 昨日无涨停（空集合）→ 今日promo=None，语义正确
    if n_fail > len(universe) * 0.05:
        print(f"[sentiment] ⚠️ K线失败率 {n_fail}/{len(universe)} 超5%，"
              f"序列可信度低", flush=True)

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps({
        "generated": now_cn().strftime("%Y-%m-%d %H:%M"),
        "range": f"{start_year}-{now_cn().year}",
        "n_fail": n_fail, "n_universe": len(universe),
        "series": series,
    }, ensure_ascii=False), encoding="utf-8")
    print(f"[sentiment] 序列落盘 {DATA_PATH}（{len(series)} 天，失败 {n_fail} 只）",
          flush=True)
    return series


def load_sentiment_history():
    """读已构建的情绪序列，无文件返回 None。"""
    try:
        js = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        if js.get("series"):
            return js["series"]
    except (ValueError, OSError):
        pass
    return None


def make_gate(series, promo_min=0.15, avg_gain_min=0.0):
    """情绪闸门：D日收盘读数触发（True=风险关闸，次日开盘清仓持币）。
    promo(d)/avg_gain(d) 均为 d 收盘前信息，无未来函数。
    无数据/缺值时返回 False（不闸门——触发保守方向的缺省）。"""
    def gate(date):
        s = series.get(str(date)[:10])
        if not s:
            return False
        promo, avg = s.get("promo"), s.get("avg_gain")
        if promo is None or avg is None:
            return False
        return promo < promo_min or avg < avg_gain_min
    return gate
