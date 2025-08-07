from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from typing import Dict, List, Literal, Optional, Tuple

import requests

Timespan = Literal["1m", "15m", "1h", "day", "month"]


@dataclass
class Candle:
    t: int  # epoch ms
    o: float
    h: float
    l: float
    c: float
    v: float


class MarketDataError(Exception):
    pass


def _get_polygon_key(explicit_key: Optional[str] = None) -> Optional[str]:
    if explicit_key:
        return explicit_key
    return os.environ.get("POLYGON_API_KEY")


def _to_iso8601(date: dt.datetime) -> str:
    return date.replace(microsecond=0).isoformat() + "Z"


def _parse_window(window: str) -> dt.timedelta:
    # e.g., '5d', '30d', '1w', '1m' (month), '3m', '1y'
    if window == "max":
        # This path is handled in the caller that knows the timespan
        raise ValueError("'max' window must be mapped per timespan before parsing")
    unit = window[-1]
    value = int(window[:-1])
    if unit == "d":
        return dt.timedelta(days=value)
    if unit == "w":
        return dt.timedelta(weeks=value)
    if unit == "m":
        # approximate months as 30 days
        return dt.timedelta(days=30 * value)
    if unit == "y":
        return dt.timedelta(days=365 * value)
    raise ValueError("Unsupported window format; use d/w/m/y (e.g., 5d, 1m)")


def _polygon_timespan(timespan: Timespan) -> Tuple[int, str]:
    if timespan == "1m":
        return 1, "minute"
    if timespan == "15m":
        return 15, "minute"
    if timespan == "1h":
        return 1, "hour"
    if timespan == "day":
        return 1, "day"
    if timespan == "month":
        return 1, "month"
    raise ValueError("Unsupported timespan")


def _yahoo_interval_and_range(timespan: Timespan, window: str) -> Tuple[str, str]:
    # Yahoo finance intervals: 1m,2m,5m,15m,30m,60m,90m,1d,5d,1wk,1mo,3mo
    if timespan == "1m":
        interval = "1m"
    elif timespan == "15m":
        interval = "15m"
    elif timespan == "1h":
        interval = "60m"
    elif timespan == "day":
        interval = "1d"
    else:
        interval = "1mo"
    # Range mirrors the provided window
    return interval, window


def fetch_candles(
    symbol: str,
    timespan: Timespan,
    window: str = "5d",
    polygon_key: Optional[str] = None,
) -> List[Candle]:
    key = _get_polygon_key(polygon_key)
    if key:
        try:
            return _fetch_candles_polygon(symbol, timespan, window, key)
        except Exception as exc:
            # fall back to yahoo if polygon fails
            pass
    return _fetch_candles_yahoo(symbol, timespan, window)


def _fetch_candles_polygon(symbol: str, timespan: Timespan, window: str, key: str) -> List[Candle]:
    now = dt.datetime.utcnow()
    # Map 'max' to large ranges that Polygon can realistically serve
    if window == "max":
        if timespan == "1m":
            delta = dt.timedelta(days=30)  # 1-minute data: ~30 days
        elif timespan == "15m":
            delta = dt.timedelta(days=180)  # ~6 months
        elif timespan == "1h":
            delta = dt.timedelta(days=730)  # ~2 years
        elif timespan == "day":
            delta = dt.timedelta(days=365 * 5)  # ~5 years
        else:  # month
            delta = dt.timedelta(days=365 * 20)  # ~20 years
    else:
        delta = _parse_window(window)
    start = now - delta
    multiplier, unit = _polygon_timespan(timespan)
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{symbol.upper()}/range/{multiplier}/{unit}/"
        f"{start.date().isoformat()}/{now.date().isoformat()}?adjusted=true&sort=asc&limit=50000&apiKey={key}"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results") or []
    candles: List[Candle] = []
    for r in results:
        candles.append(
            Candle(
                t=int(r["t"]),
                o=float(r["o"]),
                h=float(r["h"]),
                l=float(r["l"]),
                c=float(r["c"]),
                v=float(r.get("v", 0.0)),
            )
        )
    return candles


def _fetch_candles_yahoo(symbol: str, timespan: Timespan, window: str) -> List[Candle]:
    interval, rng = _yahoo_interval_and_range(timespan, window)
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval={interval}&range={rng}"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    result = (data.get("chart") or {}).get("result")
    if not result:
        raise MarketDataError("No chart data from Yahoo")
    res0 = result[0]
    timestamps = res0.get("timestamp") or []
    indicators = res0.get("indicators", {})
    quotes = (indicators.get("quote") or [{}])[0]
    opens = quotes.get("open") or []
    highs = quotes.get("high") or []
    lows = quotes.get("low") or []
    closes = quotes.get("close") or []
    volumes = quotes.get("volume") or []
    candles: List[Candle] = []
    for i in range(min(len(timestamps), len(opens), len(highs), len(lows), len(closes))):
        t_sec = int(timestamps[i])
        o = opens[i]
        h = highs[i]
        l = lows[i]
        c = closes[i]
        v = volumes[i] if i < len(volumes) else 0
        if o is None or h is None or l is None or c is None:
            continue
        candles.append(
            Candle(
                t=t_sec * 1000,
                o=float(o),
                h=float(h),
                l=float(l),
                c=float(c),
                v=float(v or 0),
            )
        )
    return candles


def search_symbols(query: str, polygon_key: Optional[str] = None, limit: int = 10) -> List[Dict[str, str]]:
    key = _get_polygon_key(polygon_key)
    if key:
        try:
            url = f"https://api.polygon.io/v3/reference/tickers?search={requests.utils.quote(query)}&active=true&limit={limit}&apiKey={key}"
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results") or []
            return [{"symbol": r.get("ticker", ""), "name": r.get("name", "")} for r in results]
        except Exception:
            pass
    # Yahoo suggest fallback
    url = f"https://autoc.finance.yahoo.com/autoc?query={requests.utils.quote(query)}&region=1&lang=en"
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    items = ((data.get("ResultSet") or {}).get("Result") or [])[:limit]
    out: List[Dict[str, str]] = []
    for itm in items:
        sym = itm.get("symbol") or ""
        name = itm.get("name") or ""
        if sym:
            out.append({"symbol": sym, "name": name})
    return out


def compute_change_percent(candles: List[Candle], window: str) -> Optional[float]:
    if not candles:
        return None
    # simple: compare last close to first open in returned window
    start = candles[0].o
    end = candles[-1].c
    if start == 0:
        return None
    return (end - start) / start * 100.0