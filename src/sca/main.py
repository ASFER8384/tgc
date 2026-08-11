import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from sca.api import catalog, demo, inbound, orders
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
    async def console_page() -> HTMLResponse:
        # Served by the API itself so there is one URL to hand anyone, and with
        # no caching because the console changes far more often than the API.
        html = console.read_text(encoding="utf-8")
        # Local development connects with no typing. The key is injected here and
        # never stored in the file, because this page is public: on any deployed
        # environment the console asks for the key instead of shipping it.
        if settings.env == "local":
            injected = f"<script>window.__SCA_DEV_KEY__ = {json.dumps(settings.api_key)};</script>"
            html = html.replace("</head>", injected + "</head>", 1)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    app.include_router(catalog.router)
    app.include_router(orders.router)
    app.include_router(inbound.router)
    app.include_router(demo.router)
    return app


app = create_app()
