from __future__ import annotations

from fastapi import FastAPI

from kestrel.api.routes import router

def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(title="Kestrel")
    app.include_router(router)
    return app
