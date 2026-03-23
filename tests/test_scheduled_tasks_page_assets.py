from fastapi.testclient import TestClient
from pathlib import Path
import json
import re
import subprocess

from src.config.settings import get_settings
from src.web.app import create_app


def _get_tag_by_id(template: str, tag: str, element_id: str) -> str:
    match = re.search(rf"<{tag}\b[^>]*\bid=\"{re.escape(element_id)}\"[^>]*>", template)
    assert match is not None, f"missing <{tag}> with id={element_id}"
    return match.group(0)


def _assert_tag_class_contains(tag_html: str, expected_class: str) -> None:
    class_match = re.search(r'class\s*=\s*"([^"]*)"', tag_html)
    assert class_match is not None, f"missing class attribute: {tag_html}"
    classes = set(class_match.group(1).split())
    assert expected_class in classes, f"class {expected_class!r} missing in {classes!r}"


def test_scheduled_tasks_page_requires_auth_and_renders_script():
    app = create_app()
    with TestClient(app) as client:
        unauthenticated = client.get("/scheduled-tasks", follow_redirects=False)
        assert unauthenticated.status_code == 302
        assert unauthenticated.headers["location"] == "/login?next=/scheduled-tasks"

        password = get_settings().webui_access_password.get_secret_value()
        login_response = client.post(
            "/login",
            data={"password": password, "next": "/scheduled-tasks"},
            follow_redirects=False,
        )
        assert login_response.status_code == 302

        response = client.get("/scheduled-tasks")
        assert response.status_code == 200
        assert 'id="scheduled-plans-table"' in response.text
        assert '/static/js/scheduled_tasks.js?v={{ static_version }}' not in response.text
        assert "/static/js/scheduled_tasks.js?v=" in response.text


def test_scheduled_tasks_script_defines_escape_html_helper():
    script = Path("static/js/scheduled_tasks.js").read_text(encoding="utf-8")
    assert "function escapeHtml(" in script
    # 至少要在定义之外被调用一次，否则无法覆盖页面渲染分支
    assert script.count("escapeHtml(") >= 2


def test_scheduled_tasks_template_contains_plan_management_hooks():
    template = Path("templates/scheduled_tasks.html").read_text(encoding="utf-8")
    assert 'id="create-plan-btn"' in template
    assert 'id="plan-form-modal"' in template
    assert 'id="plan-form"' in template
    assert 'id="plan-trigger-type"' in template
    assert 'id="plan-config-mode-table"' in template
    assert 'id="plan-config-mode-json"' in template
    assert 'id="plan-config-entries-body"' in template
    assert 'id="plan-config-add-entry-btn"' in template


def test_scheduled_tasks_template_contains_run_center_hooks():
    template = Path("templates/scheduled_tasks.html").read_text(encoding="utf-8")
    assert 'id="scheduled-runs-table"' in template
    assert 'id="scheduled-runs-table-body"' in template
    assert 'id="scheduled-run-filter-task-type"' in template
    assert 'id="scheduled-run-filter-status"' in template
    assert 'id="scheduled-run-filter-started-from"' in template
    assert 'id="scheduled-run-filter-started-to"' in template
    assert 'id="scheduled-run-filter-apply-btn"' in template
    assert 'id="scheduled-run-filter-reset-btn"' in template
    assert 'id="run-log-status-bar"' in template
    assert 'id="run-log-refresh-btn"' in template
    assert 'id="run-log-auto-scroll"' in template
    assert 'id="run-log-stop-actions"' in template
    assert 'id="run-log-stop-btn"' in template


def test_scheduled_tasks_run_center_filter_panel_uses_shared_shell_classes():
    template = Path("templates/scheduled_tasks.html").read_text(encoding="utf-8")
    assert "filter-panel" in template
    assert "filter-panel-grid" in template
    assert "filter-panel-actions" in template
    assert "pagination-panel" in template
    assert "pagination-jump" in template
    _assert_tag_class_contains(_get_tag_by_id(template, "select", "scheduled-run-filter-task-type"), "form-select")
    _assert_tag_class_contains(_get_tag_by_id(template, "select", "scheduled-run-filter-status"), "form-select")
    _assert_tag_class_contains(_get_tag_by_id(template, "input", "scheduled-run-filter-started-from"), "form-input")
    _assert_tag_class_contains(_get_tag_by_id(template, "input", "scheduled-run-filter-started-to"), "form-input")
    _assert_tag_class_contains(_get_tag_by_id(template, "input", "scheduled-run-page-jump-input"), "form-input")
    assert re.search(
        r'<div class="card-header filter-panel">\s*<h3>运行中心</h3>\s*<div class="filter-panel-grid">.*?</div>\s*<div class="filter-panel-actions">',
        template,
        re.DOTALL,
    )


def test_scheduled_tasks_template_contains_run_center_pagination_hooks():
    template = Path("templates/scheduled_tasks.html").read_text(encoding="utf-8")
    assert 'id="scheduled-run-prev-page"' in template
    assert 'id="scheduled-run-next-page"' in template
    assert 'id="scheduled-run-page-jump-input"' in template
    assert 'id="scheduled-run-page-jump-btn"' in template
    assert 'id="scheduled-run-pagination-summary"' in template


def test_scheduled_tasks_template_keeps_pagination_markup_minimal_without_inline_layout_styles():
    template = Path("templates/scheduled_tasks.html").read_text(encoding="utf-8")
    assert 'id="scheduled-run-pagination-summary" style=' not in template
    jump_input_match = re.search(r"<input[^>]*id=\"scheduled-run-page-jump-input\"[^>]*>", template)
    assert jump_input_match is not None
    assert "style=" not in jump_input_match.group(0)


