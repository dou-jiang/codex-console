# Proxy Batch Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add proxy batch import, auto-format recognition, geolocation-based naming, list filtering, and batch delete to the settings page without breaking existing proxy CRUD flows.

**Architecture:** Keep the UI inside the existing settings page, move parsing/geolocation/import orchestration into focused core modules, and extend the existing settings routes + proxy CRUD helpers rather than introducing a new subsystem. Persist `country` and `city` on `Proxy`, run geolocation lookups in a bounded thread pool, and keep DB writes/name allocation on the main thread for deterministic behavior.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy ORM, SQLite/PostgreSQL compatibility, vanilla JS, Jinja2 templates, pytest, curl-cffi/httpx-compatible monkeypatching

---

## Implementation Notes Before Starting

- Use the approved spec as the source of truth: `docs/superpowers/specs/2026-03-22-proxy-batch-import-design.md`
- If you are implementing instead of only planning, create/use a dedicated worktree before touching code.
- Keep the batch import scope narrow:
  - support IPv4 + domain
  - do not support IPv6
  - do not support `:` / `@` inside username or password
  - duplicate check is `host + port`
- Do not add new third-party dependencies unless absolutely necessary; use stdlib networking/threading first.

## File Structure Map

### Existing files to modify

- `src/database/models.py:175-229`
  - Add `country` / `city` fields to `Proxy`
  - Return location fields from `to_dict()`
- `src/database/session.py:95-144`
  - Extend SQLite auto-migration list for new proxy columns
- `src/database/crud.py:391-517`
  - Extend create/get/delete helpers for location-aware create, filters, duplicate detection, and batch delete
- `src/web/routes/settings.py:448-691`
  - Add request/response models for batch import + batch delete
  - Add filtered list endpoint behavior
  - Wire routes to the new core import service
- `templates/settings.html:48-130,154-200`
  - Add batch import card, filter controls, selection column, batch delete action, location column/result region
- `static/js/settings.js:7-75,113-235,766-977`
  - Register new DOM nodes, filter state, checkbox selection state, batch import handler, batch delete handler, enriched table rendering

### New files to create

- `src/core/proxy_import.py`
  - Parse each input line into a normalized proxy payload
  - Validate supported formats
  - Deduplicate candidates and orchestrate naming + persistence inputs
- `src/core/ip_location.py`
  - Resolve domain-to-IPv4 when needed
  - Query IP.SB first, FreeIPAPI second
  - Run bounded thread-pool lookups with per-batch caching
- `tests/test_proxy_import.py`
  - Unit tests for parsing, validation, caching, provider fallback, and name generation behavior
- `tests/test_proxy_settings_routes.py`
  - Route-level tests for batch import, filters, duplicate skip behavior, and batch delete
- `tests/test_settings_proxy_assets.py`
  - Static smoke tests ensuring the new settings UI markup/JS hooks are present

### Task 1: Build and lock down the proxy line parser

**Files:**
- Create: `tests/test_proxy_import.py`
- Create: `src/core/proxy_import.py`
- Reference: `docs/superpowers/specs/2026-03-22-proxy-batch-import-design.md`

- [ ] **Step 1: Write the failing parser tests**

```python
import pytest

from src.core.proxy_import import parse_proxy_line


def test_parse_proxy_line_supports_all_formats():
    cases = [
        ("http://1.1.1.1:8080:user:pass", "socks5", ("http", "1.1.1.1", 8080, "user", "pass")),
        ("2.2.2.2:9090:user:pass", "http", ("http", "2.2.2.2", 9090, "user", "pass")),
        ("user:pass@3.3.3.3:3128", "http", ("http", "3.3.3.3", 3128, "user", "pass")),
        ("4.4.4.4:1080@user:pass", "socks5", ("socks5", "4.4.4.4", 1080, "user", "pass")),
        ("proxy.example.com:8000", "http", ("http", "proxy.example.com", 8000, None, None)),
    ]

    for raw, default_type, expected in cases:
        parsed = parse_proxy_line(raw, default_type=default_type, line_no=1)
        assert (parsed.type, parsed.host, parsed.port, parsed.username, parsed.password) == expected


def test_parse_proxy_line_rejects_invalid_input():
    bad_lines = [
        "ftp://1.1.1.1:21:user:pass",
        "1.1.1.1:70000",
        "2001:db8::1:8080",
        "user:name@1.1.1.1:8080",
    ]

    for raw in bad_lines:
        with pytest.raises(ValueError):
            parse_proxy_line(raw, default_type="http", line_no=1)
```

