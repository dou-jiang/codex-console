const scheduledTaskElements = {
    plansBody: document.getElementById('scheduled-plans-table-body'),
    refreshBtn: document.getElementById('refresh-plans-btn'),
    planModal: document.getElementById('plan-modal'),
    planModalBody: document.getElementById('plan-modal-body'),
    runLogModal: document.getElementById('run-log-modal'),
    runLogModalBody: document.getElementById('run-log-modal-body'),
};

function getTaskTypeText(type) {
    const taskTypeMap = {
        cpa_cleanup: 'CPA 清理',
        cpa_refill: 'CPA 补量',
        account_refresh: '账号刷新',
    };
    return taskTypeMap[type] || type || '-';
}

function getTriggerText(plan) {
    if (plan.trigger_type === 'cron') {
        return `Cron: ${plan.cron_expression || '-'}`;
    }
    if (plan.trigger_type === 'interval') {
        return `间隔: ${plan.interval_value || '-'} ${plan.interval_unit || ''}`;
    }
    return plan.trigger_type || '-';
}

function getRunStatusText(status) {
    if (!status) return '-';
    const map = {
        running: '运行中',
        success: '成功',
        failed: '失败',
        skipped: '已跳过',
    };
    return map[status] || status;
}

function renderPlans(plans) {
    const rows = Array.isArray(plans) ? plans : [];

    if (rows.length === 0) {
        scheduledTaskElements.plansBody.innerHTML = `
            <tr>
                <td colspan="8">
                    <div class="empty-state">
                        <div class="empty-state-icon">📭</div>
                        <div class="empty-state-title">暂无定时计划</div>
                    </div>
                </td>
            </tr>
        `;
        return;
    }

    scheduledTaskElements.plansBody.innerHTML = rows
        .map((plan) => {
            const statusClass = plan.enabled ? 'active' : 'disabled';
            const statusText = plan.enabled ? '启用' : '禁用';
            return `
                <tr>
                    <td>${plan.id}</td>
                    <td>${escapeHtml(plan.name || '-')}</td>
                    <td>${getTaskTypeText(plan.task_type)}</td>
                    <td>${escapeHtml(getTriggerText(plan))}</td>
                    <td>${format.date(plan.next_run_at)}</td>
                    <td><span class="status-badge ${statusClass}">${statusText}</span></td>
                    <td>${format.date(plan.last_run_started_at)}</td>
                    <td>
                        <div style="display:flex;gap:4px;flex-wrap:wrap;">
                            <button class="btn btn-secondary btn-sm" onclick="showPlanDetail(${plan.id})">详情</button>
                            <button class="btn btn-secondary btn-sm" onclick="openRunLogs(${plan.id})">记录</button>
                            <button class="btn btn-primary btn-sm" onclick="runPlanNow(${plan.id})">立即执行</button>
                        </div>
                    </td>
                </tr>
            `;
        })
        .join('');
}

let scheduledPlansCache = [];

async function loadPlans() {
    try {
        const data = await api.get('/scheduled-plans');
        const plans = data.items || data.plans || data;
        scheduledPlansCache = Array.isArray(plans) ? plans : [];
        renderPlans(scheduledPlansCache);
    } catch (error) {
        scheduledTaskElements.plansBody.innerHTML = `
            <tr>
                <td colspan="8">
                    <div class="empty-state">
                        <div class="empty-state-icon">❌</div>
                        <div class="empty-state-title">加载失败</div>
                        <div class="empty-state-description">${escapeHtml(error.message || '请求失败')}</div>
                    </div>
                </td>
            </tr>
        `;
    }
}

function showPlanDetail(planId) {
    const plan = scheduledPlansCache.find((item) => item.id === planId);
    if (!plan) {
        toast.warning('未找到计划详情');
        return;
    }

    scheduledTaskElements.planModalBody.innerHTML = `
        <div class="info-grid">
            <div class="info-item">
                <span class="label">计划名称</span>
                <span class="value">${escapeHtml(plan.name || '-')}</span>
            </div>
            <div class="info-item">
                <span class="label">任务类型</span>
                <span class="value">${getTaskTypeText(plan.task_type)}</span>
            </div>
            <div class="info-item">
                <span class="label">触发方式</span>
                <span class="value">${escapeHtml(getTriggerText(plan))}</span>
            </div>
            <div class="info-item">
                <span class="label">主 CPA 服务 ID</span>
                <span class="value">${plan.cpa_service_id ?? '-'}</span>
            </div>
            <div class="info-item">
                <span class="label">下次执行</span>
                <span class="value">${format.date(plan.next_run_at)}</span>
            </div>
            <div class="info-item">
                <span class="label">最近状态</span>
                <span class="value">${getRunStatusText(plan.last_run_status)}</span>
            </div>
        </div>
    `;

    scheduledTaskElements.planModal.classList.add('active');
}

