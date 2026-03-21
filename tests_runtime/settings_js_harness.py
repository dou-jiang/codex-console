from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETTINGS_JS = ROOT / "static" / "js" / "settings.js"


def run_settings_js_scenario(name: str) -> dict:
    settings_source = SETTINGS_JS.read_text(encoding="utf-8")
    node_script = rf"""
const vm = require('vm');

const scenarioName = {json.dumps(name)};
const settingsSource = {json.dumps(settings_source)};

function escapeHtml(value) {{
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/\"/g, '&quot;')
    .replace(/'/g, '&#39;');
}}

function createMockElement(id = '') {{
  const element = {{
    id,
    tagName: 'DIV',
    style: {{}},
    dataset: {{}},
    disabled: false,
    checked: false,
    indeterminate: false,
    value: '',
    _listeners: {{}},
    _proxyCheckboxes: [],
    _submitButton: null,
    _textContent: '',
    _innerHTML: '',
    classList: {{
      add() {{}},
      remove() {{}},
      contains() {{ return false; }},
    }},
    addEventListener(type, handler) {{
      this._listeners[type] = handler;
    }},
    dispatchEvent(type, event = {{}}) {{
      if (this._listeners[type]) {{
        return this._listeners[type](event);
      }}
      return undefined;
    }},
    querySelectorAll(selector) {{
      if (selector === '.proxy-checkbox[data-id]') {{
        return this._proxyCheckboxes;
      }}
      return [];
    }},
    querySelector(selector) {{
      if (selector === 'button[type="submit"]') {{
        return this._submitButton;
      }}
      return null;
    }},
    appendChild() {{}},
    removeChild() {{}},
    remove() {{}},
    reset() {{
      this.value = '';
      this.checked = false;
    }},
    focus() {{}},
    select() {{}},
  }};

  Object.defineProperty(element, 'textContent', {{
    get() {{
      return this._textContent;
    }},
    set(value) {{
      this._textContent = String(value ?? '');
      this._innerHTML = escapeHtml(this._textContent);
    }},
  }});

  Object.defineProperty(element, 'innerHTML', {{
    get() {{
      return this._innerHTML;
    }},
    set(value) {{
      this._innerHTML = String(value ?? '');
      if (this.id === 'proxies-table') {{
        const inputTags = [...this._innerHTML.matchAll(/<input[\s\S]*?>/g)].map(match => match[0]);
        this._proxyCheckboxes = inputTags
          .filter(tag => tag.includes('class="proxy-checkbox"'))
          .map(tag => {{
            const idMatch = tag.match(/data-id="(\d+)"/);
            const checkbox = createMockElement(`proxy-checkbox-${{idMatch ? idMatch[1] : 'unknown'}}`);
            checkbox.tagName = 'INPUT';
            checkbox.type = 'checkbox';
            checkbox.dataset.id = idMatch ? idMatch[1] : '';
            checkbox.checked = /\bchecked\b/.test(tag);
            return checkbox;
          }});
      }}
    }},
  }});

  if (id === 'proxy-batch-import-form') {{
    element._submitButton = createMockElement('proxy-batch-import-submit');
    element._submitButton.tagName = 'BUTTON';
    element._submitButton.type = 'submit';
  }}

  return element;
}}

const elementsById = new Map();
function getElement(id) {{
  if (!elementsById.has(id)) {{
    elementsById.set(id, createMockElement(id));
  }}
  return elementsById.get(id);
}}

const document = {{
  getElementById(id) {{
    return getElement(id);
  }},
  querySelectorAll() {{
    return [];
  }},
  querySelector() {{
    return null;
  }},
  addEventListener() {{}},
  createElement() {{
    return createMockElement();
  }},
  body: createMockElement('body'),
}};

document.body.appendChild = () => {{}};
document.body.removeChild = () => {{}};

const logs = {{
  apiGets: [],
  toasts: [],
}};

const context = {{
  console,
  URLSearchParams,
  document,
  window: {{ location: {{}}, URL: {{ createObjectURL() {{ return 'blob:test'; }}, revokeObjectURL() {{}} }} }},
  navigator: {{ clipboard: {{ writeText: async () => {{}} }} }},
  localStorage: {{ getItem() {{ return null; }}, setItem() {{}}, removeItem() {{}} }},
  setTimeout,
  clearTimeout,
  fetch: async () => ({{ ok: true, json: async () => ({{}}), blob: async () => ({{}}), headers: {{ get() {{ return null; }} }} }}),
  theme: {{ applyTheme() {{}}, toggle() {{}} }},
  toast: {{
    success(msg) {{ logs.toasts.push(['success', msg]); }},
    error(msg) {{ logs.toasts.push(['error', msg]); }},
    warning(msg) {{ logs.toasts.push(['warning', msg]); }},
    info(msg) {{ logs.toasts.push(['info', msg]); }},
  }},
  format: {{
    date(value) {{ return value ? `DATE:${{value}}` : '-'; }},
  }},
  api: {{
    async get(path) {{
      logs.apiGets.push(path);
      return {{ proxies: [] }};
    }},
    async post() {{ return {{ success: true }}; }},
    async patch() {{ return {{ success: true }}; }},
    async delete() {{ return {{ success: true }}; }},
  }},
  confirm: async () => true,
}};

context.global = context;
context.globalThis = context;

vm.createContext(context);
vm.runInContext(
  settingsSource + `\n;globalThis.__settingsTestExports = {{\n  buildProxyQueryString,\n  handleApplyProxyFilters,\n  updateProxySelectionUi,\n  renderProxyImportResult,\n  renderProxies,\n  elements,\n  getProxyFilters: () => ({{ ...proxyFilters }}),\n  getSelectedProxyIds: () => Array.from(selectedProxyIds).sort((a, b) => a - b),\n  resetProxyState: () => {{\n    Object.assign(proxyFilters, getDefaultProxyFilters());\n    selectedProxyIds = new Set();\n  }},\n}};`,
  context,
);

const exported = context.__settingsTestExports;

function snapshotSelectionState() {{
  const selectAll = getElement('select-all-proxies');
  const batchDelete = getElement('batch-delete-proxies-btn');
  return {{
    select_all_disabled: !!selectAll.disabled,
    select_all_checked: !!selectAll.checked,
    select_all_indeterminate: !!selectAll.indeterminate,
    batch_delete_disabled: !!batchDelete.disabled,
    batch_delete_text: batchDelete.textContent,
  }};
}}

async function runScenario() {{
  exported.resetProxyState();

  switch (scenarioName) {{
    case 'apply_proxy_filters': {{
      getElement('proxy-filter-keyword').value = '  us-west  ';
      getElement('proxy-filter-type').value = 'http';
      getElement('proxy-filter-enabled').value = 'true';
      getElement('proxy-filter-is-default').value = 'false';
      getElement('proxy-filter-location').value = ' Seattle ';

      await exported.handleApplyProxyFilters({{ preventDefault() {{}} }});
      return {{
        api_path: logs.apiGets.at(-1),
        filters: exported.getProxyFilters(),
      }};
    }}
    case 'proxy_selection_ui': {{
      exported.renderProxies([]);
      const emptyState = snapshotSelectionState();

      exported.renderProxies([
        {{ id: 1, name: '代理-001', type: 'http', host: '1.1.1.1', port: 8080, country: '美国', city: '西雅图', is_default: false, enabled: true, last_used: '2026-03-22T10:00:00', username: null }},
        {{ id: 2, name: '代理-002', type: 'socks5', host: '2.2.2.2', port: 1080, country: '', city: '', is_default: true, enabled: false, last_used: null, username: 'alice' }},
      ]);
      const afterRender = snapshotSelectionState();

      const checkboxes = getElement('proxies-table').querySelectorAll('.proxy-checkbox[data-id]');
      checkboxes[0].checked = true;
      checkboxes[0].dispatchEvent('change', {{ target: checkboxes[0] }});
      const afterOneSelected = {{
        ...snapshotSelectionState(),
        selected_ids: exported.getSelectedProxyIds(),
      }};

      checkboxes[1].checked = true;
      checkboxes[1].dispatchEvent('change', {{ target: checkboxes[1] }});
      const afterAllSelected = {{
        ...snapshotSelectionState(),
        selected_ids: exported.getSelectedProxyIds(),
      }};

      return {{ empty_state: emptyState, after_render: afterRender, after_one_selected: afterOneSelected, after_all_selected: afterAllSelected }};
    }}
    case 'proxy_import_result': {{
      exported.renderProxyImportResult({{
        success: 1,
        skipped: 1,
        failed: 1,
        results: [
          {{ line_no: 1, status: 'success', proxy: {{ name: '美国-西雅图-001', host: '1.1.1.1', port: 8080 }} }},
          {{ line_no: 2, status: 'skipped', reason: 'duplicate' }},
          {{ line_no: 3, status: 'failed', reason: 'invalid format' }},
        ],
      }});
      const importResult = getElement('proxy-import-result');
      return {{
        display: importResult.style.display,
        html: importResult.innerHTML,
      }};
    }}
    case 'proxy_row_actions': {{
      exported.renderProxies([
        {{ id: 7, name: '代理-007', type: 'http', host: '7.7.7.7', port: 8080, country: '美国', city: '西雅图', is_default: false, enabled: true, last_used: '2026-03-22T10:00:00', username: 'bob' }},
      ]);
      return {{ html: getElement('proxies-table').innerHTML }};
    }}
    default:
      throw new Error(`Unknown scenario: ${{scenarioName}}`);
  }}
}}

runScenario()
  .then(result => {{
    process.stdout.write(JSON.stringify(result));
  }})
  .catch(error => {{
    process.stderr.write(String(error.stack || error));
    process.exit(1);
  }});
"""

    result = subprocess.run(
        ["node"],
        input=node_script,
        text=True,
        capture_output=True,
        cwd=ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"Node harness failed for scenario {name!r}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return json.loads(result.stdout)
