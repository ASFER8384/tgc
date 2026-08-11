import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

import cdp
from cdp.api import ingest, persons, segments
from sca.api import catalog, demo, inbound, orders
from sca.config import get_settings

# Fixed to the viewport rather than in the page flow: both consoles were built as
# standalone pages with their own layout, and threading a column through each
# would mean editing two large files that have nothing else in common.
_SIDEBAR_STYLE = """<style>
  .tgc-nav { position: fixed; left: 0; top: 0; bottom: 0; width: 132px; z-index: 50;
             background: #1b1714; color: #cfc4b8; padding: 1.1rem 0.8rem;
             font: 0.78rem/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
             display: flex; flex-direction: column; gap: 0.35rem; }
  .tgc-nav .brand { color: #8a7d70; letter-spacing: 0.08em; text-transform: uppercase;
                    font-size: 0.66rem; margin-bottom: 0.7rem; }
  .tgc-nav a { color: #cfc4b8; text-decoration: none; padding: 0.45rem 0.55rem;
               border-radius: 3px; border: 1px solid transparent; }
  .tgc-nav a:hover { border-color: #3a322c; }
  .tgc-nav a.on { background: #2a2320; color: #fff; border-color: #4a3f36; }
  body { padding-left: 132px; }
  @media (max-width: 860px) {
    .tgc-nav { position: static; width: auto; flex-direction: row; bottom: auto;
               align-items: center; padding: 0.5rem 0.8rem; }
    .tgc-nav .brand { margin: 0 0.6rem 0 0; }
    body { padding-left: 0; }
  }
</style>"""


def _sidebar(active: str) -> str:
    items = (("sca", "/", "Suppliers"), ("cdp", "/cdp", "Customers"))
    links = "".join(
        f'<a href="{href}" class="{"on" if key == active else ""}">{label}</a>'
        for key, href, label in items
    )
    return f'<nav class="tgc-nav"><div class="brand">TGC</div>{links}</nav>'


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="TGC Platform",
        version="0.1.0",
        summary=(
            "Customer profiles and supplier coordination on one database: who "
            "buys, and what has to be bought to serve them."
        ),
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

    supplier_console = Path(__file__).parent / "console" / "index.html"
    customer_console = Path(cdp.__file__).parent / "console" / "index.html"

    def render(page: Path, active: str) -> HTMLResponse:
        """One page, plus the strip that says which half you are looking at.

        The nav is injected rather than pasted into both consoles, so the two
        halves cannot drift into disagreeing about what the other is called.
        """
        html = page.read_text(encoding="utf-8")
        # Local development connects with no typing. The key is injected here and
        # never stored in either file, because these pages are public: on any
        # deployed environment the console asks for the key instead of shipping it.
        head = _SIDEBAR_STYLE
        if settings.env == "local":
            head += f"<script>window.__SCA_DEV_KEY__ = {json.dumps(settings.api_key)};</script>"
        html = html.replace("</head>", head + "</head>", 1)
        html = html.replace("<body>", "<body>" + _sidebar(active), 1)
        # No caching: the console changes far more often than the API, and a
        # stale copy looks like a bug in the API rather than in the browser.
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/", include_in_schema=False)
    async def supplier_page() -> HTMLResponse:
        return render(supplier_console, "sca")

    @app.get("/cdp", include_in_schema=False)
    async def customer_page() -> HTMLResponse:
        return render(customer_console, "cdp")

    app.include_router(catalog.router)
    app.include_router(orders.router)
    app.include_router(inbound.router)
    app.include_router(demo.router)

    # The customer side keeps its own paths. Nothing collides — it owns /persons,
    # /segments and /ingest, and the supplier side owns everything else — so
    # prefixing would only make every existing CDP client wrong for no gain.
    app.include_router(ingest.router)
    app.include_router(persons.router)
    app.include_router(segments.router)
    return app


app = create_app()
