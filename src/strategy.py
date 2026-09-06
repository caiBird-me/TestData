# -*- coding: utf-8 -*-
"""策略引擎：晚间动量/涨停选股打分 + 早间竞价确认"""

from datasource import is_limit_up, limit_up_pct

# 连板高度打分曲线：2-3板是主升启动点（最高权重），4板持平，
# 5板以上博弈性质极强（炸板率/特停风险陡增），降权处理
STREAK_SCORE = {1: 15, 2: 30, 3: 28, 4: 20}


def streak_score(streak):
    return STREAK_SCORE.get(streak, 8 if streak >= 5 else 15)


def turnover_bounds(stock, cfg):
    """换手率阈值按涨停幅度分档：20cm/30cm 板块连板股换手天然更高"""
    st = cfg["strategy"]
    lo = st["min_turnover_rate"]
    hi = st["max_turnover_rate"]
    if limit_up_pct(stock) >= 20.0:
        hi = hi * st["wide_turnover_factor"]
    return lo, hi


# ---------- 主线题材识别 ----------

def find_main_themes(limit_ups, boards, concept_map=None, min_count=None):
    """识别主线题材。

    优先：概念板块聚类（一票多属，打板实战口径）——涨停股按所属热门概念计数，count>=3 才算主线
    回退：f100 行业字段聚类（概念接口失败时）——行业口径分散，count>=2 即算
    返回前3个 [{name, count, board_pct}]
    """
    if min_count is None:
        min_count = 3 if concept_map else 2
    theme_count = {}

    if concept_map:
        # 一只涨停股可同时归入多个概念
        for s in limit_ups:
            for concept in concept_map.get(s["code"], []):
                theme_count[concept] = theme_count.get(concept, 0) + 1
    else:
        for s in limit_ups:
            board = (s.get("board") or "").strip()
            if board:
                theme_count[board] = theme_count.get(board, 0) + 1

    if not theme_count:
        return []

    board_pct = {b["name"]: b["pct"] for b in boards}
    themes = [
        {"name": name, "count": cnt, "board_pct": board_pct.get(name, 0)}
        for name, cnt in theme_count.items()
    ]
    themes.sort(key=lambda t: (t["count"], t["board_pct"]), reverse=True)
    return [t for t in themes if t["count"] >= min_count][:3]


def in_themes(stock, theme_names, concept_map=None):
    """股票是否属于任一主线题材（概念模式下一票多属，行业模式下单一归属）"""
    if concept_map:
        return bool(set(concept_map.get(stock["code"], [])) & theme_names)
    return (stock.get("board") or "").strip() in theme_names


# ---------- 封板质量（涨停池数据） ----------

def seal_quality_bonus(seal):
    """封板质量打分：封板越早、越坚决、封单越厚的板，次日溢价越高。

    seal: 涨停池条目 {first_seal, breaks, seal_amount, ltsz}
      - first_seal: 首次封板时间 HHMMSS（092500=竞价秒板，103000前算早封）
      - breaks: 炸板次数（炸板=分歧，回封也算但质量降级）
      - seal_amount/ltsz: 封单额占流通市值比（封单厚度）
    """
    if not seal:
        return 0
    bonus = 0
    fs, breaks = seal.get("first_seal") or 0, seal.get("breaks") or 0
    if fs <= 92559:        # 竞价/开盘瞬间封板（一字或秒板）
        bonus += 10
    elif fs <= 100000:     # 10:00 前封板：强势
        bonus += 8
    elif fs <= 103000:     # 10:30 前：正常偏强
        bonus += 4
    if breaks == 0:        # 全天未炸板：一致性强
        bonus += 5
    elif breaks >= 2:       # 多次炸板：分歧大，次日溢价差
        bonus -= 8
    ltsz = seal.get("ltsz") or 0
    if ltsz and (seal.get("seal_amount") or 0) / ltsz >= 0.03:
        bonus += 5         # 封单额≥流通市值3%：资金锁仓坚决
    return bonus


