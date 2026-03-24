from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "static" / "js" / "registration_batch_stats.js"


def run_registration_batch_stats_js_scenario(name: str) -> dict:
    script_source = SCRIPT.read_text(encoding="utf-8") if SCRIPT.exists() else ""
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
  apiPostPaths: [],
  apiPostBodies: [],
  toastWarnings: [],
  toastErrors: [],
}};

const context = {{
  console,
  document,
  api: {{
    async get(path) {{
      logs.apiGetPaths.push(path);
      if (path === '/registration/batch-stats/1') {{
        return {{
          id: 1,
          batch_id: 'batch-001',
          status: 'completed',
          pipeline_key: 'current_pipeline',
          target_count: 10,
          finished_count: 10,
          success_count: 8,
          failed_count: 2,
          total_duration_ms: 20000,
          avg_duration_ms: 200,
          started_at: '2026-03-24T08:00:00Z',
          completed_at: '2026-03-24T08:10:00Z',
          step_stats: [
            {{
              step_key: 'create_email',
              step_order: 2,
              sample_count: 10,
              success_count: 8,
              avg_duration_ms: 120,
              p50_duration_ms: 110,
              p90_duration_ms: 180,
            }},
          ],
          stage_stats: [
            {{
              stage_key: 'signup_prepare',
              sample_count: 10,
              avg_duration_ms: 300,
              p50_duration_ms: 280,
              p90_duration_ms: 420,
            }},
          ],
        }};
      }}
      if (path === '/registration/batch-stats') {{
        return {{
          total: 2,
          items: [
            {{ id: 1, batch_id: 'batch-001', status: 'completed', pipeline_key: 'current_pipeline', target_count: 10, success_count: 8, failed_count: 2, avg_duration_ms: 200 }},
            {{ id: 2, batch_id: 'batch-002', status: 'failed', pipeline_key: 'codexgen_pipeline', target_count: 12, success_count: 6, failed_count: 6, avg_duration_ms: 260 }},
          ],
        }};
      }}
      throw new Error(`unexpected path: ${{path}}`);
    }},
    async post(path, body) {{
      logs.apiPostPaths.push(path);
      logs.apiPostBodies.push(body);
      if (path === '/registration/batch-stats/compare') {{
        return {{
          left: {{
            id: 1,
            batch_id: 'batch-001',
            status: 'completed',
            pipeline_key: 'current_pipeline',
            target_count: 10,
            finished_count: 10,
            success_count: 8,
            failed_count: 2,
            total_duration_ms: 20000,
            avg_duration_ms: 200,
            started_at: '2026-03-24T08:00:00Z',
            completed_at: '2026-03-24T08:10:00Z',
          }},
          right: {{
            id: 2,
            batch_id: 'batch-002',
            status: 'failed',
            pipeline_key: 'codexgen_pipeline',
            target_count: 12,
            finished_count: 12,
            success_count: 6,
            failed_count: 6,
            total_duration_ms: 24000,
            avg_duration_ms: 250,
            started_at: '2026-03-24T09:00:00Z',
            completed_at: '2026-03-24T09:15:00Z',
          }},
          summary_diff: {{
            target_count: 2,
            success_count: -2,
            failed_count: 4,
            success_rate: -0.125,
            total_duration_ms: 4000,
            avg_duration_ms: 50,
          }},
          step_diffs: [
            {{
              step_key: 'create_email',
              left: {{ sample_count: 10, success_count: 8, avg_duration_ms: 120, p50_duration_ms: 110, p90_duration_ms: 180 }},
              right: {{ sample_count: 12, success_count: 6, avg_duration_ms: 150, p50_duration_ms: 140, p90_duration_ms: 210 }},
              delta_duration_ms: 30,
              delta_rate: 0.25,
            }},
          ],
          stage_diffs: [
            {{
              stage_key: 'signup_prepare',
              left: {{ sample_count: 10, avg_duration_ms: 300, p50_duration_ms: 280, p90_duration_ms: 420 }},
              right: {{ sample_count: 12, avg_duration_ms: 340, p50_duration_ms: 310, p90_duration_ms: 480 }},
              delta_duration_ms: 40,
              delta_rate: 0.1333,
            }},
          ],
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
  if (scenarioName === 'compare_two') {{
    const checkbox1 = createMockElement('cb-1');
    checkbox1.dataset.batchId = '1';
    checkbox1.checked = true;
    const checkbox2 = createMockElement('cb-2');
    checkbox2.dataset.batchId = '2';
    checkbox2.checked = true;

    if (typeof context.handleBatchSelectionChange === 'function') {{
      await context.handleBatchSelectionChange(checkbox1);
      await context.handleBatchSelectionChange(checkbox2);
    }}

    return {{
      apiGetPaths: logs.apiGetPaths,
      apiPostPaths: logs.apiPostPaths,
      apiPostBodies: logs.apiPostBodies,
      detailHtml: getElement('batch-stats-detail').innerHTML,
      compareHtml: getElement('batch-stats-compare').innerHTML,
      toastWarnings: logs.toastWarnings,
      toastErrors: logs.toastErrors,
    }};
  }}

  if (scenarioName === 'prevent_over_selection') {{
    const checkbox1 = createMockElement('cb-1');
    checkbox1.dataset.batchId = '1';
    checkbox1.checked = true;
    const checkbox2 = createMockElement('cb-2');
    checkbox2.dataset.batchId = '2';
    checkbox2.checked = true;
    const checkbox3 = createMockElement('cb-3');
    checkbox3.dataset.batchId = '3';
    checkbox3.checked = true;

    if (typeof context.handleBatchSelectionChange === 'function') {{
      await context.handleBatchSelectionChange(checkbox1);
      await context.handleBatchSelectionChange(checkbox2);
      await context.handleBatchSelectionChange(checkbox3);
    }}

    return {{
      toastWarnings: logs.toastWarnings,
      selectedIds: typeof context.getSelectedBatchIds === 'function' ? context.getSelectedBatchIds() : [],
      thirdChecked: checkbox3.checked,
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
