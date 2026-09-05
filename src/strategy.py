# -*- coding: utf-8 -*-
"""策略引擎：晚间动量/涨停选股打分 + 早间竞价确认"""

from datasource import limit_up_pct


# ---------- 主线题材识别 ----------

def find_main_themes(limit_ups, boards):
    """涨停股按所属板块聚类，找出涨停家数最多的主线题材。

    limit_ups: 今日涨停股列表
    boards: 板块涨幅榜（用于辅证题材强度）
    返回前3个题材 [{name, count, board_pct}]，count>=2 才算主线
    """
    theme_count = {}
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
    # 主排序：涨停家数；辅排序：板块自身涨幅
    themes.sort(key=lambda t: (t["count"], t["board_pct"]), reverse=True)
    return [t for t in themes if t["count"] >= 2][:3]


# ---------- 晚间选股 ----------

def evening_picks(stocks, limit_ups, streak_map, themes, cfg):
    """晚间复盘选股：从主线题材中选候选池。

    两类候选：
    A. 连板核心 —— 今日2板及以上，主线题材内
    B. 强势突破 —— 主线题材内、非涨停但涨幅>5%、放量（换手/量比）、主力净流入为正

    返回候选列表（带打分与计划），按 score 降序
    """
    st = cfg["strategy"]
    theme_names = {t["name"] for t in themes}
    rk = cfg.get("_risk")  # 由 main 注入
    picks = []

    # A. 连板核心
    for s in limit_ups:
        board = (s.get("board") or "").strip()
        if board not in theme_names:
            continue
        streak = streak_map.get(s["code"], 1)
        if streak < 2:
            continue
        if rk and not rk.affordable(s["price"]):
            continue
        if "ST" in s["name"] or "N" == s["name"][0]:
            continue
        # 连板核心打分：板数为主，量比适中加分
        score = 40 + streak * 15
        vr = s["vol_ratio"]
        if 1 <= vr <= 4:
            score += 10
        picks.append(_make_pick(s, "连板核心", streak, score, board, cfg))

    # B. 强势突破
    for s in stocks:
        board = (s.get("board") or "").strip()
        if board not in theme_names:
            continue
        from datasource import is_limit_up
        if is_limit_up(s):
            continue  # 涨停的已走A类逻辑（首板按打分也放进B类池）
        if not (5 <= s["pct"] < limit_up_pct(s) - 0.5):
            continue
        if rk and not rk.affordable(s["price"]):
            continue
        if "ST" in s["name"] or s["name"][0] == "N":
            continue
        if not (st["min_turnover_rate"] <= s["turnover"] <= st["max_turnover_rate"]):
            continue
        if s["main_inflow"] < st["min_main_inflow"]:
            continue
        # 强势突破打分：涨幅、换手、主力净流入共同决定
        score = 20 + s["pct"]
        if 8 <= s["turnover"] <= 18:
            score += 8
        if s["main_inflow"] > 5e7:
            score += 8
        elif s["main_inflow"] > 2e7:
            score += 4
        picks.append(_make_pick(s, "强势突破", 1, score, board, cfg))

    # 主线首板也纳入候选（涨停但只有1板，题材在主线上）
    for s in limit_ups:
        board = (s.get("board") or "").strip()
        if board not in theme_names:
            continue
        if streak_map.get(s["code"], 1) >= 2:
            continue  # 已在A类
        if rk and not rk.affordable(s["price"]):
            continue
        if "ST" in s["name"] or s["name"][0] == "N":
            continue
        score = 25 + s["pct"]
        if s["main_inflow"] > 5e7:
            score += 8
        picks.append(_make_pick(s, "主线首板", 1, score, board, cfg))

    picks.sort(key=lambda p: p["score"], reverse=True)
    return picks[:st["max_candidates"]]


def _make_pick(stock, kind, streak, score, board, cfg):
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
        "kind": kind,           # 连板核心 / 强势突破 / 主线首板
        "streak": streak,       # 连板数
        "score": round(score, 1),
        "price": price,         # 今日收盘价
        "buy_range": [buy_low, buy_high],
        "stop_loss": stop,
        "turnover": stock["turnover"],
        "vol_ratio": stock["vol_ratio"],
        "main_inflow": stock["main_inflow"],
        "pct": stock["pct"],
    }


# ---------- 早间确认 ----------

def morning_confirm(candidates, snapshot, cfg):
    """早间竞价确认：对晚间候选池做开盘过滤。

    candidates: 昨晚的候选列表
    snapshot: fetch_snapshot_by_codes 的实时行情 {code: stock}
    返回最终作战计划（最多 final_picks 只）
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
        result.append({
            **{k: c[k] for k in ("code", "name", "board", "kind", "streak",
                                  "stop_loss", "buy_range")},
            "open_price": s["price"],
            "gap_pct": round((s["price"] - s["pre_close"]) / s["pre_close"] * 100, 1)
            if s["pre_close"] > 0 else 0,
            "shares": shares,
            "amount": round(amount, 0),
            "action": f"竞价后若延续强势，{s['price']:.2f}元附近买入 {shares} 股（约{amount:.0f}元），"
                      f"止损 {c['stop_loss']} 元",
        })

    result.sort(key=lambda r: r.get("streak", 1) * 10, reverse=True)
    return result[:st["final_picks"]], rejected
