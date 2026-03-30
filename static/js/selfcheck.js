/**
 * 系统自检页面脚本
 */

const selfcheckState = {
    runs: [],
    selectedRunId: null,
    selectedRun: null,
    repairCatalog: {},
    repairPreview: null,
    scheduleInitialized: false,
    pollTimer: null,
};

function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = String(value ?? '');
    return div.innerHTML;
}

function normalizeStatus(status) {
    const text = String(status || '').toLowerCase();
    if (text === 'completed' || text === 'pass') return { cls: 'completed', text: '通过' };
    if (text === 'running') return { cls: 'running', text: '运行中' };
    if (text === 'pending') return { cls: 'pending', text: '等待中' };
    if (text === 'warn') return { cls: 'warning', text: '警告' };
    if (text === 'fail' || text === 'failed') return { cls: 'failed', text: '失败' };
    if (text === 'skip') return { cls: 'disabled', text: '跳过' };
    return { cls: 'disabled', text: text || '-' };
}

function statusBadge(status) {
    const normalized = normalizeStatus(status);
    return `<span class="status-badge ${normalized.cls}">${escapeHtml(normalized.text)}</span>`;
}

function parseErrorMessage(error) {
    const detail = error?.data?.detail;
    if (typeof detail === 'string') return detail;
    if (detail && typeof detail === 'object') {
        if (typeof detail.message === 'string') return detail.message;
        return JSON.stringify(detail);
    }
    return error?.message || '请求失败';
}

function getRepairName(key) {
    return selfcheckState.repairCatalog?.[key]?.name || key;
}

function renderRuntime(runtime) {
    const statusNode = document.getElementById('runtime-status');
    const nextRunNode = document.getElementById('runtime-next-run');
    const lastRunNode = document.getElementById('runtime-last-run');
    const summaryNode = document.getElementById('runtime-last-summary');
    const logsNode = document.getElementById('selfcheck-runtime-logs');
    if (!statusNode || !nextRunNode || !lastRunNode || !summaryNode || !logsNode) return;

    statusNode.textContent = normalizeStatus(runtime?.last_status).text || '-';
    nextRunNode.textContent = runtime?.next_run_at ? format.date(runtime.next_run_at) : '-';
    lastRunNode.textContent = runtime?.last_finished_at ? format.date(runtime.last_finished_at) : '-';
    summaryNode.textContent = runtime?.last_run?.summary || runtime?.last_error || '-';

    const logs = Array.isArray(runtime?.logs) ? runtime.logs : [];
    if (!logs.length) {
        logsNode.innerHTML = '<div class="selfcheck-log-item"><span>-</span><span>-</span><span>暂无调度日志</span></div>';
        return;
    }

    logsNode.innerHTML = logs.map((item) => {
        const level = String(item?.level || 'info').toLowerCase();
        return `
            <div class="selfcheck-log-item">
                <span>${escapeHtml(format.date(item?.time))}</span>
                <span class="selfcheck-log-level ${level}">${escapeHtml(level)}</span>
                <span>${escapeHtml(item?.message || '')}</span>
            </div>
        `;
    }).join('');
}

function renderSchedule(data) {
    const enabledNode = document.getElementById('selfcheck-auto-enabled');
    const intervalNode = document.getElementById('selfcheck-interval-select');
    const modeNode = document.getElementById('selfcheck-auto-mode-select');
    if (!enabledNode || !intervalNode || !modeNode) return;

    if (!selfcheckState.scheduleInitialized) {
        enabledNode.checked = Boolean(data?.enabled);
        intervalNode.value = String(data?.interval_minutes || 15);
        modeNode.value = String(data?.mode || 'quick');
        selfcheckState.scheduleInitialized = true;
    }

    renderRuntime(data?.runtime || {});
}

