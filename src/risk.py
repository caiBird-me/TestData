# -*- coding: utf-8 -*-
"""风控模块：仓位分配、止损规则、禁买条件"""


class RiskRules:
    def __init__(self, cfg):
        self.capital = cfg["capital"]["total"]
        r = cfg["risk"]
        self.max_position_pct = r["max_position_pct"]
        self.max_stocks = r["max_stocks"]
        self.stop_loss_pct = r["stop_loss_pct"]
        self.max_gap_up_pct = r["max_gap_up_pct"]
        self.min_gap_pct = r["min_gap_pct"]
        self.consecutive_loss_pause = r["consecutive_loss_pause"]
        self.min_price = r["min_price"]
        self.max_price = r["max_price"]

    # ---------- 仓位 ----------

    def max_position_amount(self):
        """单票最大仓位金额"""
        return self.capital * self.max_position_pct

    def calc_shares(self, price):
        """给定价格，算可买的整百股数（A股一手100股），返回(股数,金额)"""
        if price <= 0:
            return 0, 0.0
        amount = self.max_position_amount()
        shares = int(amount // (price * 100)) * 100
        return shares, shares * price

    def affordable(self, price):
        """3k本金是否买得起一手"""
        return self.min_price <= price <= self.max_price

    # ---------- 早间竞价过滤 ----------

    def gap_filter(self, stock):
        """竞价/开盘过滤。返回 (通过, 原因)"""
        pre = stock["pre_close"]
        if pre <= 0:
            return False, "无昨收数据"
        gap = (stock["price"] - pre) / pre
        if gap < self.min_gap_pct:
            return False, f"低开{gap*100:.1f}%（低于{self.min_gap_pct*100:.0f}%）"
        if gap > self.max_gap_up_pct:
            return False, f"高开{gap*100:.1f}%（高于{self.max_gap_up_pct*100:.0f}%，不追）"
        return True, f"开盘{gap*100:+.1f}%"

    # ---------- 止损 ----------

    def stop_loss_price(self, buy_price):
        """硬止损价"""
        return round(buy_price * (1 + self.stop_loss_pct), 2)

    def check_stop_loss(self, position):
        """检查持仓是否触发止损。返回 (触发, 描述)"""
        price = position["current_price"]
        buy = position["buy_price"]
        if price <= 0 or buy <= 0:
            return False, ""
        ret = (price - buy) / buy
        if ret <= self.stop_loss_pct:
            return True, f"已亏{ret*100:.1f}%，触发硬止损{self.stop_loss_pct*100:.0f}%，必须卖出"
        return False, ""

    # ---------- 连亏熔断 ----------

    def need_pause(self, signals):
        """根据已结算信号判断是否连续亏损需要空仓。

        signals: 已结算信号列表（有 settle_price 和 pnl_pct 字段）
        返回 (需暂停, 描述)
        """
        settled = [s for s in signals if s.get("status") == "settled"]
        settled.sort(key=lambda s: s.get("settle_date", ""), reverse=True)
        n = 0
        for s in settled:
            if s.get("pnl_pct", 0) < 0:
                n += 1
            else:
                break
        if n >= self.consecutive_loss_pause:
            return True, (f"已连续{n}笔亏损，触发熔断：今日空仓观望，明日再战")
        return False, ""
