from pathlib import Path
import subprocess
import textwrap


def test_registration_page_initial_batch_mode_submits_batch_request():
    app_js_path = Path("static/js/app.js").resolve()

    node_script = textwrap.dedent(
        f"""
        (async () => {{
        const fs = require('fs');
        const vm = require('vm');

        class FakeElement {{
          constructor(id) {{
            this.id = id;
            this.value = '';
            this.checked = false;
            this.disabled = false;
            this.innerHTML = '';
            this.textContent = '';
            this.className = '';
            this.dataset = {{}};
            this.style = {{}};
            this.children = [];
            this.listeners = {{}};
            this.scrollTop = 0;
            this.scrollHeight = 0;
          }}

          addEventListener(type, callback) {{
            this.listeners[type] = callback;
          }}

          appendChild(child) {{
            this.children.push(child);
            this.scrollHeight = this.children.length;
            return child;
          }}

          querySelectorAll() {{
            return [];
          }}

          querySelector() {{
            return null;
          }}

          closest() {{
            return null;
          }}

          contains() {{
            return false;
          }}

          remove() {{
            return null;
          }}
        }}

        const ids = [
          'registration-form', 'email-service', 'reg-mode', 'reg-mode-group',
          'batch-count-group', 'batch-count', 'batch-options', 'interval-min',
          'interval-max', 'start-btn', 'cancel-btn', 'task-status-row',
          'batch-progress-section', 'console-log', 'clear-log-btn', 'task-id',
          'task-email', 'task-status', 'task-service', 'task-status-badge',
          'batch-progress-text', 'batch-progress-percent', 'progress-bar',
          'batch-success', 'batch-failed', 'batch-remaining',
          'recent-accounts-table', 'refresh-accounts-btn',
          'outlook-batch-section', 'outlook-accounts-container',
          'outlook-interval-min', 'outlook-interval-max',
          'outlook-skip-registered', 'outlook-concurrency-mode',
          'outlook-concurrency-count', 'outlook-concurrency-hint',
          'outlook-interval-group', 'concurrency-mode', 'concurrency-count',
          'concurrency-hint', 'interval-group', 'auto-upload-cpa',
          'cpa-service-select-group', 'cpa-service-select',
          'auto-upload-sub2api', 'sub2api-service-select-group',
          'sub2api-service-select', 'auto-upload-tm',
          'tm-service-select-group', 'tm-service-select'
        ];

        const elements = Object.fromEntries(ids.map((id) => [id, new FakeElement(id)]));

        elements['email-service'].value = 'tempmail:default';
        elements['reg-mode'].value = 'batch';
        elements['batch-count'].value = '7';
        elements['interval-min'].value = '5';
        elements['interval-max'].value = '30';
        elements['concurrency-mode'].value = 'pipeline';
        elements['concurrency-count'].value = '3';
        elements['outlook-skip-registered'].checked = true;

        const documentListeners = {{}};
        const document = {{
          getElementById(id) {{
            return elements[id] || new FakeElement(id);
          }},
          addEventListener(type, callback) {{
            if (!documentListeners[type]) {{
              documentListeners[type] = [];
            }}
            documentListeners[type].push(callback);
          }},
          createElement(tag) {{
            return new FakeElement(tag);
          }},
          querySelectorAll() {{
            return [];
          }},
          body: new FakeElement('body')
        }};

        const sessionStorage = {{
          _store: {{}},
          getItem(key) {{
            return Object.prototype.hasOwnProperty.call(this._store, key) ? this._store[key] : null;
          }},
          setItem(key, value) {{
            this._store[key] = String(value);
          }},
          removeItem(key) {{
            delete this._store[key];
          }}
        }};

        let postedPath = null;
        const api = {{
          async get(path) {{
            if (path === '/registration/available-services') {{
              return {{
                tempmail: {{ available: true, count: 1, services: [{{ name: 'Tempmail.lol (临时邮箱)' }}] }},
                outlook: {{ available: false, count: 0, services: [] }},
                moe_mail: {{ available: false, count: 0, services: [] }},
                temp_mail: {{ available: false, count: 0, services: [] }},
                duck_mail: {{ available: false, count: 0, services: [] }},
                freemail: {{ available: false, count: 0, services: [] }}
              }};
            }}
            if (path === '/accounts?page=1&page_size=10') {{
              return {{ accounts: [] }};
            }}
            return [];
          }},
          async post(path) {{
            postedPath = path;
            if (path === '/registration/batch') {{
              return {{ batch_id: 'batch-1', count: 7, tasks: [] }};
            }}
            if (path === '/registration/start') {{
              return {{ task_uuid: 'task-1' }};
            }}
            return {{}};
          }}
        }};

        class FakeWebSocket {{
          constructor(url) {{
            this.url = url;
            this.readyState = FakeWebSocket.OPEN;
          }}

          close() {{
            this.readyState = FakeWebSocket.CLOSED;
          }}

          send() {{
            return null;
          }}
        }}

        FakeWebSocket.OPEN = 1;
        FakeWebSocket.CLOSED = 3;

        const context = {{
          console,
          document,
          window: {{
            location: {{
              protocol: 'http:',
              host: '127.0.0.1:8000'
            }}
          }},
          sessionStorage,
          api,
          toast: {{
            error() {{}},
            info() {{}},
            success() {{}},
            warning() {{}}
          }},
          WebSocket: FakeWebSocket,
          theme: {{ toggle() {{}} }},
          copyToClipboard() {{}},
          setInterval() {{ return 1; }},
          clearInterval() {{}},
          setTimeout,
          clearTimeout,
          Promise,
          Date,
          Math,
          JSON,
          Array,
          Object,
          String,
          parseInt,
        }};

        vm.createContext(context);
        const code = fs.readFileSync({str(app_js_path)!r}, 'utf8');
        vm.runInContext(code, context);
        vm.runInContext(
          'globalThis.__test_exports = {{ handleStartRegistration }};',
          context
        );

        for (const callback of documentListeners['DOMContentLoaded'] || []) {{
          callback();
        }}

        await Promise.resolve();
        elements['email-service'].value = 'tempmail:default';

        await context.__test_exports.handleStartRegistration({{
          preventDefault() {{}}
        }});

        if (postedPath !== '/registration/batch') {{
          throw new Error(`expected /registration/batch, got ${{postedPath}}`);
        }}
        }})().catch((error) => {{
          console.error(error);
          process.exit(1);
        }});
        """
    )

    result = subprocess.run(
        ["node", "-e", node_script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