function renderRuns() {
    const body = document.getElementById('selfcheck-runs-body');
    if (!body) return;
    const rows = selfcheckState.runs || [];
    if (!rows.length) {
        body.innerHTML = '<tr><td colspan="9"><div class="empty-state">暂无运行记录</div></td></tr>';
        return;
    }

    body.innerHTML = rows.map((run) => {
        const isSelected = Number(run.id) === Number(selfcheckState.selectedRunId);
        return `
            <tr data-run-id="${run.id}" class="${isSelected ? 'run-row-active' : ''}">
                <td>#${run.id}</td>
                <td>${escapeHtml(run.mode || '-')}</td>
                <td>${escapeHtml(run.source || '-')}</td>
                <td>${statusBadge(run.status)}</td>
                <td>${Number(run.score || 0)}</td>
                <td>${Number(run.total_checks || 0)}</td>
                <td>${escapeHtml(run.started_at ? format.date(run.started_at) : '-')}</td>
                <td>${escapeHtml(run.finished_at ? format.date(run.finished_at) : '-')}</td>
                <td>${escapeHtml(run.summary || '-')}</td>
            </tr>
        `;
    }).join('');
}

function renderRepairResults(repairs) {
    const container = document.getElementById('selfcheck-repair-results');
    if (!container) return;
    const list = Array.isArray(repairs) ? repairs : [];
    if (!list.length) {
        container.innerHTML = '';
        return;
    }
    container.innerHTML = list.map((item) => {
        return `
            <div class="repair-result-item">
                <strong>${escapeHtml(item?.name || item?.key || '修复动作')}</strong>
                <div>完成时间：${escapeHtml(format.date(item?.finished_at || ''))}</div>
                <div>耗时：${escapeHtml(String(item?.duration_ms || 0))} ms</div>
                <div>结果：<code>${escapeHtml(JSON.stringify(item?.detail || {}))}</code></div>
            </div>
        `;
    }).join('');
}

function renderRepairPreview(preview) {
    const container = document.getElementById('repair-center-preview-list');
    if (!container) return;
    const items = Array.isArray(preview?.items) ? preview.items : [];
    if (!items.length) {
        container.innerHTML = '<div class="empty-state">暂无预览结果</div>';
        return;
    }
    container.innerHTML = items.map((item) => {
        const impactCount = Number(item?.impact_count || 0);
        const checked = impactCount > 0 ? 'checked' : '';
        return `
            <label class="repair-center-preview-item">
                <input type="checkbox" class="repair-center-key" data-repair-key="${escapeHtml(item?.key || '')}" ${checked}>
                <div>
                    <div><strong>${escapeHtml(item?.name || item?.key || '-')}</strong></div>
                    <div>预计影响：${impactCount}</div>
                    <div class="hint-inline">${escapeHtml(JSON.stringify(item?.preview || {}))}</div>
                </div>
            </label>
        `;
    }).join('');
}

function renderRepairRollbacks(items) {
    const container = document.getElementById('repair-center-rollbacks');
    if (!container) return;
    const list = Array.isArray(items) ? items : [];
    if (!list.length) {
        container.innerHTML = '<div class="empty-state">暂无回滚点</div>';
        return;
    }
    container.innerHTML = list.map((item) => {
        return `
            <div class="repair-center-rollback-item">
                <div>
                    <div><strong>${escapeHtml(item?.rollback_id || '-')}</strong></div>
                    <div>${escapeHtml(format.date(item?.created_at || ''))} | run #${escapeHtml(String(item?.run_id || '-'))}</div>
                    <div>修复项：${escapeHtml((item?.repair_keys || []).join(', ') || '-')}</div>
                </div>
                <button class="btn btn-danger btn-sm repair-center-rollback-btn" data-rollback-id="${escapeHtml(item?.rollback_id || '')}">回滚</button>
            </div>
        `;
    }).join('');
}

