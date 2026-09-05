# -*- coding: utf-8 -*-
"""纯函数单元测试：仓位计算、竞价过滤、涨停识别、连板计算、结算规则"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.chdir(Path(__file__).resolve().parent.parent)

from datasource import calc_streak_codes, is_limit_up, limit_up_pct
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


class TestMorningConfirm(unittest.TestCase):
    """morning_confirm 三层过滤：gap / buy_range / 成交可行性"""

    def _cfg(self):
        cfg = {**CFG, "strategy": {**CFG["strategy"], "final_picks": 2,
                                   "min_turnover_rate": 5.0, "max_turnover_rate": 25.0,
                                   "wide_turnover_factor": 1.6}}
        cfg["_risk"] = make_rules()  # main 里由 cfg["_risk"] 注入
        return cfg

    def _cand(self, price=10.0):
        return {"code": "600001", "name": "测试", "board": "机器人", "kind": "主线首板",
                "streak": 1, "stop_loss": 9.5, "buy_range": [9.8, 10.2]}

    def test_buy_range_rejects_out_of_range(self):
        """竞价+6%超出±2%计划区间：计划已失效，不买"""
        from strategy import morning_confirm
        snap = {"600001": make_stock("600001", 6, 10.6, 10.7)}
        plan, rejected = morning_confirm([self._cand()], snap, self._cfg())
        self.assertEqual(plan, [])
        self.assertIn("超出计划区间", rejected[0][1])

    def test_buy_range_accepts_in_range(self):
        from strategy import morning_confirm
        snap = {"600001": make_stock("600001", 1, 10.1, 10.2)}
        plan, _ = morning_confirm([self._cand()], snap, self._cfg())
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["open_price"], 10.1)

    def test_unfillable_opening_limit(self):
        """09:31整分钟封死涨停（low=high）：标记unfillable，虚拟盘取消"""
        from strategy import morning_confirm
        snap = {"600001": make_stock("600001", 3, 10.3, 10.3)}
        fm = {"600001": {"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0}}
        cand = self._cand()
        cand["buy_range"] = [9.8, 11.5]  # 放宽区间让gap检查通过
        plan, _ = morning_confirm([cand], snap, self._cfg(), fm)
        self.assertTrue(plan[0].get("unfillable"))

    def test_fillable_with_intraday_range(self):
        """09:31有成交间隙（low<high）：正常可买"""
        from strategy import morning_confirm
        snap = {"600001": make_stock("600001", 3, 10.3, 10.4)}
        fm = {"600001": {"open": 10.3, "high": 10.5, "low": 10.2, "close": 10.4}}
        cand = self._cand()
        cand["buy_range"] = [9.8, 10.6]
        plan, _ = morning_confirm([cand], snap, self._cfg(), fm)
        self.assertFalse(plan[0].get("unfillable", False))


if __name__ == "__main__":
    unittest.main()
