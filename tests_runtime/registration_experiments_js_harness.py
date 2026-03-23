from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "static" / "js" / "registration_experiments.js"


def run_registration_experiments_js_scenario(name: str) -> dict:
    script_source = SCRIPT.read_text(encoding="utf-8")
    node_script = rf"""
const vm = require('vm');

const scenarioName = {json.dumps(name)};
const scriptSource = {json.dumps(script_source)};

function createMockElement(id = '') {{
  let html = '';
  let text = '';
  const listeners = {{}};
  return {{
    id,
    value: '',
    style: {{}},
    dataset: {{}},
    disabled: false,
    checked: false,
    addEventListener(type, handler) {{
      listeners[type] = handler;
    }},
    dispatchEvent(type, event = {{}}) {{
      if (listeners[type]) {{
        return listeners[type](event);
      }}
      return undefined;
    }},
    querySelectorAll() {{
      return [];
    }},
    querySelector() {{
      return null;
    }},
    classList: {{
      add() {{}},
      remove() {{}},
      contains() {{
        return false;
      }},
      toggle() {{
        return false;
      }},
    }},
    get innerHTML() {{
      return html;
    }},
    set innerHTML(next) {{
      html = String(next ?? '');
      text = html;
    }},
    get textContent() {{
      return text;
    }},
    set textContent(next) {{
      text = String(next ?? '');
      html = text;
    }},
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
}};

const logs = {{
  apiGetPaths: [],
  toastWarnings: [],
  toastErrors: [],
}};

const context = {{
  console,
  document,
  api: {{
    async get(path) {{
      logs.apiGetPaths.push(path);
      if (path === '/registration/experiments/7') {{
        return {{
          total_tasks: 4,
          status: 'completed',
          pipelines: {{
            current_pipeline: {{ success_rate: 1, avg_duration_ms: 120 }},
            codexgen_pipeline: {{ success_rate: 0.5, avg_duration_ms: 220 }},
          }},
        }};
      }}
      if (path === '/registration/experiments/7/steps') {{
        return {{
          steps: [
            {{
              step_key: 'create_email',
              pipelines: {{
                current_pipeline: {{ success_rate: 1, avg_duration_ms: 100 }},
                codexgen_pipeline: {{ success_rate: 0.5, avg_duration_ms: 180 }},
              }},
              pipeline_diff: {{ avg_duration_ms: 80 }},
            }},
          ],
        }};
      }}
      if (path === '/accounts/survival-summary?experiment_batch_id=7') {{
        return {{
          counts: {{ healthy: 3, warning: 1, dead: 0 }},
          ratios: {{ healthy: 0.75, warning: 0.25, dead: 0 }},
        }};
      }}
      throw new Error(`unexpected path: ${{path}}`);
    }},
  }},
  toast: {{
    warning(message) {{
      logs.toastWarnings.push(String(message));
    }},
    error(message) {{
      logs.toastErrors.push(String(message));
    }},
  }},
}};

vm.createContext(context);
vm.runInContext(scriptSource, context);

async function runScenario() {{
  if (scenarioName === 'load_dashboard') {{
    getElement('experiment-id-input').value = '7';
    await context.loadExperimentDashboard();
    return {{
      apiGetPaths: logs.apiGetPaths,
      summaryHtml: getElement('experiment-summary').innerHTML,
      stepHtml: getElement('experiment-step-compare').innerHTML,
      survivalHtml: getElement('survival-summary').innerHTML,
      toastWarnings: logs.toastWarnings,
      toastErrors: logs.toastErrors,
    }};
  }}

  throw new Error(`unknown scenario: ${{scenarioName}}`);
}}

runScenario()
  .then((result) => {{
    console.log(JSON.stringify(result));
  }})
  .catch((error) => {{
    console.error(error.stack || String(error));
    process.exit(1);
  }});
"""

    output = subprocess.check_output(["node", "-e", node_script], cwd=ROOT, text=True)
    return json.loads(output)
