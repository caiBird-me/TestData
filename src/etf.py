# -*- coding: utf-8 -*-
"""低频策略一/二的信号纯函数：指数ETF趋势跟随 + 行业ETF轮动。

全部为纯函数（无网络无状态），回测（backtest_lowfreq）与每日信号
（main.run_lowfreq）共用同一套规则，保证"验证的"和"执行的"不脱节。

成交口径（与打板回测一致）：D日收盘出信号 → D+1开盘价×(1+滑点)成交。
"""
import datetime as _dt


# ETF名称表（universe内全部代码，报告展示用）
ETF_NAMES = {
    "510300": "沪深300ETF", "510500": "中证500ETF", "159915": "创业板ETF",
    "512100": "中证1000ETF", "510880": "红利ETF", "518880": "黄金ETF",
    "511010": "国债ETF",
    "512690": "酒ETF", "512010": "医药ETF", "512480": "半导体ETF",
    "512660": "军工ETF", "512000": "券商ETF", "512800": "银行ETF",
    "515030": "新能源车ETF", "515790": "光伏ETF", "512400": "有色ETF",
    "515220": "煤炭ETF", "159928": "消费ETF", "159825": "农业ETF",
    "515000": "科技ETF", "515880": "通信ETF", "512720": "计算机ETF",
    "512200": "房地产ETF", "512170": "医疗ETF", "516010": "游戏ETF",
    "512760": "芯片ETF", "515210": "钢铁ETF", "512580": "环保ETF",
    "159611": "电力ETF", "561360": "石油ETF",
}

# S1 宽基/避险趋势跟随标的池（低相关资产分散趋势来源）
TREND_UNIVERSE = ["510300", "510500", "159915", "512100", "510880", "518880"]

# S2 行业/主题轮动标的池（流动性较好的行业ETF，覆盖主要赛道）
ROTATION_UNIVERSE = [
    "512690", "512010", "512480", "512660", "512000", "512800",
    "515030", "515790", "512400", "515220", "159928", "159825",
    "515000", "515880", "512720", "512200", "512170", "516010",
    "512760", "515210", "512580", "159611",
]


def etf_symbol(code):
    """ETF代码 → 行情源符号：51/56/58开头沪市(sh)，15/16/18开头深市(sz)"""
    code = str(code).zfill(6)
    if code[0] == "5":
        return "sh" + code
    if code[0] == "1":
        return "sz" + code
    raise ValueError(f"非ETF代码: {code}")


# ---------- 通用工具 ----------

def is_first_trade_day_of_month(prev_date, today):
    """月初判定：相邻两个交易日的月份不同（prev 是上一交易日的日期）。

    归一化 YYYYMMDD / YYYY-MM-DD 两种格式。回测（dates[i-1] vs dates[i]）
    与实盘（指数日K倒数两根）共用，不依赖"预知明天是否交易日"。
    """
    def _ym(d):
        d = str(d).replace("-", "")[:6]
        return d[:4], d[4:6]
    return _ym(prev_date) != _ym(today)


def is_rebalance_due(rebalance_days, last_rebalance_date, count):
    """S2调仓到期判定：count = 距上次调仓经过的交易日数（含今日）。

    last_rebalance_date 为 None（账本刚建，从未调仓）视为到期——首期建仓。
    """
    if not last_rebalance_date:
        return True
    return count >= rebalance_days


def bar_on_or_after(bars, date, max_delay=5):
    """停牌处理：bars 中日期 >= date 的第一根K线（顺延成交），顺延超过
    max_delay 个自然日视为长期停牌，返回 None（放弃，现金保留到下期）。

    bars 按 date 升序；date 归一化到 YYYY-MM-DD 前缀比较。
    """
    target = str(date)[:10]
    for b in bars:
        bd = b["date"][:10]
        if bd >= target:
            try:
                d0 = _dt.datetime.strptime(target, "%Y-%m-%d")
                d1 = _dt.datetime.strptime(bd, "%Y-%m-%d")
                if (d1 - d0).days > max_delay:
                    return None
            except ValueError:
                pass  # 日期格式异常时不做停牌惩罚
            return b
    return None


# ---------- S1 指数ETF趋势跟随 ----------

