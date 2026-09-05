# -*- coding: utf-8 -*-
"""虚拟持仓记账：信号登记 → 次日按开盘价虚拟买入 → 持有 → 卖出结算。

数据文件:
  data/portfolio.json  {cash, positions: [], total_signals}
  data/signals.json    每笔信号完整生命周期
"""
import json
from datetime import datetime
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
    def __init__(self, capital):
        self.path = DATA_DIR / "portfolio.json"
        self.signals_path = DATA_DIR / "signals.json"
        d = _load(self.path, None)
        if d is None:
            d = {"cash": capital, "positions": [], "initial_capital": capital}
        self.data = d
        self.signals = _load(self.signals_path, [])

    # ---------- 持仓 ----------

    def held_codes(self):
        return {p["code"] for p in self.data["positions"]}

    def buy(self, code, name, price, shares, board="", kind="", stop_loss=0):
        """虚拟买入"""
        amount = round(price * shares, 2)
        if amount > self.data["cash"]:
            # 买不起就减股数
            shares = int(self.data["cash"] // (price * 100)) * 100
            if shares <= 0:
                return None
            amount = round(price * shares, 2)
        self.data["cash"] = round(self.data["cash"] - amount, 2)
        pos = {
            "code": code, "name": name, "buy_price": price, "shares": shares,
            "amount": amount, "buy_date": datetime.now().strftime("%Y-%m-%d"),
            "board": board, "kind": kind, "stop_loss": stop_loss,
        }
        self.data["positions"].append(pos)
        return pos

    def sell(self, code, price, reason=""):
        """虚拟卖出全部持仓，返回盈亏%"""
        for i, p in enumerate(self.data["positions"]):
            if p["code"] == code:
                amount = round(price * p["shares"], 2)
                pnl = round(amount - p["amount"], 2)
                pnl_pct = round((price - p["buy_price"]) / p["buy_price"] * 100, 2)
                self.data["cash"] = round(self.data["cash"] + amount, 2)
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
            "buy_date": None, "buy_price": None,
            "status": "pending",              # pending -> holding -> settled
            "settle_date": None, "settle_price": None,
            "pnl": None, "pnl_pct": None, "reason": "",
        })

    def pending_signals(self):
        return [s for s in self.signals if s["status"] == "pending"]

    def activate_pending(self, date_str, snapshot):
        """次日早间：把 pending 信号按实时价虚拟买入。snapshot: {code: stock}"""
        activated = []
        for s in self.pending_signals():
            if s["code"] in self.held_codes():
                s["status"] = "cancelled"
                s["reason"] = "重复信号（已持仓）"
                continue
            st = snapshot.get(s["code"])
            if not st or st["price"] <= 0:
                s["status"] = "cancelled"
                s["reason"] = "无行情，放弃"
                continue
            # 早间确认时的现价作为虚拟买价
            pos = self.buy(s["code"], s["name"], st["price"], 100, s["board"], s["kind"])
            if pos is None:
                s["status"] = "cancelled"
                s["reason"] = "资金不足"
                continue
            s["status"] = "holding"
            s["buy_date"] = date_str
            s["buy_price"] = st["price"]
            activated.append(s)
        return activated

    def _settle_signal(self, code, price, pnl, pnl_pct, reason):
        for s in reversed(self.signals):
            if s["code"] == code and s["status"] == "holding":
                s.update({
                    "status": "settled", "settle_date": datetime.now().strftime("%Y-%m-%d"),
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