async function runPlanNow(planId) {
    try {
        await api.post(`/scheduled-plans/${planId}/run`, {});
        toast.success('已触发执行');
        loadPlans();
    } catch (error) {
        toast.error(`触发失败: ${error.message}`);
    }
}

async function openRunLogs(planId) {
    try {
        const data = await api.get(`/scheduled-plans/${planId}/runs`);
        renderRunLogModal(data.runs || []);
    } catch (error) {
        toast.error(`加载运行记录失败: ${error.message}`);
    }
}

function renderRunLogModal(runs) {
    if (!Array.isArray(runs) || runs.length === 0) {
        scheduledTaskElements.runLogModalBody.innerHTML = '<div style="color: var(--text-muted);">暂无运行记录</div>';
        scheduledTaskElements.runLogModal.classList.add('active');
        return;
    }

    scheduledTaskElements.runLogModalBody.innerHTML = `
        <div class="table-container">
            <table class="data-table">
                <thead>
                    <tr>
                        <th style="width: 90px;">Run ID</th>
                        <th style="width: 100px;">触发源</th>
                        <th style="width: 100px;">状态</th>
                        <th style="width: 180px;">开始时间</th>
                        <th style="width: 180px;">结束时间</th>
                        <th>摘要</th>
                        <th style="width: 90px;">日志</th>
                    </tr>
                </thead>
                <tbody>
                    ${runs
                        .map(
                            (run) => `
                        <tr>
                            <td>${run.id}</td>
                            <td>${escapeHtml(run.trigger_source || '-')}</td>
                            <td>${getRunStatusText(run.status)}</td>
                            <td>${format.date(run.started_at)}</td>
                            <td>${format.date(run.finished_at)}</td>
                            <td>${escapeHtml(JSON.stringify(run.summary || {}))}</td>
                            <td><button class="btn btn-secondary btn-sm" onclick="viewRunLog(${run.id})">查看</button></td>
                        </tr>
                    `,
                        )
                        .join('')}
                </tbody>
            </table>
        </div>
    `;

    scheduledTaskElements.runLogModal.classList.add('active');
}

async function viewRunLog(runId) {
    try {
        const data = await api.get(`/scheduled-plans/runs/${runId}/logs`);
        scheduledTaskElements.runLogModalBody.innerHTML = `
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                <strong>运行日志 #${runId}</strong>
                <button class="btn btn-secondary btn-sm" onclick="openRunLogs(${data.plan_id || 0})" style="display:none;">返回</button>
            </div>
            <pre style="white-space:pre-wrap;word-break:break-word;background:var(--surface-hover);padding:12px;border-radius:8px;max-height:420px;overflow:auto;">${escapeHtml(data.logs || '暂无日志')}</pre>
        `;
    } catch (error) {
        toast.error(`加载日志失败: ${error.message}`);
    }
}

function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) modal.classList.remove('active');
}

document.addEventListener('DOMContentLoaded', () => {
    loadPlans();

    if (scheduledTaskElements.refreshBtn) {
        scheduledTaskElements.refreshBtn.addEventListener('click', loadPlans);
    }

    document.querySelectorAll('[data-close-modal]').forEach((btn) => {
        btn.addEventListener('click', () => closeModal(btn.dataset.closeModal));
    });

    [scheduledTaskElements.planModal, scheduledTaskElements.runLogModal].forEach((modal) => {
        if (!modal) return;
        modal.addEventListener('click', (event) => {
            if (event.target === modal) {
                modal.classList.remove('active');
            }
        });
    });
});

window.loadPlans = loadPlans;
window.openRunLogs = openRunLogs;
window.runPlanNow = runPlanNow;
window.showPlanDetail = showPlanDetail;
window.viewRunLog = viewRunLog;