def run_scheduled_tasks_pagination_scenario(name: str) -> dict:
    script_source = Path("static/js/scheduled_tasks.js").read_text(encoding="utf-8")
    node_script = rf"""
const vm = require('vm');

const scenarioName = {json.dumps(name)};
const scriptSource = {json.dumps(script_source)};

function createMockElement(id = '') {{
  let html = '';
  let text = '';
  const classes = new Set();
  return {{
    id,
    dataset: {{}},
    style: {{}},
    disabled: false,
    value: '',
    checked: false,
    innerHTML: '',
    textContent: '',
    _listeners: {{}},
    setAttribute() {{}},
    removeAttribute() {{}},
    addEventListener(type, handler) {{ this._listeners[type] = handler; }},
    dispatchEvent(type, event = {{}}) {{
      if (this._listeners[type]) return this._listeners[type](event);
      return undefined;
    }},
    scrollIntoView() {{}},
    reset() {{}},
    classList: {{
      add(name) {{ classes.add(name); }},
      remove(name) {{ classes.delete(name); }},
      contains(name) {{ return classes.has(name); }},
      toggle(name) {{
        if (classes.has(name)) {{
          classes.delete(name);
          return false;
        }}
        classes.add(name);
        return true;
      }},
    }},
    get innerHTML() {{ return html; }},
    set innerHTML(next) {{ html = String(next ?? ''); }},
    get textContent() {{ return text; }},
    set textContent(next) {{ text = String(next ?? ''); }},
    querySelectorAll() {{ return []; }},
    querySelector() {{ return null; }},
  }};
}}

const elementsById = new Map();
function getElement(id) {{
  if (!elementsById.has(id)) {{
    elementsById.set(id, createMockElement(id));
  }}
  return elementsById.get(id);
}}

const domListeners = {{}};
const document = {{
  getElementById(id) {{ return getElement(id); }},
  querySelectorAll() {{ return []; }},
  querySelector() {{ return null; }},
  addEventListener(type, handler) {{ domListeners[type] = handler; }},
  createElement() {{
    let value = '';
    return {{
      set textContent(next) {{ value = String(next ?? ''); }},
      get textContent() {{ return value; }},
      get innerHTML() {{ return value; }},
      set innerHTML(next) {{ value = String(next ?? ''); }},
    }};
  }},
}};

const runRequests = [];
const warnings = [];
const context = {{
  console,
  URLSearchParams,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  document,
  window: {{}},
  theme: {{ toggle() {{}} }},
  format: {{ date(value) {{ return String(value ?? '-'); }} }},
  toast: {{
    warning(message) {{ warnings.push(String(message)); }},
    error() {{}},
    success() {{}},
  }},
  api: {{
    async get(path) {{
      if (path === '/scheduled-plans') return {{ items: [] }};
      if (path === '/cpa-services') return [];
      if (path.startsWith('/scheduled-runs?')) {{
        runRequests.push(path);
        const query = path.includes('?') ? path.slice(path.indexOf('?') + 1) : '';
        const params = new URLSearchParams(query);
        const page = Number.parseInt(params.get('page') || '1', 10);
        const pageSize = Number.parseInt(params.get('page_size') || '20', 10);
        return {{
          items: [],
          total: 95,
          page,
          page_size: pageSize,
        }};
      }}
      if (path.startsWith('/scheduled-runs/')) return {{}};
      throw new Error('unexpected api path: ' + path);
    }},
    async post() {{ return {{}}; }},
    async put() {{ return {{}}; }},
  }},
}};
context.global = context;
context.globalThis = context;

vm.createContext(context);
vm.runInContext(scriptSource, context);

function trigger(type, element, event = {{}}) {{
  const handler = element && element._listeners ? element._listeners[type] : null;
  if (!handler) return;
  handler(event);
}}

async function flush() {{
  await new Promise((resolve) => setTimeout(resolve, 0));
}}

async function runScenario() {{
  if (typeof domListeners.DOMContentLoaded !== 'function') {{
    throw new Error('DOMContentLoaded listener missing');
  }}

  domListeners.DOMContentLoaded();
  await flush();
  await flush();

  const jumpInput = getElement('scheduled-run-page-jump-input');
  const jumpBtn = getElement('scheduled-run-page-jump-btn');
  const prevBtn = getElement('scheduled-run-prev-page');
  const nextBtn = getElement('scheduled-run-next-page');

  switch (scenarioName) {{
    case 'jump_button_click': {{
      jumpInput.value = '3';
      trigger('click', jumpBtn, {{ preventDefault() {{}} }});
      await flush();
      await flush();
      return {{ runRequests, warnings }};
    }}
    case 'jump_enter_key': {{
      jumpInput.value = '4';
      let enterPrevented = false;
      trigger('keydown', jumpInput, {{ key: 'Enter', preventDefault() {{ enterPrevented = true; }} }});
      await flush();
      await flush();
      return {{ runRequests, warnings, enterPrevented }};
    }}
    case 'jump_invalid_input': {{
      jumpInput.value = 'abc';
      trigger('click', jumpBtn, {{ preventDefault() {{}} }});
      await flush();
      await flush();
      return {{ runRequests, warnings }};
    }}
    case 'jump_out_of_range_clamp': {{
      jumpInput.value = '999';
      trigger('click', jumpBtn, {{ preventDefault() {{}} }});
      await flush();
      await flush();
      return {{ runRequests, warnings }};
    }}
    case 'prev_next_wiring': {{
      jumpInput.value = '3';
      trigger('click', jumpBtn, {{ preventDefault() {{}} }});
      await flush();
      await flush();
      trigger('click', prevBtn, {{ preventDefault() {{}} }});
      await flush();
      await flush();
      trigger('click', nextBtn, {{ preventDefault() {{}} }});
      await flush();
      await flush();
      return {{ runRequests, warnings }};
    }}
    case 'focused_input_preserves_user_text': {{
      jumpInput.value = '444';
      document.activeElement = jumpInput;
      trigger('click', nextBtn, {{ preventDefault() {{}} }});
      await flush();
      await flush();
      return {{ runRequests, warnings, jumpInputValue: jumpInput.value }};
    }}
    default:
      throw new Error('unknown scenario: ' + scenarioName);
  }}
}}

runScenario()
  .then((result) => {{
    process.stdout.write(JSON.stringify(result));
  }})
  .catch((error) => {{
    console.error(error && error.stack ? error.stack : String(error));
    process.exit(1);
  }});
"""
    completed = subprocess.run(
        ["node", "-e", node_script],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout or "{}")


