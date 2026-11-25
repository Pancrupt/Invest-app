"""Ежечасный «движок» бумажной торговли. Запускается GitHub Actions.

  python bot/engine.py tick      — цены, стопы, исполнение заявок, график, снимок для ИИ
  python bot/engine.py validate  — проверить файлы заявок ИИ (для сеанса Claude перед коммитом)

Для каждого рынка (crypto, stocks) ведутся два счёта:
  ai    — решения шести ИИ-ролей (их пишет сеанс Claude в state/<рынок>/ai/orders.json)
  rules — контрольная стратегия без ИИ (rules.py)
Ориентиры «купил и держу» считаются внутри каждого счёта.
"""
import json
import os
import sys
import time

import market
import rules
from portfolio import MAX_POSITION_PCT, MAX_POSITIONS, STATE_ROOT, Book, load_json, save_json

STRATEGIES = ("ai", "rules")
ORDER_TTL_H = {"crypto": 6, "stocks": 30}  # заявка ИИ протухает, если её не успели исполнить
LIMITS = {"size_pct": (1, MAX_POSITION_PCT), "stop_pct": (1.5, 15), "take_profit_pct": (1.5, 60), "max_hold_h": (4, 336)}


def clamp(v, lo, hi):
    return max(lo, min(hi, float(v)))


def validate_orders(doc, universe):
    """Возвращает (список ошибок, нормализованные действия). Жёсткие лимиты риска — в коде, а не в промпте."""
    errs, acts = [], []
    if not isinstance(doc, dict):
        return ["orders.json должен быть объектом"], []
    for k in ("id", "created_at", "actions", "summary"):
        if k not in doc:
            errs.append(f"нет поля {k}")
    for i, a in enumerate(doc.get("actions") or []):
        t, s = a.get("type"), a.get("symbol")
        if t not in ("buy", "close"):
            errs.append(f"actions[{i}].type должен быть buy или close")
            continue
        if s not in universe:
            errs.append(f"actions[{i}].symbol {s} не из списка тикеров")
            continue
        n = {"type": t, "symbol": s, "reason": str(a.get("reason", ""))[:300]}
        if t == "buy":
            for k, (lo, hi) in LIMITS.items():
                if k not in a:
                    errs.append(f"actions[{i}] нет {k}")
                    break
                n[k] = clamp(a[k], lo, hi)
            else:
                acts.append(n)
            continue
        acts.append(n)
    return errs, acts


def apply_actions(book, actions, feats, prices, now, session_open):
    log = []
    for a in actions:
        s = a["symbol"]
        if s not in prices:
            log.append(f"{s}: нет цены, пропуск")
            continue
        if not session_open:
            log.append(f"{s}: рынок закрыт")
            continue
        if a["type"] == "close":
            t = book.sell(s, prices[s], "решение: " + a["reason"], now)
            log.append(f"закрыл {s}" if t else f"{s}: позиции нет")
            continue
        if s in book.state["positions"]:
            log.append(f"{s}: уже в портфеле")
            continue
        if len(book.state["positions"]) >= MAX_POSITIONS:
            log.append(f"{s}: лимит {MAX_POSITIONS} позиций")
            continue
        usd = book.value(prices) * a["size_pct"] / 100
        p = prices[s]
        t = book.buy(s, p, usd, p * (1 - a["stop_pct"] / 100), p * (1 + a["take_profit_pct"] / 100),
                     a["max_hold_h"], a["reason"], now)
        log.append(f"купил {s} на ${usd:.0f}" if t else f"{s}: мало кэша")
    return log


def tick():
    now = int(time.time())
    summary = {"ts": now, "markets": {}}
    for name, cfg in market.MARKETS.items():
        data, session_open, errors = market.load_market(name)
        if cfg["benchmark"] not in data:
            print(f"{name}: нет данных по ориентиру, пропускаю. {errors}")
            summary["markets"][name] = {"error": errors[:5]}
            continue
        prices = {s: cs[-1]["c"] for s, cs in data.items() if cs}
        feats = {s: market.features(cs) for s, cs in data.items() if len(cs) >= 30}
        msum = {"session_open": session_open, "errors": errors[:5], "books": {}}
        for strat in STRATEGIES:
            book = Book(name, strat)
            if not book.started:
                book.start(prices, cfg["benchmark"], now)
            exits = book.check_exits(data, now)
            log = [f"{t['symbol']}: {t['reason']} ({t['pnl_pct']:+.2f}%)" for t in exits]
            if strat == "rules":
                acts = rules.decide(feats, set(book.state["positions"]), len(book.state["positions"]),
                                    MAX_POSITIONS, MAX_POSITION_PCT)
                log += apply_actions(book, acts, feats, prices, now, session_open)
            else:
                log += run_ai_orders(book, name, feats, prices, now, session_open)
            point = book.mark(prices, now)
            book.save()
            msum["books"][strat] = {"log": log, **point}
        # снимок для сеанса Claude
        ai = Book(name, "ai")
        save_json(os.path.join(STATE_ROOT, name, "snapshot.json"), {
            "market": name, "title": cfg["title"], "generated_at": now,
            "generated_at_iso": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now)),
            "session_open": session_open, "benchmark": cfg["benchmark"],
            "features": feats, "news": market.news(name),
            "portfolio": ai.snapshot(prices),
            "recent_trades": ai.trades[-10:],
            "rules": {"max_positions": MAX_POSITIONS, "limits": LIMITS, "fee_pct": 0.1, "slippage_pct": 0.05},
        })
        summary["markets"][name] = msum
    save_json(os.path.join(STATE_ROOT, "last_tick.json"), summary)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


def run_ai_orders(book, name, feats, prices, now, session_open):
    path = os.path.join(STATE_ROOT, name, "ai", "orders.json")
    doc = load_json(path, None)
    if not doc or doc.get("id") in book.state["executed_orders"]:
        return []
    errs, acts = validate_orders(doc, market.MARKETS[name]["universe"])
    age_h = (now - int(doc.get("created_ts", now))) / 3600
    if age_h > ORDER_TTL_H[name]:
        book.state["executed_orders"].append(doc.get("id"))
        return [f"заявка {doc.get('id')} устарела ({age_h:.0f} ч), не исполнена"]
    if not session_open and any(a["type"] for a in acts):
        return ["рынок закрыт, заявка ИИ ждёт открытия"]
    log = [f"ошибка в заявке: {e}" for e in errs]
    log += apply_actions(book, acts, feats, prices, now, session_open)
    book.state["executed_orders"] = (book.state["executed_orders"] + [doc.get("id")])[-100:]
    return log


def validate_cli():
    ok = True
    for name, cfg in market.MARKETS.items():
        path = os.path.join(STATE_ROOT, name, "ai", "orders.json")
        doc = load_json(path, None)
        if doc is None:
            print(f"{name}: orders.json нет")
            continue
        errs, acts = validate_orders(doc, cfg["universe"])
        if "created_ts" not in doc:
            errs.append("нет поля created_ts (unix-время)")
        print(f"{name}: {len(acts)} действий, ошибок {len(errs)}")
        for e in errs:
            print("  -", e)
        ok &= not errs
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tick"
    {"tick": tick, "validate": validate_cli}[cmd]()
