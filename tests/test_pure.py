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

CFG = {
    "capital": {"total": 3000},
    "risk": {
        "max_position_pct": 0.50, "max_stocks": 2, "stop_loss_pct": -0.05,
        "max_gap_up_pct": 0.07, "min_gap_pct": -0.02, "consecutive_loss_pause": 3,
        "min_price": 2.0, "max_price": 30.0,
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


if __name__ == "__main__":
    unittest.main()