def test_scheduled_tasks_pagination_jump_button_click_updates_requested_page():
    result = run_scheduled_tasks_pagination_scenario("jump_button_click")
    assert result["runRequests"][-1].endswith("page=3&page_size=20")


def test_scheduled_tasks_pagination_enter_key_updates_requested_page():
    result = run_scheduled_tasks_pagination_scenario("jump_enter_key")
    assert result["enterPrevented"] is True
    assert result["runRequests"][-1].endswith("page=4&page_size=20")


def test_scheduled_tasks_pagination_invalid_input_warns_without_extra_request():
    result = run_scheduled_tasks_pagination_scenario("jump_invalid_input")
    assert len(result["runRequests"]) == 1
    assert result["runRequests"][0].endswith("page=1&page_size=20")
    assert result["warnings"]


def test_scheduled_tasks_pagination_out_of_range_input_clamps_and_warns():
    result = run_scheduled_tasks_pagination_scenario("jump_out_of_range_clamp")
    assert result["runRequests"][-1].endswith("page=5&page_size=20")
    assert result["warnings"]


def test_scheduled_tasks_pagination_prev_next_buttons_change_requested_page():
    result = run_scheduled_tasks_pagination_scenario("prev_next_wiring")
    assert any(request.endswith("page=2&page_size=20") for request in result["runRequests"])
    assert result["runRequests"][-1].endswith("page=3&page_size=20")


def test_scheduled_tasks_pagination_keeps_jump_input_when_focused():
    result = run_scheduled_tasks_pagination_scenario("focused_input_preserves_user_text")
    assert result["runRequests"][-1].endswith("page=2&page_size=20")
    assert result["jumpInputValue"] == "444"


def test_shared_list_templates_opt_into_table_shell_styling():
    scheduled_template = Path("templates/scheduled_tasks.html").read_text(encoding="utf-8")
    accounts_template = Path("templates/accounts.html").read_text(encoding="utf-8")
    email_template = Path("templates/email_services.html").read_text(encoding="utf-8")

    assert scheduled_template.count("table-shell") >= 2
    assert "table-shell" in accounts_template
    assert email_template.count("table-shell") >= 2


def test_shared_style_sheet_contains_card_list_system_hooks():
    stylesheet = Path("static/css/style.css").read_text(encoding="utf-8")
    assert ".table-shell" in stylesheet
    assert ".table-actions" in stylesheet
    assert ".scheduled-run-summary" in stylesheet
    assert ".scheduled-run-detail-head" in stylesheet
    assert ".scheduled-run-log-panel" in stylesheet


def test_scheduled_tasks_script_contains_create_edit_enable_disable_hooks():
    script = Path("static/js/scheduled_tasks.js").read_text(encoding="utf-8")
    assert "function openCreatePlanModal(" in script
    assert "function openEditPlanModal(" in script
    assert "function submitPlanForm(" in script
    assert "function togglePlanEnabled(" in script
    assert "/scheduled-plans/${planId}/enable" in script
    assert "/scheduled-plans/${planId}/disable" in script


def test_scheduled_tasks_script_surfaces_cpa_service_load_failures_to_users():
    script = Path("static/js/scheduled_tasks.js").read_text(encoding="utf-8")
    assert "CPA 服务列表加载失败" in script
    assert "toast.error(" in script
    assert "loadCpaServices(true)" in script


def test_scheduled_tasks_script_provides_safe_default_cleanup_config():
    script = Path("static/js/scheduled_tasks.js").read_text(encoding="utf-8")
    assert "TASK_CONFIG_SCHEMAS" in script
    assert "max_probe_count" in script
    assert "max_cleanup_count" in script
    assert "refresh_after_days" in script


def test_scheduled_tasks_script_contains_config_editor_mode_and_serialization_hooks():
    script = Path("static/js/scheduled_tasks.js").read_text(encoding="utf-8")
    assert "function renderConfigEntries(" in script
    assert "function buildConfigPayloadFromEntries(" in script
    assert "function syncRawJsonFromConfigEntries(" in script
    assert "function syncConfigEntriesFromRawJson(" in script
    assert "function switchConfigEditorMode(" in script
    assert "config_meta" in script