def trend_target(bars_by_code, date, ma_window=20, ma_confirm=1, mom_window=20):
    """趋势跟随信号（纯函数）。

    bars_by_code: {code: [bar]}，bar={date,open,close,high,low}，按date升序
    date: 信号日（用截至该日（含）的K线）
    候选条件：收盘 > MA(ma_window) 且 MA 今日 > MA(ma_confirm) 日前（均线上行确认）
    候选为空 → 返回 None（空仓）；否则返回近 mom_window 日动量最高者。
    返回 (target_code | None, debug: {code: {ma, mom, pass_trend}})
    """
    target = str(date)[:10]
    debug = {}
    best, best_mom = None, None
    for code, bars in bars_by_code.items():
        hist = [b for b in bars if b["date"][:10] <= target]
        if len(hist) < ma_window + ma_confirm + 1:
            continue  # 历史不足（上市晚/停牌），不参与
        closes = [b["close"] for b in hist]
        n = len(closes)
        ma_today = sum(closes[n - ma_window:]) / ma_window
        ma_ref = sum(closes[n - ma_window - ma_confirm:n - ma_confirm]) / ma_window
        mom_ref = closes[n - mom_window - 1] if n > mom_window else closes[0]
        mom = (closes[-1] / mom_ref - 1) * 100 if mom_ref > 0 else 0.0
        ok = closes[-1] > ma_today and ma_today > ma_ref
        debug[code] = {"ma": round(ma_today, 4), "mom": round(mom, 2),
                       "pass_trend": ok}
        if ok and (best is None or mom > best_mom):
            best, best_mom = code, mom
    return best, debug


# ---------- S2 行业ETF轮动 ----------

def rotation_targets(bars_by_code, date, mom_window=20, top_n=2, min_mom=0.0):
    """行业轮动信号（纯函数）。

    按近 mom_window 日动量降序取前 top_n；全部动量 < min_mom 时返回
    (["__CASH__"], debug)（空仓持现金）——轮动池整体走弱时不硬选。
    返回 (targets: [code] 或 ["__CASH__"], debug: {code: {mom}})
    """
    target = str(date)[:10]
    debug, ranked = {}, []
    for code, bars in bars_by_code.items():
        hist = [b for b in bars if b["date"][:10] <= target]
        if len(hist) < mom_window + 1:
            continue  # 历史不足（上市晚），不参与排名
        closes = [b["close"] for b in hist]
        ref = closes[-(mom_window + 1)]
        mom = (closes[-1] / ref - 1) * 100 if ref > 0 else 0.0
        debug[code] = {"mom": round(mom, 2)}
        ranked.append((mom, code))
    ranked.sort(reverse=True)
    if not ranked:
        return ["__CASH__"], debug
    if ranked[0][0] < min_mom:
        return ["__CASH__"], debug  # 最强者都为负：整体退潮，持币
    return [c for _, c in ranked[:top_n]], debug


def gate_reading(sent_data, date_str, promo_min=0.15, avg_gain_min=0.0):
    """S2 情绪闸门读数（影子账本用，实盘温度计口径）。

    sent_data 为 data/sentiment.json 内容（evening 19:10 写入的 D 日收盘情绪：
    promotion_rate=晋级率、sentiment=昨日涨停股今日均涨幅%）。判定与回测
    make_gate 同口径：晋级率<promo_min 或 均涨幅<avg_gain_min 触发。
    返回 (gate_on: bool, desc: str, ok: bool)：
      ok=True  —— 读数完整，gate_on 可信（触发/未触发）
      ok=False —— 数据缺失/过期/部分缺失，gate_on 恒 False（与回测 make_gate
                  缺数据不触发的缺省一致，已披露偏乐观）。**与 gate_on=False 的
                  区别在调用方**：空仓态下读到 ok=False 不许重进——坏数据夜凭
                  "非触发"满仓重进、次日数据恢复又清仓，白付一来回成本
                  （~0.4%/次），系统性污染影子账本的 A/B 对照。
    desc 供日报披露读数与原因。
    """
    if not sent_data or sent_data.get("date") != str(date_str)[:10]:
        return False, "情绪读数缺失/过期（sentiment.json 非今日），今晚不判闸门", False
    promo, avg = sent_data.get("promotion_rate"), sent_data.get("sentiment")
    if promo is None or avg is None:
        return False, "晋级率或均涨幅缺失，今晚不判闸门", False
    if promo < promo_min or avg < avg_gain_min:
        why = []
        if promo < promo_min:
            why.append(f"晋级率{promo * 100:.0f}%<{promo_min * 100:.0f}%")
        if avg < avg_gain_min:
            why.append(f"均涨幅{avg:+.1f}%<{avg_gain_min:+.0f}%")
        return True, "🧊 闸门触发（" + "、".join(why) + "）", True
    return False, f"晋级率{promo * 100:.0f}% / 均涨幅{avg:+.1f}%，闸门未触发", True
