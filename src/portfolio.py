# -*- coding: utf-8 -*-
"""虚拟持仓记账：信号登记 → 次日按开盘价虚拟买入 → 持有 → 卖出结算。

数据文件:
  data/portfolio.json  {cash, positions: [], total_signals}
  data/signals.json    每笔信号完整生命周期
"""
import json
from datetime import datetime

from datasource import now_cn
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _load(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return default


def _save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


class Portfolio:
    # 默认交易成本：佣金万2.5最低5元/笔，印花税万5（仅卖出）。
    # 3k资金每笔几乎都触发5元最低佣金——一轮买卖约11元（占1500元仓位0.73%），
    # 不计成本的虚拟盘会系统性高估收益率。
    DEFAULT_COSTS = {
        "commission_rate": 0.00025,
        "commission_min": 5.0,
        "stamp_duty": 0.0005,
    }

    def __init__(self, capital, costs=None):
        self.path = DATA_DIR / "portfolio.json"
        self.signals_path = DATA_DIR / "signals.json"
        d = _load(self.path, None)
        if d is None:
            d = {"cash": capital, "positions": [], "initial_capital": capital}
        # 兼容旧数据文件：无 total_costs 字段时初始化
        d.setdefault("total_costs", 0.0)
        self.data = d
        self.signals = _load(self.signals_path, [])
        self.costs = {**self.DEFAULT_COSTS, **(costs or {})}

    # ---------- 持仓 ----------

    def held_codes(self):
        return {p["code"] for p in self.data["positions"]}

    def _buy_fee(self, amount):
        return round(max(amount * self.costs["commission_rate"],
                         self.costs["commission_min"]), 2)

    def _sell_fee(self, amount):
        return round(max(amount * self.costs["commission_rate"],
                         self.costs["commission_min"])
                    + amount * self.costs["stamp_duty"], 2)

    def buy(self, code, name, price, shares, board="", kind="", stop_loss=0, date_str=None):
        """虚拟买入（含买入佣金）"""
        amount = round(price * shares, 2)
        fee = self._buy_fee(amount)
        if amount + fee > self.data["cash"]:
            # 买不起就减股数（留足佣金）
            shares = int((self.data["cash"] - fee) // (price * 100)) * 100
            if shares <= 0:
                return None
            amount = round(price * shares, 2)
            fee = self._buy_fee(amount)
        self.data["cash"] = round(self.data["cash"] - amount - fee, 2)
        self.data["total_costs"] = round(self.data["total_costs"] + fee, 2)
        pos = {
            "code": code, "name": name, "buy_price": price, "shares": shares,
            "amount": amount, "buy_cost": fee,
            "buy_date": date_str or now_cn().strftime("%Y-%m-%d"),
            "board": board, "kind": kind, "stop_loss": stop_loss,
        }
        self.data["positions"].append(pos)
        return pos

    def sell(self, code, price, reason=""):
        """虚拟卖出全部持仓（含卖出佣金+印花税），返回 (盈亏, 盈亏%)"""
        for i, p in enumerate(self.data["positions"]):
            if p["code"] == code:
                gross = round(price * p["shares"], 2)
                fee = self._sell_fee(gross)
                net = round(gross - fee, 2)
                invested = p["amount"] + p.get("buy_cost", 0)
                pnl = round(net - invested, 2)
                pnl_pct = round(pnl / invested * 100, 2) if invested > 0 else 0.0
                self.data["cash"] = round(self.data["cash"] + net, 2)
                self.data["total_costs"] = round(self.data["total_costs"] + fee, 2)
                self.data["positions"].pop(i)
                self._settle_signal(code, price, pnl, pnl_pct, reason)
                return pnl, pnl_pct
        return None

    # ---------- 信号 ----------

    def register_signal(self, pick, date_str):
        """登记晚间信号（待次日买入）"""
        self.signals.append({
            "code": pick["code"], "name": pick["name"], "board": pick["board"],
            "kind": pick["kind"], "streak": pick.get("streak", 1),
            "signal_date": date_str,          # 信号产生日（晚间）
            "price": pick.get("price"),       # 信号日收盘价（计算止损用）
            "stop_loss": pick.get("stop_loss"),
            "buy_date": None, "buy_price": None,
            "status": "pending",              # pending -> holding -> settled
            "settle_date": None, "settle_price": None,
            "pnl": None, "pnl_pct": None, "reason": "",
        })

    def pending_signals(self):
        return [s for s in self.signals if s["status"] == "pending"]

    def execute_plan(self, date_str, plan, snapshot, max_stocks):
        """早间按作战计划虚拟买入：只买确认通过的票、用计划股数、守 max_stocks 上限。

        plan: morning_confirm 的输出（含 shares/open_price/stop_loss）
        未买入的 pending 信号标记 cancelled（原因可审计）
        """
        held = self.held_codes()
        slots = max_stocks - len(self.data["positions"])
        bought = []
        for p in plan:
            if slots <= 0:
                break
            if p["code"] in held:
                continue
            pos = self.buy(p["code"], p["name"], p["open_price"], p["shares"],
                           p["board"], p["kind"], p.get("stop_loss", 0), date_str)
            if not pos:
                continue
            for s in self.signals:
                if s["code"] == p["code"] and s["status"] == "pending":
                    s.update({"status": "holding", "buy_date": date_str,
                              "buy_price": p["open_price"]})
                    break
            bought.append(p)
            slots -= 1
        # 其余 pending 信号作废（竞价被过滤/仓位满），记录原因供统计审计
        for s in self.pending_signals():
            s["status"] = "cancelled"
            s["reason"] = "竞价过滤或仓位已满，未买入"
        return bought

    def settle_positions(self, date_str, snapshot, limit_up_codes):
        """收盘结算持仓（卖出规则在这里落地，否则虚拟盘永远不结算）。

        snapshot: {code: stock}，收盘后 price 即收盘价
        limit_up_codes: 今日涨停股代码集合
        规则（按优先级）：
        1. 收盘价 ≤ 止损价 → 止损卖出
        2. 今日涨停 → 继续持有（让利润奔跑）
        3. 买入满 1 个交易日（T+1 可卖）→ 尾盘按收盘价卖出
        4. 今日刚买入 → 明日再看
        """
        results = []
        for p in list(self.data["positions"]):
            st = snapshot.get(p["code"])
            if not st or st["price"] <= 0:
                results.append({"code": p["code"], "name": p["name"],
                                "action": "hold", "reason": "无行情，继续持有"})
                continue
            price = st["price"]
            stop = p.get("stop_loss") or 0
            if stop and price <= stop:
                pnl, pnl_pct = self.sell(p["code"], price, "止损")
                results.append({"code": p["code"], "name": p["name"], "action": "sell",
                                "reason": "触发止损", "buy_price": p["buy_price"],
                                "price": price, "pnl_pct": pnl_pct})
            elif p["code"] in limit_up_codes:
                results.append({"code": p["code"], "name": p["name"], "action": "hold",
                                "reason": "今日涨停，继续持有", "buy_price": p["buy_price"],
                                "price": price})
            elif p["buy_date"] < date_str:
                pnl, pnl_pct = self.sell(p["code"], price, "T+1尾盘卖出")
                results.append({"code": p["code"], "name": p["name"], "action": "sell",
                                "reason": "T+1尾盘卖出", "buy_price": p["buy_price"],
                                "price": price, "pnl_pct": pnl_pct})
            else:
                results.append({"code": p["code"], "name": p["name"], "action": "hold",
                                "reason": "今日买入，T+1明日可卖", "buy_price": p["buy_price"],
                                "price": price})
        return results

    def _settle_signal(self, code, price, pnl, pnl_pct, reason):
        for s in reversed(self.signals):
            if s["code"] == code and s["status"] == "holding":
                s.update({
                    "status": "settled", "settle_date": now_cn().strftime("%Y-%m-%d"),
                    "settle_price": price, "pnl": pnl, "pnl_pct": pnl_pct, "reason": reason,
                })
                return

    def market_value(self, prices):
        """当前总市值。prices: {code: 现价}"""
        mv = self.data["cash"]
        for p in self.data["positions"]:
            mv += prices.get(p["code"], p["buy_price"]) * p["shares"]
        return round(mv, 2)

    def save(self):
        _save(self.path, self.data)
        _save(self.signals_path, self.signals)

    # ---------- 统计 ----------

    def stats(self):
        settled = [s for s in self.signals if s["status"] == "settled"]
        if not settled:
            return {"total": 0, "win": 0, "lose": 0, "win_rate": 0,
                    "avg_pnl_pct": 0, "total_pnl": 0, "profit_factor": 0}
        win = [s for s in settled if s["pnl_pct"] > 0]
        lose = [s for s in settled if s["pnl_pct"] <= 0]
        total_win_amt = sum(s["pnl"] for s in win)
        total_lose_amt = abs(sum(s["pnl"] for s in lose))
        return {
            "total": len(settled),
            "win": len(win),
            "lose": len(lose),
            "win_rate": round(len(win) / len(settled) * 100, 1),
            "avg_pnl_pct": round(sum(s["pnl_pct"] for s in settled) / len(settled), 2),
            "total_pnl": round(sum(s["pnl"] for s in settled), 2),
            "profit_factor": round(total_win_amt / total_lose_amt, 2) if total_lose_amt > 0 else None,
        }
