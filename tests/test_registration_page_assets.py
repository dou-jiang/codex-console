from pathlib import Path
import re

from tests_runtime.app_js_harness import run_app_js_scenario


def test_registration_template_contains_unlimited_mode_and_domain_stats_container():
    template = Path("templates/index.html").read_text(encoding="utf-8")
    assert '<option value="unlimited">无限注册</option>' in template
    assert 'id="batch-consecutive-failures"' in template
    assert 'id="batch-domain-stats"' in template


def test_registration_template_nav_contains_scheduled_tasks_link():
    template = Path("templates/index.html").read_text(encoding="utf-8")
    assert 'href="/scheduled-tasks"' in template


def test_registration_template_recent_accounts_uses_shared_table_shell():
    template = Path("templates/index.html").read_text(encoding="utf-8")
    assert "recent-accounts-table table-shell" in template or "table-shell recent-accounts-table" in template


def test_app_js_posts_count_zero_for_unlimited_mode():
    result = run_app_js_scenario("unlimited_mode_request")
    assert result["batch_count_display"] == "none"
    assert result["request_payload"]["count"] == 0
    assert result["saved_active_task"]["mode"] == "unlimited"


def test_app_js_renders_unlimited_progress_without_domain_stats_until_finished():
    result = run_app_js_scenario("unlimited_progress_running")
    assert result["progress_text"] == "5/∞"
    assert result["progress_percent"] == "运行中"
    assert result["progress_bar_indeterminate"] is True
    assert result["consecutive_failures_text"] == "3/10"
    assert result["domain_stats_display"] == "none"
    assert result["domain_stats_html"] == ""


def test_app_js_renders_unlimited_final_domain_stats_with_rate_columns():
    result = run_app_js_scenario("unlimited_progress_finished")
    assert result["progress_text"] == "8/∞"
    assert result["progress_percent"] == "已结束"
    assert result["progress_bar_indeterminate"] is True
    assert "gmail.com" in result["domain_stats_html"]
    assert "成功率" in result["domain_stats_html"]
    assert "失败率" in result["domain_stats_html"]
    assert "75.00%" in result["domain_stats_html"]
    assert "25.00%" in result["domain_stats_html"]


def test_app_js_restore_unlimited_task_uses_batch_endpoint():
    result = run_app_js_scenario("restore_unlimited_task")
    assert result["api_get_paths"] == ["/registration/batch/batch-unlimited-01"]
    assert result["batch_progress_display"] == "block"


def test_accounts_template_contains_pagination_jump_controls():
    template = Path("templates/accounts.html").read_text(encoding="utf-8")
    assert 'id="page-jump-input"' in template
    assert 'id="page-jump-btn"' in template


def test_accounts_template_filter_panel_uses_shared_shell_classes():
    template = Path("templates/accounts.html").read_text(encoding="utf-8")
    assert "filter-panel" in template
    assert "filter-panel-grid" in template
    assert "filter-panel-actions" in template
    assert "pagination-panel" in template
    assert "pagination-jump" in template
    toolbar_card_tag = _get_div_by_class_tokens(template, {"card", "toolbar-card"})
    assert toolbar_card_tag is not None
    _assert_tag_class_lacks(toolbar_card_tag, "filter-panel")
    _assert_tag_class_lacks(toolbar_card_tag, "toolbar")

    toolbar_body_tag = _get_div_by_class_tokens(template, {"card-body", "filter-panel"})
    assert toolbar_body_tag is not None
    _assert_tag_class_contains(toolbar_body_tag, "card-body")
    _assert_tag_class_contains(toolbar_body_tag, "filter-panel")
    _assert_tag_class_lacks(toolbar_body_tag, "toolbar")

    cpa_input_tag = _get_tag_by_id(template, "input", "filter-primary-cpa-service-id")
    search_input_tag = _get_tag_by_id(template, "input", "search-input")
    page_jump_input_tag = _get_tag_by_id(template, "input", "page-jump-input")
    assert "style=" not in cpa_input_tag
    assert "style=" not in search_input_tag
    assert "style=" not in page_jump_input_tag


def test_shared_stylesheet_defines_filter_and_pagination_panel_selectors():
    stylesheet = Path("static/css/style.css").read_text(encoding="utf-8")
    assert ".filter-panel" in stylesheet
    assert ".filter-panel-grid" in stylesheet
    assert ".filter-panel-actions" in stylesheet
    assert ".pagination-panel" in stylesheet
    assert ".pagination-jump" in stylesheet
    assert ".filter-input-cpa-service-id" in stylesheet
    assert ".filter-input-search" in stylesheet
    assert ".pagination-jump-input" in stylesheet


