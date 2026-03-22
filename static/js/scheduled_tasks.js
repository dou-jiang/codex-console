const scheduledTaskElements = {
    plansBody: document.getElementById('scheduled-plans-table-body'),
    refreshBtn: document.getElementById('refresh-plans-btn'),
    createPlanBtn: document.getElementById('create-plan-btn'),
    planFormModal: document.getElementById('plan-form-modal'),
    planFormTitle: document.getElementById('plan-form-title'),
    planForm: document.getElementById('plan-form'),
    planIdInput: document.getElementById('plan-id'),
    planFormSubmitBtn: document.getElementById('plan-form-submit-btn'),
    planNameInput: document.getElementById('plan-name'),
    planTaskTypeInput: document.getElementById('plan-task-type'),
    planCpaServiceSelect: document.getElementById('plan-cpa-service-select'),
    planCpaServiceIdInput: document.getElementById('plan-cpa-service-id'),
    planTriggerTypeInput: document.getElementById('plan-trigger-type'),
    planCronGroup: document.getElementById('plan-cron-group'),
    planCronExpressionInput: document.getElementById('plan-cron-expression'),
    planIntervalGroup: document.getElementById('plan-interval-group'),
    planIntervalValueInput: document.getElementById('plan-interval-value'),
    planIntervalUnitInput: document.getElementById('plan-interval-unit'),
    planConfigJsonInput: document.getElementById('plan-config-json'),
    planEnabledInput: document.getElementById('plan-enabled'),
    planModal: document.getElementById('plan-modal'),
    planModalBody: document.getElementById('plan-modal-body'),
    runLogModal: document.getElementById('run-log-modal'),
    runLogModalBody: document.getElementById('run-log-modal-body'),
};

let scheduledPlansCache = [];
let currentRunList = [];
let cpaServicesCache = [];
let submittingPlanForm = false;

function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(text);
    return div.innerHTML;
}

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

function getDefaultConfigJson(taskType) {
    if (taskType === 'cpa_refill') {
        return JSON.stringify({ max_consecutive_failures: 10 }, null, 2);
    }
    return '{}';
}

