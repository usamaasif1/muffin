from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
import datetime as dt

from backend.services.github_reader import read_github_file
from backend.services.market_data import (
    Candle,
    Timespan,
    fetch_candles,
    fetch_candles_alpaca_public,
    search_symbols,
)

# Load environment variables from .env if present (try repo root and app root)
_here = Path(__file__).resolve()
_candidates = [
    _here.parents[2] / ".env",  # repo root (../../.. from this file)
    _here.parents[1] / ".env",  # backend/.. = app root
    Path.cwd() / ".env",
]
for _env in _candidates:
    if _env.is_file():
        load_dotenv(dotenv_path=str(_env))
        break


def _active_provider() -> str:
    if os.environ.get("ALPACA_API_KEY_ID") and os.environ.get("ALPACA_API_SECRET_KEY"):
        return "alpaca"
    if os.environ.get("POLYGON_API_KEY"):
        return "polygon"
    return "yahoo"


def compute_change_percent(candles: list[Candle], window: str) -> float | None:
    if not candles:
        return None
    start = candles[0].o
    end = candles[-1].c
    if start == 0:
        return None
    return (end - start) / start * 100.0


class ReadGithubRequest(BaseModel):
    url: str
    token: Optional[str] = None


app = FastAPI(title="Muffin App")

# CORS: safe defaults; since we serve the frontend from the same server, this is mostly redundant
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict:
    provider = (
        "alpaca" if (os.environ.get("ALPACA_API_KEY_ID") and os.environ.get("ALPACA_API_SECRET_KEY"))
        else ("polygon" if os.environ.get("POLYGON_API_KEY") else "yahoo")
    )
    has_keys = bool(os.environ.get("ALPACA_API_KEY_ID") and os.environ.get("ALPACA_API_SECRET_KEY"))
    branch = os.environ.get("RENDER_GIT_BRANCH") or os.environ.get("GIT_BRANCH") or "unknown"
    return {"provider": provider, "hasKeys": has_keys, "branch": branch}


@app.post("/api/read-github-file")
async def read_github_file_endpoint(payload: ReadGithubRequest) -> dict:
    try:
        result = read_github_file(url=payload.url, token=payload.token)
        return {
            "file_name": result.file_name,
            "size_bytes": result.size_bytes,
            "source": result.source,
            "content": result.content_text,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/search")
async def api_search(q: str = Query(..., min_length=1), x_api_key: Optional[str] = Header(default=None)) -> dict:
    try:
        items = search_symbols(q, polygon_key=x_api_key)
        return {"items": items}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/candles")
async def api_candles(
    symbol: str = Query(..., min_length=1),
    timespan: Timespan = Query("1m"),
    window: str = Query("5d"),
    x_api_key: Optional[str] = Header(default=None),
) -> dict:
    try:
        # Force Alpaca for candles
        bars = fetch_candles_alpaca_public(symbol=symbol, timespan=timespan, window=window)
        return {
            "symbol": symbol.upper(),
            "timespan": timespan,
            "window": window,
            "source": "alpaca",
            "candles": [c.__dict__ for c in bars],
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class BigMoversRequest(BaseModel):
    symbols: List[str]
    window: str = "1d"
    timespan: Timespan = "15m"
    threshold: float = 15.0
    api_key: Optional[str] = None


@app.post("/api/bigmovers")
async def api_big_movers(payload: BigMoversRequest) -> dict:
    movers = []
    for sym in payload.symbols:
        try:
            candles = fetch_candles(symbol=sym, timespan=payload.timespan, window=payload.window, polygon_key=payload.api_key)
            pct = compute_change_percent(candles, payload.window)
            if pct is not None and abs(pct) >= payload.threshold:
                movers.append({"symbol": sym.upper(), "change_pct": pct})
        except Exception:
            continue
    movers.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
    return {"movers": movers}


@app.get("/api/backtest")
async def api_backtest(
    symbol: str = Query(..., min_length=1),
    x_api_key: Optional[str] = Header(default=None),
) -> dict:
    try:
        candles_1m = fetch_candles(symbol=symbol, timespan="1m", window="7d", polygon_key=x_api_key)
        candles_1h = fetch_candles(symbol=symbol, timespan="1h", window="7d", polygon_key=x_api_key)
        candles_1d_90 = fetch_candles(symbol=symbol, timespan="day", window="90d", polygon_key=x_api_key)

        # Reference date: first day in the selected backtest range (NY date of (now - 7d))
        now_utc = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
        ny = ZoneInfo("America/New_York")
        ref_local_date = (now_utc - dt.timedelta(days=7)).astimezone(ny).date()

        # Compute historical levels from daily bars
        def to_ny_date(ms: int) -> dt.date:
            return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).astimezone(ny).date()

        day_bars = candles_1d_90
        # Rolling last month: trailing 31 days ending day before ref
        lm_end = ref_local_date - dt.timedelta(days=1)
        lm_start = lm_end - dt.timedelta(days=31)
        lm_window = [b for b in day_bars if lm_start <= to_ny_date(b.t) <= lm_end]
        lml = min((b.l for b in lm_window), default=None)
        lmh = max((b.h for b in lm_window), default=None)

        # Previous calendar month
        prev_month_year = ref_local_date.year
        prev_month = ref_local_date.month - 1
        if prev_month == 0:
            prev_month = 12
            prev_month_year -= 1
        ppm_start = dt.date(prev_month_year, prev_month, 1)
        # last day of previous month
        if prev_month == 12:
            ppm_end = dt.date(prev_month_year, 12, 31)
        else:
            ppm_end = dt.date(prev_month_year, prev_month + 1, 1) - dt.timedelta(days=1)
        ppm_window = [b for b in day_bars if ppm_start <= to_ny_date(b.t) <= ppm_end]
        ppml = min((b.l for b in ppm_window), default=None)
        ppmh = max((b.h for b in ppm_window), default=None)

        levels = {
            "lml": lml,
            "lmh": lmh,
            "ppml": ppml,
            "ppmh": ppmh,
            "reference_date": ref_local_date.isoformat(),
            "source": "historical",
        }
        return {
            "symbol": symbol.upper(),
            "range": "last_7_days",
            "candles_1m": [c.__dict__ for c in candles_1m],
            "candles_1h": [c.__dict__ for c in candles_1h],
            "candles_1d_90": [c.__dict__ for c in candles_1d_90],
            "levels": levels,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
frontend_dir = os.path.abspath(frontend_dir)

if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")