# -*- coding: utf-8 -*-
"""低频策略三的信号纯函数：小市值轮动选股。

实盘选股（filter_smallcaps）用东财快照；历史回测选股（hist_cap_targets）
用"当前流通股本 × 历史收盘价"近似历史市值——送转/增发使股本时变的已知
偏差，报告必须披露（见 backtest_lowfreq 偏差块）。
"""
import datetime as _dt


def _norm_date(d):
    """YYYYMMDD(int/str) 或 YYYY-MM-DD → YYYY-MM-DD"""
    s = str(d).replace("-", "")[:8]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def filter_smallcaps(stocks, today, min_list_days=365, min_price=2.0, top_n=5):
    """小市值实盘选股（纯函数）。

    stocks: datasource 快照（normalize 后，含 name/price/mktcap/list_date）
    today: 今日日期（同上格式），用于次新过滤
    剔除：名称含ST / 北交所代码(4/8/9开头) / 上市 < min_list_days 天 /
          价格 < min_price / 无流通市值数据。
    返回按流通市值升序的前 top_n 只。
    """
    try:
        d_today = _dt.datetime.strptime(_norm_date(today), "%Y-%m-%d")
    except ValueError:
        return []

    def ok(s):
        name = s.get("name") or ""
        if "ST" in name or name.startswith("N"):
            return False
        code = s.get("code") or ""
        if code[0] in ("4", "8", "9"):
            return False
        if s.get("price", 0) < min_price:
            return False
        if not s.get("mktcap"):
            return False
        ld = s.get("list_date")
        if not ld:
            return False  # 上市日期缺失（数据漂移）保守剔除
        try:
            d_list = _dt.datetime.strptime(_norm_date(ld), "%Y-%m-%d")
        except ValueError:
            return False
        if (d_today - d_list).days < min_list_days:
            return False
        return True

    qualified = sorted((s for s in stocks if ok(s)), key=lambda s: s["mktcap"])
    return qualified[:top_n]


def hist_cap_targets(bars_by_code, shares_map, date, top_n=5):
    """小市值回测选股（纯函数）。

    bars_by_code: {code: [bar]}（按date升序）
    shares_map: {code: 当前流通股本（股）}，来自 datasource.fetch_market_caps
    date: 选股日，用当日收盘价算近似市值 = shares × close(date)。
    剔除当日无K线的（停牌买不进）与无股本数据的。
    返回按近似市值升序的前 top_n 只 [(code, cap, close)]。
    """
    target = str(date)[:10]
    rows = []
    for code, bars in bars_by_code.items():
        shares = shares_map.get(code)
        if not shares:
            continue
        bar = None
        for b in bars:
            if b["date"][:10] == target:
                bar = b
                break
            if b["date"][:10] > target:
                break  # 升序排列，超过目标日仍未命中=当日停牌
        if bar is None:
            continue
        cap = shares * bar["close"]
        rows.append((code, cap, bar["close"]))
    rows.sort(key=lambda r: r[1])
    return rows[:top_n]
