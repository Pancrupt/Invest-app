"""Виртуальный счёт: исполнение по рыночной цене с комиссией и проскальзыванием,
стоп-лоссы и тейк-профиты по максимумам/минимумам свечей, учёт расходов на ИИ."""
import json
import os
import time

FEE = 0.001        # 0.1% комиссия за сделку (как у большинства бирж для мейкеров/тейкеров начального уровня)
SLIPPAGE = 0.0005  # 0.05% проскальзывание
START_CASH = float(os.environ.get("START_CASH", "1000"))
MAX_POSITIONS = 3
MAX_POSITION_PCT = 30.0
STATE_ROOT = os.environ.get("STATE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state"))


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


class Book:
    """Один виртуальный счёт: state/<рынок>/<стратегия>/"""

    def __init__(self, market, strategy):
        self.dir = os.path.join(STATE_ROOT, market, strategy)
        self.state = load_json(self._p("state.json"), None)
        self.trades = load_json(self._p("trades.json"), [])
        self.equity = load_json(self._p("equity.json"), [])

    def _p(self, name):
        return os.path.join(self.dir, name)

    @property
    def started(self):
        return self.state is not None

    def start(self, prices, benchmark, now):
        self.state = {
            "started_at": now, "start_cash": START_CASH, "cash": START_CASH,
            "positions": {}, "fees_usd": 0.0, "last_check": now, "executed_orders": [],
            # ориентиры для сравнения: «купил и держу» индекс/BTC и корзину всех тикеров поровну
            "benchmark": benchmark,
            "bench_qty": START_CASH * (1 - FEE) / prices[benchmark],
            "basket_qty": {s: START_CASH / len(prices) * (1 - FEE) / p for s, p in prices.items()},
        }

    # ---- исполнение ----
    def buy(self, symbol, price, usd, stop, tp, max_hold_h, reason, now):
        usd = min(usd, self.state["cash"])
        if usd < 10:
            return None
        fill = price * (1 + SLIPPAGE)
        fee = usd * FEE
        qty = (usd - fee) / fill
        self.state["cash"] -= usd
        self.state["fees_usd"] += fee
        self.state["positions"][symbol] = {
            "qty": qty, "entry": fill, "cost": usd, "opened_at": now,
            "stop": stop, "tp": tp, "max_hold_h": max_hold_h, "reason": reason,
        }
        t = {"ts": now, "side": "buy", "symbol": symbol, "price": fill, "qty": qty, "usd": usd, "fee": fee, "reason": reason}
        self.trades.append(t)
        return t

    def sell(self, symbol, price, why, now, slip=True):
        pos = self.state["positions"].pop(symbol, None)
        if not pos:
            return None
        fill = price * (1 - SLIPPAGE) if slip else price
        gross = pos["qty"] * fill
        fee = gross * FEE
        self.state["cash"] += gross - fee
        self.state["fees_usd"] += fee
        pnl = gross - fee - pos["cost"]
        t = {"ts": now, "side": "sell", "symbol": symbol, "price": fill, "qty": pos["qty"], "usd": gross,
             "fee": fee, "pnl": pnl, "pnl_pct": pnl / pos["cost"] * 100, "reason": why,
             "held_h": round((now - pos["opened_at"]) / 3600, 1)}
        self.trades.append(t)
        return t

    def check_exits(self, candles_by_symbol, now):
        """Проходим по свечам с прошлой проверки. Если в одной свече задеты и стоп, и цель,
        пессимистично считаем, что сработал стоп. Гэп за стоп исполняется по цене открытия."""
        done = []
        for sym in list(self.state["positions"]):
            pos = self.state["positions"][sym]
            for c in candles_by_symbol.get(sym, []):
                if c["t"] + 3600 <= max(pos["opened_at"], self.state["last_check"]):
                    continue
                if c["l"] <= pos["stop"]:
                    px = min(c["o"], pos["stop"])
                    done.append(self.sell(sym, px, "стоп-лосс", now))
                    break
                if c["h"] >= pos["tp"]:
                    px = max(c["o"], pos["tp"])
                    done.append(self.sell(sym, px, "тейк-профит", now, slip=False))
                    break
            else:
                if (now - pos["opened_at"]) / 3600 >= pos["max_hold_h"]:
                    if not candles_by_symbol.get(sym):
                        continue
                    last = candles_by_symbol[sym][-1]["c"]
                    done.append(self.sell(sym, last, "вышел срок удержания", now))
        self.state["last_check"] = now
        return [d for d in done if d]

    # ---- оценка ----
    def value(self, prices):
        return self.state["cash"] + sum(p["qty"] * prices[s] for s, p in self.state["positions"].items())

    def mark(self, prices, now):
        eq = self.value(prices)
        bench = self.state["bench_qty"] * prices[self.state["benchmark"]]
        basket = sum(q * prices[s] for s, q in self.state["basket_qty"].items() if s in prices)
        point = {"ts": now, "equity": round(eq, 2), "bench_hold": round(bench, 2), "basket_hold": round(basket, 2)}
        if self.equity and now - self.equity[-1]["ts"] < 1800:
            self.equity[-1] = point  # не плодим точки при ручных перезапусках
        else:
            self.equity.append(point)
        return point

    def snapshot(self, prices):
        return {
            "cash": round(self.state["cash"], 2),
            "equity": round(self.value(prices), 2),
            "positions": [
                {"symbol": s, "entry": p["entry"], "now": prices[s], "pnl_pct": round((prices[s] / p["entry"] - 1) * 100, 2),
                 "weight_pct": round(p["qty"] * prices[s] / self.value(prices) * 100, 1),
                 "stop": p["stop"], "tp": p["tp"], "held_h": round((time.time() - p["opened_at"]) / 3600, 1)}
                for s, p in self.state["positions"].items()
            ],
            "max_positions": MAX_POSITIONS, "max_position_pct": MAX_POSITION_PCT,
        }

    def save(self):
        save_json(self._p("state.json"), self.state)
        save_json(self._p("trades.json"), self.trades)
        save_json(self._p("equity.json"), self.equity)
