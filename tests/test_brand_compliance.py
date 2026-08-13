"""Brand compliance: mostly tests that the module refuses to overstate itself.

The failure this subject invites is a reassuring number. A site with nothing
recorded against it looks compliant, a rule nobody applied looks passed, and a
photograph too dark to read still gets judged. Each of those turns an absence
into a claim, and each has a test here.
"""

import pytest

SITE = {
    "code": "RUH-ALN-01",
    "name": "Aleena — Riyadh Park",
    "brand": "aleena",
    "kind": "activation",
    "city": "Riyadh",
    "contact": "activation lead",
}

HERO = {
    "code": "ALN-HERO",
    "title": "Hero product at eye level",
    "rule": "The campaign hero sits on the top shelf, unobstructed.",
    "compliance_class": "configuration",
    "severity": "high",
    "brand": "aleena",
}

PRICE = {
    "code": "PRICE-CUR",
    "title": "Price label is the current price",
    "rule": "Every displayed price matches the live price list.",
    "compliance_class": "text",
    "severity": "medium",
}


async def _observe(client, site_code=SITE["code"]) -> str:
    body = (await client.post("/brand/observations", json={
        "site_code": site_code, "source": "mobile", "captured_by": "field staff",
    })).json()
    return body["id"]


async def _site(client, code=SITE["code"]) -> dict:
    estate = (await client.get("/brand/sites")).json()
    return next(s for s in estate["sites"] if s["code"] == code)


# ----------------------------------------------------------------- coverage
@pytest.mark.asyncio
async def test_a_site_nobody_visited_reads_as_never_observed(client):
    """Not as compliant. This is the whole point of the module: an empty record
    is an absence of looking, not an absence of problems."""
    await client.post("/brand/sites", json=SITE)
    await client.post("/brand/standards", json=HERO)

    site = await _site(client)
    assert site["reads_as"] == "never observed"
    assert site["rules_applicable"] == 1
    assert site["rules_checked"] == 0


@pytest.mark.asyncio
async def test_a_photograph_nobody_reviewed_is_not_a_look(client):
    await client.post("/brand/sites", json=SITE)
    await client.post("/brand/standards", json=HERO)
    await _observe(client)

    site = await _site(client)
    assert site["reads_as"] == "not reviewed"


@pytest.mark.asyncio
async def test_checking_some_rules_is_not_checking_all_of_them(client):
    """Everything looked at passed, and the site still does not read as checked,
    because a rule nobody applied is not a rule that passed."""
    await client.post("/brand/sites", json=SITE)
    await client.post("/brand/standards", json=HERO)
    await client.post("/brand/standards", json=PRICE)
    observation = await _observe(client)

    await client.post(f"/brand/observations/{observation}/review", json={
        "checks": [{"standard_code": "ALN-HERO", "passed": True}],
    })
    site = await _site(client)
    assert site["reads_as"] == "partly checked"
    assert (site["rules_checked"], site["rules_applicable"]) == (1, 2)

    await client.post(f"/brand/observations/{observation}/review", json={
        "checks": [
            {"standard_code": "ALN-HERO", "passed": True},
            {"standard_code": "PRICE-CUR", "passed": True},
        ],
    })
    site = await _site(client)
    assert site["reads_as"] == "checked"


@pytest.mark.asyncio
async def test_only_the_latest_observation_counts(client):
    """A rule that passed in March must not cover for the same rule failing
    today, so coverage is read from the most recent look and not accumulated."""
    await client.post("/brand/sites", json=SITE)
    await client.post("/brand/standards", json=HERO)

    first = await _observe(client)
    await client.post(f"/brand/observations/{first}/review", json={
        "checks": [{"standard_code": "ALN-HERO", "passed": True}],
    })
    assert (await _site(client))["reads_as"] == "checked"

    await _observe(client)
    site = await _site(client)
    assert site["reads_as"] == "not reviewed"
    assert site["rules_checked"] == 0


# ------------------------------------------------------------ quality gating
@pytest.mark.asyncio
async def test_an_unusable_observation_cannot_be_judged(client):
    """The gate that stops an unreadable photograph being scored anyway."""
    await client.post("/brand/sites", json=SITE)
    await client.post("/brand/standards", json=HERO)
    observation = await _observe(client)

    rejected = await client.post(f"/brand/observations/{observation}/quality", json={
        "quality": "unusable", "reason": "too dark to see the fixture",
    })
    assert rejected.json()["recapture_requested"] is True

    refused = await client.post(f"/brand/observations/{observation}/review", json={
        "checks": [{"standard_code": "ALN-HERO", "passed": False}],
    })
    assert refused.status_code == 409


@pytest.mark.asyncio
async def test_rejecting_an_observation_requires_a_reason(client):
    """Somebody has to be told what to redo, or the rejection costs a visit and
    produces nothing."""
    await client.post("/brand/sites", json=SITE)
    observation = await _observe(client)
    refused = await client.post(f"/brand/observations/{observation}/quality", json={
        "quality": "unusable",
    })
    assert refused.status_code == 422