async function withButtonBusy(button, action) {
    if (!button) {
        return action();
    }
    if (button.dataset.busy === 'true') {
        return;
    }

    button.dataset.busy = 'true';
    button.setAttribute('data-busy', 'true');
    button.disabled = true;

    try {
        return await action();
    } finally {
        delete button.dataset.busy;
        button.removeAttribute('data-busy');
        button.disabled = false;
    }
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
            const toggleLabel = plan.enabled ? '禁用' : '启用';
            const shouldEnable = plan.enabled ? 'false' : 'true';
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
                            <button
                                class="btn btn-secondary btn-sm"
                                data-action="detail"
                                data-plan-id="${plan.id}"
                                onclick="handlePlanAction(this)"
                            >详情</button>
                            <button
                                class="btn btn-secondary btn-sm"
                                data-action="logs"
                                data-plan-id="${plan.id}"
                                onclick="handlePlanAction(this)"
                            >记录</button>
                            <button
                                class="btn btn-secondary btn-sm"
                                data-action="edit"
                                data-plan-id="${plan.id}"
                                onclick="handlePlanAction(this)"
                            >编辑</button>
                            <button
                                class="btn btn-secondary btn-sm"
                                data-action="toggle"
                                data-plan-id="${plan.id}"
                                data-should-enable="${shouldEnable}"
                                onclick="handlePlanAction(this)"
                            >${toggleLabel}</button>
                            <button
                                class="btn btn-primary btn-sm"
                                data-action="run-now"
                                data-plan-id="${plan.id}"
                                onclick="handlePlanAction(this)"
                            >立即执行</button>
                        </div>
                    </td>
                </tr>
            `;
        })
        .join('');
}

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

async function loadCpaServices(showErrorToast = false) {
    try {
        const services = await api.get('/cpa-services');
        cpaServicesCache = Array.isArray(services) ? services : [];
    } catch (error) {
        cpaServicesCache = [];
        if (showErrorToast) {
            toast.error('CPA 服务列表加载失败，请手动输入服务 ID');
        }
    }
}

function renderCpaServiceOptions(selectedId = null) {
    const select = scheduledTaskElements.planCpaServiceSelect;
    if (!select) return;

    const options = ['<option value="">请选择</option>'];
    options.push(
        ...cpaServicesCache.map((service) => {
            const label = `${service.id} - ${service.name}${service.enabled ? '' : '（已禁用）'}`;
            return `<option value="${service.id}">${escapeHtml(label)}</option>`;
        }),
    );

    if (selectedId && !cpaServicesCache.some((service) => Number(service.id) === Number(selectedId))) {
        options.push(`<option value="${selectedId}">${selectedId} - 当前计划绑定服务</option>`);
    }

    select.innerHTML = options.join('');

    if (selectedId) {
        select.value = String(selectedId);
    } else if (cpaServicesCache.length > 0) {
        select.value = String(cpaServicesCache[0].id);
    }
}

function updateTriggerInputs() {
    const triggerType = scheduledTaskElements.planTriggerTypeInput?.value;
    const isCron = triggerType === 'cron';

    if (scheduledTaskElements.planCronGroup) {
        scheduledTaskElements.planCronGroup.style.display = isCron ? '' : 'none';
    }
    if (scheduledTaskElements.planIntervalGroup) {
        scheduledTaskElements.planIntervalGroup.style.display = isCron ? 'none' : '';
    }

    if (scheduledTaskElements.planCronExpressionInput) {
        scheduledTaskElements.planCronExpressionInput.required = isCron;
    }
    if (scheduledTaskElements.planIntervalValueInput) {
        scheduledTaskElements.planIntervalValueInput.required = !isCron;
    }
    if (scheduledTaskElements.planIntervalUnitInput) {
        scheduledTaskElements.planIntervalUnitInput.required = !isCron;
    }
}

function maybeSetDefaultConfigForTaskType() {
    if (!scheduledTaskElements.planConfigJsonInput || !scheduledTaskElements.planTaskTypeInput) {
        return;
    }

    const current = (scheduledTaskElements.planConfigJsonInput.value || '').trim();
    if (current !== '' && current !== '{}' && current !== '{\n}') {
        return;
    }

    const taskType = scheduledTaskElements.planTaskTypeInput.value;
    scheduledTaskElements.planConfigJsonInput.value = getDefaultConfigJson(taskType);
}

async function openCreatePlanModal() {
    if (!scheduledTaskElements.planFormModal || !scheduledTaskElements.planForm) return;

    scheduledTaskElements.planForm.reset();
    scheduledTaskElements.planIdInput.value = '';
    scheduledTaskElements.planFormTitle.textContent = '新建计划';
    scheduledTaskElements.planFormSubmitBtn.textContent = '创建计划';
    scheduledTaskElements.planTaskTypeInput.value = 'cpa_cleanup';
    scheduledTaskElements.planTriggerTypeInput.value = 'interval';
    scheduledTaskElements.planIntervalValueInput.value = 60;
    scheduledTaskElements.planIntervalUnitInput.value = 'minutes';
    scheduledTaskElements.planConfigJsonInput.value = getDefaultConfigJson('cpa_cleanup');
    scheduledTaskElements.planEnabledInput.checked = true;

    await loadCpaServices(true);
    renderCpaServiceOptions();
    scheduledTaskElements.planCpaServiceIdInput.value =
        scheduledTaskElements.planCpaServiceSelect.value || '';
    updateTriggerInputs();

    scheduledTaskElements.planFormModal.classList.add('active');
}

async function openEditPlanModal(planId) {
    const plan = scheduledPlansCache.find((item) => Number(item.id) === Number(planId));
    if (!plan) {
        toast.warning('未找到计划');
        return;
    }

    scheduledTaskElements.planIdInput.value = String(plan.id);
    scheduledTaskElements.planFormTitle.textContent = `编辑计划 #${plan.id}`;
    scheduledTaskElements.planFormSubmitBtn.textContent = '保存修改';
    scheduledTaskElements.planNameInput.value = plan.name || '';
    scheduledTaskElements.planTaskTypeInput.value = plan.task_type || 'cpa_cleanup';
    scheduledTaskElements.planTriggerTypeInput.value = plan.trigger_type || 'interval';
    scheduledTaskElements.planCronExpressionInput.value = plan.cron_expression || '';
    scheduledTaskElements.planIntervalValueInput.value = plan.interval_value || 60;
    scheduledTaskElements.planIntervalUnitInput.value = plan.interval_unit || 'minutes';
    scheduledTaskElements.planEnabledInput.checked = Boolean(plan.enabled);

    try {
        scheduledTaskElements.planConfigJsonInput.value = JSON.stringify(plan.config || {}, null, 2);
    } catch (error) {
        scheduledTaskElements.planConfigJsonInput.value = '{}';
    }

    await loadCpaServices(true);
    renderCpaServiceOptions(plan.cpa_service_id);
    scheduledTaskElements.planCpaServiceIdInput.value = plan.cpa_service_id || '';
    updateTriggerInputs();

    scheduledTaskElements.planFormModal.classList.add('active');
}