def test_scheduled_tasks_script_renders_key_description_value_description_columns():
    script = Path("static/js/scheduled_tasks.js").read_text(encoding="utf-8")
    assert "键说明" in script
    assert "值说明" in script
    assert "value_type" in script


def test_scheduled_tasks_script_contains_button_busy_guard_logic():
    script = Path("static/js/scheduled_tasks.js").read_text(encoding="utf-8")
    assert "data-busy" in script
    assert "disabled = true" in script
    assert "disabled = false" in script


def test_scheduled_tasks_script_renders_plan_row_action_markers():
    script = Path("static/js/scheduled_tasks.js").read_text(encoding="utf-8")
    assert 'data-action="detail"' in script
    assert 'data-action="logs"' in script
    assert 'data-action="edit"' in script
    assert 'data-action="toggle"' in script
    assert 'data-action="run-now"' in script
    assert 'data-plan-id="${plan.id}"' in script
    assert 'data-should-enable="${shouldEnable}"' in script
    assert 'onclick="handlePlanAction(this)"' in script


def test_scheduled_tasks_script_routes_actions_through_busy_guard_handlers():
    script = Path("static/js/scheduled_tasks.js").read_text(encoding="utf-8")
    assert "async function withButtonBusy(" in script
    assert "async function handlePlanAction(button)" in script
    assert "async function handleRunLogAction(button)" in script
    assert "return withButtonBusy(button, async () => {" in script
    assert "onclick=\"handleRunLogAction(this)\"" in script
    assert "withButtonBusy(event.currentTarget, () => loadPlans())" in script


def test_scheduled_tasks_script_contains_run_center_live_log_and_stop_hooks():
    script = Path("static/js/scheduled_tasks.js").read_text(encoding="utf-8")
    assert "function buildScheduledRunQuery(" in script
    assert "async function loadScheduledRuns(" in script
    assert "function renderScheduledRuns(" in script
    assert "function openScheduledRunDetail(" in script
    assert "function openScheduledRunLog(" in script
    assert "function startScheduledRunLogPolling(" in script
    assert "function stopScheduledRunLogPolling(" in script
    assert "function appendScheduledRunLogChunk(" in script
    assert ("setInterval(" in script or "setTimeout(" in script) and "scheduled-runs" in script
    assert 'data-action="filter-plan-runs"' in script
    assert 'data-action="view-run-detail"' in script
    assert 'data-action="view-run-log"' in script
    assert 'data-action="stop-run"' in script
    assert "async function stopScheduledRun(" in script
    assert '"stopping"' in script or "'stopping'" in script


def test_scheduled_tasks_script_contains_run_center_pagination_jump_hooks():
    script = Path("static/js/scheduled_tasks.js").read_text(encoding="utf-8")
    assert "scheduled-run-page-jump-input" in script
    assert "scheduled-run-page-jump-btn" in script
    assert "scheduled-run-prev-page" in script
    assert "scheduled-run-next-page" in script
    assert "keydown" in script and "Enter" in script
    assert "params.set('page_size'" in script
    assert "toast.warning(" in script