@pytest.mark.asyncio
async def test_an_unusable_observation_is_not_the_last_time_we_looked(client):
    await client.post("/brand/sites", json=SITE)
    await client.post("/brand/standards", json=HERO)
    observation = await _observe(client)
    await client.post(f"/brand/observations/{observation}/quality", json={
        "quality": "unusable", "reason": "blurred",
    })

    site = await _site(client)
    assert site["reads_as"] == "never observed"
    assert site["last_observed_at"] is None


# ------------------------------------------------------------------ findings
@pytest.mark.asyncio
async def test_a_failure_raises_one_finding_and_not_a_second(client):
    """The same rule failing again while the first is still open tells nobody
    anything new, and would only make the queue longer."""
    await client.post("/brand/sites", json=SITE)
    await client.post("/brand/standards", json=HERO)

    first = await _observe(client)
    raised = (await client.post(f"/brand/observations/{first}/review", json={
        "checks": [{"standard_code": "ALN-HERO", "passed": False, "note": "hero on the floor"}],
    })).json()
    assert raised["findings_raised"] == ["ALN-HERO"]

    second = await _observe(client)
    again = (await client.post(f"/brand/observations/{second}/review", json={
        "checks": [{"standard_code": "ALN-HERO", "passed": False, "note": "still on the floor"}],
    })).json()
    assert again["findings_raised"] == []
    assert again["already_open"] == ["ALN-HERO"]

    findings = (await client.get("/brand/findings")).json()
    assert len(findings) == 1
    assert findings[0]["detail"] == "hero on the floor"


@pytest.mark.asyncio
async def test_a_dismissal_does_not_erase_the_check_that_raised_it(client):
    """The observation still says what it said. A dismissal is a second
    judgement recorded beside the first, which is the only way to notice that
    one rule gets dismissed every time it fires."""
    await client.post("/brand/sites", json=SITE)
    await client.post("/brand/standards", json=HERO)
    observation = await _observe(client)
    await client.post(f"/brand/observations/{observation}/review", json={
        "checks": [{"standard_code": "ALN-HERO", "passed": False, "note": "obstructed"}],
    })
    finding = (await client.get("/brand/findings")).json()[0]

    await client.post(f"/brand/findings/{finding['id']}/close", json={
        "status": "dismissed", "note": "the fixture was mid-restock",
    })
    assert (await client.get("/brand/findings")).json() == []

    labels = (await client.get("/brand/labels")).json()
    assert [row["label"] for row in labels] == ["violation"]


# ----------------------------------------------------------------- standards
@pytest.mark.asyncio
async def test_revising_a_rule_supersedes_it_rather_than_editing_it(client):
    """A judgement made in March must still point at what the standard said in
    March, so a change publishes the next version."""
    await client.post("/brand/standards", json=PRICE)
    unchanged = (await client.post("/brand/standards", json=PRICE)).json()
    assert unchanged == {"code": "PRICE-CUR", "version": 1, "changed": False}

    revised = (await client.post("/brand/standards", json={
        **PRICE, "rule": "Every displayed price matches the live list, including sale tickets.",
    })).json()
    assert revised == {"code": "PRICE-CUR", "version": 2, "changed": True}

    live = (await client.get("/brand/standards")).json()
    assert [(s["code"], s["version"]) for s in live] == [("PRICE-CUR", 2)]
    everything = (await client.get("/brand/standards?include_retired=true")).json()
    assert len(everything) == 2


@pytest.mark.asyncio
async def test_a_finding_names_the_version_that_was_applied(client):
    await client.post("/brand/sites", json=SITE)
    await client.post("/brand/standards", json=HERO)
    observation = await _observe(client)
    await client.post(f"/brand/observations/{observation}/review", json={
        "checks": [{"standard_code": "ALN-HERO", "passed": False, "note": "obstructed"}],
    })
    # The rule is rewritten after the judgement was made.
    await client.post("/brand/standards", json={**HERO, "rule": "Something else entirely."})

    finding = (await client.get("/brand/findings")).json()[0]
    assert finding["standard_version"] == 1


@pytest.mark.asyncio
async def test_retiring_a_rule_does_not_inflate_coverage(client):
    """Found by retiring a rule in the console, which reported "2 of 1 checked".

    A rule that has left the standard is no longer something the site can be
    judged on, so a check made against it stops counting as coverage.
    """
    await client.post("/brand/sites", json=SITE)
    await client.post("/brand/standards", json=HERO)
    await client.post("/brand/standards", json=PRICE)
    observation = await _observe(client)
    await client.post(f"/brand/observations/{observation}/review", json={
        "checks": [
            {"standard_code": "ALN-HERO", "passed": True},
            {"standard_code": "PRICE-CUR", "passed": True},
        ],
    })
    assert (await _site(client))["rules_checked"] == 2

    await client.delete("/brand/standards/ALN-HERO")
    site = await _site(client)
    assert site["rules_applicable"] == 1
    assert site["rules_checked"] == 1
    assert site["rules_checked"] <= site["rules_applicable"]


