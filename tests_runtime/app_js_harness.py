from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "static" / "js" / "app.js"


def run_app_js_scenario(name: str) -> dict:
    app_source = APP_JS.read_text(encoding="utf-8")
    node_script = rf"""
const vm = require('vm');

const scenarioName = {json.dumps(name)};
const appSource = {json.dumps(app_source)};

function createClassList() {{
  const classes = new Set();
  return {{
    add(...tokens) {{
      tokens.filter(Boolean).forEach(token => classes.add(token));
    }},
    remove(...tokens) {{
      tokens.filter(Boolean).forEach(token => classes.delete(token));
    }},
    contains(token) {{
      return classes.has(token);
    }},
    toggle(token, force) {{
      if (force === true) {{
        classes.add(token);
        return true;
      }}
      if (force === false) {{
        classes.delete(token);
        return false;
      }}
      if (classes.has(token)) {{
        classes.delete(token);
        return false;
      }}
      classes.add(token);
      return true;
    }},
    toString() {{
      return [...classes].join(' ');
    }},
  }};
}}

function createMockElement(id = '') {{
  const element = {{
    id,
    tagName: 'DIV',
    style: {{}},
    dataset: {{}},
    disabled: false,
    checked: false,
    value: '',
    className: '',
    classList: createClassList(),
    _textContent: '',
    _innerHTML: '',
    _children: [],
    _listeners: {{}},
    addEventListener(type, handler) {{
      this._listeners[type] = handler;
    }},
    dispatchEvent(type, event = {{}}) {{
      if (this._listeners[type]) {{
        return this._listeners[type](event);
      }}
      return undefined;
    }},
    appendChild(child) {{
      this._children.push(child);
      return child;
    }},
    removeChild(child) {{
      this._children = this._children.filter(item => item !== child);
      return child;
    }},
    querySelectorAll(selector) {{
      if (selector === '.log-line') {{
        return this._children.filter(child => String(child.className || '').split(/\s+/).includes('log-line'));
      }}
      return [];
    }},
    querySelector() {{
      return null;
    }},
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
      this._innerHTML = this._textContent;
    }},
  }});

  Object.defineProperty(element, 'innerHTML', {{
    get() {{
      return this._innerHTML;
    }},
    set(value) {{
      this._innerHTML = String(value ?? '');
      this._textContent = this._innerHTML;
    }},
  }});

  return element;
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
  getElementById(id) {{
    return getElement(id);
  }},
  querySelectorAll() {{
    return [];
  }},
  querySelector() {{
    return null;
  }},
  addEventListener(type, handler) {{
    domListeners[type] = handler;
  }},
  createElement() {{
    return createMockElement();
  }},
  body: createMockElement('body'),
}};

document.body.appendChild = () => {{}};
document.body.removeChild = () => {{}};

const sessionStore = new Map();
const logs = {{
  lastPostPath: null,
  lastPostPayload: null,
  apiGetPaths: [],
}};

class MockWebSocket {{
  static OPEN = 1;
  static CLOSED = 3;

  constructor(url) {{
    this.url = url;
    this.readyState = MockWebSocket.OPEN;
    this.onopen = null;
    this.onclose = null;
    this.onerror = null;
    this.onmessage = null;
  }}

  send() {{}}

  close() {{
    this.readyState = MockWebSocket.CLOSED;
    if (typeof this.onclose === 'function') {{
      this.onclose({{ code: 1000 }});
    }}
  }}
}}

const context = {{
  console,
  URLSearchParams,
  document,
  window: {{ location: {{ protocol: 'http:', host: 'localhost' }} }},
  navigator: {{ clipboard: {{ writeText: async () => {{}} }} }},
  localStorage: {{ getItem() {{ return null; }}, setItem() {{}}, removeItem() {{}} }},
  sessionStorage: {{
    getItem(key) {{
      return sessionStore.has(key) ? sessionStore.get(key) : null;
    }},
    setItem(key, value) {{
      sessionStore.set(key, String(value));
    }},
    removeItem(key) {{
      sessionStore.delete(key);
    }},
  }},
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  WebSocket: MockWebSocket,
  theme: {{ applyTheme() {{}}, toggle() {{}} }},
  toast: {{ success() {{}}, error() {{}}, warning() {{}}, info() {{}} }},
  format: {{ date(value) {{ return value ? String(value) : '-'; }} }},
  escapeHtml(value) {{ return String(value ?? ''); }},
  getServiceTypeText(value) {{ return String(value ?? '-'); }},
  fetch: async () => ({{ ok: true, json: async () => ({{}}), blob: async () => ({{}}), headers: {{ get() {{ return null; }} }} }}),
  confirm: async () => true,
  api: {{
    async post(path, payload) {{
      logs.lastPostPath = path;
      logs.lastPostPayload = JSON.parse(JSON.stringify(payload));
      if (path === '/registration/start') {{
        return {{ task_uuid: 'task-single-01', status: 'pending' }};
      }}
      return {{ batch_id: 'batch-001', count: payload.count }};
    }},
    async get(path) {{
      logs.apiGetPaths.push(path);

      if (path === '/registration/batch/batch-unlimited-01') {{
        return {{
          total: 0,
          completed: 4,
          success: 2,
          failed: 2,
          finished: false,
          is_unlimited: true,
          consecutive_failures: 1,
          max_consecutive_failures: 10,
          domain_stats: [],
        }};
      }}
      if (path === '/registration/tasks/task-single-01') {{
        return {{
          task_uuid: 'task-single-01',
          status: 'running',
          email: 'tester@example.com',
          email_service: 'tempmail',
          steps: [
            {{ step_key: 'create_email', status: 'running', duration_ms: 88 }},
          ],
        }};
      }}

      return {{ accounts: [], finished: false }};
    }},
    async patch() {{ return {{ success: true }}; }},
    async delete() {{ return {{ success: true }}; }},
  }},
}};

context.global = context;
context.globalThis = context;

vm.createContext(context);
vm.runInContext(
  appSource + `\n;globalThis.__appTestExports = {{\n  handleModeChange,\n  handleBatchRegistration,\n  handleSingleRegistration,\n  renderTaskSteps,\n  showBatchStatus,\n  updateBatchProgress,\n  restoreActiveTask,\n  elements,\n}};`,
  context,
);

const exported = context.__appTestExports;

function setupBaseElements() {{
  getElement('reg-mode').value = 'single';
  getElement('batch-count').value = '5';
  getElement('interval-min').value = '5';
  getElement('interval-max').value = '30';
  getElement('concurrency-count').value = '3';
  getElement('concurrency-mode').value = 'pipeline';
  getElement('pipeline-key').value = 'current_pipeline';
  getElement('batch-count-group').style.display = 'none';
  getElement('batch-options').style.display = 'none';
  getElement('batch-domain-stats').innerHTML = '';
  getElement('batch-consecutive-failures').textContent = '-';
}}

async function runScenario() {{
  setupBaseElements();

  switch (scenarioName) {{
    case 'unlimited_mode_request': {{
      const regMode = getElement('reg-mode');
      regMode.value = 'unlimited';
      exported.handleModeChange({{ target: regMode }});

      const requestPayload = {{ email_service_type: 'tempmail' }};
      await exported.handleBatchRegistration(requestPayload);

      return {{
        batch_count_display: getElement('batch-count-group').style.display || '',
        request_payload: logs.lastPostPayload,
        saved_active_task: JSON.parse(context.sessionStorage.getItem('activeTask') || 'null'),
      }};
    }}
    case 'pipeline_batch_request': {{
      getElement('pipeline-key').value = 'codexgen_pipeline';
      const requestPayload = {{ email_service_type: 'tempmail' }};
      await exported.handleBatchRegistration(requestPayload);
      return {{
        request_payload: logs.lastPostPayload,
      }};
    }}
    case 'render_task_steps': {{
      exported.renderTaskSteps([
        {{ step_key: 'create_email', status: 'completed', duration_ms: 123 }},
        {{ step_key: 'submit_login_email', status: 'failed', duration_ms: 456, error_message: 'timeout' }},
      ]);
      return {{
        waterfall_html: getElement('task-step-waterfall').innerHTML,
      }};
    }}
    case 'single_task_step_refresh': {{
      await exported.handleSingleRegistration({{ email_service_type: 'tempmail', pipeline_key: 'codexgen_pipeline' }});
      return {{
        api_get_paths: logs.apiGetPaths.slice(),
        waterfall_html: getElement('task-step-waterfall').innerHTML,
      }};
    }}
    case 'unlimited_progress_running': {{
      exported.showBatchStatus({{ count: 0 }});
      exported.updateBatchProgress({{
        is_unlimited: true,
        completed: 5,
        total: 0,
        finished: false,
        success: 2,
        failed: 1,
        consecutive_failures: 3,
        max_consecutive_failures: 10,
        domain_stats: [
          {{ domain: 'gmail.com', success: 3, failed: 1, total: 4, success_rate: 75, failure_rate: 25 }},
        ],
      }});

      return {{
        progress_text: getElement('batch-progress-text').textContent,
        progress_percent: getElement('batch-progress-percent').textContent,
        progress_bar_indeterminate: getElement('progress-bar').classList.contains('indeterminate'),
        consecutive_failures_text: getElement('batch-consecutive-failures').textContent,
        domain_stats_display: getElement('batch-domain-stats').style.display || '',
        domain_stats_html: getElement('batch-domain-stats').innerHTML,
      }};
    }}
    case 'unlimited_progress_finished': {{
      exported.showBatchStatus({{ count: 0 }});
      exported.updateBatchProgress({{
        is_unlimited: true,
        completed: 8,
        total: 0,
        finished: true,
        success: 6,
        failed: 2,
        consecutive_failures: 0,
        max_consecutive_failures: 10,
        domain_stats: [
          {{ domain: 'gmail.com', success: 3, failed: 1, total: 4, success_rate: 75, failure_rate: 25 }},
        ],
      }});

      return {{
        progress_text: getElement('batch-progress-text').textContent,
        progress_percent: getElement('batch-progress-percent').textContent,
        progress_bar_indeterminate: getElement('progress-bar').classList.contains('indeterminate'),
        domain_stats_html: getElement('batch-domain-stats').innerHTML,
      }};
    }}
    case 'restore_unlimited_task': {{
      context.sessionStorage.setItem('activeTask', JSON.stringify({{
        batch_id: 'batch-unlimited-01',
        mode: 'unlimited',
        total: 0,
      }}));

      await exported.restoreActiveTask();

      return {{
        api_get_paths: logs.apiGetPaths.slice(),
        batch_progress_display: getElement('batch-progress-section').style.display || '',
      }};
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