def test_scheduled_tasks_render_summaries_as_concise_business_metrics():
    node_script = r"""
const fs = require('fs');
const vm = require('vm');

function makeElement() {
  let html = '';
  const classes = new Set();
  return {
    dataset: {},
    style: {},
    disabled: false,
    setAttribute: () => {},
    removeAttribute: () => {},
    addEventListener: () => {},
    scrollIntoView: () => {},
    get innerHTML() { return html; },
    set innerHTML(next) { html = String(next ?? ''); },
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
    },
  };
}

const runsBody = makeElement();

global.window = {};
global.document = {
  getElementById: (id) => (id === 'scheduled-runs-table-body' ? runsBody : null),
  querySelectorAll: () => [],
  addEventListener: () => {},
  createElement: () => {
    let value = '';
    return {
      set textContent(next) { value = String(next ?? ''); },
      get textContent() { return value; },
      get innerHTML() { return value; },
      set innerHTML(next) { value = String(next ?? ''); },
    };
  },
};

global.api = { get: async () => ({}), post: async () => ({}), put: async () => ({}) };
global.toast = { error: () => {}, warning: () => {}, success: () => {} };
global.format = { date: (value) => String(value ?? '-') };
global.theme = { toggle: () => {} };

vm.runInThisContext(fs.readFileSync('static/js/scheduled_tasks.js', 'utf8'), {
  filename: 'static/js/scheduled_tasks.js',
});

window.renderScheduledRuns([
  {
    id: 1,
    plan_id: 101,
    plan_name: 'cleanup',
    task_type: 'cpa_cleanup',
    trigger_source: 'manual',
    status: 'success',
    started_at: null,
    finished_at: null,
    summary: { invalid_items_found: 20, remote_deleted: 18 },
  },
  {
    id: 2,
    plan_id: 102,
    plan_name: 'refill',
    task_type: 'cpa_refill',
    trigger_source: 'manual',
    status: 'success',
    started_at: null,
    finished_at: null,
    summary: { uploaded_success: 6 },
  },
  {
    id: 3,
    plan_id: 103,
    plan_name: 'refresh',
    task_type: 'account_refresh',
    trigger_source: 'manual',
    status: 'success',
    started_at: null,
    finished_at: null,
    summary: { processed: 30, refreshed_success: 28, uploaded_success: 27 },
  },
]);

const html = runsBody.innerHTML;
if (!html.includes('检测 20 · 清理 18')) throw new Error('missing cleanup concise summary: ' + html);
if (!html.includes('补号 6')) throw new Error('missing refill concise summary: ' + html);
if (!html.includes('处理 30 · 刷新 28 · 上传 27')) throw new Error('missing refresh concise summary: ' + html);
"""
    completed = subprocess.run(
        ["node", "-e", node_script],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_scheduled_tasks_failed_or_cancelled_runs_append_short_reason_to_summary():
    node_script = r"""
const fs = require('fs');
const vm = require('vm');

function makeElement() {
  let html = '';
  const classes = new Set();
  return {
    dataset: {},
    style: {},
    disabled: false,
    setAttribute: () => {},
    removeAttribute: () => {},
    addEventListener: () => {},
    scrollIntoView: () => {},
    get innerHTML() { return html; },
    set innerHTML(next) { html = String(next ?? ''); },
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
    },
  };
}

const runsBody = makeElement();

global.window = {};
global.document = {
  getElementById: (id) => (id === 'scheduled-runs-table-body' ? runsBody : null),
  querySelectorAll: () => [],
  addEventListener: () => {},
  createElement: () => {
    let value = '';
    return {
      set textContent(next) { value = String(next ?? ''); },
      get textContent() { return value; },
      get innerHTML() { return value; },
      set innerHTML(next) { value = String(next ?? ''); },
    };
  },
};

global.api = { get: async () => ({}), post: async () => ({}), put: async () => ({}) };
global.toast = { error: () => {}, warning: () => {}, success: () => {} };
global.format = { date: (value) => String(value ?? '-') };
global.theme = { toggle: () => {} };

vm.runInThisContext(fs.readFileSync('static/js/scheduled_tasks.js', 'utf8'), {
  filename: 'static/js/scheduled_tasks.js',
});

window.renderScheduledRuns([
  {
    id: 1,
    plan_id: 101,
    plan_name: 'cleanup',
    task_type: 'cpa_cleanup',
    trigger_source: 'manual',
    status: 'failed',
    started_at: null,
    finished_at: null,
    error_message: '接口超时',
    summary: { invalid_items_found: 12, remote_deleted: 4 },
  },
  {
    id: 2,
    plan_id: 102,
    plan_name: 'refill',
    task_type: 'cpa_refill',
    trigger_source: 'manual',
    status: 'cancelled',
    started_at: null,
    finished_at: null,
    error_message: null,
    summary: { uploaded_success: 3 },
  },
]);

const html = runsBody.innerHTML;
if (!html.includes('检测 12 · 清理 4 · 原因：接口超时')) {
  throw new Error('missing failed cleanup reason summary: ' + html);
}
if (!html.includes('补号 3 · 原因：用户停止')) {
  throw new Error('missing cancelled refill fallback summary: ' + html);
}
"""
    completed = subprocess.run(
        ["node", "-e", node_script],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_scheduled_tasks_open_run_detail_renders_structured_summary_and_log_sections():
    node_script = r"""
const fs = require('fs');
const vm = require('vm');

function makeElement() {
  let html = '';
  let text = '';
  const classes = new Set();
  return {
    dataset: {},
    style: {},
    disabled: false,
    setAttribute: () => {},
    removeAttribute: () => {},
    addEventListener: () => {},
    scrollIntoView: () => {},
    get innerHTML() { return html; },
    set innerHTML(next) { html = String(next ?? ''); },
    get textContent() { return text; },
    set textContent(next) { text = String(next ?? ''); },
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
    },
  };
}

const elements = new Map([
  ['run-log-modal', makeElement()],
  ['run-log-status-bar', makeElement()],
  ['run-log-modal-body', makeElement()],
  ['run-log-stop-btn', makeElement()],
]);

global.window = {};
global.document = {
  getElementById: (id) => {
    if (id === 'scheduled-run-log-output') {
      if (!elements.has(id)) elements.set(id, makeElement());
      return elements.get(id);
    }
    return elements.get(id) ?? null;
  },
  querySelectorAll: () => [],
  addEventListener: () => {},
  createElement: () => {
    let value = '';
    return {
      set textContent(next) { value = String(next ?? ''); },
      get textContent() { return value; },
      get innerHTML() { return value; },
      set innerHTML(next) { value = String(next ?? ''); },
    };
  },
};

global.api = {
  get: async (url) => {
    if (url === '/scheduled-runs/123') {
      return {
        id: 123,
        plan_id: 1,
        plan_name: 'nightly cleanup',
        task_type: 'cpa_cleanup',
        trigger_source: 'manual',
        started_at: '2026-03-23T10:00:00',
        finished_at: '2026-03-23T10:05:00',
        error_message: null,
        summary: { invalid_items_found: 18, remote_deleted: 16 },
        status: 'success',
        last_log_at: '2026-03-23T10:05:00',
        is_running: false,
        stop_requested_at: null,
        can_stop: false,
      };
    }
    if (url.startsWith('/scheduled-runs/123/logs?offset=')) {
      return {
        chunk: 'done',
        next_offset: 4,
        has_more: false,
        is_running: false,
        status: 'success',
        stop_requested_at: null,
        log_version: 1,
        last_log_at: '2026-03-23T10:05:00',
      };
    }
    throw new Error('unexpected url: ' + url);
  },
  post: async () => ({}),
  put: async () => ({}),
};
global.toast = { error: () => {}, warning: () => {}, success: () => {} };
global.format = { date: (value) => String(value ?? '-') };
global.theme = { toggle: () => {} };

vm.runInThisContext(fs.readFileSync('static/js/scheduled_tasks.js', 'utf8'), {
  filename: 'static/js/scheduled_tasks.js',
});

async function main() {
  await window.openScheduledRunDetail(123);
  const statusBarHtml = elements.get('run-log-status-bar').innerHTML;
  const detailBodyHtml = elements.get('run-log-modal-body').innerHTML;
  const logText = elements.get('scheduled-run-log-output').textContent;

  if (!statusBarHtml.includes('scheduled-run-meta-bar')) throw new Error('missing status bar hook: ' + statusBarHtml);
  if (!detailBodyHtml.includes('scheduled-run-detail-head')) throw new Error('missing detail head hook: ' + detailBodyHtml);
  if (!detailBodyHtml.includes('scheduled-run-detail-grid')) throw new Error('missing detail grid hook: ' + detailBodyHtml);
  if (!detailBodyHtml.includes('scheduled-run-summary')) throw new Error('missing summary hook: ' + detailBodyHtml);
  if (!detailBodyHtml.includes('scheduled-run-log-panel')) throw new Error('missing log panel hook: ' + detailBodyHtml);
  if (!detailBodyHtml.includes('检测 18 · 清理 16')) throw new Error('missing concise summary: ' + detailBodyHtml);
  if (logText !== 'done') throw new Error('expected final log text to be loaded');
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
"""
    completed = subprocess.run(
        ["node", "-e", node_script],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_scheduled_tasks_script_drops_stale_builtin_keys_when_task_type_switches():
    node_script = r"""
const fs = require('fs');
const vm = require('vm');

global.window = {};
global.document = {
  getElementById: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {},
  createElement: () => {
    let value = '';
    return {
      set textContent(next) { value = String(next ?? ''); },
      get textContent() { return value; },
      get innerHTML() { return value; },
      set innerHTML(next) { value = String(next ?? ''); },
    };
  },
};
global.api = { get: async () => ({}), post: async () => ({}), put: async () => ({}) };
global.toast = { error: () => {}, warning: () => {}, success: () => {} };
global.format = { date: (value) => String(value ?? '-') };
global.theme = { toggle: () => {} };

vm.runInThisContext(fs.readFileSync('static/js/scheduled_tasks.js', 'utf8'), {
  filename: 'static/js/scheduled_tasks.js',
});

if (typeof window.prepareConfigEntriesForTaskTypeSwitch !== 'function') {
  throw new Error('prepareConfigEntriesForTaskTypeSwitch is not exposed');
}

const result = window.prepareConfigEntriesForTaskTypeSwitch(
  'cpa_refill',
  'cpa_cleanup',
  [
    {
      key: 'max_probe_count',
      keyDescription: '旧内置键',
      rawValue: '100',
      valueDescription: '旧说明',
      valueType: 'number',
      builtin: true,
      readonlyKey: true,
    },
    {
      key: 'max_cleanup_count',
      keyDescription: '旧内置键2',
      rawValue: '10',
      valueDescription: '旧说明2',
      valueType: 'number',
      builtin: true,
      readonlyKey: true,
    },
    {
      key: 'custom_keep',
      keyDescription: '自定义键',
      rawValue: 'custom-value',
      valueDescription: '保留我',
      valueType: 'string',
      builtin: false,
      readonlyKey: false,
    },
  ],
);

console.log(JSON.stringify(result.map((entry) => ({
  key: entry.key,
  builtin: entry.builtin,
  valueDescription: entry.valueDescription,
}))));
"""
    completed = subprocess.run(
        ["node", "-e", node_script],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    keys = json.loads(completed.stdout)
    assert {"key": "custom_keep", "builtin": False, "valueDescription": "保留我"} in keys
    assert all(item["key"] not in {"max_probe_count", "max_cleanup_count"} for item in keys)
    assert any(item["key"] == "target_valid_count" and item["builtin"] is True for item in keys)


def test_scheduled_tasks_run_log_loading_drains_chunks_even_when_run_is_finished():
    node_script = r"""
const fs = require('fs');
const vm = require('vm');

function makeElement() {
  let html = '';
  let text = '';
  const classes = new Set();
  return {
    dataset: {},
    style: {},
    disabled: false,
    setAttribute: () => {},
    removeAttribute: () => {},
    scrollIntoView: () => {},
    get innerHTML() { return html; },
    set innerHTML(next) { html = String(next ?? ''); },
    get textContent() { return text; },
    set textContent(next) { text = String(next ?? ''); },
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
    },
  };
}

const elements = new Map([
  ['run-log-modal', makeElement()],
  ['run-log-status-bar', makeElement()],
  ['run-log-modal-body', makeElement()],
  ['run-log-stop-btn', makeElement()],
]);

global.window = {};
global.document = {
  getElementById: (id) => {
    if (id === 'scheduled-run-log-output') {
      if (!elements.has(id)) elements.set(id, makeElement());
      return elements.get(id);
    }
    return elements.get(id) ?? null;
  },
  querySelectorAll: () => [],
  addEventListener: () => {},
  createElement: () => {
    let value = '';
    return {
      set textContent(next) { value = String(next ?? ''); },
      get textContent() { return value; },
      get innerHTML() { return value; },
      set innerHTML(next) { value = String(next ?? ''); },
    };
  },
};

let logCalls = 0;
global.api = {
  get: async (url) => {
    if (url === '/scheduled-runs/123') {
      return {
        id: 123,
        plan_id: 1,
        plan_name: 'plan',
        task_type: 'cpa_cleanup',
        trigger_source: 'manual',
        started_at: null,
        finished_at: null,
        error_message: null,
        summary: {},
        status: 'finished',
        last_log_at: null,
        is_running: false,
        stop_requested_at: null,
        can_stop: false,
      };
    }
    if (url.startsWith('/scheduled-runs/123/logs?offset=')) {
      logCalls += 1;
      if (url.endsWith('offset=0')) {
        return {
          chunk: 'a',
          next_offset: 1,
          has_more: true,
          is_running: false,
          status: 'finished',
          stop_requested_at: null,
          log_version: 1,
          last_log_at: null,
        };
      }
      if (url.endsWith('offset=1')) {
        return {
          chunk: 'b',
          next_offset: 2,
          has_more: false,
          is_running: false,
          status: 'finished',
          stop_requested_at: null,
          log_version: 1,
          last_log_at: null,
        };
      }
      throw new Error('unexpected offset url: ' + url);
    }
    throw new Error('unexpected url: ' + url);
  },
  post: async () => ({}),
  put: async () => ({}),
};
global.toast = { error: () => {}, warning: () => {}, success: () => {} };
global.format = { date: (value) => String(value ?? '-') };
global.theme = { toggle: () => {} };

vm.runInThisContext(fs.readFileSync('static/js/scheduled_tasks.js', 'utf8'), {
  filename: 'static/js/scheduled_tasks.js',
});

async function main() {
  await window.openScheduledRunDetail(123);
  const output = document.getElementById('scheduled-run-log-output');
  if (logCalls !== 2) throw new Error('expected 2 log calls, got ' + logCalls);
  if (output.textContent !== 'ab') {
    throw new Error('expected log output \"ab\", got: ' + JSON.stringify(output.textContent));
  }
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
"""
    completed = subprocess.run(
        ["node", "-e", node_script],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_scheduled_tasks_run_log_polling_is_single_flight():
    node_script = r"""
const fs = require('fs');
const vm = require('vm');

function makeElement() {
  let html = '';
  const classes = new Set();
  return {
    dataset: {},
    style: {},
    disabled: false,
    setAttribute: () => {},
    removeAttribute: () => {},
    scrollIntoView: () => {},
    get innerHTML() { return html; },
    set innerHTML(next) { html = String(next ?? ''); },
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
    },
  };
}

const elements = new Map([
  ['run-log-modal', makeElement()],
  ['run-log-status-bar', makeElement()],
  ['run-log-modal-body', makeElement()],
  ['run-log-stop-btn', makeElement()],
]);

global.window = {};
global.document = {
  getElementById: (id) => elements.get(id) ?? null,
  querySelectorAll: () => [],
  addEventListener: () => {},
  createElement: () => {
    let value = '';
    return {
      set textContent(next) { value = String(next ?? ''); },
      get textContent() { return value; },
      get innerHTML() { return value; },
      set innerHTML(next) { value = String(next ?? ''); },
    };
  },
};

let intervalCb = null;
global.setInterval = () => {
  throw new Error('expected self-scheduling polling, not setInterval');
};
global.clearInterval = () => {};
global.setTimeout = (cb) => {
  intervalCb = cb;
  return 1;
};
global.clearTimeout = () => {
  intervalCb = null;
};

let deferredResolve = null;
const deferred = new Promise((resolve) => { deferredResolve = resolve; });

let logCalls = 0;
global.api = {
  get: async (url) => {
    if (url === '/scheduled-runs/123') {
      return {
        id: 123,
        plan_id: 1,
        plan_name: 'plan',
        task_type: 'cpa_cleanup',
        trigger_source: 'manual',
        started_at: null,
        finished_at: null,
        error_message: null,
        summary: {},
        status: 'running',
        last_log_at: null,
        is_running: true,
        stop_requested_at: null,
        can_stop: true,
      };
    }
    if (url.startsWith('/scheduled-runs/123/logs?offset=')) {
      logCalls += 1;
      if (logCalls === 1) {
        return {
          chunk: '',
          next_offset: 0,
          has_more: false,
          is_running: true,
          status: 'running',
          stop_requested_at: null,
          log_version: 1,
          last_log_at: null,
        };
      }
      if (logCalls === 2) return deferred;
      return {
        chunk: '',
        next_offset: 0,
        has_more: false,
        is_running: true,
        status: 'running',
        stop_requested_at: null,
        log_version: 1,
        last_log_at: null,
      };
    }
    throw new Error('unexpected url: ' + url);
  },
  post: async () => ({}),
  put: async () => ({}),
};
global.toast = { error: () => {}, warning: () => {}, success: () => {} };
global.format = { date: (value) => String(value ?? '-') };
global.theme = { toggle: () => {} };

vm.runInThisContext(fs.readFileSync('static/js/scheduled_tasks.js', 'utf8'), {
  filename: 'static/js/scheduled_tasks.js',
});

async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
}

async function main() {
  await window.openScheduledRunDetail(123);
  if (typeof intervalCb !== 'function') throw new Error('expected polling timer callback to be registered');

  intervalCb();
  intervalCb();
  if (logCalls !== 2) {
    throw new Error('expected single-flight polling (2 total log calls incl. initial), got ' + logCalls);
  }

  deferredResolve({
    chunk: '',
    next_offset: 0,
    has_more: false,
    is_running: true,
    status: 'running',
    stop_requested_at: null,
    log_version: 1,
    last_log_at: null,
  });
  await flush();

  if (typeof intervalCb !== 'function') throw new Error('expected polling timer callback to remain registered');
  intervalCb();
  if (logCalls !== 3) throw new Error('expected next polling fetch after completion, got logCalls=' + logCalls);
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
"""
    completed = subprocess.run(
        ["node", "-e", node_script],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_scheduled_tasks_stop_run_failure_does_not_reject_promise():
    node_script = r"""
const fs = require('fs');
const vm = require('vm');

global.window = {};
global.document = {
  getElementById: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {},
  createElement: () => {
    let value = '';
    return {
      set textContent(next) { value = String(next ?? ''); },
      get textContent() { return value; },
      get innerHTML() { return value; },
      set innerHTML(next) { value = String(next ?? ''); },
    };
  },
};

global.api = {
  get: async () => ({}),
  post: async () => { throw new Error('stop failed'); },
  put: async () => ({}),
};
global.toast = { error: () => {}, warning: () => {}, success: () => {} };
global.format = { date: (value) => String(value ?? '-') };
global.theme = { toggle: () => {} };

vm.runInThisContext(fs.readFileSync('static/js/scheduled_tasks.js', 'utf8'), {
  filename: 'static/js/scheduled_tasks.js',
});

async function main() {
  await window.stopScheduledRun(99);
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
"""
    completed = subprocess.run(
        ["node", "-e", node_script],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_scheduled_tasks_closing_log_modal_cancels_stale_async_work_and_polling():
    node_script = r"""
const fs = require('fs');
const vm = require('vm');

function makeElement() {
  let html = '';
  const classes = new Set();
  return {
    dataset: {},
    style: {},
    disabled: false,
    setAttribute: () => {},
    removeAttribute: () => {},
    scrollIntoView: () => {},
    get innerHTML() { return html; },
    set innerHTML(next) { html = String(next ?? ''); },
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
    },
  };
}

const runLogModal = makeElement();
const runLogStatusBar = makeElement();
const runLogModalBody = makeElement();
const runLogStopBtn = makeElement();

const elements = new Map([
  ['run-log-modal', runLogModal],
  ['run-log-status-bar', runLogStatusBar],
  ['run-log-modal-body', runLogModalBody],
  ['run-log-stop-btn', runLogStopBtn],
]);

global.window = {};
global.document = {
  getElementById: (id) => elements.get(id) ?? null,
  querySelectorAll: () => [],
  addEventListener: () => {},
  createElement: () => {
    let value = '';
    return {
      set textContent(next) { value = String(next ?? ''); },
      get textContent() { return value; },
      get innerHTML() { return value; },
      set innerHTML(next) { value = String(next ?? ''); },
    };
  },
};

let intervalCreated = 0;
global.setInterval = () => {
  throw new Error('expected self-scheduling polling, not setInterval');
};
global.clearInterval = () => {};
global.setTimeout = () => {
  intervalCreated += 1;
  return 1;
};
global.clearTimeout = () => {};

let resolveDetail = null;
const detailPromise = new Promise((resolve) => { resolveDetail = resolve; });

let resolveLogs = null;
const logsPromise = new Promise((resolve) => { resolveLogs = resolve; });

global.api = {
  get: async (url) => {
    if (url === '/scheduled-runs/123') return detailPromise;
    if (url.startsWith('/scheduled-runs/123/logs?offset=')) return logsPromise;
    throw new Error('unexpected url: ' + url);
  },
  post: async () => ({}),
  put: async () => ({}),
};
global.toast = { error: () => {}, warning: () => {}, success: () => {} };
global.format = { date: (value) => String(value ?? '-') };
global.theme = { toggle: () => {} };

vm.runInThisContext(fs.readFileSync('static/js/scheduled_tasks.js', 'utf8'), {
  filename: 'static/js/scheduled_tasks.js',
});

async function main() {
  const openPromise = window.openScheduledRunDetail(123);

  const closeButton = {
    dataset: { action: 'back-to-runs' },
    disabled: false,
    setAttribute: () => {},
    removeAttribute: () => {},
  };
  await window.handleRunLogAction(closeButton);

  resolveDetail({
    id: 123,
    plan_id: 1,
    plan_name: 'plan',
    task_type: 'cpa_cleanup',
    trigger_source: 'manual',
    started_at: null,
    finished_at: null,
    error_message: null,
    summary: {},
    status: 'running',
    last_log_at: null,
    is_running: true,
    stop_requested_at: null,
    can_stop: true,
  });
  resolveLogs({
    chunk: 'stale',
    next_offset: 4,
    has_more: false,
    is_running: true,
    status: 'running',
    stop_requested_at: null,
    log_version: 1,
    last_log_at: null,
  });

  await openPromise;

  if (runLogModal.classList.contains('active')) throw new Error('modal should remain closed');
  if (intervalCreated !== 0) {
    throw new Error('polling timer should not start after close; got intervalCreated=' + intervalCreated);
  }
  if (runLogStatusBar.innerHTML !== '<span>未选择运行记录</span>') {
    throw new Error('expected status bar to stay reset, got: ' + JSON.stringify(runLogStatusBar.innerHTML));
  }
  if (!String(runLogModalBody.innerHTML).includes('暂无记录')) {
    throw new Error('expected modal body to stay reset, got: ' + JSON.stringify(runLogModalBody.innerHTML));
  }
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
"""
    completed = subprocess.run(
        ["node", "-e", node_script],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