@pytest.mark.asyncio
async def test_a_finding_against_a_retired_rule_is_counted_apart(client):
    """The site read "1 of 1 checked · 2 open", which looks like a miscount.

    It was not: one finding was against a rule retired since. That finding still
    stands — retiring the rule does not straighten the shelf — but it is counted
    separately, so the coverage figure and the open figure describe the same
    standard as each other.
    """
    await client.post("/brand/sites", json=SITE)
    await client.post("/brand/standards", json=HERO)
    await client.post("/brand/standards", json=PRICE)
    observation = await _observe(client)
    await client.post(f"/brand/observations/{observation}/review", json={
        "checks": [
            {"standard_code": "ALN-HERO", "passed": False, "note": "hero on the bottom shelf"},
            {"standard_code": "PRICE-CUR", "passed": False, "note": "sale ticket is last season"},
        ],
    })
    site = await _site(client)
    assert (site["findings_open"], site["findings_retired_rule"]) == (2, 0)

    await client.delete("/brand/standards/ALN-HERO")
    site = await _site(client)
    assert site["rules_applicable"] == 1
    assert site["findings_open"] == 1
    assert site["findings_retired_rule"] == 1
    # High severity travels with the finding that is still live, not the orphan.
    assert site["findings_high"] == 0

    findings = (await client.get("/brand/findings")).json()
    marked = {f["standard"]: f["standard_retired"] for f in findings}
    assert marked == {"ALN-HERO": True, "PRICE-CUR": False}


@pytest.mark.asyncio
async def test_a_retired_rule_cannot_be_applied(client):
    await client.post("/brand/sites", json=SITE)
    await client.post("/brand/standards", json=HERO)
    await client.delete("/brand/standards/ALN-HERO")
    observation = await _observe(client)

    refused = await client.post(f"/brand/observations/{observation}/review", json={
        "checks": [{"standard_code": "ALN-HERO", "passed": False}],
    })
    assert refused.status_code == 422


# ------------------------------------------------------------ applicability
@pytest.mark.asyncio
async def test_a_rule_for_another_brand_does_not_apply(client):
    await client.post("/brand/sites", json=SITE)
    await client.post("/brand/standards", json=HERO)
    await client.post("/brand/standards", json={
        **PRICE, "code": "RWS-ONLY", "brand": "rawash",
    })
    assert (await _site(client))["rules_applicable"] == 1


@pytest.mark.asyncio
async def test_a_rule_with_no_brand_applies_to_everything(client):
    """Narrowing is opt-in. The opposite default would let a rule be written and
    quietly apply to nothing."""
    await client.post("/brand/sites", json=SITE)
    await client.post("/brand/standards", json=PRICE)
    assert (await _site(client))["rules_applicable"] == 1


@pytest.mark.asyncio
async def test_a_rule_for_a_different_kind_of_site_does_not_apply(client):
    await client.post("/brand/sites", json=SITE)
    await client.post("/brand/standards", json={**PRICE, "site_kind": "digital"})
    assert (await _site(client))["rules_applicable"] == 0


# --------------------------------------------------------------------- misc
@pytest.mark.asyncio
async def test_an_unknown_site_is_refused_rather_than_created(client):
    refused = await client.post("/brand/observations", json={"site_code": "NOPE"})
    assert refused.status_code == 404


@pytest.mark.asyncio
async def test_the_same_photograph_twice_is_stored_once(client):
    await client.post("/brand/sites", json=SITE)
    observation = await _observe(client)
    photo = b"\x89PNG\r\n\x1a\n" + b"pretend this is a shop window" * 4

    first = await client.post(
        f"/brand/observations/{observation}/images",
        files={"file": ("window.png", photo, "image/png")},
    )
    second = await client.post(
        f"/brand/observations/{observation}/images",
        files={"file": ("window.png", photo, "image/png")},
    )
    assert first.json()["stored"] is True
    assert second.json()["stored"] is False
    assert first.json()["sha256"] == second.json()["sha256"]


@pytest.mark.asyncio
async def test_every_judgement_is_available_as_a_label(client):
    """Adjudications are the training set, accumulated as a by-product of
    reviewing rather than as a labelling project."""
    await client.post("/brand/sites", json=SITE)
    await client.post("/brand/standards", json=HERO)
    await client.post("/brand/standards", json=PRICE)
    observation = await _observe(client)
    await client.post(f"/brand/observations/{observation}/review", json={
        "checks": [
            {"standard_code": "ALN-HERO", "passed": False, "note": "obstructed"},
            {"standard_code": "PRICE-CUR", "passed": True},
        ],
        "reviewed_by": "brand manager",
    })

    labels = (await client.get("/brand/labels")).json()
    assert {row["standard"]: row["label"] for row in labels} == {
        "ALN-HERO": "violation",
        "PRICE-CUR": "compliant",
    }
    assert {row["judged_by_kind"] for row in labels} == {"human"}
    assert {row["judged_by"] for row in labels} == {"brand manager"}