def _seal_of(code, zt_map):
    s = zt_map.get(code)
    if not s:
        return None
    return {
        "first_seal": s["first_seal"], "breaks": s["breaks"],
        "seal_amount": s["seal_amount"], "ltsz": s["ltsz"],
    }


# ---------- 晚间选股 ----------

def evening_picks(stocks, limit_ups, streak_map, themes, cfg, concept_map=None,
                  zt_pool=None):
    """晚间复盘选股：从主线题材中选候选池。

    A. 连板核心 —— 2板及以上（打分曲线：2-3板最优，5板以上降权）
    B. 主线首板 —— 涨停且题材在主线上
    C. 强势突破 —— 主线内、涨幅>5%、放量（换手分档）、主力净流入加分（非硬条件）

    zt_pool: 东财涨停池（封板质量数据）。封板时间/炸板/封单影响打分——
    早封+零炸板的板次日溢价显著高于烂板（尾盘偷袭板）。
    返回候选列表（带打分与计划），按 score 降序
    """
    st = cfg["strategy"]
    theme_names = {t["name"] for t in themes}
    rk = cfg.get("_risk")  # 由 main 注入
    zt_map = {s["code"]: s for s in (zt_pool or [])}
    picks = []

    def basic_ok(s):
        if rk and not rk.affordable(s["price"]):
            return False
        if "ST" in s["name"] or s["name"][0] == "N":
            return False
        return in_themes(s, theme_names, concept_map)

    # A. 连板核心 + B. 主线首板（一次遍历，streak>=2 走 A）
    for s in limit_ups:
        if not basic_ok(s):
            continue
        streak = streak_map.get(s["code"], 1)
        if streak >= 2:
            score = 40 + streak_score(streak)
            kind = "连板核心"
        else:
            score = 25 + s["pct"]
            kind = "主线首板"
        # 主力净流入是高噪声指标，只作加分不作硬条件
        if s["main_inflow"] > 5e7:
            score += 8
        score += seal_quality_bonus(_seal_of(s["code"], zt_map))
        picks.append(_make_pick(s, kind, streak, score,
                                _theme_of(s, theme_names, concept_map), cfg,
                                zt_map.get(s["code"])))

    # C. 强势突破（非涨停，无封板质量数据）
    for s in stocks:
        if is_limit_up(s):
            continue
        if not (5 <= s["pct"] < limit_up_pct(s) - 0.5):
            continue
        if not basic_ok(s):
            continue
        lo, hi = turnover_bounds(s, cfg)
        if not (lo <= s["turnover"] <= hi):
            continue
        score = 20 + s["pct"]
        if 8 <= s["turnover"] <= 18:
            score += 8
        if s["main_inflow"] > 5e7:
            score += 8
        elif s["main_inflow"] > 2e7:
            score += 4
        picks.append(_make_pick(s, "强势突破", 1, score,
                                _theme_of(s, theme_names, concept_map), cfg))

    picks.sort(key=lambda p: p["score"], reverse=True)
    return picks[:st["max_candidates"]]


def _theme_of(stock, theme_names, concept_map):
    """取该股票所属的主线题材名（多个取第一个）"""
    if concept_map:
        hit = [c for c in concept_map.get(stock["code"], []) if c in theme_names]
        if hit:
            return hit[0]
        return ""
    return (stock.get("board") or "").strip()