function renderRunDetail(run) {
    const titleNode = document.getElementById('selfcheck-detail-title');
    const listNode = document.getElementById('selfcheck-check-list');
    if (!titleNode || !listNode) return;

    if (!run) {
        selfcheckState.selectedRun = null;
        titleNode.textContent = '请在上方点击一条运行记录';
        listNode.innerHTML = '<div class="empty-state">暂无检查数据</div>';
        renderRepairResults([]);
        renderRepairPreview(null);
        return;
    }
    selfcheckState.selectedRun = run;

    titleNode.textContent = `运行 #${run.id} | ${run.mode} | ${normalizeStatus(run.status).text} | 评分 ${run.score || 0}`;
    const checks = Array.isArray(run?.result_data?.checks) ? run.result_data.checks : [];
    if (!checks.length) {
        listNode.innerHTML = '<div class="empty-state">当前运行暂无检查结果</div>';
        renderRepairResults(run?.result_data?.repairs || []);
        return;
    }

    listNode.innerHTML = checks.map((check) => {
        const fixes = Array.isArray(check?.fixes) ? check.fixes : [];
        const detailsText = check?.details ? JSON.stringify(check.details, null, 2) : '';
        return `
            <div class="check-item">
                <div class="check-item-top">
                    <div class="check-item-title">${escapeHtml(check?.name || check?.key || '-')}</div>
                    <div>${statusBadge(check?.status)} <span class="hint-inline">${Number(check?.duration_ms || 0)}ms</span></div>
                </div>
                <div class="check-item-desc">${escapeHtml(check?.message || '-')}</div>
                ${detailsText ? `<details style="margin-top:8px;"><summary>查看明细</summary><pre style="margin-top:6px;white-space:pre-wrap;word-break:break-word;">${escapeHtml(detailsText)}</pre></details>` : ''}
                ${fixes.length ? `
                    <div class="check-fixes">
                        ${fixes.map((fixKey) => `
                            <button class="btn btn-warning btn-sm selfcheck-repair-btn" data-run-id="${run.id}" data-repair-key="${escapeHtml(fixKey)}">
                                ${escapeHtml(getRepairName(fixKey))}
                            </button>
                        `).join('')}
                    </div>
                ` : ''}
            </div>
        `;
    }).join('');

    renderRepairResults(run?.result_data?.repairs || []);
}

async function loadRepairCatalog() {
    try {
        const data = await api.get('/selfcheck/repairs', { requestKey: 'selfcheck-repairs', cancelPrevious: true });
        selfcheckState.repairCatalog = data?.repairs || {};
    } catch (error) {
        selfcheckState.repairCatalog = {};
    }
}

async function loadScheduleAndRuntime() {
    const data = await api.get('/selfcheck/schedule', { requestKey: 'selfcheck-schedule', cancelPrevious: true, silentNetworkError: true, priority: 'low' });
    renderSchedule(data);
    return data;
}

async function loadRuns() {
    const data = await api.get('/selfcheck/runs?limit=60', { requestKey: 'selfcheck-runs', cancelPrevious: true });
    selfcheckState.runs = Array.isArray(data?.runs) ? data.runs : [];
    if (!selfcheckState.selectedRunId && selfcheckState.runs.length) {
        selfcheckState.selectedRunId = selfcheckState.runs[0].id;
    }
    renderRuns();
    if (selfcheckState.selectedRunId) {
        await loadRunDetail(selfcheckState.selectedRunId);
    } else {
        renderRunDetail(null);
    }
}

async function loadRunDetail(runId) {
    if (!runId) {
        renderRunDetail(null);
        return;
    }
    const run = await api.get(`/selfcheck/runs/${Number(runId)}`, { requestKey: `selfcheck-run-${runId}`, cancelPrevious: true });
    selfcheckState.selectedRunId = Number(run.id);
    renderRuns();
    renderRunDetail(run);
}

async function startRun() {
    const modeNode = document.getElementById('selfcheck-mode-select');
    const runBtn = document.getElementById('selfcheck-run-btn');
    if (!modeNode || !runBtn) return;

    loading.show(runBtn, '执行中...');
    try {
        const payload = { mode: modeNode.value, source: 'manual', run_async: true };
        const result = await api.post('/selfcheck/runs', payload);
        const run = result?.run || null;
        if (run?.id) {
            selfcheckState.selectedRunId = Number(run.id);
        }
        toast.success(result?.message || '自检任务已启动');
        await loadRuns();
    } catch (error) {
        const message = parseErrorMessage(error);
        toast.error(`启动失败: ${message}`);
    } finally {
        loading.hide(runBtn);
    }
}

async function saveSchedule() {
    const btn = document.getElementById('selfcheck-save-schedule-btn');
    const enabledNode = document.getElementById('selfcheck-auto-enabled');
    const intervalNode = document.getElementById('selfcheck-interval-select');
    const modeNode = document.getElementById('selfcheck-auto-mode-select');
    if (!btn || !enabledNode || !intervalNode || !modeNode) return;

    loading.show(btn, '保存中...');
    try {
        const payload = {
            enabled: Boolean(enabledNode.checked),
            interval_minutes: Number(intervalNode.value || 15),
            mode: String(modeNode.value || 'quick'),
            run_now: false,
        };
        const data = await api.post('/selfcheck/schedule', payload);
        renderSchedule({ ...payload, runtime: data?.runtime || {} });
        toast.success(data?.message || '保存成功');
    } catch (error) {
        toast.error(`保存失败: ${parseErrorMessage(error)}`);
    } finally {
        loading.hide(btn);
    }
}