def _get_div_by_class_tokens(template: str, class_tokens: set[str]) -> str | None:
    for match in re.finditer(r"<div\b[^>]*>", template):
        tag = match.group(0)
        class_match = re.search(r'class\s*=\s*"([^"]*)"', tag)
        if class_match is None:
            continue
        classes = set(class_match.group(1).split())
        if class_tokens.issubset(classes):
            return tag
    return None


def _get_tag_by_id(template: str, tag: str, element_id: str) -> str:
    match = re.search(rf"<{tag}\b[^>]*\bid=\"{re.escape(element_id)}\"[^>]*>", template)
    assert match is not None, f"missing <{tag}> with id={element_id}"
    return match.group(0)


def _assert_tag_class_contains(tag_html: str, expected_class: str) -> None:
    class_match = re.search(r'class\s*=\s*"([^"]*)"', tag_html)
    assert class_match is not None, f"missing class attribute: {tag_html}"
    classes = set(class_match.group(1).split())
    assert expected_class in classes, f"class {expected_class!r} missing in {classes!r}"


def _assert_tag_class_lacks(tag_html: str, unexpected_class: str) -> None:
    class_match = re.search(r'class\s*=\s*"([^"]*)"', tag_html)
    assert class_match is not None, f"missing class attribute: {tag_html}"
    classes = set(class_match.group(1).split())
    assert unexpected_class not in classes, f"class {unexpected_class!r} unexpectedly present in {classes!r}"