def _make_pick(stock, kind, streak, score, board, cfg, seal=None):
    """生成候选对象的统一结构"""
    price = stock["price"]
    # 计划买入价区间：今日收盘价 ±2%
    buy_low = round(price * 0.98, 2)
    buy_high = round(price * 1.02, 2)
    rk = cfg.get("_risk")
    stop = rk.stop_loss_price(buy_high) if rk else round(buy_high * 0.95, 2)
    return {
        "code": stock["code"],
        "name": stock["name"],
        "board": board,
        "kind": kind,           # 连板核心 / 主线首板 / 强势突破
        "streak": streak,       # 连板数
        "score": round(score, 1),
        "price": price,         # 今日收盘价
        "buy_range": [buy_low, buy_high],
        "stop_loss": stop,
        "turnover": stock["turnover"],
        "vol_ratio": stock["vol_ratio"],
        "main_inflow": stock["main_inflow"],
        "pct": stock["pct"],
        "seal": seal,           # 封板质量 {first_seal, breaks, seal_amount, ltsz}
    }


# ---------- 早间确认 ----------

def morning_confirm(candidates, snapshot, cfg, first_minutes=None):
    """早间竞价确认：对晚间候选池做开盘过滤。

    candidates: 昨晚的候选列表
    snapshot: fetch_snapshot_by_codes 的实时行情 {code: stock}
    first_minutes: {code: 09:31分钟K} 成交可行性校验用（可选，无则跳过）
    返回 (最终作战计划, 被拒列表)

    两层过滤：
    1. gap 过滤（低开<-2% / 高开>7%）——唯一的高开/低开约束，与回测
       simulate_event 的 gap 窗口 [-2%, +7%] 完全同口径。曾有 buy_range(±2%)
       校验与之叠加，使实盘实际执行 [-2%,+2%] 而回测验证 [-2%,+7%]——
       两个策略（buy_range 字段保留仅作报告展示的"计划区间"参考）
    2. 成交可行性：09:31 已封涨停的票（秒板/快速封板）标记 unfillable，
       虚拟盘取消成交——"赢家买不进，输家随便买"是系统性正向偏差
    """
    st = cfg["strategy"]
    rk = cfg.get("_risk")
    result, rejected = [], []

    for c in candidates:
        s = snapshot.get(c["code"])
        if not s:
            rejected.append((c, "无实时行情"))
            continue
        if rk:
            ok, reason = rk.gap_filter(s)
            if not ok:
                rejected.append((c, reason))
                continue
            if not rk.affordable(s["price"]):
                rejected.append((c, f"价格{s['price']}元超出仓位可承受范围"))
                continue
        shares, amount = rk.calc_shares(s["price"]) if rk else (0, 0)
        if shares <= 0:
            rejected.append((c, f"价格{s['price']}元太贵，一手需{s['price']*100:.0f}元"))
            continue

        pick = {
            **{k: c[k] for k in ("code", "name", "board", "kind", "streak",
                                  "stop_loss", "buy_range")},
            "open_price": s["price"],
            "gap_pct": round((s["price"] - s["pre_close"]) / s["pre_close"] * 100, 1)
            if s["pre_close"] > 0 else 0,
            "shares": shares,
            "amount": round(amount, 0),
        }

        # 成交可行性：09:31分钟K若整根封死涨停（low=high=涨停价），
        # 开盘瞬间已封板，真人挂单排队未必成交 → 虚拟盘也不买
        fm = (first_minutes or {}).get(c["code"])
        if fm and fm["low"] >= fm["high"] - 0.01 and fm["close"] >= fm["open"]:
            pct = (fm["close"] - s["pre_close"]) / s["pre_close"] * 100 if s["pre_close"] else 0
            if pct >= limit_up_pct(s) - 0.3:
                pick["unfillable"] = True
                pick["unfillable_reason"] = f"开盘即封板（09:31整分钟无成交间隙），排队未必成交"

        pick["action"] = (f"竞价后若延续强势，{s['price']:.2f}元附近买入 {shares} 股"
                          f"（约{amount:.0f}元），止损 {c['stop_loss']} 元")
        result.append(pick)

    # 连板核心优先于首板/突破（同分时高度优先）
    result.sort(key=lambda r: (r.get("streak", 1) >= 2, r.get("streak", 1)), reverse=True)
    return result[:st["final_picks"]], rejected
