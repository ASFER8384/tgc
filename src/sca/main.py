import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

import cdp
from cdp.api import automations, ingest, persons, proof, segments
from sca.api import catalog, coordination, demo, inbound, orders
from sca.config import get_settings

# Injected rather than pasted into both consoles, which were built as standalone
# pages with nothing else in common, so the two halves cannot drift into
# disagreeing about what the other is called.
#
# The shell is the admin panel from the DSC app, ported rather than copied: that
# one is React and these are static pages, so the markup is rewritten and the
# measurements are kept — 260px column, the same two gradients, the same drawer
# below 768px. Matching an interface the client already uses is worth more than
# a better one they have to learn.
_NAV_STYLE = """<style>
  :root { --tgc-rail: 208px; }
  .tgc-rail {
    position: fixed; left: 0; top: 0; width: var(--tgc-rail); height: 100vh; z-index: 1001;
    background: var(--sunk); color: var(--ink);
    border-right: 1px solid var(--rule);
    display: flex; flex-direction: column;
    font-family: var(--sans);
  }
  .tgc-rail .head {
    padding: 16px; border-bottom: 1px solid var(--rule);
    display: flex; align-items: center; gap: 10px;
  }
  .tgc-rail .mark {
    width: 30px; height: 30px; flex: none; border-radius: 7px;
    background: var(--accent); color: var(--accent-ink);
    display: flex; align-items: center; justify-content: center;
  }
  .tgc-rail .head b { display: block; font-size: 14px; font-weight: 600; }
  .tgc-rail .head span { font-size: 11px; color: var(--muted); }
  /* No inset. The rail already has an edge — its own border — so a margin
     around the items only reads as a second one that does not line up. */
  .tgc-rail nav { padding: 0; flex: 1; overflow-y: auto; }
  /* A label, not a row: smaller, quieter and not clickable, so it cannot be
     mistaken for a destination that does nothing when pressed. */
  .tgc-rail nav .group {
    padding: 14px 16px 5px; font-size: 10px; font-weight: 600;
    letter-spacing: 0.09em; text-transform: uppercase; color: var(--muted);
  }
  .tgc-rail nav > a:first-child { margin-top: 8px; }
  .tgc-rail nav a {
    display: flex; align-items: center; gap: 10px; padding: 9px 16px;
    text-decoration: none; font-size: 13px;
    color: var(--ink-soft); transition: background 0.15s, color 0.15s;
  }
  .tgc-rail nav a:hover { background: var(--panel); color: var(--ink); }
  .tgc-rail nav a.on {
    color: var(--accent); font-weight: 600; background: var(--accent-soft);
  }
  .tgc-rail nav a svg { flex: none; width: 17px; height: 17px; }
  .tgc-rail .foot {
    padding: 12px 16px; border-top: 1px solid var(--rule);
    font-size: 11px; color: var(--muted); display: flex; align-items: center; gap: 7px;
  }
  .tgc-rail .foot i {
    width: 7px; height: 7px; border-radius: 50%; background: var(--ok); flex: none;
  }

  .tgc-bar { display: none; }
  .tgc-scrim { display: none; }
  body { padding-left: var(--tgc-rail); }

  @media (max-width: 768px) {
    .tgc-rail { transform: translateX(-100%); transition: transform 0.3s ease; }
    .tgc-rail.open { transform: translateX(0); }
    .tgc-bar {
      display: flex; position: fixed; top: 0; left: 0; right: 0; height: 48px; z-index: 1000;
      background: var(--sunk); color: var(--ink); border-bottom: 1px solid var(--rule);
      align-items: center; gap: 10px; padding: 0 12px;
      font: 600 15px/1 var(--sans);
    }
    .tgc-bar button {
      background: none; border: none; color: var(--ink); cursor: pointer; padding: 6px;
    }
    .tgc-scrim { position: fixed; inset: 0; background: rgba(0,0,0,0.4); z-index: 1000; }
    .tgc-scrim.open { display: block; }
    body { padding-left: 0; padding-top: 48px; }
  }
  @media (prefers-reduced-motion: reduce) { .tgc-rail { transition: none; } }
</style>"""