function buildPlanPayloadFromForm() {
    const name = (scheduledTaskElements.planNameInput?.value || '').trim();
    if (!name) {
        throw new Error('计划名称不能为空');
    }

    const taskType = scheduledTaskElements.planTaskTypeInput?.value;
    const triggerType = scheduledTaskElements.planTriggerTypeInput?.value;
    const rawConfig = (scheduledTaskElements.planConfigJsonInput?.value || '').trim() || '{}';

    const cpaServiceRaw =
        (scheduledTaskElements.planCpaServiceIdInput?.value || '').trim() ||
        (scheduledTaskElements.planCpaServiceSelect?.value || '').trim();
    const cpaServiceId = Number.parseInt(cpaServiceRaw, 10);

    if (!Number.isInteger(cpaServiceId) || cpaServiceId <= 0) {
        throw new Error('CPA 服务 ID 无效');
    }

    let config;
    try {
        config = JSON.parse(rawConfig);
    } catch (error) {
        throw new Error('配置 JSON 解析失败');
    }

    if (!config || typeof config !== 'object' || Array.isArray(config)) {
        throw new Error('配置 JSON 必须是对象');
    }

    const payload = {
        name,
        task_type: taskType,
        cpa_service_id: cpaServiceId,
        trigger_type: triggerType,
        config,
        enabled: Boolean(scheduledTaskElements.planEnabledInput?.checked),
    };

    if (triggerType === 'cron') {
        const cronExpression = (scheduledTaskElements.planCronExpressionInput?.value || '').trim();
        if (!cronExpression) {
            throw new Error('Cron 表达式不能为空');
        }
        payload.cron_expression = cronExpression;
        payload.interval_value = null;
        payload.interval_unit = null;
    } else {
        const intervalValue = Number.parseInt(scheduledTaskElements.planIntervalValueInput?.value || '', 10);
        const intervalUnit = scheduledTaskElements.planIntervalUnitInput?.value;
        if (!Number.isInteger(intervalValue) || intervalValue <= 0) {
            throw new Error('间隔值必须为正整数');
        }
        payload.interval_value = intervalValue;
        payload.interval_unit = intervalUnit;
        payload.cron_expression = null;
    }

    return payload;
}