- [ ] **Step 2: Run the parser test file and verify it fails**

Run: `pytest tests/test_proxy_import.py -v`

Expected: FAIL because `src.core.proxy_import` and `parse_proxy_line` do not exist yet.

- [ ] **Step 3: Implement the minimal parser**

```python
@dataclass
class ParsedProxyLine:
    line_no: int
    raw_line: str
    type: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None


def parse_proxy_line(raw_line: str, default_type: str, line_no: int) -> ParsedProxyLine:
    # trim -> skip comments/empty before caller
    # match formats in fixed priority order
    # validate host/port/protocol/auth separators
    return ParsedProxyLine(...)
```

Implementation notes:
- Use fixed recognition order from the spec.
- Validate domain names with a conservative regex or stdlib checks; do not accept IPv6.
- Use `default_type` only for formats without protocol.

- [ ] **Step 4: Re-run the parser tests**

Run: `pytest tests/test_proxy_import.py -v`

Expected: PASS for parser cases; any remaining missing tests are acceptable at this point only if they belong to later tasks.

- [ ] **Step 5: Commit the parser baseline**

```bash
git add tests/test_proxy_import.py src/core/proxy_import.py
git commit -m "feat: add proxy batch import parser"
```

### Task 2: Add threaded geolocation lookup and deterministic naming helpers

**Files:**
- Modify: `tests/test_proxy_import.py`
- Modify: `src/core/proxy_import.py`
- Create: `src/core/ip_location.py`

- [ ] **Step 1: Extend the test file with location lookup, fallback, cache, and naming tests**

```python
from src.core.ip_location import IPLocation, lookup_locations
from src.core.proxy_import import allocate_proxy_names


def test_lookup_locations_falls_back_and_caches(monkeypatch):
    calls = []

    def fake_ip_sb(ip):
        calls.append(("ip_sb", ip))
        raise RuntimeError("boom")

    def fake_freeipapi(ip):
        calls.append(("freeipapi", ip))
        return IPLocation(ip=ip, country="United States", city="Seattle")

    result = lookup_locations(["1.1.1.1", "1.1.1.1"], ip_sb_lookup=fake_ip_sb, freeip_lookup=fake_freeipapi)

    assert result["1.1.1.1"].country == "United States"
    assert calls == [("ip_sb", "1.1.1.1"), ("freeipapi", "1.1.1.1")]


def test_allocate_proxy_names_falls_back_when_country_or_city_missing():
    names = allocate_proxy_names(
        prefixes_in_db={"代理": 2, "美国-西雅图": 1},
        locations=[
            {"host": "1.1.1.1", "country": "美国", "city": "西雅图"},
            {"host": "2.2.2.2", "country": "美国", "city": ""},
        ],
    )
    assert names == {
        "1.1.1.1": "美国-西雅图-002",
        "2.2.2.2": "代理-003",
    }
```

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: `pytest tests/test_proxy_import.py -k "lookup_locations or allocate_proxy_names" -v`

Expected: FAIL because `src.core.ip_location` and/or helper functions are missing.

- [ ] **Step 3: Implement bounded threaded lookup + naming**