# Grouped, because these are two halves of a platform rather than six pages of
# one. Flat, the supplier desk read as a peer of the customer list and gave no
# warning that reaching it means leaving the document — which is also why it is
# the only entry that reloads rather than switching a view.
#
# The customer half is one document with several destinations in it, so the rail
# addresses them by query rather than by path: every panel reads from the same
# connection, and splitting them into separate documents would mean connecting,
# and refetching, once per section.
_NAV_GROUPS = (
    # Ungrouped, and first, because it answers for both halves: customers on the
    # left of it, the supplier network on the right. Putting it inside either
    # group would claim it belongs to one of them.
    ("", (
    (
        "cdp:dashboard",
        "/cdp?view=dashboard",
        "Dashboard",
        "M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 "
        "1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6",
    ),
    )),
    ("CDP & Marketing", (
    (
        "cdp:customers",
        "/cdp?view=customers",
        "Customers",
        "M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 "
        "20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 "
        "019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 "
        "11-4 0 2 2 0 014 0z",
    ),
    (
        "cdp:segments",
        "/cdp?view=segments",
        "Smart Segments",
        "M22 3H2l8 9.46V19l4 2v-8.54L22 3z",
    ),
    (
        "cdp:broadcast",
        "/cdp?view=broadcast",
        "Broadcast Message",
        "M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3zM19 10v2a7 7 0 0 1-14 0v-2"
        "M12 19v4M8 23h8",
    ),
    (
        "cdp:automation",
        "/cdp?view=automation",
        "Automation",
        "M13 10V3L4 14h7v7l9-11h-7z",
    ),
    )),
    # Three destinations, not one, because they run on different clocks. The desk
    # is a queue somebody works every morning; the item and supplier lists are
    # records that change when a SKU is added or a mill is onboarded. Filed
    # together they buried the queue under reference data.
    ("Supplier", (
    (
        "sca:desk",
        "/",
        "Supplier Desk",
        "M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 "
        "4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4",
    ),
    (
        "sca:items",
        "/?view=items",
        "Items",
        "M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4",
    ),
    (
        "sca:suppliers",
        "/?view=suppliers",
        "Suppliers",
        "M12 8c1.657 0 3-.895 3-2s-1.343-2-3-2-3 .895-3 2 1.343 2 3 2zm0 0v2m0 "
        "10a8 8 0 100-16 8 8 0 000 16zm0 0v-2m-6.4-4H8m8 0h2.4",
    ),
    )),
)

# Which section a page opens on when the address carries no view. Both consoles
# are single documents with several destinations in them, so this is the one
# thing that differs between them.
_DEFAULT_VIEW = {"cdp": "dashboard", "sca": "desk"}
_PAGE_PATH = {"cdp": "/cdp", "sca": "/"}


def _nav(active: str, env: str) -> str:
    """`active` is the page; the rail marks the destination within it from the
    query, so that reloading a section keeps its own item lit."""
    links = "".join(
        (f'<div class="group">{group}</div>' if group else "")
        + "".join(
            f'<a href="{href}" data-nav="{key}">'
            f'<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            f'<path d="{icon}" /></svg>{label}</a>'
            for key, href, label, icon in items
        )
        for group, items in _NAV_GROUPS
    )
    base = _PAGE_PATH.get(active, "/")
    default_view = _DEFAULT_VIEW.get(active, "dashboard")
    return f"""<div class="tgc-bar">
  <button type="button" aria-label="Open menu" aria-controls="tgcRail" aria-expanded="false"
          onclick="tgcRail(true)">
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2"><path d="M3 12h18M3 6h18M3 18h18" /></svg>
  </button>TGC Platform</div>
<div class="tgc-scrim" id="tgcScrim" onclick="tgcRail(false)"></div>
<aside class="tgc-rail" id="tgcRail">
  <div class="head">
    <div class="mark">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="2">
        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
      </svg>
    </div>
    <div><b>TGC Platform</b><span>Aleena · Rawash · Aynola</span></div>
  </div>
  <nav>{links}</nav>
  <div class="foot"><i></i>{env}</div>
</aside>
<script>
  function tgcRail(open) {{
    document.getElementById("tgcRail").classList.toggle("open", open);
    document.getElementById("tgcScrim").classList.toggle("open", open);
    var b = document.querySelector(".tgc-bar button");
    if (b) b.setAttribute("aria-expanded", String(open));
  }}
  (function () {{
    var page = {active!r};
    var base = {base!r};
    var view = new URLSearchParams(location.search).get("view") || {default_view!r};
    var want = page + ":" + view;
    document.querySelectorAll(".tgc-rail nav a").forEach(function (a) {{
      a.classList.toggle("on", a.dataset.nav === want);
    }});
    // Within one page the rail switches views without a reload, so the panels
    // keep the connection they already made. The address still changes, so the
    // section can be linked to and the back button works.
    document.querySelectorAll('.tgc-rail nav a[data-nav^="' + page + ':"]').forEach(function (a) {{
      a.addEventListener("click", function (e) {{
        if (typeof window.applyView !== "function") return;
        e.preventDefault();
        var next = a.dataset.nav.slice(page.length + 1);
        history.pushState({{}}, "", base + "?view=" + next);
        window.applyView(next);
        document.querySelectorAll(".tgc-rail nav a").forEach(function (o) {{
          o.classList.toggle("on", o === a);
        }});
        tgcRail(false);
      }});
    }});
  }})();
</script>"""


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
        """One page, plus the rail that says which half you are looking at."""
        html = page.read_text(encoding="utf-8")
        # Local development connects with no typing. The key is injected here and
        # never stored in either file, because these pages are public: on any
        # deployed environment the console asks for the key instead of shipping it.
        head = _NAV_STYLE
        if settings.env == "local":
            head += f"<script>window.__SCA_DEV_KEY__ = {json.dumps(settings.api_key)};</script>"
        html = html.replace("</head>", head + "</head>", 1)
        html = html.replace("<body>", "<body>" + _nav(active, settings.env), 1)
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
    app.include_router(coordination.router)
    app.include_router(demo.router)

    # The customer side keeps its own paths. Nothing collides — it owns /persons,
    # /segments and /ingest, and the supplier side owns everything else — so
    # prefixing would only make every existing CDP client wrong for no gain.
    app.include_router(ingest.router)
    app.include_router(persons.router)
    app.include_router(segments.router)
    app.include_router(proof.router)
    app.include_router(automations.router)
    return app


app = create_app()