async function submitPlanForm(event) {
    event.preventDefault();

    if (submittingPlanForm) return;

    let payload;
    try {
        payload = buildPlanPayloadFromForm();
    } catch (error) {
        toast.warning(error.message || '表单校验失败');
        return;
    }

    const planId = Number.parseInt(scheduledTaskElements.planIdInput?.value || '', 10);
    const isEdit = Number.isInteger(planId) && planId > 0;
    const submitButton = event.submitter || scheduledTaskElements.planFormSubmitBtn;

    await withButtonBusy(submitButton, async () => {
        submittingPlanForm = true;

        try {
            if (isEdit) {
                await api.put(`/scheduled-plans/${planId}`, payload);
                toast.success('计划已更新');
            } else {
                await api.post('/scheduled-plans', payload);
                toast.success('计划已创建');
            }
            closeModal('plan-form-modal');
            await loadPlans();
        } catch (error) {
            toast.error(`${isEdit ? '更新' : '创建'}失败: ${error.message}`);
        } finally {
            submittingPlanForm = false;
        }
    });
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

async function togglePlanEnabled(planId, shouldEnable) {
    const endpoint = shouldEnable
        ? `/scheduled-plans/${planId}/enable`
        : `/scheduled-plans/${planId}/disable`;

    try {
        await api.post(endpoint, {});
        toast.success(shouldEnable ? '计划已启用' : '计划已禁用');
        await loadPlans();
    } catch (error) {
        toast.error(`${shouldEnable ? '启用' : '禁用'}失败: ${error.message}`);
    }
}

async function runPlanNow(planId) {
    try {
        await api.post(`/scheduled-plans/${planId}/run`, {});
        toast.success('已触发执行');
        await loadPlans();
    } catch (error) {
        toast.error(`触发失败: ${error.message}`);
    }
}

async function openRunLogs(planId) {
    try {
        const data = await api.get(`/scheduled-plans/${planId}/runs`);
        currentRunList = Array.isArray(data.runs) ? data.runs : [];
        renderRunLogModal(currentRunList);
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
                            <td>
                                <button
                                    class="btn btn-secondary btn-sm"
                                    data-action="view-run-log"
                                    data-run-id="${run.id}"
                                    onclick="handleRunLogAction(this)"
                                >查看</button>
                            </td>
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
                <button
                    class="btn btn-secondary btn-sm"
                    data-action="back-to-runs"
                    onclick="handleRunLogAction(this)"
                >返回记录</button>
            </div>
            <pre style="white-space:pre-wrap;word-break:break-word;background:var(--surface-hover);padding:12px;border-radius:8px;max-height:420px;overflow:auto;">${escapeHtml(data.logs || '暂无日志')}</pre>
        `;
    } catch (error) {
        toast.error(`加载日志失败: ${error.message}`);
    }
}

async function handlePlanAction(button) {
    const action = button?.dataset?.action;
    const planId = Number.parseInt(button?.dataset?.planId || '', 10);

    return withButtonBusy(button, async () => {
        switch (action) {
            case 'detail':
                showPlanDetail(planId);
                return;
            case 'logs':
                await openRunLogs(planId);
                return;
            case 'edit':
                await openEditPlanModal(planId);
                return;
            case 'toggle':
                await togglePlanEnabled(planId, button?.dataset?.shouldEnable === 'true');
                return;
            case 'run-now':
                await runPlanNow(planId);
                return;
            default:
                return;
        }
    });
}

async function handleRunLogAction(button) {
    const action = button?.dataset?.action;

    return withButtonBusy(button, async () => {
        if (action === 'view-run-log') {
            const runId = Number.parseInt(button?.dataset?.runId || '', 10);
            await viewRunLog(runId);
            return;
        }

        if (action === 'back-to-runs') {
            renderRunLogModal(currentRunList);
        }
    });
}

function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (!modal) return;
    modal.classList.remove('active');
}

document.addEventListener('DOMContentLoaded', () => {
    loadPlans();
    loadCpaServices();

    if (scheduledTaskElements.refreshBtn) {
        scheduledTaskElements.refreshBtn.addEventListener('click', (event) => {
            void withButtonBusy(event.currentTarget, () => loadPlans());
        });
    }

    if (scheduledTaskElements.createPlanBtn) {
        scheduledTaskElements.createPlanBtn.addEventListener('click', (event) => {
            void withButtonBusy(event.currentTarget, () => openCreatePlanModal());
        });
    }

    if (scheduledTaskElements.planForm) {
        scheduledTaskElements.planForm.addEventListener('submit', submitPlanForm);
    }

    if (scheduledTaskElements.planTriggerTypeInput) {
        scheduledTaskElements.planTriggerTypeInput.addEventListener('change', updateTriggerInputs);
    }

    if (scheduledTaskElements.planTaskTypeInput) {
        scheduledTaskElements.planTaskTypeInput.addEventListener('change', maybeSetDefaultConfigForTaskType);
    }

    if (scheduledTaskElements.planCpaServiceSelect) {
        scheduledTaskElements.planCpaServiceSelect.addEventListener('change', () => {
            if (!scheduledTaskElements.planCpaServiceIdInput) return;
            scheduledTaskElements.planCpaServiceIdInput.value = scheduledTaskElements.planCpaServiceSelect.value || '';
        });
    }

    document.querySelectorAll('[data-close-modal]').forEach((btn) => {
        btn.addEventListener('click', (event) => {
            void withButtonBusy(event.currentTarget, () => closeModal(btn.dataset.closeModal));
        });
    });

    [scheduledTaskElements.planFormModal, scheduledTaskElements.planModal, scheduledTaskElements.runLogModal].forEach((modal) => {
        if (!modal) return;
        modal.addEventListener('click', (event) => {
            if (event.target === modal) {
                modal.classList.remove('active');
            }
        });
    });
});

window.loadPlans = loadPlans;
window.openCreatePlanModal = openCreatePlanModal;
window.openEditPlanModal = openEditPlanModal;
window.submitPlanForm = submitPlanForm;
window.openRunLogs = openRunLogs;
window.runPlanNow = runPlanNow;
window.showPlanDetail = showPlanDetail;
window.togglePlanEnabled = togglePlanEnabled;
window.viewRunLog = viewRunLog;
window.handlePlanAction = handlePlanAction;
window.handleRunLogAction = handleRunLogAction;
