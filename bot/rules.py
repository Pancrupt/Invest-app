"""Контрольная стратегия без ИИ: простой импульс по тренду.
Нужна, чтобы понять, добавляют ли ИИ-агенты что-то сверх обычных правил."""


def decide(feats, held, n_open, max_positions, max_pos_pct):
    actions = []
    # выход: тренд сломался
    for s in held:
        f = feats.get(s)
        if f and f["vs_sma50h_pct"] < -1.0:
            actions.append({"type": "close", "symbol": s, "reason": "цена ушла ниже 50-часовой средней"})
    free = max_positions - n_open + len(actions)
    if free <= 0:
        return actions
    cands = []
    for s, f in feats.items():
        if s in held or None in (f["ret_24h_pct"], f["ret_7d_pct"], f["volume_24h_vs_avg"]):
            continue
        if (f["vs_sma50h_pct"] > 0 and f["vs_sma200h_pct"] > 0 and 50 <= f["rsi14_1h"] <= 70
                and f["ret_24h_pct"] > 0 and f["volume_24h_vs_avg"] >= 1.0):
            cands.append((f["ret_7d_pct"], s, f))
    for _, s, f in sorted(cands, reverse=True)[:free]:
        stop = min(max(2.5 * f["atr1h_pct"], 2.0), 10.0)
        actions.append({"type": "buy", "symbol": s, "size_pct": max_pos_pct, "stop_pct": round(stop, 2),
                        "take_profit_pct": round(stop * 2, 2), "max_hold_h": 72,
                        "reason": f"тренд вверх, RSI {f['rsi14_1h']}, за 7 дней {f['ret_7d_pct']}%"})
    return actions
