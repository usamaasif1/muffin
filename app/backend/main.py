from __future__ import annotations

import os
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.services.github_reader import read_github_file
from backend.services.market_data import (
    Candle,
    Timespan,
    compute_change_percent,
    fetch_candles,
    search_symbols,
)


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
    return {"status": "ok"}


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
        bars = fetch_candles(symbol=symbol, timespan=timespan, window=window, polygon_key=x_api_key)
        return {
            "symbol": symbol.upper(),
            "timespan": timespan,
            "window": window,
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


frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
frontend_dir = os.path.abspath(frontend_dir)

if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")