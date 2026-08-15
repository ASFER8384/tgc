import json
import logging
import re
from pathlib import Path

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse

import brand
import brand.api as brand_api
import cdp
import forecast
import forecast.api as forecast_api
from cdp.api import automations, ingest, persons, proof, segments
from sca.api import catalog, coordination, demo, inbound, orders, sales
from sca.api import settings as settings_api
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
        "/dashboard",
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
    #
    # Procurement, not Supplier: two of the three entries are records *about*
    # suppliers, so heading the group "Supplier" named it after its subject
    # rather than after the work. For the same reason the desk is the buying
    # desk — "Supplier Desk" beside "Supplier list" said supplier twice and
    # neither of them said what you do there.
    ("Procurement", (
    (
        "sca:desk",
        "/procure",
        "Buying desk",
        "M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 "
        "4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4",
    ),
    (
        "sca:items",
        "/procure?view=items",
        "Item list",
        "M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4",
    ),
    (
        "sca:suppliers",
        "/procure?view=suppliers",
        "Supplier list",
        "M12 8c1.657 0 3-.895 3-2s-1.343-2-3-2-3 .895-3 2 1.343 2 3 2zm0 0v2m0 "
        "10a8 8 0 100-16 8 8 0 000 16zm0 0v-2m-6.4-4H8m8 0h2.4",
    ),
    # Filed under procurement rather than in a settings area of its own,
    # because what it holds is buying policy — the reorder point, the approval
    # threshold, the chase window. A platform-wide settings entry would promise
    # customer and compliance settings that are not there, and would put the
    # numbers a buyer tunes a page further from the desk they tune them for.
    (
        "sca:settings",
        "/procure?view=settings",
        "Settings",
        "M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c"
        "1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 "
        "2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a"
        "1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 "
        "00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c"
        "-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826"
        "-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065zM15 12a3 3 0 11-6 0 3 3 0 016 0z",
    ),
    )),
    # The fourth module, and the only one that reads both halves: it learns from
    # what customers bought and writes what has to be bought to serve them. Its
    # own group rather than a page inside Procurement, because it runs on a
    # different clock — the buying screens change when a buyer acts, these change
    # when a model runs — and because filing it under either half would claim it
    # belongs to that half.
    # One entry, not three. What will sell, who will buy and how the run scored
    # are one answer read at three depths — three addresses made them look like
    # three separate findings, and invited the question of why they disagreed.
    ("Demand Forecast", (
    (
        "forecast:forecast",
        "/forecast-console",
        "Forecast",
        "M13 7h8m0 0v8m0-8l-8 8-4-4-6 6",
    ),
    )),
    # A third module, and the first whose findings are made by people rather than
    # derived from records. It sits apart from procurement because a site is not
    # a supplier and a rule is not an order — what they share is the platform
    # underneath and the habit of never asserting more than was actually checked.
    ("Brand", (
    (
        "brand:sites",
        "/brand-console",
        "Sites",
        "M17.657 16.657L13.414 20.9a2 2 0 01-2.827 0l-4.244-4.243a8 8 0 1111.314 0z"
        "M15 11a3 3 0 11-6 0 3 3 0 016 0z",
    ),
    (
        "brand:standards",
        "/brand-console?view=standards",
        "The standard",
        "M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414"
        "a1 1 0 01.293.707V19a2 2 0 01-2 2z",
    ),
    (
        "brand:review",
        "/brand-console?view=review",
        "Review",
        "M15 12a3 3 0 11-6 0 3 3 0 016 0z"
        "M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7"
        "-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z",
    ),
    (
        "brand:findings",
        "/brand-console?view=findings",
        "Findings",
        "M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c"
        "-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z",
    ),
    # Separate from Findings because they answer different questions. Findings
    # is a queue and empties; this is the record and only grows. A lapse
    # corrected four times reads as nothing at all in the queue, which is what
    # the queue is for and exactly what makes a second view necessary.
    (
        "brand:history",
        "/brand-console?view=history",
        "The record",
        "M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z",
    ),
    )),
)

# Which section opens when the address carries no view of its own. Keyed by
# route rather than by document, because /dashboard and /cdp are the same file
# arriving at different destinations.
_DEFAULT_VIEW = {
    "/dashboard": "dashboard", "/cdp": "dashboard",
    "/procure": "desk", "/brand-console": "sites",
    "/forecast-console": "forecast",
}

