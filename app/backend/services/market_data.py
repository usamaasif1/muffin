from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from typing import Dict, List, Literal, Optional, Tuple

import requests
from urllib.parse import urlencode
import time

Timespan = Literal["1m", "5m", "15m", "1h", "day", "month"]


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
    if timespan == "5m":
        return 5, "minute"
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
    elif timespan == "5m":
        interval = "5m"
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
        # With a key, do NOT fall back to Yahoo. Bubble up Polygon errors.
        return _fetch_candles_polygon(symbol, timespan, window, key)
    # No key available: best-effort Yahoo fallback
    return _fetch_candles_yahoo(symbol, timespan, window)


def _fetch_candles_polygon(symbol: str, timespan: Timespan, window: str, key: str) -> List[Candle]:
    now = dt.datetime.utcnow()
    # Map 'max' to conservative ranges to avoid provider 429s
    if window == "max":
        if timespan == "1m":
            delta = dt.timedelta(days=7)   # ~7 days of 1-minute bars
        elif timespan == "5m":
            delta = dt.timedelta(days=30)  # ~30 days of 5-minute bars
        elif timespan == "15m":
            delta = dt.timedelta(days=60)  # ~60 days of 15-minute bars
        elif timespan == "1h":
            delta = dt.timedelta(days=365) # ~1 year of hourly bars
        elif timespan == "day":
            delta = dt.timedelta(days=365 * 20)  # ~20 years for daily
        else:  # month
            delta = dt.timedelta(days=365 * 30)  # ~30 years
    else:
        delta = _parse_window(window)

    multiplier, unit = _polygon_timespan(timespan)

    def build_url(start_dt: dt.datetime) -> str:
        base = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol.upper()}/range/{multiplier}/{unit}/"
            f"{start_dt.date().isoformat()}/{now.date().isoformat()}"
        )
        params = {
            "adjusted": "true",
            "sort": "asc",
            "limit": 50000,
            "apiKey": key,
        }
        return base + "?" + urlencode(params)

    # Try with backoff on 429 by shrinking the window
    attempts_remaining = 5
    current_delta = delta

    while attempts_remaining > 0:
        start = now - current_delta
        url = build_url(start)
        candles: List[Candle] = []
        # Initial request with 429 handling
        resp = requests.get(url, timeout=30)
        if resp.status_code == 429:
            attempts_remaining -= 1
            # shrink window by half and try again
            shrink_days = max(1, int(current_delta.days * 0.5))
            current_delta = dt.timedelta(days=shrink_days)
            time.sleep(1.0)
            continue
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results") or []
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
        next_url = data.get("next_url") or data.get("nextUrl")
        while next_url:
            # Respectful small delay to reduce chance of 429
            time.sleep(0.5)
            separator = "&" if "?" in next_url else "?"
            page_url = f"{next_url}{separator}apiKey={key}"
            page_resp = requests.get(page_url, timeout=30)
            if page_resp.status_code == 429:
                # Backoff and retry the entire range with a smaller window
                attempts_remaining -= 1
                shrink_days = max(1, int(current_delta.days * 0.5))
                current_delta = dt.timedelta(days=shrink_days)
                time.sleep(1.0)
                break
            page_resp.raise_for_status()
            page_data = page_resp.json()
            page_results = page_data.get("results") or []
            for r in page_results:
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
            next_url = page_data.get("next_url") or page_data.get("nextUrl")
        else:
            # Completed pagination without 429
            return candles
        # If we broke out due to 429 mid-pagination, loop to retry smaller window
    # If all attempts exhausted
    raise MarketDataError("Polygon rate limit (429). Reduce range or try a higher timespan.")


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