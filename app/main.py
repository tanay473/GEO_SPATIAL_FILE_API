import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from app.api.files import router as files_router
from app.config import get_settings
from app.db import init_db
from app.errors import PROBLEM_JSON, register_error_handlers
from app.services.processor import warm_up

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

MULTIPART_OVERHEAD = 64 * 1024


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    warm_up()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Geospatial File Measurement API",
        version="1.0.0",
        description="Upload a zipped Shapefile or KML file and get per-feature area/length measurements.",
        lifespan=lifespan,
    )
    register_error_handlers(app)
    # Level 5: ~3x faster than the default 9 for a few % larger bodies.
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)

    @app.middleware("http")
    async def reject_oversized_uploads(request: Request, call_next):
        # Fail fast on Content-Length, before the multipart body is read at all.
        # (save_stream still enforces the limit for chunked uploads without a length.)
        if request.method == "POST":
            length = request.headers.get("content-length")
            limit = get_settings().max_upload_bytes
            if length and length.isdigit() and int(length) > limit + MULTIPART_OVERHEAD:
                return JSONResponse(
                    status_code=413,
                    media_type=PROBLEM_JSON,
                    content={
                        "type": "https://errors.geo-api/file_too_large",
                        "title": "File too large",
                        "status": 413,
                        "detail": f"File exceeds the {limit // (1024 * 1024)} MB limit.",
                        "instance": request.url.path,
                    },
                )
        return await call_next(request)

    app.include_router(files_router)
    return app


app = create_app()