```python
@dataclass
class IPLocation:
    ip: str
    country: str = ""
    city: str = ""


def lookup_locations(hosts: list[str], ..., max_workers: int = 8) -> dict[str, IPLocation]:
    # resolve domains -> IPv4
    # de-duplicate targets
    # ThreadPoolExecutor(max_workers=max_workers)
    # try IP.SB first, FreeIPAPI second
    # cache by resolved IPv4 / host


def allocate_proxy_names(prefixes_in_db: dict[str, int], locations: list[dict[str, str]]) -> dict[str, str]:
    # only country+city => 国家-城市-001
    # else => 代理-001
```

Implementation notes:
- Keep provider functions injectable for tests.
- Set short request timeouts and swallow network exceptions into empty results.
- Do not write to DB here.

- [ ] **Step 4: Re-run the unit test file**

Run: `pytest tests/test_proxy_import.py -v`

Expected: PASS for parser, lookup, cache, fallback, and name allocation tests.

- [ ] **Step 5: Commit the geolocation helpers**

```bash
git add tests/test_proxy_import.py src/core/proxy_import.py src/core/ip_location.py
git commit -m "feat: add proxy geolocation lookup helpers"
```

### Task 3: Extend proxy persistence for location fields, filters, duplicate detection, and batch delete

**Files:**
- Modify: `src/database/models.py:175-229`
- Modify: `src/database/session.py:95-144`
- Modify: `src/database/crud.py:391-517`
- Create: `tests/test_proxy_settings_routes.py`

- [ ] **Step 1: Write failing persistence/filter tests**

```python
def test_get_proxies_supports_keyword_status_default_and_location_filters(temp_db):
    crud.create_proxy(db, name="美国-西雅图-001", type="http", host="1.1.1.1", port=8080, country="美国", city="西雅图")
    crud.create_proxy(db, name="代理-001", type="socks5", host="2.2.2.2", port=1080, enabled=False)
    crud.set_proxy_default(db, proxy_id=1)

    rows = crud.get_proxies(db, keyword="美国", type="http", enabled=True, is_default=True, location="西雅图")
    assert [row.host for row in rows] == ["1.1.1.1"]


def test_delete_proxies_batch_ignores_missing_ids(temp_db):
    deleted = crud.delete_proxies(db, [1, 999])
    assert deleted == 1
```

- [ ] **Step 2: Run the new test target and confirm it fails**

Run: `pytest tests/test_proxy_settings_routes.py -k "crud or filters or delete" -v`

Expected: FAIL because `country/city` fields, filtered `get_proxies`, and batch delete helpers do not exist yet.

- [ ] **Step 3: Implement the persistence changes**

```python
class Proxy(Base):
    country = Column(String(100))
    city = Column(String(100))


def create_proxy(..., country: str | None = None, city: str | None = None) -> Proxy:
    ...


def get_proxies(..., keyword=None, type=None, is_default=None, location=None):
    ...


def find_proxy_by_host_port(db: Session, host: str, port: int) -> Proxy | None:
    ...


def delete_proxies(db: Session, proxy_ids: list[int]) -> int:
    ...
```

Implementation notes:
- Update SQLite auto-migrations with `("proxies", "country", "VARCHAR(100)")` and `("proxies", "city", "VARCHAR(100)")`.
- Keep PostgreSQL compatibility by only using SQLAlchemy query APIs in CRUD.
- Return `country` / `city` from `Proxy.to_dict()`.

- [ ] **Step 4: Re-run the persistence/filter tests**

Run: `pytest tests/test_proxy_settings_routes.py -k "crud or filters or delete" -v`

Expected: PASS for the DB-level helper coverage.

- [ ] **Step 5: Commit the persistence layer**

```bash
git add src/database/models.py src/database/session.py src/database/crud.py tests/test_proxy_settings_routes.py
git commit -m "feat: add proxy location fields and filters"
```

### Task 4: Implement batch import, filtered list route, and batch delete route

