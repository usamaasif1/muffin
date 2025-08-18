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


def _get_alpaca_keys(
    explicit_key_id: Optional[str] = None,
    explicit_secret: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    key_id = explicit_key_id or os.environ.get("ALPACA_API_KEY_ID")
    secret = explicit_secret or os.environ.get("ALPACA_API_SECRET_KEY")
    return key_id, secret


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


def _alpaca_timeframe(timespan: Timespan) -> str:
    # Alpaca timeframes: 1Min,5Min,15Min,1Hour,1Day,1Month
    if timespan == "1m":
        return "1Min"
    if timespan == "5m":
        return "5Min"
    if timespan == "15m":
        return "15Min"
    if timespan == "1h":
        return "1Hour"
    if timespan == "day":
        return "1Day"
    if timespan == "month":
        return "1Month"
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

# Cap Yahoo ranges to avoid 429 on free tier
def _cap_yahoo_range(timespan: Timespan, requested_range: str) -> str:
    r = (requested_range or '').lower()
    if r and r != 'max':
        try:
            if r.endswith('d'):
                days = int(r[:-1])
                if timespan == '1m' and days > 14:
                    return '14d'
                if timespan == '5m' and days > 60:
                    return '60d'
                if timespan == '15m' and days > 180:
                    return '180d'
                if timespan == '1h' and days > 180:
                    return '180d'
            if r.endswith('y'):
                years = int(r[:-1])
                if timespan == 'day' and years > 10:
                    return '10y'
        except Exception:
            pass
        return requested_range
    if timespan == '1m':
        return '14d'
    if timespan == '5m':
        return '60d'
    if timespan == '15m':
        return '180d'
    if timespan == '1h':
        return '180d'
    if timespan == 'day':
        return '10y'
    return '30y'


def fetch_candles(
    symbol: str,
    timespan: Timespan,
    window: str = "5d",
    polygon_key: Optional[str] = None,
) -> List[Candle]:
    # Prefer Alpaca if configured
    alpaca_key_id, alpaca_secret = _get_alpaca_keys()
    if alpaca_key_id and alpaca_secret:
        return _fetch_candles_alpaca(symbol, timespan, window, alpaca_key_id, alpaca_secret)
    # Next, try Polygon if key is provided (explicit or env)
    key = _get_polygon_key(polygon_key)
    if key:
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


def _fetch_candles_alpaca(
    symbol: str,
    timespan: Timespan,
    window: str,
    key_id: str,
    secret: str,
) -> List[Candle]:
    """Fetch candles from Alpaca Market Data v2.

    Uses single-symbol endpoint with pagination via next_page_token.
    """
    now = dt.datetime.utcnow().replace(microsecond=0)
    # Map 'max' to conservative ranges similar to Polygon code above
    if window == "max":
        if timespan == "1m":
            delta = dt.timedelta(days=7)
        elif timespan == "5m":
            delta = dt.timedelta(days=30)
        elif timespan == "15m":
            delta = dt.timedelta(days=60)
        elif timespan == "1h":
            delta = dt.timedelta(days=180)
        elif timespan == "day":
            delta = dt.timedelta(days=365 * 20)
        else:  # month
            delta = dt.timedelta(days=365 * 30)
    else:
        delta = _parse_window(window)

    start = now - delta
    tf = _alpaca_timeframe(timespan)
    base_url = f"https://data.alpaca.markets/v2/stocks/{symbol.upper()}/bars"
    headers = {
        "APCA-API-KEY-ID": key_id,
        "APCA-API-SECRET-KEY": secret,
    }
    params: Dict[str, str] = {
        "timeframe": tf,
        "start": start.replace(microsecond=0).isoformat() + "Z",
        "end": now.replace(microsecond=0).isoformat() + "Z",
        "adjustment": "all",
        "limit": "10000",
    }

    candles: List[Candle] = []
    page_token: Optional[str] = None
    while True:
        q = params.copy()
        if page_token:
            q["page_token"] = page_token
        resp = requests.get(base_url, headers=headers, params=q, timeout=30)
        if resp.status_code == 429:
            raise MarketDataError("429: Too Many Requests (Alpaca)")
        resp.raise_for_status()
        data = resp.json() or {}
        bars = data.get("bars") or []
        for b in bars:
            t_iso = b.get("t")
            # Parse RFC3339 timestamp to epoch ms
            try:
                # Support trailing Z
                if isinstance(t_iso, str):
                    dt_utc = dt.datetime.fromisoformat(t_iso.replace("Z", "+00:00"))
                    t_ms = int(dt_utc.timestamp() * 1000)
                else:
                    t_ms = 0
            except Exception:
                t_ms = 0
            if not t_ms:
                continue
            candles.append(
                Candle(
                    t=t_ms,
                    o=float(b.get("o", 0.0)),
                    h=float(b.get("h", 0.0)),
                    l=float(b.get("l", 0.0)),
                    c=float(b.get("c", 0.0)),
                    v=float(b.get("v", 0.0)),
                )
            )
        page_token = data.get("next_page_token") or data.get("nextPageToken")
        if not page_token:
            break

    return candles


def fetch_candles_alpaca_public(symbol: str, timespan: Timespan, window: str) -> List[Candle]:
    """Public wrapper to fetch Alpaca candles using env vars, raising if missing."""
    key_id, secret = _get_alpaca_keys()
    if not key_id or not secret:
        raise MarketDataError("Alpaca credentials not configured")
    return _fetch_candles_alpaca(symbol, timespan, window, key_id, secret)


def _fetch_candles_yahoo(symbol: str, timespan: Timespan, window: str) -> List[Candle]:
    interval, rng = _yahoo_interval_and_range(timespan, window)
    rng = _cap_yahoo_range(timespan, rng)
    # Chunk minute/hour intervals using period1/period2 to reduce 429s
    if interval in ("1m", "5m", "15m", "60m"):
        def range_to_days(r: str) -> int:
            r = (r or '').lower()
            if r.endswith('d'):
                return max(0, int(r[:-1]))
            if r.endswith('y'):
                return max(0, int(r[:-1]) * 365)
            return 0
        chunk_days_map = {"1m": 3, "5m": 7, "15m": 14, "60m": 30}
        total_days = range_to_days(rng) or chunk_days_map[interval]
        end_dt = dt.datetime.utcnow().replace(microsecond=0)
        start_dt = end_dt - dt.timedelta(days=total_days)
        candles: List[Candle] = []
        cur = start_dt
        while cur < end_dt:
            nxt = min(cur + dt.timedelta(days=chunk_days_map[interval]), end_dt)
            url = (
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
                f"?period1={int(cur.timestamp())}&period2={int(nxt.timestamp())}&interval={interval}&includePrePost=true"
            )
            resp = requests.get(url, timeout=30)
            if resp.status_code == 429:
                raise MarketDataError("429: Too Many Requests (Yahoo)")
            resp.raise_for_status()
            data = resp.json()
            result = (data.get("chart") or {}).get("result")
            if result:
                res0 = result[0]
                timestamps = res0.get("timestamp") or []
                quotes = ((res0.get("indicators") or {}).get("quote") or [{}])[0]
                opens = quotes.get("open") or []
                highs = quotes.get("high") or []
                lows = quotes.get("low") or []
                closes = quotes.get("close") or []
                volumes = quotes.get("volume") or []
                for i in range(min(len(timestamps), len(opens), len(highs), len(lows), len(closes))):
                    t_sec = int(timestamps[i])
                    o = opens[i]; h = highs[i]; l = lows[i]; c = closes[i]
                    v = volumes[i] if i < len(volumes) else 0
                    if o is None or h is None or l is None or c is None:
                        continue
                    candles.append(Candle(t=t_sec*1000, o=float(o), h=float(h), l=float(l), c=float(c), v=float(v or 0)))
            time.sleep(0.2)
            cur = nxt
        # Sort and dedup
        candles.sort(key=lambda c: c.t)
        dedup: List[Candle] = []
        seen_t = set()
        for c in candles:
            if c.t in seen_t:
                continue
            seen_t.add(c.t)
            dedup.append(c)
        return dedup

    # Single call fallback for daily/monthly
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval={interval}&range={rng}&includePrePost=true"
    )
    resp = requests.get(url, timeout=30)
    if resp.status_code == 429:
        raise MarketDataError("429: Too Many Requests (Yahoo)")
    resp.raise_for_status()
    data = resp.json()
    result = (data.get("chart") or {}).get("result")
    if not result:
        raise MarketDataError("No chart data from Yahoo")
    res0 = result[0]
    timestamps = res0.get("timestamp") or []
    quotes = ((res0.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quotes.get("open") or []
    highs = quotes.get("high") or []
    lows = quotes.get("low") or []
    closes = quotes.get("close") or []
    volumes = quotes.get("volume") or []
    out: List[Candle] = []
    for i in range(min(len(timestamps), len(opens), len(highs), len(lows), len(closes))):
        t_sec = int(timestamps[i])
        o = opens[i]; h = highs[i]; l = lows[i]; c = closes[i]
        v = volumes[i] if i < len(volumes) else 0
        if o is None or h is None or l is None or c is None:
            continue
        out.append(Candle(t=t_sec*1000, o=float(o), h=float(h), l=float(l), c=float(c), v=float(v or 0)))
    return out