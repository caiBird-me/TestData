# -*- coding: utf-8 -*-
"""纯函数单元测试：仓位计算、竞价过滤、涨停识别、连板计算、结算规则"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.chdir(Path(__file__).resolve().parent.parent)

from datasource import calc_streak_codes, code_to_secid, is_limit_up, limit_up_pct
from risk import RiskRules
from strategy import (STREAK_SCORE, find_main_themes, in_themes,
                      streak_score, turnover_bounds)

CFG = {
    "capital": {"total": 3000},
    "risk": {
        "max_position_pct": 0.50, "max_stocks": 2, "stop_loss_pct": -0.05,
        "max_gap_up_pct": 0.07, "min_gap_pct": -0.02, "consecutive_loss_pause": 3,
        "min_price": 2.0, "max_price": 30.0,
    },
    "strategy": {
        "min_turnover_rate": 5.0, "max_turnover_rate": 25.0,
        "wide_turnover_factor": 1.6,
    },
}


def make_rules():
    return RiskRules(CFG)


def make_stock(code, pct, price, high, pre_close=10.0, **kw):
    s = {"code": code, "name": kw.get("name", "测试股"), "pct": pct, "price": price,
         "high": high, "low": price, "open": price, "pre_close": pre_close,
         "turnover": kw.get("turnover", 10.0), "vol_ratio": kw.get("vol_ratio", 2.0),
         "mktcap": 1e9, "main_inflow": kw.get("main_inflow", 1e7), "board": kw.get("board", "")}
    return s


class TestCalcShares(unittest.TestCase):
    def test_cheap_stock(self):
        """5元股：1500元上限能买300股（3手）"""
        shares, amount = make_rules().calc_shares(5.0)
        self.assertEqual((shares, amount), (300, 1500.0))

    def test_boundary_15yuan(self):
        """15元股：一手正好1500元"""
        shares, amount = make_rules().calc_shares(15.0)
        self.assertEqual((shares, amount), (100, 1500.0))

    def test_expensive_stock_zero(self):
        """30元股：一手3000元超仓位上限，买不了"""
        shares, _ = make_rules().calc_shares(30.0)
        self.assertEqual(shares, 0)

    def test_affordable_respects_position_cap(self):
        r = make_rules()
        self.assertTrue(r.affordable(10.0))
        self.assertFalse(r.affordable(16.0))  # 一手1600 > 1500上限
        self.assertFalse(r.affordable(1.0))   # 低于min_price


class TestGapFilter(unittest.TestCase):
    def test_normal_open(self):
        ok, _ = make_rules().gap_filter(make_stock("600000", 1, 10.2, 10.5))
        self.assertTrue(ok)

    def test_gap_down_rejected(self):
        ok, reason = make_rules().gap_filter(make_stock("600000", -3, 9.7, 9.8))
        self.assertFalse(ok)
        self.assertIn("低开", reason)

    def test_gap_up_too_high(self):
        ok, reason = make_rules().gap_filter(make_stock("600000", 8, 10.8, 10.9))
        self.assertFalse(ok)
        self.assertIn("高开", reason)

    def test_boundary_minus2(self):
        """刚好-2%不触发（条件是 < -2%）"""
        ok, _ = make_rules().gap_filter(make_stock("600000", -2, 9.8, 9.9))
        self.assertTrue(ok)


class TestEtfSymbol(unittest.TestCase):
    """ETF代码 → 行情源符号"""

    def test_symbol(self):
        from etf import etf_symbol
        self.assertEqual(etf_symbol("510300"), "sh510300")
        self.assertEqual(etf_symbol("518880"), "sh518880")
        self.assertEqual(etf_symbol("159915"), "sz159915")
        with self.assertRaises(ValueError):
            etf_symbol("600000")


def _bar(date, close, open_=None, high=None, low=None):
    return {"date": date, "open": open_ if open_ is not None else close,
            "close": close, "high": high if high is not None else close,
            "low": low if low is not None else close}


class TestTrendSignal(unittest.TestCase):
    """S1趋势跟随：价>MA且MA上行→动量最高者；破MA/MA走平→空仓"""

    def _bars(self, closes, start="2026-08-01"):
        import datetime as dt
        d0 = dt.date.fromisoformat(start)
        return [_bar((d0 + dt.timedelta(days=i)).isoformat(), c)
                for i, c in enumerate(closes)]

    def test_uptrend_picks_best_momentum(self):
        from etf import trend_target
        bars = {
            # 涨得快的（+2%/日）和涨得慢的（+0.5%/日）都过趋势过滤
            "510300": self._bars([10 * (1.005 ** i) for i in range(30)]),
            "518880": self._bars([10 * (1.02 ** i) for i in range(30)]),
        }
        target, debug = trend_target(bars, "2026-08-30", ma_window=5,
                                     ma_confirm=1, mom_window=10)
        self.assertEqual(target, "518880")
        self.assertTrue(debug["510300"]["pass_trend"])

    def test_below_ma_flat(self):
        """价格跌破MA/趋势走平：全部候选失效→空仓"""
        from etf import trend_target
        bars = {"510300": self._bars([10 - i * 0.05 for i in range(30)])}  # 阴跌
        target, _ = trend_target(bars, "2026-08-30", ma_window=5, mom_window=10)
        self.assertIsNone(target)

    def test_price_above_ma_but_ma_falling(self):
        """价>MA但MA下行（反弹不到位）：空仓保护"""
        from etf import trend_target
        # 先深跌再小反弹，MA(5)仍在下行
        closes = [10, 9, 8, 7, 6, 5.5, 5.4, 5.6, 5.5, 5.6]
        bars = {"510300": self._bars(closes)}
        target, _ = trend_target(bars, closes and "2026-08-10",
                                 ma_window=5, mom_window=3)
        self.assertIsNone(target)

    def test_insufficient_history_skipped(self):
        from etf import trend_target
        bars = {"510300": self._bars([10, 10.1, 10.2])}  # 历史不足
        target, debug = trend_target(bars, "2026-08-03", ma_window=5, mom_window=5)
        self.assertIsNone(target)
        self.assertEqual(debug, {})


class TestRotationTargets(unittest.TestCase):
    """S2行业轮动：动量前2；全负→空仓"""

    def _bars_of(self, code, closes):
        import datetime as dt
        d0 = dt.date(2026, 8, 1)
        return code, [_bar((d0 + dt.timedelta(days=i)).isoformat(), c)
                      for i, c in enumerate(closes)]

    def test_top2_by_momentum(self):
        from etf import rotation_targets
        bars = dict([
            self._bars_of("512690", [10 * (1.03 ** i) for i in range(25)]),
            self._bars_of("512010", [10 * (1.02 ** i) for i in range(25)]),
            self._bars_of("512000", [10 * (1.01 ** i) for i in range(25)]),
        ])
        targets, debug = rotation_targets(bars, "2026-08-25", mom_window=20, top_n=2)
        self.assertEqual(targets, ["512690", "512010"])

    def test_all_negative_cash(self):
        from etf import rotation_targets
        bars = dict([
            self._bars_of("512690", [10 * (0.98 ** i) for i in range(25)]),
            self._bars_of("512010", [10 * (0.97 ** i) for i in range(25)]),
        ])
        targets, _ = rotation_targets(bars, "2026-08-25", mom_window=20, top_n=2)
        self.assertEqual(targets, ["__CASH__"])

    def test_insufficient_history_not_ranked(self):
        from etf import rotation_targets
        bars = dict([self._bars_of("512690", [10, 10.1])])  # 历史不足
        targets, _ = rotation_targets(bars, "2026-08-02", mom_window=20)
        self.assertEqual(targets, ["__CASH__"])


class TestMonthFirstTradeDay(unittest.TestCase):
    """月初判定：相邻交易日月份不同"""

    def test_month_boundary(self):
        from etf import is_first_trade_day_of_month
        self.assertTrue(is_first_trade_day_of_month("20260831", "20260901"))
        self.assertFalse(is_first_trade_day_of_month("20260901", "20260902"))
        self.assertTrue(is_first_trade_day_of_month("20261231", "20270105"))  # 跨年
        self.assertTrue(is_first_trade_day_of_month("2026-02-27", "2026-03-02"))
        self.assertFalse(is_first_trade_day_of_month("2026-02-26", "2026-02-27"))


class TestRebalanceDue(unittest.TestCase):
    def test_due(self):
        from etf import is_rebalance_due
        self.assertTrue(is_rebalance_due(20, None, 0))          # 从未调仓
        self.assertFalse(is_rebalance_due(20, "20260901", 19))
        self.assertTrue(is_rebalance_due(20, "20260901", 20))
        self.assertTrue(is_rebalance_due(20, "20260901", 21))


class TestAllocateEqual(unittest.TestCase):
    """等权分配整百取整：一手超槽位×1.5的跳过"""

    def test_normal(self):
        from etf import allocate_equal
        # 1000元2槽=500/槽；1元ETF一手100元→5手=500股；4元ETF一手400元→1手
        shares, skipped = allocate_equal(1000, {"A": 1.0, "B": 4.0}, 2)
        self.assertEqual(shares, {"A": 500, "B": 100})
        self.assertEqual(skipped, [])

    def test_skip_expensive_lot(self):
        """一手金额超槽位1.5倍：跳过防单票失衡"""
        from etf import allocate_equal
        shares, skipped = allocate_equal(1000, {"A": 1.0, "B": 8.0}, 2)
        self.assertEqual(shares, {"A": 500})
        self.assertEqual(skipped, ["B"])


class TestBarOnOrAfter(unittest.TestCase):
    """停牌顺延≤5自然日，超过放弃"""

    def test_exact_day(self):
        from etf import bar_on_or_after
        bars = [_bar("2026-09-01", 1), _bar("2026-09-02", 1)]
        self.assertEqual(bar_on_or_after(bars, "2026-09-02")["date"], "2026-09-02")

    def test_suspend_delayed(self):
        from etf import bar_on_or_after
        bars = [_bar("2026-09-01", 1), _bar("2026-09-04", 1)]  # 停2天
        self.assertEqual(bar_on_or_after(bars, "2026-09-02")["date"], "2026-09-04")

    def test_long_suspend_none(self):
        from etf import bar_on_or_after
        bars = [_bar("2026-09-01", 1), _bar("2026-09-20", 1)]  # 停超5天
        self.assertIsNone(bar_on_or_after(bars, "2026-09-02"))

    def test_no_later_bar(self):
        from etf import bar_on_or_after
        self.assertIsNone(bar_on_or_after([_bar("2026-09-01", 1)], "2026-09-02"))


class TestSmallcapFilter(unittest.TestCase):
    """小市值过滤：ST/北交所/次新/低价/无市值剔除，市值升序取5"""

    def _s(self, code, name, cap, price=5.0, ld=20200101):
        return {"code": code, "name": name, "price": price, "mktcap": cap,
                "list_date": ld}

    def test_filters_and_order(self):
        from smallcap import filter_smallcaps
        stocks = [
            self._s("600001", "正常A", 8e8),
            self._s("600002", "ST退", 5e8),          # ST剔除
            self._s("600003", "次新股", 6e8, ld=20260801),  # 次新剔除
            self._s("830001", "北交所", 3e8),         # 北交所剔除
            self._s("600004", "低价股", 7e8, price=1.5),   # 低价剔除
            self._s("600005", "正常B", 9e8),
            self._s("600006", "正常C", 1e9),
        ]
        picks = filter_smallcaps(stocks, "2026-09-06", top_n=5)
        self.assertEqual([p["code"] for p in picks], ["600001", "600005", "600006"])

    def test_top_n(self):
        from smallcap import filter_smallcaps
        stocks = [self._s(f"60000{i}", f"股{i}", (i + 1) * 1e8) for i in range(7)]
        picks = filter_smallcaps(stocks, "2026-09-06", top_n=5)
        self.assertEqual(len(picks), 5)
        self.assertEqual(picks[0]["code"], "600000")  # 市值最小


class TestHistCapTargets(unittest.TestCase):
    """回测选股：当前股本×历史价近似市值排名"""

    def test_ranking(self):
        from smallcap import hist_cap_targets
        bars = {
            "600001": [_bar("2026-09-01", 10), _bar("2026-09-02", 10)],
            "600002": [_bar("2026-09-01", 5), _bar("2026-09-02", 5)],
            "600003": [_bar("2026-09-01", 20)],  # 09-02停牌
        }
        shares = {"600001": 1e8, "600002": 2e8, "600003": 1e8}
        picks = hist_cap_targets(bars, shares, "2026-09-02", top_n=2)
        # 市值: 600001=10亿, 600002=10亿(并列), 600003停牌剔除
        self.assertEqual(len(picks), 2)
        codes = {p[0] for p in picks}
        self.assertEqual(codes, {"600001", "600002"})

    def test_missing_shares_excluded(self):
        from smallcap import hist_cap_targets
        bars = {"600001": [_bar("2026-09-01", 10)]}
        picks = hist_cap_targets(bars, {}, "2026-09-01")
        self.assertEqual(picks, [])


class TestPortfolioBooks(unittest.TestCase):
    """多账本：book参数走 data/books/，默认构造保持原打板路径（向后兼容）"""

    def test_book_paths(self):
        from portfolio import Portfolio
        p = Portfolio(1000, book="trend")
        self.assertEqual(p.path.name, "trend.json")
        self.assertTrue(str(p.path).endswith("books\\trend.json")
                        or str(p.path).endswith("books/trend.json"))
        self.assertEqual(p.signals_path.name, "trend_signals.json")

    def test_default_paths_unchanged(self):
        from portfolio import Portfolio
        p = Portfolio(3000)
        self.assertEqual(p.path.name, "portfolio.json")
        self.assertEqual(p.signals_path.name, "signals.json")

    def test_lowfreq_extension_fields_defaulted(self):
        from portfolio import Portfolio
        p = Portfolio(1000, book="rotation")
        self.assertEqual(p.data["pending_trades"], [])
        self.assertEqual(p.data["nav_history"], [])
        self.assertEqual(p.data["rebalance_state"], {})


class TestEtfCodeMapping(unittest.TestCase):
    """ETF secid映射：5开头沪ETF→1.xxx，1开头深ETF→0.xxx；股票分支回归"""

    def test_sh_etf(self):
        self.assertEqual(code_to_secid("510300"), "1.510300")
        self.assertEqual(code_to_secid("518880"), "1.518880")
        self.assertEqual(code_to_secid("512690"), "1.512690")

    def test_sz_etf(self):
        self.assertEqual(code_to_secid("159915"), "0.159915")
        self.assertEqual(code_to_secid("159928"), "0.159928")

    def test_stock_branches_unchanged(self):
        self.assertEqual(code_to_secid("600000"), "1.600000")
        self.assertEqual(code_to_secid("000001"), "0.000001")
        self.assertEqual(code_to_secid("300750"), "0.300750")
        self.assertEqual(code_to_secid("830001"), "0.830001")


class TestLimitUp(unittest.TestCase):
    def test_main_board_10pct(self):
        self.assertEqual(limit_up_pct(make_stock("600000", 10, 11, 11)), 10.0)
        self.assertTrue(is_limit_up(make_stock("600000", 10.05, 11.0, 11.0)))

    def test_chinext_20pct(self):
        self.assertEqual(limit_up_pct(make_stock("300001", 20, 12, 12)), 20.0)
        self.assertTrue(is_limit_up(make_stock("300001", 19.98, 12.0, 12.0)))
        # 创业板10%不算涨停
        self.assertFalse(is_limit_up(make_stock("300001", 10.0, 11.0, 11.5)))

    def test_star_20pct(self):
        self.assertEqual(limit_up_pct(make_stock("688001", 20, 24, 24)), 20.0)

    def test_bj_30pct(self):
        self.assertEqual(limit_up_pct(make_stock("830001", 30, 13, 13)), 30.0)

    def test_not_sealed(self):
        """涨幅够但收盘没封住（收盘<最高）不算"""
        self.assertFalse(is_limit_up(make_stock("600000", 10.1, 10.9, 11.0)))

    def test_zero_or_negative(self):
        self.assertFalse(is_limit_up(make_stock("600000", -5, 9.5, 9.6)))
        self.assertFalse(is_limit_up(make_stock("600000", 0, 10.0, 10.1)))


class TestCalcStreak(unittest.TestCase):
    def test_two_day_streak(self):
        today = [make_stock("600001", 10, 11, 11)]
        prev = {"20260904": {"600001"}, "20260903": {"600001"}}
        self.assertEqual(calc_streak_codes(today, prev)["600001"], 3)

    def test_broken_streak(self):
        """中间断一天不算连板"""
        today = [make_stock("600001", 10, 11, 11)]
        prev = {"20260904": {"600002"}, "20260903": {"600001"}}
        self.assertEqual(calc_streak_codes(today, prev)["600001"], 1)

    def test_missing_archive_inflates(self):
        """归档缺失的已知缺陷：周一、周三涨停被算成2连板（由verify_streaks用K线修正）"""
        today = [make_stock("600001", 10, 11, 11)]
        prev = {"20260902": {"600001"}}  # 周二，跳过了周一
        self.assertEqual(calc_streak_codes(today, prev)["600001"], 2)


class TestStopLoss(unittest.TestCase):
    def test_stop_loss_price(self):
        self.assertEqual(make_rules().stop_loss_price(10.0), 9.5)

    def test_triggered(self):
        hit, desc = make_rules().check_stop_loss(
            {"buy_price": 10.0, "current_price": 9.4})
        self.assertTrue(hit)

    def test_not_triggered(self):
        hit, _ = make_rules().check_stop_loss(
            {"buy_price": 10.0, "current_price": 9.8})
        self.assertFalse(hit)


class TestNeedPause(unittest.TestCase):
    def test_three_losses_pause(self):
        signals = [{"status": "settled", "pnl_pct": -1, "settle_date": f"2026-09-0{i}"}
                   for i in (1, 2, 3)]
        pause, desc = make_rules().need_pause(signals)
        self.assertTrue(pause)

    def test_two_losses_no_pause(self):
        signals = [{"status": "settled", "pnl_pct": -1, "settle_date": "2026-09-01"},
                   {"status": "settled", "pnl_pct": -1, "settle_date": "2026-09-02"},
                   {"status": "settled", "pnl_pct": 2, "settle_date": "2026-09-03"}]
        pause, _ = make_rules().need_pause(signals)
        self.assertFalse(pause)

    def test_win_breaks_streak(self):
        """最近一笔（日期最大）盈利则不熔断"""
        signals = [{"status": "settled", "pnl_pct": -1, "settle_date": "2026-09-01"},
                   {"status": "settled", "pnl_pct": -1, "settle_date": "2026-09-02"},
                   {"status": "settled", "pnl_pct": -1, "settle_date": "2026-09-03"},
                   {"status": "settled", "pnl_pct": 5, "settle_date": "2026-09-04"}]
        pause, _ = make_rules().need_pause(signals)
        self.assertFalse(pause)

    def test_pending_ignored(self):
        signals = [{"status": "pending", "pnl_pct": None}]
        pause, _ = make_rules().need_pause(signals)
        self.assertFalse(pause)


class TestStreakScore(unittest.TestCase):
    def test_curve_peaks_at_2_3(self):
        """2-3板是主升启动点，权重最高"""
        self.assertEqual(streak_score(2), STREAK_SCORE[2])
        self.assertTrue(streak_score(2) >= streak_score(4))
        self.assertTrue(streak_score(3) >= streak_score(5))
        self.assertTrue(streak_score(5) < streak_score(2))

    def test_high_streak_downweighted(self):
        """5板以上博弈性质强，降权"""
        self.assertEqual(streak_score(6), 8)


class TestTurnoverBounds(unittest.TestCase):
    def test_main_board(self):
        lo, hi = turnover_bounds(make_stock("600000", 5, 10, 10.5), CFG)
        self.assertEqual((lo, hi), (5.0, 25.0))

    def test_wide_for_20cm(self):
        """创业板/科创板换手上限放大"""
        lo, hi = turnover_bounds(make_stock("300001", 15, 12, 12), CFG)
        self.assertEqual((lo, hi), (5.0, 40.0))


class TestConceptThemes(unittest.TestCase):
    def setUp(self):
        self.lu = [make_stock("600001", 10, 11, 11), make_stock("600002", 10, 21, 21),
                   make_stock("600003", 10, 31, 31)]
        self.boards = [{"name": "机器人", "pct": 3.0}]

    def test_multi_membership(self):
        """一票多属：同一票计入多个概念，count>=3 才算主线"""
        cmap = {"600001": ["机器人", "减速器"], "600002": ["机器人"],
                "600003": ["机器人", "AI算力"]}
        themes = find_main_themes(self.lu, self.boards, cmap)
        self.assertEqual(len(themes), 1)
        self.assertEqual(themes[0]["name"], "机器人")
        self.assertEqual(themes[0]["count"], 3)

    def test_min_count_fallback(self):
        """概念模式下 count<3 不成主线"""
        cmap = {"600001": ["机器人"], "600002": ["机器人"]}
        themes = find_main_themes(self.lu, self.boards, cmap)
        self.assertEqual(themes, [])

    def test_industry_fallback(self):
        """无概念映射时回退行业字段（count>=2 即可）"""
        for s, b in zip(self.lu, ["酿酒", "酿酒"]):
            s["board"] = b
        themes = find_main_themes(self.lu, self.boards, None)
        self.assertEqual(themes[0]["count"], 2)

    def test_in_themes(self):
        cmap = {"600001": ["机器人"]}
        names = {"机器人"}
        self.assertTrue(in_themes(make_stock("600001", 5, 10, 10), names, cmap))
        self.assertFalse(in_themes(make_stock("600099", 5, 10, 10), names, cmap))


class TestCosts(unittest.TestCase):
    """交易成本建模：3k资金的最低佣金是重税，必须计入虚拟盘"""

    def _pf(self):
        from portfolio import Portfolio
        return Portfolio(3000)

    def test_round_trip_costs(self):
        """1000元仓位一轮买卖：买佣5 + 卖佣5 + 印花税0.5 = 总成本10.5元"""
        p = self._pf()
        p.buy("600000", "测试", 10.0, 100, date_str="2026-09-01")
        # 买入：1000元 + 5元佣金，现金 3000-1005=1995
        self.assertAlmostEqual(p.data["cash"], 1995.0)
        pnl, pnl_pct = p.sell("600000", 10.0)  # 平价卖出
        # 卖出净额 = 1000 - (5 + 0.5) = 994.5；盈亏 = 994.5 - 1005 = -10.5
        self.assertAlmostEqual(pnl, -10.5)
        self.assertAlmostEqual(p.data["total_costs"], 10.5)
        self.assertAlmostEqual(p.data["cash"], 1995.0 + 994.5)

    def test_cost_drag_on_small_position(self):
        """5.6元股100股（560元仓位）成本拖累：平价买卖亏1.9%"""
        p = self._pf()
        p.buy("600975", "测试", 5.6, 100, date_str="2026-09-01")
        pnl, pnl_pct = p.sell("600975", 5.6)
        # 成本10.53元 / 560元仓位 ≈ 1.9%
        self.assertLess(pnl_pct, -1.8)
        self.assertGreater(pnl_pct, -2.0)

    def test_gross_profit_can_be_eaten(self):
        """涨1%但仓位只有500元时，成本吞掉全部利润"""
        p = self._pf()
        p.buy("600001", "测试", 5.0, 100, date_str="2026-09-01")
        pnl, pnl_pct = p.sell("600001", 5.05)  # +1%
        self.assertLess(pnl, 0)  # 500仓位涨1%=5元利润 < 10.25成本


class TestSealQuality(unittest.TestCase):
    """封板质量打分：早封/零炸/厚封单加分，烂板降分"""

    def _seal(self, fs=100000, breaks=0, amount=5e7, ltsz=1e9):
        return {"first_seal": fs, "breaks": breaks, "seal_amount": amount, "ltsz": ltsz}

    def test_instant_seal_best(self):
        from strategy import seal_quality_bonus
        b = seal_quality_bonus(self._seal(fs=92500))
        self.assertGreaterEqual(b, 15)  # 秒板+零炸+厚封单

    def test_late_board_worse(self):
        from strategy import seal_quality_bonus
        self.assertGreater(seal_quality_bonus(self._seal(fs=100000)),
                           seal_quality_bonus(self._seal(fs=143000)))

    def test_breaks_penalty(self):
        from strategy import seal_quality_bonus
        self.assertGreater(seal_quality_bonus(self._seal(breaks=0)),
                           seal_quality_bonus(self._seal(breaks=3)))

    def test_thin_seal_no_bonus(self):
        from strategy import seal_quality_bonus
        # 封单占流通市值<3% 无厚度加分
        self.assertEqual(seal_quality_bonus(self._seal(amount=1e6)), 13)  # 8+5, 无厚度分

    def test_none(self):
        from strategy import seal_quality_bonus
        self.assertEqual(seal_quality_bonus(None), 0)


class TestMorningConfirm(unittest.TestCase):
    """morning_confirm 两层过滤：gap（与回测同口径）/ 成交可行性"""

    def _cfg(self):
        cfg = {**CFG, "strategy": {**CFG["strategy"], "final_picks": 2,
                                   "min_turnover_rate": 5.0, "max_turnover_rate": 25.0,
                                   "wide_turnover_factor": 1.6}}
        cfg["_risk"] = make_rules()  # main 里由 cfg["_risk"] 注入
        return cfg

    def _cand(self, price=10.0):
        return {"code": "600001", "name": "测试", "board": "机器人", "kind": "主线首板",
                "streak": 1, "stop_loss": 9.5, "buy_range": [9.8, 10.2]}

    def test_gap_5pct_passes(self):
        """高开+5%必须通过：gap过滤是唯一高开约束（与回测[-2%,+7%]同口径）。
        曾有buy_range(±2%)叠加把实盘过滤压成[-2%,+2%]而回测验证[-2%,+7%]——回归测试防止口径再分叉"""
        from strategy import morning_confirm
        snap = {"600001": make_stock("600001", 5, 10.5, 10.6)}
        plan, rejected = morning_confirm([self._cand()], snap, self._cfg())
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["open_price"], 10.5)
        self.assertEqual(rejected, [])

    def test_gap_75pct_rejected(self):
        """高开+7.5%：被gap过滤拒绝（>7%上限）"""
        from strategy import morning_confirm
        snap = {"600001": make_stock("600001", 7.5, 10.75, 10.8)}
        plan, rejected = morning_confirm([self._cand()], snap, self._cfg())
        self.assertEqual(plan, [])
        self.assertEqual(len(rejected), 1)

    def test_normal_open_passes(self):
        from strategy import morning_confirm
        snap = {"600001": make_stock("600001", 1, 10.1, 10.2)}
        plan, _ = morning_confirm([self._cand()], snap, self._cfg())
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["open_price"], 10.1)
        # buy_range字段保留在计划里（报告展示"计划区间"参考），不再参与过滤
        self.assertEqual(plan[0]["buy_range"], [9.8, 10.2])

    def test_unfillable_opening_limit(self):
        """09:31整分钟封死涨停（low=high）：标记unfillable，虚拟盘取消"""
        from strategy import morning_confirm
        snap = {"600001": make_stock("600001", 3, 10.3, 10.3)}
        fm = {"600001": {"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0}}
        plan, _ = morning_confirm([self._cand()], snap, self._cfg(), fm)
        self.assertTrue(plan[0].get("unfillable"))

    def test_fillable_with_intraday_range(self):
        """09:31有成交间隙（low<high）：正常可买"""
        from strategy import morning_confirm
        snap = {"600001": make_stock("600001", 3, 10.3, 10.4)}
        fm = {"600001": {"open": 10.3, "high": 10.5, "low": 10.2, "close": 10.4}}
        plan, _ = morning_confirm([self._cand()], snap, self._cfg(), fm)
        self.assertFalse(plan[0].get("unfillable", False))


class TestDecideSettlement(unittest.TestCase):
    """持仓卖出决策（收盘结算与14:45尾卖提醒共用的同一套规则）"""

    def _pos(self, buy_date="2026-09-02", stop_loss=9.5, buy_price=10.0):
        return {"code": "600001", "name": "测试", "buy_price": buy_price,
                "buy_date": buy_date, "stop_loss": stop_loss}

    def test_stop_loss_sells(self):
        from portfolio import decide_settlement
        d = decide_settlement(self._pos(), 9.4, False, "2026-09-03")
        self.assertEqual(d["action"], "sell")
        self.assertEqual(d["sell_reason"], "止损")

    def test_limit_up_holds(self):
        from portfolio import decide_settlement
        d = decide_settlement(self._pos(), 10.5, True, "2026-09-03")
        self.assertEqual(d["action"], "hold")
        self.assertIn("涨停", d["reason"])

    def test_t1_expiry_sells(self):
        from portfolio import decide_settlement
        d = decide_settlement(self._pos(), 10.2, False, "2026-09-03")
        self.assertEqual(d["action"], "sell")
        self.assertEqual(d["sell_reason"], "T+1尾盘卖出")

    def test_same_day_holds(self):
        """当日买入T+1不可卖：即使破止损价也只能持有观察（次日早盘竞价处理）"""
        from portfolio import decide_settlement
        d = decide_settlement(self._pos(buy_date="2026-09-03"), 9.4, False, "2026-09-03")
        self.assertEqual(d["action"], "hold")
        self.assertIn("T+1", d["reason"])

    def test_no_stop_loss_field(self):
        """无止损价的持仓不触发止损卖出"""
        from portfolio import decide_settlement
        d = decide_settlement(self._pos(stop_loss=0), 9.0, False, "2026-09-03")
        self.assertEqual(d["action"], "sell")
        self.assertEqual(d["sell_reason"], "T+1尾盘卖出")


class TestBacktest(unittest.TestCase):
    """回测纯函数：涨停判定、事件模拟（T+1/止损/续持/gap过滤/成本）"""

    def _bars(self):
        """构造一段合成日K：D日涨停 → D+1高开3% → D+2平收"""
        return [
            {"date": "2026-09-01", "open": 9.5, "close": 10.0, "high": 10.0,
             "low": 9.4, "volume": 100, "pct": 9.9},
            {"date": "2026-09-02", "open": 10.3, "close": 10.4, "high": 10.5,
             "low": 10.2, "volume": 100, "pct": 4.0},
            {"date": "2026-09-03", "open": 10.4, "close": 10.4, "high": 10.6,
             "low": 10.3, "volume": 100, "pct": 0.0},
            {"date": "2026-09-04", "open": 10.4, "close": 10.2, "high": 10.7,
             "low": 10.0, "volume": 100, "pct": -1.9},
        ]

    def _costs(self):
        return {"commission_rate": 0.00025, "commission_min": 5.0, "stamp_duty": 0.0005}

    def test_limit_threshold(self):
        from backtest import limit_threshold
        self.assertEqual(limit_threshold("600000"), 9.85)
        self.assertEqual(limit_threshold("300001"), 19.85)
        self.assertEqual(limit_threshold("688001"), 19.85)
        self.assertEqual(limit_threshold("830001"), 29.7)

    def test_bj_code_filter(self):
        """北交所代码识别（baostock/腾讯均无北证K线，回测剔除）"""
        from backtest import is_bj_code
        self.assertTrue(is_bj_code("430047"))
        self.assertTrue(is_bj_code("832566"))
        self.assertTrue(is_bj_code("920008"))
        self.assertFalse(is_bj_code("600000"))
        self.assertFalse(is_bj_code("000001"))
        self.assertFalse(is_bj_code("300750"))

    def test_bars_with_pct(self):
        """腾讯原始K线 → 附加相邻日涨跌幅（除权日失真是已知披露偏差）"""
        from backtest import bars_with_pct
        # [date, open, close, high, low]（腾讯字段序：close是第2列）
        raw = [["2026-09-01", 9.5, 10.0, 10.0, 9.4],
               ["2026-09-02", 10.3, 10.4, 10.5, 10.2],
               ["2026-09-03", 10.4, 10.4, 10.6, 10.3]]
        bars = bars_with_pct(raw)
        self.assertEqual(bars[0]["pct"], 0.0)  # 首日无前收
        self.assertAlmostEqual(bars[1]["pct"], 4.0, places=2)
        self.assertAlmostEqual(bars[2]["pct"], 0.0, places=2)
        self.assertEqual(bars[1]["close"], 10.4)
        self.assertEqual(bars[1]["high"], 10.5)

    def test_exdiv_miss_detector(self):
        """收盘=最高但涨幅落在涨停-3%~-0.15%：除权日漏检近似口径（上界估计）"""
        from backtest import is_probably_exdiv_miss
        self.assertFalse(is_probably_exdiv_miss(
            {"pct": 9.9, "close": 10.0, "high": 10.0}, "600000"))  # 真涨停
        self.assertFalse(is_probably_exdiv_miss(
            {"pct": 5.0, "close": 10.0, "high": 10.0}, "600000"))  # 涨幅太低
        self.assertFalse(is_probably_exdiv_miss(
            {"pct": 7.0, "close": 10.0, "high": 10.5}, "600000"))  # 没收在最高
        self.assertTrue(is_probably_exdiv_miss(
            {"pct": 8.0, "close": 10.0, "high": 10.0}, "600000"))  # 疑似除权涨停

    def test_scan_events(self):
        from backtest import scan_stock_events
        results, exdiv, n_detected = scan_stock_events(
            "600000", self._bars(), 2026, 2026)
        self.assertEqual(exdiv, 0)  # 无除权嫌疑日
        self.assertEqual(n_detected, 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["streak"], 1)  # 首板
        self.assertEqual(results[0]["date"], "2026-09-02")  # 买入日=D+1

    def test_last_bar_limit_up_no_crash(self):
        """末根K线涨停：无D+1可模拟——计入事件池但不崩（云端实测踩过的坑）"""
        from backtest import scan_stock_events
        bars = [
            {"date": "2026-09-03", "open": 9.0, "close": 9.5, "high": 9.6,
             "low": 8.9, "pct": 5.5},
            {"date": "2026-09-04", "open": 9.8, "close": 10.45, "high": 10.45,
             "low": 9.7, "pct": 10.0},
        ]
        results, exdiv, n_detected = scan_stock_events("600000", bars, 2026, 2026)
        self.assertEqual(n_detected, 1)
        self.assertEqual(results, [])

    def test_streak_counting(self):
        from backtest import scan_stock_events
        # 3连板（开盘价控制gap在-2%~+7%内，模拟才能成交）
        bars = [
            {"date": "2026-09-01", "open": 10.0, "close": 11.0, "high": 11.0,
             "low": 9.9, "volume": 1, "pct": 10.0},
            {"date": "2026-09-02", "open": 10.9, "close": 12.1, "high": 12.1,
             "low": 10.8, "volume": 1, "pct": 10.0},
            {"date": "2026-09-03", "open": 12.2, "close": 13.31, "high": 13.31,
             "low": 12.1, "volume": 1, "pct": 10.0},
            {"date": "2026-09-04", "open": 13.5, "close": 13.4, "high": 13.6,
             "low": 13.2, "volume": 1, "pct": 1.2},
            {"date": "2026-09-07", "open": 13.3, "close": 13.0, "high": 13.4,
             "low": 12.9, "volume": 1, "pct": -0.4},
        ]
        results, _, n_detected = scan_stock_events("600000", bars, 2026, 2026)
        self.assertEqual(n_detected, 3)
        streaks = sorted(r["streak"] for r in results)
        self.assertEqual(streaks, [1, 2, 3])  # 连板数依次1/2/3

    def test_gap_filter_rejects_one_word_board(self):
        from backtest import simulate_event
        bars = [
            {"date": "2026-09-01", "open": 9.5, "close": 10.0, "high": 10.0,
             "low": 9.4, "volume": 1, "pct": 9.9},
            # D+1 一字板：高开10%（>7%上限，买不进也不该追）
            {"date": "2026-09-02", "open": 11.0, "close": 11.0, "high": 11.0,
             "low": 11.0, "volume": 1, "pct": 10.0},
        ] + self._bars()[2:]
        self.assertIsNone(simulate_event("600000", bars, 0))

    def test_normal_event_with_costs(self):
        from backtest import simulate_event
        r = simulate_event("600000", self._bars(), 0)
        self.assertIsNotNone(r)
        self.assertEqual(r["reason"], "收盘卖出")
        # 买入 10.3*1.003=10.33，卖出10.4：毛利0.7%被最低佣金吞掉大半
        self.assertEqual(r["shares"], 100)
        self.assertLess(r["pnl_pct"], 0.7)
        self.assertGreater(r["pnl_pct"], -0.5)

    def test_stop_loss(self):
        from backtest import simulate_event
        bars = self._bars()
        # D+2 盘中砸到-5%以下
        bars[2] = {"date": "2026-09-03", "open": 10.4, "close": 9.9, "high": 10.4,
                   "low": 9.5, "volume": 1, "pct": -4.8}
        r = simulate_event("600000", bars, 0)
        self.assertEqual(r["reason"], "止损")
        # 止损价 = 10.33 * 0.95 ≈ 9.81
        self.assertAlmostEqual(r["sell_price"], 9.81, places=1)

    def test_stop_loss_gap_down_fills_at_open(self):
        """D+2跳空低开低于止损价：真实成交价是开盘价而非止损价"""
        from backtest import simulate_event
        bars = self._bars()
        bars[2] = {"date": "2026-09-03", "open": 9.5, "close": 9.6, "high": 9.7,
                   "low": 9.4, "volume": 1, "pct": -7.7}
        r = simulate_event("600000", bars, 0)
        self.assertEqual(r["reason"], "止损")
        # 开盘9.5 < 止损价9.81 → 按开盘价成交
        self.assertEqual(r["sell_price"], 9.5)

    def test_limit_up_hold(self):
        from backtest import simulate_event
        bars = self._bars()
        # D+2 收盘涨停 → 续持到 D+3
        bars[2] = {"date": "2026-09-03", "open": 10.4, "close": 11.44, "high": 11.44,
                   "low": 10.3, "volume": 1, "pct": 10.0}
        r = simulate_event("600000", bars, 0)
        self.assertEqual(r["days"], 2)
        self.assertEqual(r["sell_price"], 10.2)  # D+3 收盘

    def test_too_expensive_skipped(self):
        from backtest import simulate_event
        bars = [
            {"date": "2026-09-01", "open": 19.0, "close": 20.0, "high": 20.0,
             "low": 19.0, "volume": 1, "pct": 9.9},
            {"date": "2026-09-02", "open": 20.4, "close": 20.5, "high": 20.6,
             "low": 20.2, "volume": 1, "pct": 2.5},
        ] + self._bars()[2:]
        self.assertIsNone(simulate_event("600000", bars, 0))

    def test_circuit_breaker_trips(self):
        """熔断器：失败率过半即跳闸（昨天的教训——限流下不磨十小时）"""
        from backtest import _CircuitBreaker
        cb = _CircuitBreaker(window=100, threshold=0.5)
        for _ in range(30):
            cb.record(True)
        self.assertIsNone(cb.tripped())
        for _ in range(30):
            cb.record(False)
        # 60次里30次失败=50%，未超熔断线（阈值是">"）
        self.assertIsNone(cb.tripped())
        for _ in range(10):
            cb.record(False)
        # 70次里40次失败≈57% > 50%
        self.assertIsNotNone(cb.tripped())


class TestLayeredStats(unittest.TestCase):
    """虚拟盘信号分层统计 vs 回测基线（过滤溢价的实证对比表）"""

    def _sig(self, kind, streak, pct, year="2026"):
        return {"status": "settled", "kind": kind, "streak": streak,
                "pnl_pct": pct, "signal_date": f"{year}-03-10",
                "settle_date": f"{year}-03-11"}

    def test_layered_stats(self):
        from report import layered_stats
        sigs = [
            self._sig("连板核心", 2, 3.0), self._sig("连板核心", 3, -2.0),
            self._sig("主线首板", 1, 1.5), self._sig("主线首板", 1, -1.0),
        ]
        md = layered_stats(sigs, {"2026": -2.12})
        self.assertIn("选股分类", md)
        self.assertIn("连板核心 | 2 | 50.0% | +0.50%", md)
        self.assertIn("2-3板", md)
        self.assertIn("首板", md)
        self.assertIn("回测基线", md)
        # 年度精选溢价 = 全部4笔均值+0.38% - 基线(-2.12%) = +2.50%
        self.assertIn("+2.50%", md)

    def test_layered_stats_empty(self):
        from report import layered_stats
        self.assertEqual(layered_stats([], {}), "")
        # 不足3笔不出表（噪声大）
        sigs = [self._sig("连板核心", 2, 3.0)]
        self.assertEqual(layered_stats(sigs, {}), "")


class TestLtbQuality(unittest.TestCase):
    """龙虎榜资金面与席位质量打分"""

    def test_ltb_quality_bonus(self):
        from strategy import ltb_quality_bonus
        self.assertEqual(ltb_quality_bonus(None), 0)
        self.assertEqual(ltb_quality_bonus({}), 0)
        # 净买1.5亿：+6
        self.assertEqual(ltb_quality_bonus({"net_buy": 1.5e8, "explanation": ""}), 6)
        # 小额净买：+4
        self.assertEqual(ltb_quality_bonus({"net_buy": 5e6, "explanation": ""}), 4)
        # 大额净卖：-6
        self.assertEqual(ltb_quality_bonus({"net_buy": -2e8, "explanation": ""}), -6)
        # 异常波动（连续三日上榜）再扣3
        self.assertEqual(
            ltb_quality_bonus({"net_buy": 1.5e8,
                               "explanation": "连续三个交易日内收盘价格涨幅偏离值累计达到20%"}),
            3)

    def test_seats_quality(self):
        from strategy import seats_quality
        bonus, labels = seats_quality([
            {"name": "华鑫证券上海分公司"},
            {"name": "机构专用"}, {"name": "银河证券绍兴营业部"},
        ])
        self.assertIn("绍兴帮", labels)
        self.assertIn("机构", labels)
        self.assertEqual(bonus, 7)  # 游资+4、机构+3
        # 空席位
        self.assertEqual(seats_quality([]), (0, []))

    def test_seats_quality_lasa_penalty(self):
        from strategy import seats_quality
        bonus, labels = seats_quality([
            {"name": "东方财富证券拉萨团结路第二证券营业部"},
            {"name": "东方财富证券拉萨东环路第二证券营业部"},
        ])
        self.assertIn("拉萨天团(散户)", labels)
        self.assertEqual(bonus, -4)


if __name__ == "__main__":
    unittest.main()
