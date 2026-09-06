# -*- coding: utf-8 -*-
"""低频策略三的信号纯函数：小市值轮动选股。

实盘选股（filter_smallcaps）用东财快照按流通市值升序拉取过滤；
历史回测选股在 backtest_lowfreq.select_monthly 里（用"当前流通股本 ×
历史收盘价"近似历史市值——送转/增发使股本时变的已知偏差，报告披露）。
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