async function runNow() {
    const btn = document.getElementById('selfcheck-run-now-btn');
    if (!btn) return;
    loading.show(btn, '请求中...');
    try {
        const data = await api.post('/selfcheck/schedule/run-now', {});
        renderRuntime(data?.runtime || {});
        toast.success(data?.message || '已请求立即执行');
        await loadRuns();
    } catch (error) {
        toast.error(parseErrorMessage(error));
    } finally {
        loading.hide(btn);
    }
}

async function executeRepair(runId, repairKey, btn) {
    if (!runId || !repairKey) return;
    if (btn) loading.show(btn, '执行中...');
    try {
        const data = await api.post(`/selfcheck/runs/${Number(runId)}/repairs/${encodeURIComponent(repairKey)}`, {});
        toast.success(`${getRepairName(repairKey)} 执行完成`);
        const run = data?.run;
        if (run?.id) {
            renderRunDetail(run);
            await loadRuns();
        } else {
            await loadRunDetail(runId);
        }
    } catch (error) {
        toast.error(`修复失败: ${parseErrorMessage(error)}`);
    } finally {
        if (btn) loading.hide(btn);
    }
}

function collectRepairKeysFromSelectedRun() {
    const run = selfcheckState.selectedRun;
    if (!run) return [];
    const checks = Array.isArray(run?.result_data?.checks) ? run.result_data.checks : [];
    const keys = [];
    checks.forEach((check) => {
        const fixes = Array.isArray(check?.fixes) ? check.fixes : [];
        fixes.forEach((key) => {
            const text = String(key || '').trim();
            if (text && !keys.includes(text)) {
                keys.push(text);
            }
        });
    });
    if (keys.length) return keys;
    return Object.keys(selfcheckState.repairCatalog || {});
}

function collectCheckedPreviewKeys() {
    const nodes = Array.from(document.querySelectorAll('.repair-center-key:checked'));
    return nodes.map((node) => String(node?.dataset?.repairKey || '').trim()).filter(Boolean);
}

async function loadRepairRollbacks() {
    try {
        const data = await api.get('/selfcheck/repair-center/rollbacks?limit=20', {
            requestKey: 'selfcheck-repair-rollbacks',
            cancelPrevious: true,
            silentNetworkError: true,
            priority: 'low',
        });
        renderRepairRollbacks(data?.items || []);
    } catch (error) {
        renderRepairRollbacks([]);
    }
}

async function previewRepairCenter() {
    const btn = document.getElementById('repair-center-preview-btn');
    const run = selfcheckState.selectedRun;
    if (!run?.id) {
        toast.warning('请先选择一条自检运行记录');
        return;
    }
    const keys = collectRepairKeysFromSelectedRun();
    if (!keys.length) {
        toast.warning('当前运行没有可预览的修复项');
        return;
    }
    if (btn) loading.show(btn, '预览中...');
    try {
        const data = await api.post('/selfcheck/repair-center/preview', {
            run_id: Number(run.id),
            repair_keys: keys,
        });
        selfcheckState.repairPreview = data?.preview || null;
        renderRepairPreview(selfcheckState.repairPreview);
        toast.success('预览完成');
    } catch (error) {
        toast.error(`预览失败: ${parseErrorMessage(error)}`);
    } finally {
        if (btn) loading.hide(btn);
    }
}

async function executeRepairCenter() {
    const btn = document.getElementById('repair-center-execute-btn');
    const run = selfcheckState.selectedRun;
    if (!run?.id) {
        toast.warning('请先选择一条自检运行记录');
        return;
    }
    const keys = collectCheckedPreviewKeys();
    if (!keys.length) {
        toast.warning('请先勾选要执行的修复项');
        return;
    }
    if (btn) loading.show(btn, '执行中...');
    try {
        const data = await api.post('/selfcheck/repair-center/execute', {
            run_id: Number(run.id),
            repair_keys: keys,
        });
        const rollbackId = data?.result?.rollback_id;
        toast.success(`修复完成${rollbackId ? `，回滚点: ${rollbackId}` : ''}`);
        await loadRunDetail(Number(run.id));
        await loadRepairRollbacks();
    } catch (error) {
        toast.error(`执行失败: ${parseErrorMessage(error)}`);
    } finally {
        if (btn) loading.hide(btn);
    }
}

