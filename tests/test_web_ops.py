"""Operator pages (web/ops_views.py): resolve dry-run, job profiles, raw job, skills, audit."""

import json

import pytest

from hil_controller.db.connection import audit_event, get_db
from hil_controller.web.ops_views import redact, render_markdown_safe

TOKEN = "test-token-for-ci"
COOKIE = {"hil_token": TOKEN}

GET_PAGES = [
    "/ui/jobs/resolve",
    "/ui/jobs/profiles",
    "/ui/jobs/raw",
    "/ui/skills",
    "/ui/skills/hil-job-api",
    "/ui/audit",
]

PY_JOB = {
    "target": {"device": {"kind": "sbc"}, "pool": "wippersnapper-python"},
    "script": "pytest-suite",
    "params": {"entry": "python", "args": ["-m", "pytest", "-v"]},
    "payload": {
        "kind": "git-source",
        "source": {"repo": "https://github.com/adafruit/Wippersnapper_Python.git", "ref": "main"},
    },
}

# Distinctive fake secrets so a leak is unambiguous in the rendered HTML.
SECRET_KEY = "aio_SuperSecretValueXYZ1234"
SECRET_WIFI = "hunter2-wifi-passphrase"
SECRET_PAT = "ghp_patTokenValue987654"


# ---------------------------------------------------------------------------
# Auth + rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", GET_PAGES)
async def test_pages_require_auth(client, path):
    r = await client.get(path, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/ui/jobs/raw/validate", "/ui/jobs/raw/submit"])
async def test_raw_posts_require_auth(client, path):
    r = await client.post(path, data={"job_json": json.dumps(PY_JOB)})
    assert r.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("path", GET_PAGES)
async def test_pages_render(client, path):
    r = await client.get(path, cookies=COOKIE)
    assert r.status_code == 200, r.text[:300]
    assert 'class="ops-nav"' in r.text
    # Not swallowed by the legacy /ui/jobs/{job_id} detail route.
    assert "Job not found" not in r.text


# ---------------------------------------------------------------------------
# 1. Resolve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_shows_match_with_aux_binding(seeded_client):
    r = await seeded_client.get(
        "/ui/jobs/resolve",
        params={"run": "1", "pool": "public", "kind": "microcontroller", "aux_kind": "display"},
        cookies=COOKIE,
    )
    assert r.status_code == 200
    assert 'data-testid="resolve-match"' in r.text
    assert "fake-qtpy-01" in r.text and "fake-mcu-host" in r.text
    assert "fake-oled-01" in r.text  # topology aux binding
    assert "kind=&#39;microcontroller&#39; matched" in r.text


@pytest.mark.asyncio
async def test_resolve_no_match_reason(seeded_client):
    r = await seeded_client.get(
        "/ui/jobs/resolve",
        params={"run": "1", "pool": "wippersnapper-python", "kind": "sbc", "model": "pi-zero"},
        cookies=COOKIE,
    )
    assert 'data-testid="resolve-nomatch"' in r.text
    assert "No available device matched job target" in r.text
    assert "no_device" in r.text  # topology-resolve rejection reason


@pytest.mark.asyncio
async def test_resolve_i2c_strand_requirement(seeded_client):
    # No strand yet: the requirement alone rules the device out.
    params = {"run": "1", "pool": "public", "kind": "microcontroller", "strand_caps": "PMSA003I"}
    r = await seeded_client.get("/ui/jobs/resolve", params=params, cookies=COOKIE)
    assert 'data-testid="resolve-nomatch"' in r.text
    assert "no_strand" in r.text

    strand = {
        "id": "strand-air",
        "mux_aux": "mux1",
        "mux_group": "muxA",
        "components": [{"id": "c1", "model": "pmsa003i", "capabilities": ["sensor:pm25"]}],
        "routes": [{"device": "fake-qtpy-01", "channel": 3}],
    }
    assert (await seeded_client.post("/v1/strands", json=strand)).status_code == 201
    r = await seeded_client.get("/ui/jobs/resolve", params=params, cookies=COOKIE)
    assert 'data-testid="resolve-match"' in r.text
    assert "strand-air" in r.text and "mux ch 3" in r.text
    assert "select_i2c_strand would be auto-prepended" in r.text


@pytest.mark.asyncio
async def test_resolve_raw_target_json(seeded_client):
    target = {"device": {"id": "fake-pi5-01"}, "pool": "wippersnapper-python"}
    r = await seeded_client.get(
        "/ui/jobs/resolve",
        params={"run": "1", "target_json": json.dumps({"target": target, "script": "x"})},
        cookies=COOKIE,
    )
    assert 'data-testid="resolve-match"' in r.text
    assert "device.id=fake-pi5-01 is authoritative" in r.text

    bad = await seeded_client.get(
        "/ui/jobs/resolve", params={"run": "1", "target_json": "{nope"}, cookies=COOKIE
    )
    assert "Target JSON is not valid JSON" in bad.text


@pytest.mark.asyncio
async def test_resolve_headless_controller_uses_db(client):
    # No topology file: the page builds a DB-only registry instead of crashing.
    r = await client.get(
        "/ui/jobs/resolve", params={"run": "1", "kind": "microcontroller"}, cookies=COOKIE
    )
    assert r.status_code == 200
    assert 'data-testid="resolve-nomatch"' in r.text


# ---------------------------------------------------------------------------
# 2. Profiles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profiles_cards_and_use_this(client):
    r = await client.get("/ui/jobs/profiles", cookies=COOKIE)
    assert "io.adafruit.us" in r.text and "dev-lan" in r.text
    assert "io_ws_expect" in r.text
    assert "IO_USERNAME" in r.text and "IO_KEY" in r.text
    assert "AgentJob JSON schema" in r.text and "&#34;runtime&#34;" in r.text
    assert 'href="/ui/jobs/raw?recipe=staging-io-checkin"' in r.text

    pre = await client.get("/ui/jobs/raw", params={"recipe": "circuitpython-code"}, cookies=COOKIE)
    assert "HIL_CPY_EXAMPLE_OK" in pre.text
    staging = await client.get(
        "/ui/jobs/raw", params={"recipe": "staging-io-checkin"}, cookies=COOKIE
    )
    assert '<option value="staging" selected>' in staging.text


# ---------------------------------------------------------------------------
# 3. Raw job
# ---------------------------------------------------------------------------


async def _validate(client, job, **form):
    text = job if isinstance(job, str) else json.dumps(job)
    return await client.post(
        "/ui/jobs/raw/validate", data={"job_json": text, **form}, cookies=COOKIE
    )


@pytest.mark.asyncio
async def test_raw_bad_json_clear_error(client):
    r = await _validate(client, '{"target": {"device": {}},\n  "script": }')
    assert 'data-testid="raw-errors"' in r.text
    assert "Invalid JSON" in r.text and "line 2" in r.text

    r = await _validate(client, "[1, 2]")
    assert "must be a JSON object" in r.text


@pytest.mark.asyncio
async def test_raw_schema_error_names_field_without_echo(client):
    r = await _validate(client, {"script": "pytest-suite", "secrets": {"IO_KEY": SECRET_KEY}})
    assert "target: Field required" in r.text
    assert SECRET_KEY not in r.text


@pytest.mark.asyncio
async def test_raw_agent_job_builds_mcp_request(client):
    r = await _validate(
        client, {"runtime": "wippersnapper-arduino", "device_id": "dut-1"}, resolve="1"
    )
    assert 'data-testid="raw-valid"' in r.text
    assert "agent-job" in r.text
    assert "verify_checkin" in r.text and "write_secrets_msc" in r.text
    assert "bench-protomq" in r.text
    assert "Device resolution" in r.text  # dry-run of the built target
    # Public defaults are masked too (redaction never shows a whole secret value).
    assert "free4all" not in r.text and "****4all" in r.text


@pytest.mark.asyncio
async def test_raw_preview_redacts_secrets(client):
    job = {
        **PY_JOB,
        "params": {**PY_JOB["params"], "pat": SECRET_PAT},
        "payload": {
            "kind": "git-source",
            "source": {
                "repo": f"https://x-access-token:{SECRET_PAT}@github.com/acme/private.git",
                "ref": "main",
            },
        },
        "secrets": {"IO_USERNAME": "someone", "IO_KEY": SECRET_KEY, "WIFI_PASSWORD": SECRET_WIFI},
        "metadata": {"note": f"key is {SECRET_KEY}"},
    }
    r = await _validate(client, job)
    assert 'data-testid="raw-valid"' in r.text
    for secret in (SECRET_KEY, SECRET_WIFI, SECRET_PAT, "SuperSecret", "hunter2"):
        assert secret not in r.text
    assert "****1234" in r.text  # redact.py keeps only the last 4 characters
    assert "****@github.com" in r.text


@pytest.mark.asyncio
async def test_raw_staging_broker_stamps_io_stages(client):
    job = {
        "target": {"device": {"id": "dut-1"}},
        "script": "firmware-bench",
        "params": {
            "stages": [{"type": "write_secrets_msc"}, {"type": "io_ws_expect", "action": "checkin"}]
        },
        "secrets": {"IO_USERNAME": "stage-user", "IO_KEY": SECRET_KEY},
    }
    r = await _validate(client, job, broker="staging")
    assert 'data-testid="raw-valid"' in r.text, r.text
    preview = r.text.split('data-testid="raw-preview">', 1)[1]
    assert "&#34;io_host&#34;: &#34;io.adafruit.us&#34;" in preview
    assert "&#34;io_url&#34;: &#34;io.adafruit.us&#34;" in preview
    assert "adafruit-staging" in preview
    assert SECRET_KEY not in r.text

    # Without a broker the io_* stage is flagged; without credentials staging refuses.
    plain = await _validate(client, job)
    assert "has no io_host" in plain.text
    no_creds = await _validate(client, {**job, "secrets": {}}, broker="staging")
    assert "needs your own IO_USERNAME and IO_KEY" in no_creds.text


@pytest.mark.asyncio
async def test_raw_agent_job_placeholder_and_dev_lan(client):
    from hil_controller.web.ops_views import RECIPES

    staging = RECIPES["staging-io-checkin"]["job"]
    r = await _validate(client, staging)
    assert "Replace the placeholder secret values" in r.text

    filled = {**staging, "secrets": {"IO_USERNAME": "lan-user", "IO_KEY": SECRET_KEY}}
    r = await _validate(
        client, filled, broker="dev-lan", broker_host="10.0.0.5", broker_port="1883"
    )
    assert 'data-testid="raw-valid"' in r.text, r.text
    assert "&#34;io_host&#34;: &#34;10.0.0.5&#34;" in r.text and "1883" in r.text
    r = await _validate(client, filled, broker="dev-lan")
    assert "dev-lan requires broker_host and broker_port" in r.text


@pytest.mark.asyncio
async def test_raw_submit_creates_job(app, client):
    job = {**PY_JOB, "secrets": {"IO_KEY": SECRET_KEY}}
    r = await client.post(
        "/ui/jobs/raw/submit",
        data={"job_json": json.dumps(job)},
        cookies=COOKIE,
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    location = r.headers["HX-Redirect"]
    assert location.startswith("/ui/jobs/")
    job_id = location.rsplit("/", 1)[1]
    snap = await client.get(f"/v1/jobs/{job_id}", headers={"Authorization": f"Bearer {TOKEN}"})
    assert snap.status_code == 200
    async with get_db(app.state.db_path) as db:
        async with db.execute("SELECT request_json FROM jobs WHERE id = ?", (job_id,)) as cur:
            stored = json.loads((await cur.fetchone())["request_json"])
        async with db.execute("SELECT event FROM audit_log WHERE entity_id = ?", (job_id,)) as cur:
            events = [row["event"] for row in await cur.fetchall()]
    assert stored["script"] == "pytest-suite"
    assert stored["metadata"]["submitted_via"] == "ui/jobs/raw"
    assert events == ["job.submit"]  # same handler as POST /v1/jobs

    # Without HTMX the same submit falls back to a normal redirect.
    r = await client.post(
        "/ui/jobs/raw/submit",
        data={"job_json": json.dumps(PY_JOB)},
        cookies=COOKIE,
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/ui/jobs/")


@pytest.mark.asyncio
async def test_raw_submit_invalid_does_not_create(app, client):
    r = await client.post(
        "/ui/jobs/raw/submit",
        data={"job_json": "{not json"},
        cookies=COOKIE,
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 422
    assert "HX-Redirect" not in r.headers
    assert "Not submitted" in r.text and "Invalid JSON" in r.text
    async with get_db(app.state.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM jobs") as cur:
            assert (await cur.fetchone())[0] == 0


# ---------------------------------------------------------------------------
# 4. Skills
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skills_list_and_read(client):
    from hil_controller.mcp_server import SkillStore

    r = await client.get("/ui/skills", cookies=COOKIE)
    names = [s["name"] for s in SkillStore().catalog()]
    assert "hil-job-api" in names
    for name in names:
        assert f'href="/ui/skills/{name}"' in r.text
    detail = await client.get("/ui/skills/hil-job-api", cookies=COOKIE)
    assert '<h2 class="md-h">hil-job-api</h2>' in detail.text
    assert '<pre class="md-code">' in detail.text
    assert SkillStore().catalog()[names.index("hil-job-api")]["sha256"] in detail.text


@pytest.mark.asyncio
async def test_skill_html_is_escaped(client, tmp_path, monkeypatch):
    skill = tmp_path / "evil-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        '---\nname: evil-skill\ndescription: "<img src=x onerror=alert(1)>"\n---\n'
        "# Title <script>alert('h')</script>\n\n"
        "Some `<b>code</b>` and <iframe src=//evil></iframe> **bold**\n\n"
        "```html\n<script>alert('fence')</script>\n```\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HIL_SKILLS_DIR", str(tmp_path))
    listing = await client.get("/ui/skills", cookies=COOKIE)
    assert "evil-skill" in listing.text
    assert "<img src=x" not in listing.text and "&lt;img src=x" in listing.text

    r = await client.get("/ui/skills/evil-skill", cookies=COOKIE)
    assert r.status_code == 200
    body = r.text
    assert "<script>alert" not in body
    assert "<iframe" not in body and "<img src=x" not in body
    assert "&lt;script&gt;alert(&#x27;h&#x27;)&lt;/script&gt;" in body
    assert "<code>&lt;b&gt;code&lt;/b&gt;</code>" in body
    assert "<strong>bold</strong>" in body

    missing = await client.get("/ui/skills/Not_A_Skill", cookies=COOKIE)
    assert missing.status_code == 404


def test_render_markdown_safe_unit():
    out = render_markdown_safe("## H `x<y`\ntext & <b>\n```\n</pre><script>\n")
    assert '<h3 class="md-h">H <code>x&lt;y</code></h3>' in out
    assert "text &amp; &lt;b&gt;" in out
    assert "&lt;/pre&gt;&lt;script&gt;" in out  # unterminated fence still escaped
    assert "<script>" not in out


# ---------------------------------------------------------------------------
# 5. Audit
# ---------------------------------------------------------------------------


async def _seed_audit(app):
    async with get_db(app.state.db_path) as db:
        await audit_event(
            db,
            "job.submit",
            subject="ci",
            entity_id="job-aaa",
            detail={"pool": "public", "script": "firmware-bench"},
        )
        await audit_event(db, "job.cancel", subject="ci", entity_id="job-aaa")
        await audit_event(
            db,
            "device.rename",
            subject="rename",
            entity_id="qtpy-new",
            detail={"old_id": "qtpy-old", "new_id": "qtpy-new"},
        )
        await audit_event(
            db,
            "topology.seed.device_drift",
            subject="seeder",
            entity_id="qtpy-new",
            detail={"fields": {"serial_port": "x"}},
        )
        await audit_event(db, "auth.fail", detail={"hint": "hil_abcdefgh..."})


@pytest.mark.asyncio
async def test_audit_filters_and_detail(app, client):
    await _seed_audit(app)
    r = await client.get("/ui/audit", cookies=COOKIE)
    assert r.text.count("data-audit-id=") == 5
    assert "&#34;old_id&#34;: &#34;qtpy-old&#34;" in r.text  # pretty-printed detail
    assert "<details>" in r.text
    assert "hil_abcdefgh" not in r.text  # token-prefix hint masked

    r = await client.get("/ui/audit", params={"event": "device.rename"}, cookies=COOKIE)
    assert r.text.count("data-audit-id=") == 1 and "qtpy-old" in r.text

    r = await client.get("/ui/audit", params={"event": "job.*"}, cookies=COOKIE)
    assert r.text.count("data-audit-id=") == 2

    r = await client.get("/ui/audit", params={"entity_id": "qtpy"}, cookies=COOKIE)
    assert r.text.count("data-audit-id=") == 2

    r = await client.get("/ui/audit", params={"event": "job.*", "entity_id": "aaa"}, cookies=COOKIE)
    assert r.text.count("data-audit-id=") == 2


@pytest.mark.asyncio
async def test_audit_pagination(app, client):
    await _seed_audit(app)
    r = await client.get("/ui/audit", params={"per_page": 2, "page": 2}, cookies=COOKIE)
    assert r.text.count("data-audit-id=") == 2
    assert "page 2 / 3" in r.text
    assert "page=1" in r.text and "page=3" in r.text
    last = await client.get("/ui/audit", params={"per_page": 2, "page": 99}, cookies=COOKIE)
    assert last.text.count("data-audit-id=") == 1  # clamped to the last page


# ---------------------------------------------------------------------------
# Redaction helper
# ---------------------------------------------------------------------------


def test_redact_masks_nested_and_embedded_values():
    req = {
        "secrets": {"WIFI_SSID": "bench-wifi", "IO_KEY": SECRET_KEY},
        "params": {"stages": [{"type": "io_ws_expect", "device_key": "dev-123"}]},
        "metadata": {"log": f"used {SECRET_KEY} for io"},
    }
    out = redact(req)
    assert out["secrets"] == {"WIFI_SSID": "****wifi", "IO_KEY": "****1234"}
    assert out["params"]["stages"][0]["device_key"] == "dev-123"  # identifier, not a secret
    assert SECRET_KEY not in json.dumps(out)
    assert req["secrets"]["IO_KEY"] == SECRET_KEY  # input untouched