def run_accounts_js_scenario(name: str) -> dict:
    import json
    import subprocess

    accounts_source = Path("static/js/accounts.js").read_text(encoding="utf-8")
    node_script = rf"""
const vm = require('vm');

const scenarioName = {json.dumps(name)};
const accountsSource = {json.dumps(accounts_source)};

function createMockElement(id = '') {{
  return {{
    id,
    style: {{}},
    dataset: {{}},
    className: '',
    classList: {{ add() {{}}, remove() {{}}, contains() {{ return false; }}, toggle() {{ return false; }} }},
    disabled: false,
    checked: false,
    value: '',
    textContent: '',
    innerHTML: '',
    _listeners: {{}},
    addEventListener(type, handler) {{ this._listeners[type] = handler; }},
    dispatchEvent(type, event = {{}}) {{
      if (this._listeners[type]) {{
        return this._listeners[type](event);
      }}
      return undefined;
    }},
    querySelectorAll() {{ return []; }},
    querySelector() {{ return null; }},
    insertAdjacentElement() {{}},
    blur() {{}},
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
  activeElement: null,
  getElementById(id) {{ return getElement(id); }},
  querySelectorAll() {{ return []; }},
  querySelector() {{ return null; }},
  addEventListener(type, handler) {{ domListeners[type] = handler; }},
  createElement() {{ return createMockElement(); }},
}};

const context = {{
  console,
  URLSearchParams,
  document,
  window: {{ location: {{ protocol: 'http:', host: 'localhost' }} }},
  navigator: {{ clipboard: {{ writeText: async () => {{}} }} }},
  localStorage: {{ getItem() {{ return null; }}, setItem() {{}}, removeItem() {{}} }},
  setTimeout,
  clearTimeout,
  theme: {{ toggle() {{}} }},
  debounce: (fn) => fn,
  delegate() {{}},
  escapeHtml(value) {{ return String(value); }},
  copyToClipboard() {{}},
  format: {{
    number(value) {{ return String(value ?? 0); }},
    date(value) {{ return value ? String(value) : '-'; }},
  }},
  api: {{
    async get(path) {{
      if (path.includes('/accounts?')) return {{ total: 0, accounts: [] }};
      if (path === '/accounts/stats/summary') return {{ total: 0, by_status: {{ active: 0, expired: 0, failed: 0 }} }};
      return {{}};
    }},
    async post() {{ return {{ success: true }}; }},
    async patch() {{ return {{ success: true }}; }},
    async delete() {{ return {{ success: true }}; }},
  }},
  __toasts: [],
  toast: {{
    info(message) {{ context.__toasts.push(String(message)); }},
    success() {{}},
    warning() {{}},
    error() {{}},
  }},
  confirm: async () => true,
  fetch: async () => ({{ ok: true, json: async () => ({{}}), blob: async () => ({{}}), headers: {{ get() {{ return null; }} }} }}),
}};
context.global = context;
context.globalThis = context;

vm.createContext(context);
vm.runInContext(
  accountsSource + `
;globalThis.__accountsTestExports = {{
    initEventListeners,
    updatePagination,
    elements,
    setState(state) {{
      if ('currentPage' in state) currentPage = state.currentPage;
      if ('pageSize' in state) pageSize = state.pageSize;
      if ('totalAccounts' in state) totalAccounts = state.totalAccounts;
      if ('isLoading' in state) isLoading = state.isLoading;
    }},
    getState() {{ return {{ currentPage, pageSize, totalAccounts, isLoading }}; }},
    replaceLoadAccounts(fn) {{ loadAccounts = fn; }},
    getToasts() {{ return [...globalThis.__toasts]; }},
  }};`,
  context,
);

const exported = context.__accountsTestExports;

function trigger(type, element, event = {{}}) {{
  if (element._listeners[type]) {{
    element._listeners[type](event);
  }}
}}

function runScenario() {{
  const input = exported.elements.pageJumpInput;
  const button = exported.elements.pageJumpBtn;

  switch (scenarioName) {{
    case 'jump_wiring_click_and_enter': {{
      let loadCalls = 0;
      let enterPrevented = false;
      exported.replaceLoadAccounts(() => {{ loadCalls += 1; }});
      exported.setState({{ currentPage: 1, pageSize: 20, totalAccounts: 100, isLoading: false }});
      exported.initEventListeners();

      input.value = '3';
      trigger('click', button);

      input.value = '4';
      trigger('keydown', input, {{ key: 'Enter', preventDefault() {{ enterPrevented = true; }} }});

      return {{
        hasClickListener: Boolean(button._listeners.click),
        hasKeydownListener: Boolean(input._listeners.keydown),
        loadCalls,
        enterPrevented,
        currentPage: exported.getState().currentPage,
      }};
    }}
    case 'jump_clamps_and_avoids_duplicate_reload': {{
      let loadCalls = 0;
      exported.replaceLoadAccounts(() => {{ loadCalls += 1; }});
      exported.setState({{ currentPage: 2, pageSize: 20, totalAccounts: 95, isLoading: false }});
      exported.initEventListeners();

      input.value = '999';
      trigger('click', button);

      input.value = '5';
      trigger('click', button);

      return {{
        currentPage: exported.getState().currentPage,
        loadCalls,
        toasts: exported.getToasts(),
      }};
    }}
    case 'jump_rejects_decimal': {{
      let loadCalls = 0;
      exported.replaceLoadAccounts(() => {{ loadCalls += 1; }});
      exported.setState({{ currentPage: 3, pageSize: 20, totalAccounts: 95, isLoading: false }});
      exported.initEventListeners();

      input.value = '1.9';
      trigger('click', button);

      return {{
        currentPage: exported.getState().currentPage,
        loadCalls,
        toasts: exported.getToasts(),
      }};
    }}
    case 'update_pagination_focus_and_max': {{
      exported.setState({{ currentPage: 4, pageSize: 20, totalAccounts: 95, isLoading: false }});
      input.value = '42';
      document.activeElement = input;
      exported.updatePagination();
      const focusedValue = input.value;
      const maxAfterFocused = input.max;

      document.activeElement = createMockElement('other');
      exported.updatePagination();
      const blurredValue = input.value;

      return {{
        focusedValue,
        blurredValue,
        maxAfterFocused,
      }};
    }}
    default:
      throw new Error(`Unknown scenario: ${{scenarioName}}`);
  }}
}}

const result = runScenario();
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run(["node", "-e", node_script], check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def test_accounts_script_wires_jump_input_to_button_click_and_enter_behavior():
    result = run_accounts_js_scenario("jump_wiring_click_and_enter")
    assert result["hasClickListener"] is True
    assert result["hasKeydownListener"] is True
    assert result["enterPrevented"] is True
    assert result["loadCalls"] == 2
    assert result["currentPage"] == 4


def test_accounts_script_clamps_jump_range_and_avoids_duplicate_reload():
    result = run_accounts_js_scenario("jump_clamps_and_avoids_duplicate_reload")
    assert result["currentPage"] == 5
    assert result["loadCalls"] == 1
    assert any("页码超出范围" in message for message in result["toasts"])


def test_accounts_script_rejects_decimal_jump_input_without_coercion():
    result = run_accounts_js_scenario("jump_rejects_decimal")
    assert result["currentPage"] == 3
    assert result["loadCalls"] == 0
    assert any("请输入有效页码" in message for message in result["toasts"])


def test_accounts_script_update_pagination_does_not_override_focused_input_and_sets_max():
    result = run_accounts_js_scenario("update_pagination_focus_and_max")
    assert result["focusedValue"] == "42"
    assert result["blurredValue"] == "4"
    assert result["maxAfterFocused"] == "5"
