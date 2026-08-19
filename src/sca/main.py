import html
import json
import logging
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import brand
import brand.api as brand_api
import cdp
import forecast
import forecast.api as forecast_api
from cdp.api import automations, ingest, persons, proof, segments
from sca import auth
from sca.api import catalog, coordination, demo, inbound, inventory, orders, sales
from sca.api import settings as settings_api
from sca.api import whatsapp as whatsapp_api
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
  /* The two bands that run across every console: the rail's head and the page
     title beside it, then the rail's foot and the page footer beside that. They
     read as one line each, so their heights come from here rather than from each
     page's own stylesheet — four consoles each guessing at 62px is four chances
     for one of them to sit a pixel proud of the rail.

     Borders are inside these numbers (border-box), so the rule at the bottom of
     the rail head and the rule at the bottom of the page title land on exactly
     the same row. */
  :root { --tgc-rail: 208px; --tgc-head: 56px; --tgc-foot: 38px; }
  .tgc-rail {
    position: fixed; left: 0; top: 0; width: var(--tgc-rail); height: 100vh; z-index: 1001;
    background: var(--sunk); color: var(--ink);
    border-right: 1px solid var(--rule);
    display: flex; flex-direction: column;
    font-family: var(--sans);
  }
  .tgc-rail .head {
    height: var(--tgc-head); box-sizing: border-box;
    padding: 0 16px; border-bottom: 1px solid var(--rule);
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
    min-height: var(--tgc-foot); box-sizing: border-box;
    padding: 0 16px; border-top: 1px solid var(--rule);
    font-size: 11px; color: var(--muted); display: flex; align-items: center; gap: 7px;
  }
  .tgc-rail .foot i {
    width: 7px; height: 7px; border-radius: 50%; background: var(--ok); flex: none;
  }
  /* Who is signed in, on the one line the foot has. An address is longer than
     176px of rail more often than not, so it is cut with an ellipsis rather
     than allowed to wrap and push the sign-out button off the bottom — the
     whole of it is in the title, and the part that identifies a person is at
     the front anyway. */
  .tgc-rail .foot .who {
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0;
  }
  .tgc-rail .foot form { margin-left: auto; display: flex; flex: none; }
  .tgc-rail .foot button {
    background: none; border: none; padding: 4px; cursor: pointer; color: var(--muted);
    display: flex; border-radius: 5px;
  }
  .tgc-rail .foot button:hover { color: var(--alert); background: var(--panel); }

  /* The page's half of each band. `min-height`, not `height`: the footer's
     sentence wraps on a narrow window and a fixed height would cut it in half
     rather than let the two sides disagree for a moment. */
  .top, body > footer {
    min-height: var(--tgc-head); box-sizing: border-box;
    padding: 0 16px; display: flex; align-items: center;
  }
  body > footer { min-height: var(--tgc-foot); }

  /* Held at the top while the page scrolls under it, so the rail's head and the
     page's title stay one band rather than the left half staying put and the
     right half sliding away.

     Sticky rather than fixed: fixed would take it out of flow and the first
     panel would start underneath it, which then wants a top padding on body
     that has to be kept in step with the height by hand. Sticky keeps the space
     it occupies.

     It needs its own background — it is transparent otherwise, and the charts
     would scroll through the title — and a z-index under the rail's, so the
     drawer and the dialogs still cover it. */
  .top {
    position: sticky; top: 0; z-index: 900;
    background: var(--ground);
  }

  .tgc-bar { display: none; }
  .tgc-scrim { display: none; }
  /* A column at least as tall as the window, with the page's own content taking
     whatever is left over. Without it a short page — a search that matched two
     customers, an empty forecast — leaves the footer floating halfway up with
     ground beneath it, which reads as content that failed to load.

     Not a fixed footer: it stays under the content and goes off the bottom when
     there is more than a screenful, rather than sitting over it. border-box so
     the mobile bar's padding-top comes out of the 100vh instead of adding to it
     and giving every short page a scrollbar. */
  body {
    padding-left: var(--tgc-rail); box-sizing: border-box;
    min-height: 100vh; display: flex; flex-direction: column;
  }
  /* width:100% is not redundant. The page column centres itself with
     `margin: 0 auto`, and auto side margins on a flex child beat the default
     stretch — so without a width of its own it shrank to fit its contents and
     the whole console collapsed into a narrow strip down the middle. */
  .app { flex: 1 0 auto; width: 100%; }

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
    /* Under the bar rather than at zero: the bar is fixed over the page, so a
       title stuck to the top of the scroll area would slide beneath it. */
    .top { top: 48px; }
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
    # Its own group rather than a page inside Procurement. What is on a shelf is
    # not a buying decision — a shop assistant counting stock in Jeddah and a
    # buyer placing an order with a mill are different people asking different
    # questions. Filing the count under the ordering screens made it read as
    # something only a buyer had reason to open, which is exactly backwards now
    # that every shop holds its own.
    ("Inventory", (
    (
        "sca:items",
        "/procure?view=items",
        "Inventory",
        "M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4",
    ),
    )),
    # Separate destinations, because they run on different clocks. The desk is a
    # queue somebody works every morning; the supplier list is a record that
    # changes when a mill is onboarded. Filed together they buried the queue
    # under reference data.
    #
    # Procurement, not Supplier: the supplier list and the settings are records
    # *about* suppliers, so heading the group "Supplier" named it after its subject
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
    # What will sell and who will buy stay together — they are one answer read at
    # two depths, and separate addresses made them look like two findings that
    # disagreed. The log is genuinely a different question: not what the forecast
    # says but whether it has been worth believing, which is asked on the days a
    # figure looks wrong rather than on the days somebody is buying.
    ("Demand Forecast", (
    (
        "forecast:forecast",
        "/forecast-console",
        "Forecast",
        "M13 7h8m0 0v8m0-8l-8 8-4-4-6 6",
    ),
    (
        "forecast:log",
        "/forecast-console?view=log",
        "Log",
        "M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 "
        "002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01",
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


def _nav(active: str, env: str, default_view: str, user: str) -> str:
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
  <div class="foot">
    <i title="{env}"></i><span class="who" title="{user} · {env}">{user}</span>
    <form method="post" action="/logout">
      <button type="submit" title="Sign out" aria-label="Sign out">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9" />
        </svg>
      </button>
    </form>
  </div>
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


# Sign-in attempts per address, so a password guessed at speed runs into a wall
# well before it runs out of passwords. In memory and per process, which means a
# restart clears it and two workers each keep their own count — worth saying
# plainly, and still the difference between thousands of guesses a minute and a
# handful. A shared store is the upgrade, when there is a reason for one.
# Shown wherever an empty account list is the reason a sign-in cannot succeed.
# It names the variable, because the fault is almost always that it was set on a
# laptop and never on the server — .env is not deployed, deliberately.
_NO_ACCOUNTS = (
    "No accounts are configured on this service, so nobody can sign in yet. "
    "Set <code>SCA_CONSOLE_USERS</code> in the environment — "
    "<code>python -m scripts.make_user you@example.com</code> prints the line."
)

_ATTEMPT_LIMIT = 6
_ATTEMPT_WINDOW = 300.0
_attempts: dict[str, list[float]] = {}


def _throttled(email: str) -> bool:
    """True when this address has failed too often lately. Keyed by address
    rather than by IP: a team behind one office connection shares an address at
    the door, and locking the office out because one person fat-fingered their
    password is the failure mode that gets a control switched off."""
    now = time.time()
    recent = [t for t in _attempts.get(email, ()) if now - t < _ATTEMPT_WINDOW]
    _attempts[email] = recent
    return len(recent) >= _ATTEMPT_LIMIT


def _record_failure(email: str) -> None:
    _attempts.setdefault(email, []).append(time.time())


def _safe_next(target: str | None) -> str:
    """Where to go after signing in, refusing anywhere that is not this site.

    A `next` parameter is a redirect somebody else can write — it arrives in a
    link — so anything with a scheme or a host in it is dropped rather than
    followed. "//evil.example" is the one that catches people out: no scheme, and
    a browser still treats it as another origin.
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/dashboard"
    return target


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="TGC Platform",
        version="0.1.0",
        summary=(
            "Customer profiles and supplier coordination on one database: who "
            "buys, and what has to be bought to serve them."
        ),
        # Turned off here and served again below, behind the sign-in. The
        # reference returns no customer and no order, but it is a complete map of
        # the API — every path, every field name, every shape — and handing that
        # to whoever finds the URL is the reconnaissance step done for them.
        # /health stays open: uptime checks call it, and it says only whether the
        # service is up and which environment it is.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
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

    # Read once at start-up rather than per request: the accounts come from the
    # environment, so they cannot change without a restart anyway, and hashing a
    # password is the only expensive thing on the sign-in path.
    users = auth.parse_users(settings.console_users)
    log = logging.getLogger("sca")
    if not users:
        log.warning(
            "no console accounts configured: nobody can sign in. Create one with "
            "`python -m scripts.make_user you@example.com` and put the line it "
            "prints in SCA_CONSOLE_USERS."
        )

    # A secret per process when none is configured. Signing still works; a cookie
    # issued by a previous process, or by the other worker, does not verify. The
    # symptom is being signed out at random, which is worth naming here because
    # it looks like a bug in the sign-in rather than a missing variable.
    session_secret = settings.session_secret or secrets.token_urlsafe(32)
    if not settings.session_secret and settings.env != "local":
        log.warning(
            "SCA_SESSION_SECRET is unset in env=%s: sessions are signed with a key "
            "generated at start-up, so every restart and every extra worker signs "
            "everyone out.",
            settings.env,
        )

    login_page = Path(__file__).parent / "console" / "login.html"

    def _who(request: Request) -> str | None:
        """The signed-in address, or None. The only question the page routes ask."""
        return auth.read(request.cookies.get(auth.COOKIE), session_secret)

    def _login_html(next_to: str, note: str = "", code: int = 200) -> HTMLResponse:
        page = login_page.read_text(encoding="utf-8")
        # Escaped on the way in. `next` comes from the address bar and lands in an
        # attribute; a message can quote an address somebody typed.
        page = page.replace("__NEXT__", html.escape(next_to, quote=True))
        page = page.replace("__ENV__", html.escape(settings.env))
        if note:
            page = page.replace("<!--NOTE-->", f'<p class="note">{note}</p>', 1)
        return HTMLResponse(page, status_code=code, headers={"Cache-Control": "no-store"})

    @app.get("/login", include_in_schema=False)
    async def login_form(request: Request, next: str = "/dashboard") -> Response:
        # Already signed in: the sign-in page is not a destination, it is a gate,
        # and showing a form to somebody who is through it invites them to sign
        # out of a session that is working.
        if _who(request):
            return RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
        return _login_html(_safe_next(next), _NO_ACCOUNTS if not users else "")

    @app.post("/login", include_in_schema=False)
    async def login_submit(
        email: str = Form(...),
        password: str = Form(...),
        next: str = Form("/dashboard"),
    ) -> Response:
        target = _safe_next(next)
        key = email.strip().lower()
        if _throttled(key):
            return _login_html(
                target,
                "Too many attempts. Wait five minutes and try again.",
                status.HTTP_429_TOO_MANY_REQUESTS,
            )
        # Said before the attempt is judged, because "that email and password do
        # not match" is true and useless when there is nothing to match against:
        # it sends somebody hunting for a typo when the actual fault is a
        # variable nobody set on the server. This is not the same as naming which
        # addresses exist — there are none.
        if not users:
            return _login_html(target, _NO_ACCOUNTS, status.HTTP_503_SERVICE_UNAVAILABLE)
        who = auth.authenticate(email, password, users)
        if not who:
            _record_failure(key)
            # One message for a wrong password and for an address that does not
            # exist. Telling them apart is a way of asking this service which of
            # a list of addresses are real.
            return _login_html(
                target, "That email and password do not match.", status.HTTP_401_UNAUTHORIZED
            )
        _attempts.pop(key, None)
        # 303, not 307: the browser must switch to GET for the dashboard rather
        # than reposting the credentials to it.
        response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            auth.COOKIE,
            auth.issue(who, session_secret, settings.session_hours),
            max_age=settings.session_hours * 3600,
            httponly=True,          # script on the page can never read it
            samesite="lax",         # not sent from another site's form or fetch
            secure=settings.env != "local",  # https only, except on a laptop
            path="/",
        )
        return response

    # POST, and only POST. A sign-out on a GET is a link an image tag can fire,
    # and the browser's own prefetching has been known to fire it too.
    @app.post("/logout", include_in_schema=False)
    async def logout() -> Response:
        response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(auth.COOKIE, path="/")
        return response

    # The API reference, behind the same gate as the consoles. Same three
    # addresses as FastAPI's own, so every existing bookmark and every link in
    # the README still lands in the right place — it just asks who you are first.
    def _reference_gate(request: Request, at: str) -> Response | None:
        if _who(request):
            return None
        return RedirectResponse(
            "/login?next=" + quote(at), status_code=status.HTTP_303_SEE_OTHER
        )

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi_schema(request: Request) -> Response:
        gate = _reference_gate(request, "/docs")
        return gate or JSONResponse(app.openapi())

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui(request: Request) -> Response:
        gate = _reference_gate(request, "/docs")
        # The assets come from the same /static mount as the charting library,
        # for the same reasons: no CDN in the page, and it keeps working on a
        # shop's wifi. Falls back to the CDN if they have not been vendored.
        return gate or get_swagger_ui_html(
            openapi_url="/openapi.json", title="TGC Platform — API reference"
        )

    @app.get("/redoc", include_in_schema=False)
    async def redoc_ui(request: Request) -> Response:
        gate = _reference_gate(request, "/redoc")
        return gate or get_redoc_html(
            openapi_url="/openapi.json", title="TGC Platform — API reference"
        )


    supplier_console = Path(__file__).parent / "console" / "index.html"
    customer_console = Path(cdp.__file__).parent / "console" / "index.html"
    brand_console = Path(brand.__file__).parent / "console" / "index.html"
    forecast_console = Path(forecast.__file__).parent / "console" / "index.html"

    def render(request: Request, page: Path, active: str, route: str) -> Response:
        """One page, plus the rail that says which half you are looking at.

        ``route`` rather than ``active`` decides which section opens, because
        /dashboard and /cdp serve the same document and arrive at different
        destinations in it.

        Nobody signed in gets the sign-in page instead, carrying where they were
        going: a link to a supplier sent in a message should land on that
        supplier after signing in, not on the dashboard with the reason for
        opening the link lost.
        """
        user = _who(request)
        if not user:
            return RedirectResponse(
                "/login?next=" + quote(request.url.path + ("?" + request.url.query
                                                           if request.url.query else "")),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        markup = page.read_text(encoding="utf-8")
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
        markup = markup.replace("</head>", head + "</head>", 1)
        title = _PAGE_TITLE.get(route)
        if title:
            markup = re.sub(r"<title>[^<]*</title>", f"<title>{title}</title>", markup, count=1)
        default_view = _DEFAULT_VIEW.get(route, "dashboard")
        markup = markup.replace(
            "<body>", "<body>" + _nav(active, settings.env, default_view, user), 1
        )
        # No caching: the console changes far more often than the API, and a
        # stale copy looks like a bug in the API rather than in the browser. It
        # also now carries a name, and a shared laptop should not show the last
        # person's back button their colleague's console.
        return HTMLResponse(markup, headers={"Cache-Control": "no-store"})

    # The dashboard answers for both halves — customers on one side of it, the
    # supplier network on the other — so it has its own address rather than
    # sitting inside one of them and implying it belongs there. It is served
    # from the customer document because that is where its panels live; the URL
    # is what the reader sees, and the file is an implementation detail.
    @app.get("/", include_in_schema=False)
    async def home() -> RedirectResponse:
        return RedirectResponse("/dashboard", status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    # The charting library, served from here rather than a CDN. Three reasons and
    # they all point the same way: the consoles are opened on a laptop that is
    # sometimes on a shop's wifi and sometimes not; a chart that goes blank when
    # unpkg is unreachable looks like the data is missing rather than the script;
    # and a script tag pointing at somebody else's server is a third party that
    # can change what runs inside a page holding an API key.
    #
    # Cached hard, unlike the console pages: these are versioned, immutable files
    # and re-downloading half a megabyte on every view of the dashboard is the
    # cost that makes people stop opening it.
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard_page(request: Request) -> Response:
        return render(request, customer_console, "cdp", "/dashboard")

    @app.get("/cdp", include_in_schema=False)
    async def customer_page(request: Request) -> Response:
        return render(request, customer_console, "cdp", "/cdp")

    @app.get("/procure", include_in_schema=False)
    async def procurement_page(request: Request) -> Response:
        return render(request, supplier_console, "sca", "/procure")

    # Not /brand: the API router already owns that prefix, and a page served
    # there would shadow the endpoints the page itself calls.
    @app.get("/brand-console", include_in_schema=False)
    async def brand_page(request: Request) -> Response:
        return render(request, brand_console, "brand", "/brand-console")

    # Same reason as the brand page: the API router owns /forecast, and a page
    # served there would shadow the endpoints the page itself calls.
    @app.get("/forecast-console", include_in_schema=False)
    async def forecast_page(request: Request) -> Response:
        return render(request, forecast_console, "forecast", "/forecast-console")

    app.include_router(brand_api.router)
    app.include_router(forecast_api.router)
    app.include_router(catalog.router)
    app.include_router(inventory.router)
    app.include_router(settings_api.router)
    app.include_router(orders.router)
    app.include_router(sales.router)
    app.include_router(inbound.router)
    app.include_router(whatsapp_api.router)
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
