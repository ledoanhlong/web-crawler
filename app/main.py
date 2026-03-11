"""FastAPI application entry point."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.api.routes import router
from app.config import settings
from app.services.provider_health import get_provider_health_snapshot, refresh_provider_health
from app.utils.logging import get_logger

_FRONTEND_DIR = Path(__file__).parent / "frontend"

log = get_logger(__name__)

app = FastAPI(
    title="Web Crawler AI Agent",
    description=(
        "Multi-agent web crawler for extracting seller and company "
        "information from marketplaces, trade fairs, and directories."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.on_event("shutdown")
async def _shutdown() -> None:
    from app.utils.http import close_shared_client
    await close_shared_client()


@app.on_event("startup")
async def _startup() -> None:
    # Warm provider health state so /health immediately reports endpoint reachability.
    try:
        await refresh_provider_health()
    except Exception as exc:
        log.warning("Provider health startup check failed: %s", exc)


@app.get("/health")
async def health() -> dict:
    try:
        snapshot = await refresh_provider_health()
    except Exception:
        snapshot = get_provider_health_snapshot()
    return {
        "status": "ok" if snapshot.get("status") == "ok" else "degraded",
        "providers": snapshot,
    }


@app.get("/", include_in_schema=False)
async def serve_frontend() -> FileResponse:
    """Serve the single-page frontend UI."""
    return FileResponse(_FRONTEND_DIR / "index.html", media_type="text/html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
