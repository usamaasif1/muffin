from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.services.github_reader import read_github_file


class ReadGithubRequest(BaseModel):
    url: str
    token: Optional[str] = None


app = FastAPI(title="GitHub File Reader")

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


frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
frontend_dir = os.path.abspath(frontend_dir)

if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")