async function rollbackRepairPoint(rollbackId, btn) {
    const id = String(rollbackId || '').trim();
    if (!id) return;
    if (btn) loading.show(btn, '回滚中...');
    try {
        const data = await api.post(`/selfcheck/repair-center/rollbacks/${encodeURIComponent(id)}/rollback`, {});
        toast.success(`回滚完成：恢复账号 ${Number(data?.result?.restored_accounts || 0)} 条`);
        await loadRuns();
        await loadRepairRollbacks();
    } catch (error) {
        toast.error(`回滚失败: ${parseErrorMessage(error)}`);
    } finally {
        if (btn) loading.hide(btn);
    }
}

function bindEvents() {
    const runBtn = document.getElementById('selfcheck-run-btn');
    const refreshBtn = document.getElementById('selfcheck-refresh-btn');
    const saveScheduleBtn = document.getElementById('selfcheck-save-schedule-btn');
    const runNowBtn = document.getElementById('selfcheck-run-now-btn');
    const runsBody = document.getElementById('selfcheck-runs-body');
    const checkList = document.getElementById('selfcheck-check-list');
    const repairPreviewBtn = document.getElementById('repair-center-preview-btn');
    const repairExecuteBtn = document.getElementById('repair-center-execute-btn');
    const repairRollbackRefreshBtn = document.getElementById('repair-center-refresh-rollbacks-btn');
    const rollbackBox = document.getElementById('repair-center-rollbacks');

    runBtn?.addEventListener('click', startRun);
    refreshBtn?.addEventListener('click', async () => {
        try {
            await loadRuns();
            await loadScheduleAndRuntime();
            toast.success('刷新完成');
        } catch (error) {
            toast.error(`刷新失败: ${parseErrorMessage(error)}`);
        }
    });
    saveScheduleBtn?.addEventListener('click', saveSchedule);
    runNowBtn?.addEventListener('click', runNow);
    repairPreviewBtn?.addEventListener('click', previewRepairCenter);
    repairExecuteBtn?.addEventListener('click', executeRepairCenter);
    repairRollbackRefreshBtn?.addEventListener('click', loadRepairRollbacks);

    runsBody?.addEventListener('click', (event) => {
        const target = event.target.closest('tr[data-run-id]');
        if (!target) return;
        const runId = Number(target.dataset.runId || 0);
        if (!runId) return;
        loadRunDetail(runId).catch((error) => {
            toast.error(`加载运行详情失败: ${parseErrorMessage(error)}`);
        });
    });

    checkList?.addEventListener('click', (event) => {
        const button = event.target.closest('.selfcheck-repair-btn');
        if (!button) return;
        const runId = Number(button.dataset.runId || 0);
        const repairKey = String(button.dataset.repairKey || '');
        executeRepair(runId, repairKey, button);
    });

    rollbackBox?.addEventListener('click', (event) => {
        const button = event.target.closest('.repair-center-rollback-btn');
        if (!button) return;
        const rollbackId = String(button.dataset.rollbackId || '');
        rollbackRepairPoint(rollbackId, button);
    });
}

function startPolling() {
    if (selfcheckState.pollTimer) {
        clearInterval(selfcheckState.pollTimer);
    }
    selfcheckState.pollTimer = setInterval(async () => {
        try {
            await loadScheduleAndRuntime();
            const running = selfcheckState.runs.some((run) => ['running', 'pending'].includes(String(run.status || '').toLowerCase()));
            if (running) {
                await loadRuns();
            }
        } catch (error) {
            // 轮询静默失败，不打断页面交互
        }
    }, 5000);
}

async function initSelfcheckPage() {
    bindEvents();
    try {
        await loadRepairCatalog();
        await loadScheduleAndRuntime();
        await loadRuns();
        await loadRepairRollbacks();
    } catch (error) {
        toast.error(`初始化失败: ${parseErrorMessage(error)}`);
    }
    startPolling();
}

document.addEventListener('DOMContentLoaded', initSelfcheckPage);
