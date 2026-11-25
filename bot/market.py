"""Рыночные данные из открытых источников без ключей:
крипта — часовые свечи Coinbase, акции — часовые свечи Yahoo Finance, новости — RSS."""
import math
import os
import random
import re
import time
import xml.etree.ElementTree as ET

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (paper-trading research bot)"}

MARKETS = {
    "crypto": {
        "title": "Крипта",
        "benchmark": "BTC",
        "universe": ["BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK",
                     "DOT", "LTC", "BCH", "UNI", "ATOM", "NEAR", "AAVE"],
        "news": ["https://www.coindesk.com/arc/outboundfeeds/rss/",
                 "https://cointelegraph.com/rss",
                 "https://decrypt.co/feed"],
    },
    "stocks": {
        "title": "Акции США",
        "benchmark": "SPY",
        "universe": ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD",
                     "AVGO", "NFLX", "JPM", "V", "COST", "LLY", "PLTR", "COIN", "UBER", "ASML"],
        "news": ["https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY,QQQ,AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA&region=US&lang=en-US",
                 "https://feeds.finance.yahoo.com/rss/2.0/headline?s=AMD,AVGO,NFLX,JPM,V,COST,LLY,PLTR,COIN,UBER,ASML&region=US&lang=en-US"],
    },
}


def mock():
    return os.environ.get("MOCK_DATA") == "1"


# ---------------------------------------------------------------- свечи
def _mock_candles(symbol, n=300):
    """Синтетическое случайное блуждание, согласованное между запусками (для офлайн-тестов)."""
    rnd = random.Random(symbol)
    price = {"BTC": 90000, "ETH": 3200, "SPY": 600}.get(symbol, 10 + rnd.random() * 300)
    t0, now = 1790000000 // 3600 * 3600, int(time.time() // 3600) * 3600
    out = []
    for t in range(t0, now + 1, 3600):
        o = price
        price *= math.exp(rnd.gauss(0.0001, 0.01))
        c = price
        out.append({"t": t, "o": o, "h": max(o, c) * (1 + abs(rnd.gauss(0, 0.003))),
                    "l": min(o, c) * (1 - abs(rnd.gauss(0, 0.003))), "c": c, "v": 1000 + rnd.random() * 500})
    return out[-n:]


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def crypto_candles(symbol, n=300):
    url = f"https://api.exchange.coinbase.com/products/{symbol}-USD/candles"
    end = int(time.time())
    r = requests.get(url, params={"granularity": 3600, "start": _iso(end - 299 * 3600), "end": _iso(end)},
                     headers=HEADERS, timeout=20)
    r.raise_for_status()
    # формат Coinbase: [time, low, high, open, close, volume], от новых к старым
    rows = sorted(r.json(), key=lambda x: x[0])
    return [{"t": t, "o": o, "h": h, "l": l, "c": c, "v": v} for t, l, h, o, c, v in rows][-n:]


def stock_candles(symbol, n=300):
    """Возвращает (свечи, торговая_сессия_открыта)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    r = requests.get(url, params={"interval": "1h", "range": "60d"}, headers=HEADERS, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(res.get("timestamp") or []):
        o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
        if None in (o, h, l, c):
            continue
        out.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": v or 0})
    meta = res.get("meta", {})
    reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    now = time.time()
    is_open = bool(reg) and reg.get("start", 0) <= now < reg.get("end", 0)
    last_price = meta.get("regularMarketPrice")
    if out and last_price and is_open:
        out[-1]["c"] = last_price
    return out[-n:], is_open


def load_market(name):
    """Свечи по всем тикерам рынка + признак открытой сессии."""
    cfg = MARKETS[name]
    data, errors, session_open = {}, [], True
    for s in cfg["universe"]:
        try:
            if mock():
                data[s] = _mock_candles(s)
            elif name == "crypto":
                data[s] = crypto_candles(s)
            else:
                data[s], is_open = stock_candles(s)
                if s == cfg["benchmark"]:
                    session_open = is_open
            if not mock():
                time.sleep(0.15)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{s}: {e}")
    if mock() and name == "stocks":
        session_open = os.environ.get("MOCK_STOCKS_OPEN", "1") == "1"
    return data, session_open, errors


# ---------------------------------------------------------------- индикаторы
def features(cs):
    closes = [x["c"] for x in cs]
    last = closes[-1]

    def ret(h):
        return round((last / closes[-1 - h] - 1) * 100, 2) if len(closes) > h else None

    def sma(k):
        k = min(k, len(closes))
        return sum(closes[-k:]) / k

    gains = losses = 0.0
    for a, b in zip(closes[-15:-1], closes[-14:]):
        gains += max(b - a, 0)
        losses += max(a - b, 0)
    rsi = 100 - 100 / (1 + gains / (losses or 1e-9))
    trs = [max(x["h"] - x["l"], abs(x["h"] - p["c"]), abs(x["l"] - p["c"])) for p, x in zip(cs[-25:-1], cs[-24:])]
    atr_pct = sum(trs) / max(len(trs), 1) / last * 100
    vol24 = sum(x["v"] for x in cs[-24:])
    week = cs[-168:]
    vol7d = sum(x["v"] for x in week) / (len(week) / 24) if week else 0
    return {
        "price": round(last, 6),
        "ret_1h_pct": ret(1), "ret_24h_pct": ret(24), "ret_7d_pct": ret(168),
        "rsi14_1h": round(rsi, 1), "atr1h_pct": round(atr_pct, 2),
        "vs_sma50h_pct": round((last / sma(50) - 1) * 100, 2),
        "vs_sma200h_pct": round((last / sma(200) - 1) * 100, 2),
        "volume_24h_vs_avg": round(vol24 / vol7d, 2) if vol7d else None,
        "pct_below_7d_high": round((max(x["h"] for x in week) / last - 1) * 100, 2),
    }


# ---------------------------------------------------------------- новости
def news(name, limit=30):
    if mock():
        return ["(mock) Fed minutes due this week", "(mock) Large ETF inflows reported"]
    items = []
    for url in MARKETS[name]["news"]:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            for it in ET.fromstring(r.content).iter("item"):
                title = (it.findtext("title") or "").strip()
                if title and title not in items:
                    items.append(re.sub(r"\s+", " ", title))
        except Exception as e:  # noqa: BLE001
            print(f"news {url}: {e}")
    return items[:limit]