**Files:**
- Modify: `src/web/routes/settings.py:448-691`
- Modify: `tests/test_proxy_settings_routes.py`
- Modify: `src/core/proxy_import.py`
- Reference: `src/database/crud.py`, `src/core/ip_location.py`

- [ ] **Step 1: Add failing route tests for batch import and batch delete**

```python
def test_batch_import_proxies_creates_rows_and_skips_duplicates(monkeypatch, temp_db):
    monkeypatch.setattr(settings_routes, "lookup_locations", lambda hosts, **_: {
        "1.1.1.1": IPLocation(ip="1.1.1.1", country="美国", city="西雅图"),
        "2.2.2.2": IPLocation(ip="2.2.2.2", country="", city=""),
    })

    result = asyncio.run(settings_routes.batch_import_proxies(
        settings_routes.ProxyBatchImportRequest(
            data="1.1.1.1:8080\nuser:pass@2.2.2.2:1080\n1.1.1.1:8080",
            default_type="http",
        )
    ))

    assert result["success"] == 2
    assert result["skipped"] == 1
    assert result["failed"] == 0


def test_get_proxies_list_accepts_filter_query_params(monkeypatch, temp_db):
    result = asyncio.run(settings_routes.get_proxies_list(
        enabled=True, keyword="美国", type="http", is_default=True, location="西雅图"
    ))
    assert result["total"] == 1


def test_batch_delete_route_returns_requested_deleted_and_missing(monkeypatch, temp_db):
    result = asyncio.run(settings_routes.batch_delete_proxies(
        settings_routes.ProxyBatchDeleteRequest(ids=[1, 2, 404])
    ))
    assert result == {"success": True, "requested": 3, "deleted": 2, "missing": [404]}
```

- [ ] **Step 2: Run the route test file and verify it fails**

Run: `pytest tests/test_proxy_settings_routes.py -v`

Expected: FAIL because request models/routes/service wiring are not implemented yet.

- [ ] **Step 3: Implement the route surface and batch import orchestration**

```python
class ProxyBatchImportRequest(BaseModel):
    data: str
    default_type: Literal["http", "socks5"] = "http"


class ProxyBatchDeleteRequest(BaseModel):
    ids: list[int]


@router.post("/proxies/batch-import")
async def batch_import_proxies(request: ProxyBatchImportRequest):
    # parse lines
    # skip comments/empty lines
    # query duplicates by host+port
    # lookup locations
    # allocate names
    # create proxies
    # return per-line results + totals


@router.post("/proxies/batch-delete")
async def batch_delete_proxies(request: ProxyBatchDeleteRequest):
    # delete known ids
    # return requested/deleted/missing summary
```

Implementation notes:
- Keep all DB writes on the main thread.
- Reuse `crud.find_proxy_by_host_port` for duplicate checks.
- Return stable machine-readable statuses: `success`, `skipped`, `failed`.

- [ ] **Step 4: Re-run the full route test file**

Run: `pytest tests/test_proxy_settings_routes.py -v`

Expected: PASS for batch import, filtered list, duplicate skip, and batch delete behavior.

- [ ] **Step 5: Commit the route implementation**

```bash
git add src/web/routes/settings.py src/core/proxy_import.py tests/test_proxy_settings_routes.py
git commit -m "feat: add proxy batch import routes"
```

### Task 5: Add settings page UI for batch import, filter controls, selection, and batch delete

**Files:**
- Modify: `templates/settings.html:48-130,154-200`
- Modify: `static/js/settings.js:7-75,113-235,766-977`
- Create: `tests/test_settings_proxy_assets.py`

- [ ] **Step 1: Write failing UI smoke tests**