# The browser tab follows the address, not the file behind it. /dashboard is
# served from the customer document, and a tab reading "CDP" there would put the
# reader back inside the half the dashboard exists to sit above.
_PAGE_TITLE = {
    "/dashboard": "TGC Platform — Dashboard",
    "/cdp": "TGC Customers",
    "/procure": "TGC Procurement",
    "/brand-console": "TGC Brand Compliance",
    "/forecast-console": "TGC Demand Forecast",
}


def _nav(active: str, env: str, default_view: str) -> str:
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
        // The link's own address, not one assembled here. It is the only way a
        // destination can sit somewhere other than under its document's path —
        // the dashboard answers for both halves, so it is /dashboard and not a
        // section of either one.
        history.pushState({{}}, "", a.getAttribute("href"));
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

    # Said out loud, once, in the logs of the environment it is dangerous in.
    # A setting that quietly disables authentication should be impossible to
    # leave on by accident six months after the meeting it was turned on for.
    if settings.console_auto_connect and settings.env != "local":
        logging.getLogger("sca").warning(
            "console_auto_connect is ON in env=%s: the API key is served inside "
            "the console page, so anyone with the URL has full API access — "
            "customer records, supplier pricing, placing and cancelling orders. "
            "This service has no authentication while that is true. Set "
            "SCA_CONSOLE_AUTO_CONNECT=false and rotate SCA_API_KEY before this "
            "URL outlives the demonstration.",
            settings.env,
        )

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "env": settings.env}

    supplier_console = Path(__file__).parent / "console" / "index.html"
    customer_console = Path(cdp.__file__).parent / "console" / "index.html"
    brand_console = Path(brand.__file__).parent / "console" / "index.html"
    forecast_console = Path(forecast.__file__).parent / "console" / "index.html"

    def render(page: Path, active: str, route: str) -> HTMLResponse:
        """One page, plus the rail that says which half you are looking at.

        ``route`` rather than ``active`` decides which section opens, because
        /dashboard and /cdp serve the same document and arrive at different
        destinations in it.
        """
        html = page.read_text(encoding="utf-8")
        # The key is injected as the page is served and never written into either
        # file, so it stays out of the repository and out of git history, and
        # rotating it is one environment variable rather than a commit.
        #
        # Injected always in local development, and elsewhere only when somebody
        # has deliberately set console_auto_connect — which hands the key to
        # anyone who opens the URL. See the setting for what that costs.
        head = _NAV_STYLE
        if settings.env == "local" or settings.console_auto_connect:
            head += f"<script>window.__SCA_DEV_KEY__ = {json.dumps(settings.api_key)};</script>"
        html = html.replace("</head>", head + "</head>", 1)
        title = _PAGE_TITLE.get(route)
        if title:
            html = re.sub(r"<title>[^<]*</title>", f"<title>{title}</title>", html, count=1)
        default_view = _DEFAULT_VIEW.get(route, "dashboard")
        html = html.replace(
            "<body>", "<body>" + _nav(active, settings.env, default_view), 1
        )
        # No caching: the console changes far more often than the API, and a
        # stale copy looks like a bug in the API rather than in the browser.
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    # The dashboard answers for both halves — customers on one side of it, the
    # supplier network on the other — so it has its own address rather than
    # sitting inside one of them and implying it belongs there. It is served
    # from the customer document because that is where its panels live; the URL
    # is what the reader sees, and the file is an implementation detail.
    @app.get("/", include_in_schema=False)
    async def home() -> RedirectResponse:
        return RedirectResponse("/dashboard", status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard_page() -> HTMLResponse:
        return render(customer_console, "cdp", "/dashboard")

    @app.get("/cdp", include_in_schema=False)
    async def customer_page() -> HTMLResponse:
        return render(customer_console, "cdp", "/cdp")

    @app.get("/procure", include_in_schema=False)
    async def procurement_page() -> HTMLResponse:
        return render(supplier_console, "sca", "/procure")

    # Not /brand: the API router already owns that prefix, and a page served
    # there would shadow the endpoints the page itself calls.
    @app.get("/brand-console", include_in_schema=False)
    async def brand_page() -> HTMLResponse:
        return render(brand_console, "brand", "/brand-console")

    # Same reason as the brand page: the API router owns /forecast, and a page
    # served there would shadow the endpoints the page itself calls.
    @app.get("/forecast-console", include_in_schema=False)
    async def forecast_page() -> HTMLResponse:
        return render(forecast_console, "forecast", "/forecast-console")

    app.include_router(brand_api.router)
    app.include_router(forecast_api.router)
    app.include_router(catalog.router)
    app.include_router(settings_api.router)
    app.include_router(orders.router)
    app.include_router(sales.router)
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
