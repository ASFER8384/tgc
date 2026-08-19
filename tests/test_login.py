"""The console is behind a sign-in, and there is no way to make an account from
a browser.

The API is deliberately not covered here: it authenticates on X-API-Key and that
has not changed, which is what the last test in this file asserts. Every other
test in the suite depends on it staying true.
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import sca.main
from sca.auth import COOKIE, hash_password, issue, parse_users, read, verify_password
from sca.config import get_settings
from sca.db import session_dep
from sca.main import create_app

# Hashing is deliberately slow, so the suite pays for it once rather than per
# test.
PASSWORD = "correct-horse-battery"
HASH = hash_password(PASSWORD)
EMAIL = "buyer@tgc.sa"


@pytest_asyncio.fixture
async def client(sessionmaker_fixture, monkeypatch):
    monkeypatch.setenv("SCA_CONSOLE_USERS", f"{EMAIL}:{HASH}")
    monkeypatch.setenv("SCA_SESSION_SECRET", "test-secret")
    get_settings.cache_clear()
    # Per process and never reset by the app itself, so a test that exhausts the
    # limit would otherwise lock out the tests after it.
    sca.main._attempts.clear()
    app = create_app()

    async def override():
        async with sessionmaker_fixture() as session:
            yield session
            await session.commit()

    app.dependency_overrides[session_dep] = override
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        # Carried on the client so a test can sweep the app's own route table
        # rather than a list of paths written by hand and left behind.
        client.app = app
        yield client
    get_settings.cache_clear()


async def sign_in(client, email=EMAIL, password=PASSWORD, next_to="/dashboard"):
    return await client.post(
        "/login", data={"email": email, "password": password, "next": next_to}
    )


# ---------- the gate ----------


@pytest.mark.parametrize(
    "path", ["/dashboard", "/cdp", "/procure", "/brand-console", "/forecast-console"]
)
async def test_every_console_needs_a_signed_in_person(client, path):
    response = await client.get(path)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


async def test_the_page_you_asked_for_is_where_you_land(client):
    response = await client.get("/procure?view=settings")
    assert response.headers["location"] == "/login?next=/procure%3Fview%3Dsettings"

    signed_in = await sign_in(client, next_to="/procure?view=settings")
    assert signed_in.headers["location"] == "/procure?view=settings"


async def test_signing_in_then_reading_the_dashboard(client):
    response = await sign_in(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    assert COOKIE in response.cookies

    page = await client.get("/dashboard")
    assert page.status_code == 200
    assert "tgc-rail" in page.text
    # Who is signed in is on the page, and so is the way out of it.
    assert EMAIL in page.text
    assert 'action="/logout"' in page.text


async def test_signing_out_puts_the_gate_back(client):
    await sign_in(client)
    out = await client.post("/logout")
    assert out.status_code == 303
    assert out.headers["location"] == "/login"
    assert (await client.get("/dashboard")).status_code == 303


async def test_the_key_is_not_served_to_a_stranger(client):
    """The console page carries the API key, which is the whole reason the page
    itself has to be behind the sign-in and not only the data on it."""
    assert "__SCA_DEV_KEY__" not in (await client.get("/dashboard")).text
    await sign_in(client)
    assert "__SCA_DEV_KEY__" in (await client.get("/dashboard")).text


# ---------- refusals ----------


async def test_a_wrong_password_gets_nothing(client):
    response = await sign_in(client, password="not-it")
    assert response.status_code == 401
    assert COOKIE not in response.cookies
    assert (await client.get("/dashboard")).status_code == 303


async def test_an_unknown_address_reads_the_same_as_a_wrong_password(client):
    """Telling them apart would make this a way of asking which addresses are
    real."""
    unknown = await sign_in(client, email="nobody@tgc.sa")
    wrong = await sign_in(client, password="not-it")
    assert unknown.status_code == wrong.status_code == 401
    assert "do not match" in unknown.text and "do not match" in wrong.text


async def test_guessing_fast_stops_working(client):
    for _ in range(6):
        assert (await sign_in(client, password="guess")).status_code == 401
    # And the real password is refused too, which is the point: the lock is on
    # the address, not on the wrongness of the attempt.
    assert (await sign_in(client)).status_code == 429


async def test_next_cannot_point_at_another_site(client):
    for target in ["//evil.example", "https://evil.example", "javascript:alert(1)"]:
        response = await sign_in(client, next_to=target)
        assert response.headers["location"] == "/dashboard"


async def test_there_is_nowhere_to_register(client):
    page = await client.get("/login")
    assert page.status_code == 200
    assert "/register" not in page.text
    assert "sign up" not in page.text.lower()
    # Not "method not allowed" — the route does not exist at all.
    assert (await client.post("/register", data={})).status_code == 404


async def test_a_signed_in_person_is_not_shown_the_form_again(client):
    await sign_in(client)
    response = await client.get("/login")
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"


async def test_the_page_never_explains_the_service_to_a_stranger(
    monkeypatch, sessionmaker_fixture
):
    """A console with no accounts refuses like any other failure and says nothing
    about why. Whoever runs the service is told in the log at start-up; whoever
    finds the URL is not told which variables it reads or what feeds them."""
    monkeypatch.setenv("SCA_CONSOLE_USERS", "")
    get_settings.cache_clear()
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False
    ) as bare:
        first_load = await bare.get("/login")
        attempt = await bare.post(
            "/login", data={"email": EMAIL, "password": PASSWORD, "next": "/dashboard"}
        )
    get_settings.cache_clear()

    assert first_load.status_code == 200
    assert attempt.status_code == 401
    for response in (first_load, attempt):
        for leak in ["SCA_", "make_user", "python -m", ".env", "environment"]:
            assert leak not in response.text, leak


async def test_the_reference_is_behind_the_gate_too(client):
    """It returns no data, but it is a complete map of the API — every path and
    every field name — and that is the reconnaissance step, not the break-in."""
    for path in ["/docs", "/redoc", "/openapi.json"]:
        assert (await client.get(path)).status_code == 303, path
    await sign_in(client)
    for path in ["/docs", "/redoc", "/openapi.json"]:
        assert (await client.get(path)).status_code == 200, path


async def test_nothing_but_the_door_answers_a_stranger(client):
    """Swept over every route the app declares rather than a list written by
    hand, so a router added next year is covered the day it is added.

    Three things answer and all three have to: the redirect to the sign-in, the
    sign-in itself, and the health check an uptime monitor calls — which says
    only whether the service is up, and holds nothing belonging to anybody.
    """
    paths = [
        (method.upper(), path)
        for path, operations in client.app.openapi()["paths"].items()
        for method in operations
        if method.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE")
    ]
    paths += [("GET", p) for p in ["/", "/dashboard", "/cdp", "/procure", "/login",
                                   "/brand-console", "/forecast-console", "/health"]]
    assert len(paths) > 100, "the sweep found almost nothing, so it proves nothing"

    answered = []
    for method, path in sorted(set(paths)):
        url = path
        while "{" in url:
            url = url[: url.index("{")] + "1" + url[url.index("}") + 1 :]
        response = await client.request(
            method, url, json={} if method in ("POST", "PUT", "PATCH") else None
        )
        refused = response.status_code in (401, 403) or (
            response.status_code in (303, 307)
            and "/login" in response.headers.get("location", "")
        )
        if not refused and response.status_code not in (404, 405):
            answered.append(f"{method} {path} -> {response.status_code}")

    assert sorted(answered) == [
        "GET / -> 307",
        "GET /health -> 200",
        "GET /login -> 200",
    ], answered


async def test_the_api_did_not_change(client):
    """No session, and the API still answers on the key alone."""
    key = get_settings().api_key
    assert (await client.get("/suppliers", headers={"X-API-Key": key})).status_code == 200
    assert (await client.get("/suppliers")).status_code == 401


# ---------- the pieces underneath ----------


def test_a_password_never_appears_in_its_hash():
    stored = hash_password("hunter2hunter2")
    assert "hunter2hunter2" not in stored
    assert verify_password("hunter2hunter2", stored)
    assert not verify_password("hunter2hunter3", stored)
    # Salted, so two people with the same password do not have the same record.
    assert stored != hash_password("hunter2hunter2")


def test_a_damaged_record_fails_shut():
    for stored in ["", "nonsense", "pbkdf2_sha256$600000$onlythree", "md5$1$a$b"]:
        assert not verify_password("anything", stored)


def test_accounts_are_read_forgivingly_but_not_loosely():
    parsed = parse_users(f" Buyer@TGC.sa:{HASH} , , broken-entry ,")
    assert list(parsed) == [EMAIL]


def test_a_session_cookie_is_only_ours():
    token = issue(EMAIL, "secret", 12)
    assert read(token, "secret") == EMAIL
    assert read(token, "another-secret") is None
    assert read(token[:-2] + "xx", "secret") is None
    assert read(None, "secret") is None
    # Expiry is inside what is signed, so it cannot be edited without breaking it.
    assert read(issue(EMAIL, "secret", -1), "secret") is None
