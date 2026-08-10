from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from sca.api import catalog, inbound, orders
from sca.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="TGC Supplier Coordination",
        version="0.1.0",
        summary="Purchase orders, supplier replies and exceptions across time zones.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "env": settings.env}

    console = Path(__file__).parent / "console" / "index.html"

    @app.get("/", include_in_schema=False)
    async def console_page() -> FileResponse:
        # Served by the API itself so there is one URL to hand anyone, and with
        # no caching because the console changes far more often than the API.
        return FileResponse(
            console, media_type="text/html", headers={"Cache-Control": "no-store"}
        )

    app.include_router(catalog.router)
    app.include_router(orders.router)
    app.include_router(inbound.router)
    return app


app = create_app()