```python
from pathlib import Path


def test_settings_template_contains_proxy_batch_import_controls():
    template = Path("templates/settings.html").read_text(encoding="utf-8")
    assert 'id="proxy-batch-import-form"' in template
    assert 'id="proxy-import-default-type"' in template
    assert 'id="proxy-filter-keyword"' in template
    assert 'id="batch-delete-proxies-btn"' in template


def test_settings_js_contains_proxy_batch_handlers():
    script = Path("static/js/settings.js").read_text(encoding="utf-8")
    assert "handleProxyBatchImport" in script
    assert "handleApplyProxyFilters" in script
    assert "handleBatchDeleteProxies" in script
```

- [ ] **Step 2: Run the UI smoke tests and confirm they fail**

Run: `pytest tests/test_settings_proxy_assets.py -v`

Expected: FAIL because the new markup IDs and JS handlers are not present yet.

- [ ] **Step 3: Implement the template + JS changes**

```html
<!-- template additions -->
<form id="proxy-batch-import-form">...</form>
<div class="proxy-filter-bar">...</div>
<button id="batch-delete-proxies-btn" ...>批量删除</button>
<tbody id="proxies-table"></tbody>
```

```javascript
const selectedProxyIds = new Set();
const proxyFilters = { keyword: "", type: "", enabled: "", is_default: "", location: "" };

async function handleProxyBatchImport() { ... }
async function handleApplyProxyFilters() { ... }
async function handleBatchDeleteProxies() { ... }
function renderProxies(proxies) { ... } // add checkbox + location column
```

Implementation notes:
- Keep the existing add/edit/test single-proxy flows working.
- Add a location column or fold country/city into the name/details cell.
- Make batch delete disabled when nothing is selected.
- Reuse existing `toast`, `confirm`, and `api` utilities.

- [ ] **Step 4: Re-run the UI smoke tests**

Run: `pytest tests/test_settings_proxy_assets.py -v`

Expected: PASS with the new IDs and handler names in place.

- [ ] **Step 5: Commit the UI changes**

```bash
git add templates/settings.html static/js/settings.js tests/test_settings_proxy_assets.py
git commit -m "feat: add proxy batch import settings ui"
```

### Task 6: Run integrated verification and do a manual smoke check

**Files:**
- Modify: none expected unless verification reveals defects
- Test: `tests/test_proxy_import.py`
- Test: `tests/test_proxy_settings_routes.py`
- Test: `tests/test_settings_proxy_assets.py`
- Reference: existing regression suite in `tests/`

- [ ] **Step 1: Run the focused automated regression suite**

Run: `pytest tests/test_proxy_import.py tests/test_proxy_settings_routes.py tests/test_settings_proxy_assets.py -v`

Expected: PASS for all new proxy-related tests.

- [ ] **Step 2: Run the full test suite**

Run: `pytest tests -v`

Expected: PASS with no regressions in existing mail/upload/registration/static-asset tests.

- [ ] **Step 3: Start the app and manually verify the UI**

Run: `python webui.py --host 127.0.0.1 --port 8000 --access-password testpass`

Manual checklist:
- Open `/settings`
- Import a mixed batch containing all 5 supported formats
- Confirm duplicates are reported as skipped
- Confirm location-based naming uses `国家-城市-001` only when both fields are present
- Confirm fallback naming uses `代理-001`
- Filter by keyword/type/status/default/location
- Multi-select rows and batch delete them

- [ ] **Step 4: Fix any verification failures before declaring success**

If any command above fails:
- capture the exact error
- write/adjust a failing test first
- make the minimal fix
- re-run the failing command and then the full suite

- [ ] **Step 5: Commit only if verification required follow-up fixes**

```bash
git add <fixed-files>
git commit -m "fix: address proxy batch import verification issues"
```

## Definition of Done

- Supported formats parse exactly as specified in the spec
- Duplicate `host + port` entries are skipped, not overwritten
- `country` and `city` are stored on proxy records
- Batch import returns line-by-line statuses
- Settings page exposes batch import, filters, row selection, and batch delete
- Focused tests and full regression suite pass
- Manual smoke check confirms the end-to-end UX
