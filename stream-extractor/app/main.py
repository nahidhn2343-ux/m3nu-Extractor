"""
main.py
-------
FastAPI application entrypoint.

Routes:
    GET  /                -> renders the single-page UI (templates/index.html)
    POST /api/extract     -> {"url": "..."}  ->  JSON ExtractionResult
    GET  /healthz          -> simple liveness check for hosting platforms

Run locally:
    uvicorn app.main:app --reload --port 8000

See README.md for deployment notes (Render / Koyeb).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.extractor import extract_stream_url

BASE_DIR = Path(__file__).resolve().parent.parent

app = FastAPI(
    title="Stream URL Extractor",
    description="Resolves short links / video pages down to direct .m3u8 / .mpd stream URLs.",
    version="1.0.0",
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class ExtractRequest(BaseModel):
    url: str = Field(..., min_length=4, max_length=4000, description="Short link, redirect, or video page URL")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Updated to compatibility syntax for newer Jinja2Templates / FastAPI versions
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/api/extract")
async def api_extract(payload: ExtractRequest):
    """Main extraction endpoint. Always returns 200 with a JSON body
    describing success/failure — HTTP-level errors are reserved for
    malformed requests, so the frontend can uniformly branch on the
    `success` field."""
    try:
        result = await extract_stream_url(payload.url)
    except Exception as exc:  # noqa: BLE001 - last-resort safety net
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "message": f"Unexpected server error: {exc.__class__.__name__}",
                "original_url": payload.url,
            },
        )

    return JSONResponse(status_code=200, content=_serialize(result))


def _serialize(result) -> dict:
    return {
        "success": result.success,
        "message": result.message,
        "original_url": result.original_url,
        "final_page_url": result.final_page_url,
        "redirect_chain": result.redirect_chain,
        "stream_url": result.stream_url,
        "stream_type": result.stream_type,
        "elapsed_ms": result.elapsed_ms,
        "alternates": [
            {
                "url": c.url,
                "kind": c.kind,
                "source": c.source,
                "validated": c.validated,
                "status_code": c.status_code,
                "content_type": c.content_type,
            }
            for c in result.alternates
        ],
    }