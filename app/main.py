"""FastAPI application entry point."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

from app.api.routes import _jobs, router
from app.config import settings
from app.services.job_store import job_store
from app.services.provider_health import get_provider_health_snapshot, refresh_provider_health
from app.utils.logging import get_logger

_FRONTEND_DIR = Path(__file__).parent / "frontend"

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    from app.utils.http import close_shared_client

    try:
        job_store.load_into(_jobs)
        job_store.recover_interrupted_jobs(_jobs)
        try:
            await refresh_provider_health()
        except Exception as exc:
            log.warning("Provider health startup check failed: %s", exc)
        yield
    finally:
        await close_shared_client()

app = FastAPI(
    title="Web Crawler AI Agent",
    description=(
        "Multi-agent web crawler for extracting seller and company "
        "information from marketplaces, trade fairs, and directories."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


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
async def serve_frontend() -> Response:
    """Serve the single-page frontend UI (no-cache to avoid stale JS)."""
    return FileResponse(
        _FRONTEND_DIR / "index.html",
        media_type="text/html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
