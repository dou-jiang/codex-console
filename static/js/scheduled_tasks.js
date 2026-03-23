const scheduledTaskElements = {
    plansBody: document.getElementById('scheduled-plans-table-body'),
    runsCard: document.getElementById('scheduled-runs-card'),
    runsBody: document.getElementById('scheduled-runs-table-body'),
    refreshBtn: document.getElementById('refresh-plans-btn'),
    createPlanBtn: document.getElementById('create-plan-btn'),
    runFilterTaskTypeInput: document.getElementById('scheduled-run-filter-task-type'),
    runFilterStatusInput: document.getElementById('scheduled-run-filter-status'),
    runFilterStartedFromInput: document.getElementById('scheduled-run-filter-started-from'),
    runFilterStartedToInput: document.getElementById('scheduled-run-filter-started-to'),
    runFilterApplyBtn: document.getElementById('scheduled-run-filter-apply-btn'),
    runFilterResetBtn: document.getElementById('scheduled-run-filter-reset-btn'),
    runPaginationSummary: document.getElementById('scheduled-run-pagination-summary'),
    runPrevPageBtn: document.getElementById('scheduled-run-prev-page'),
    runNextPageBtn: document.getElementById('scheduled-run-next-page'),
    runPageJumpInput: document.getElementById('scheduled-run-page-jump-input'),
    runPageJumpBtn: document.getElementById('scheduled-run-page-jump-btn'),
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
    planConfigModeTableBtn: document.getElementById('plan-config-mode-table'),
    planConfigModeJsonBtn: document.getElementById('plan-config-mode-json'),
    planConfigAddEntryBtn: document.getElementById('plan-config-add-entry-btn'),
    planConfigEditorPanel: document.getElementById('plan-config-editor-panel'),
    planConfigEntriesBody: document.getElementById('plan-config-entries-body'),
    planConfigJsonPanel: document.getElementById('plan-config-json-panel'),
    planConfigJsonInput: document.getElementById('plan-config-json'),
    planEnabledInput: document.getElementById('plan-enabled'),
    planModal: document.getElementById('plan-modal'),
    planModalBody: document.getElementById('plan-modal-body'),
    runLogModal: document.getElementById('run-log-modal'),
    runLogStatusBar: document.getElementById('run-log-status-bar'),
    runLogRefreshBtn: document.getElementById('run-log-refresh-btn'),
    runLogAutoScrollInput: document.getElementById('run-log-auto-scroll'),
    runLogStopActions: document.getElementById('run-log-stop-actions'),
    runLogStopBtn: document.getElementById('run-log-stop-btn'),
    runLogModalBody: document.getElementById('run-log-modal-body'),
};

const CONFIG_EDITOR_MODE_TABLE = 'table';
const CONFIG_EDITOR_MODE_JSON = 'json';
const CONFIG_VALUE_TYPES = ['string', 'number', 'boolean', 'object', 'array'];
const CONFIG_EDITOR_COLUMN_LABELS = {
    key_description: '键说明',
    value_description: '值说明',
};
const TASK_CONFIG_SCHEMAS = {
    cpa_cleanup: [
        {
            key: 'max_probe_count',
            key_description: '单次最多探测多少个远端账号',
            value_type: 'number',
            default_value: 100,
            value_description: '用于限制运行时长，0 表示不限制',
            readonly_key: true,
        },
        {
            key: 'max_cleanup_count',
            key_description: '单次最多清理多少个失效账号',
            value_type: 'number',
            default_value: 10,
            value_description: '只限制删除数量，不等于探测数量',
            readonly_key: true,
        },
    ],
    cpa_refill: [
        {
            key: 'target_valid_count',
            key_description: '目标有效账号数量',
            value_type: 'number',
            default_value: 50,
            value_description: '达到该数量后停止补号',
            readonly_key: true,
        },
        {
            key: 'max_refill_count',
            key_description: '单次最多补号数量',
            value_type: 'number',
            default_value: 10,
            value_description: '本轮运行最多注册并上传多少个账号',
            readonly_key: true,
        },
        {
            key: 'max_consecutive_failures',
            key_description: '连续失败阈值',
            value_type: 'number',
            default_value: 10,
            value_description: '达到后自动禁用当前定时任务',
            readonly_key: true,
        },
        {
            key: 'email_service_type',
            key_description: '注册使用的邮箱服务类型',
            value_type: 'string',
            default_value: 'tempmail',
            value_description: '如 tempmail / outlook / moe_mail',
            readonly_key: true,
        },
        {
            key: 'email_service_id',
            key_description: '邮箱服务 ID',
            value_type: 'number',
            default_value: 0,
            value_description: '0 或空表示不绑定具体服务',
            readonly_key: true,
        },
        {
            key: 'email_service_config',
            key_description: '邮箱服务附加配置',
            value_type: 'object',
            default_value: {},
            value_description: '复杂配置请使用 JSON 对象',
            readonly_key: true,
        },
        {
            key: 'proxy',
            key_description: '注册使用的代理地址',
            value_type: 'string',
            default_value: '',
            value_description: '留空则按默认逻辑处理',
            readonly_key: true,
        },
    ],
    account_refresh: [
        {
            key: 'refresh_after_days',
            key_description: '注册或上次刷新后多少天执行刷新',
            value_type: 'number',
            default_value: 7,
            value_description: '达到天数阈值后才会进入刷新队列',
            readonly_key: true,
        },
        {
            key: 'max_refresh_count',
            key_description: '单次最多刷新数量',
            value_type: 'number',
            default_value: 100,
            value_description: '本轮最多处理多少个账号',
            readonly_key: true,
        },
        {
            key: 'proxy',
            key_description: '刷新和订阅检测使用的代理',
            value_type: 'string',
            default_value: '',
            value_description: '留空则按默认逻辑处理',
            readonly_key: true,
        },
    ],
};

let scheduledPlansCache = [];
let scheduledRunsCache = [];
let currentRunList = [];
let cpaServicesCache = [];
let submittingPlanForm = false;
let currentConfigEntries = [];
let currentConfigEditorMode = CONFIG_EDITOR_MODE_TABLE;
let currentConfigTaskType = 'cpa_cleanup';
let scheduledRunFilters = {
    taskType: '',
    status: '',
    startedFrom: '',
    startedTo: '',
    planId: null,
    page: 1,
    pageSize: 20,
};
let scheduledRunPaginationMeta = {
    total: 0,
    page: 1,
    pageSize: 20,
};
let activeScheduledRunId = null;
let activeScheduledRunDetail = null;
let currentScheduledRunLogOffset = 0;
let scheduledRunLogPollingTimer = null;
let scheduledRunLogPollingInFlight = false;
let scheduledRunLogLoadToken = 0;

function isScheduledRunLogRequestActive(runId, token) {
    return token === scheduledRunLogLoadToken && activeScheduledRunId === Number(runId);
}

function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(text);
    return div.innerHTML;
}

function cloneValue(value) {
    if (Array.isArray(value) || (value && typeof value === 'object')) {
        try {
            return JSON.parse(JSON.stringify(value));
        } catch (error) {
            return value;
        }
    }
    return value;
}

function isPlainObject(value) {
    return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function normalizeValueType(valueType) {
    return CONFIG_VALUE_TYPES.includes(valueType) ? valueType : 'string';
}

function inferValueType(value) {
    if (Array.isArray(value)) return 'array';
    if (isPlainObject(value)) return 'object';
    if (typeof value === 'boolean') return 'boolean';
    if (typeof value === 'number' && Number.isFinite(value)) return 'number';
    return 'string';
}

function getDefaultValueForType(valueType) {
    switch (normalizeValueType(valueType)) {
        case 'number':
            return 0;
        case 'boolean':
            return false;
        case 'object':
            return {};
        case 'array':
            return [];
        default:
            return '';
    }
}

function serializeEditorValue(value, valueType) {
    const normalizedType = normalizeValueType(valueType || inferValueType(value));
    const safeValue = value === undefined ? getDefaultValueForType(normalizedType) : value;

    if (normalizedType === 'object' || normalizedType === 'array') {
        return JSON.stringify(safeValue, null, 2);
    }
    if (normalizedType === 'boolean') {
        return safeValue ? 'true' : 'false';
    }
    if (normalizedType === 'number') {
        return safeValue === null || safeValue === undefined ? '' : String(safeValue);
    }
    return safeValue === null || safeValue === undefined ? '' : String(safeValue);
}

function parseEditorValue(rawValue, valueType) {
    const normalizedType = normalizeValueType(valueType);
    const text = rawValue === null || rawValue === undefined ? '' : String(rawValue);

    if (normalizedType === 'string') {
        return text;
    }
    if (normalizedType === 'number') {
        if (text.trim() === '') {
            throw new Error('数字类型的值不能为空');
        }
        const parsed = Number(text);
        if (!Number.isFinite(parsed)) {
            throw new Error('数字类型的值无效');
        }
        return parsed;
    }
    if (normalizedType === 'boolean') {
        if (text === 'true') return true;
        if (text === 'false') return false;
        throw new Error('布尔类型的值必须为 true 或 false');
    }

    let parsed;
    try {
        parsed = JSON.parse(text || (normalizedType === 'array' ? '[]' : '{}'));
    } catch (error) {
        throw new Error(`${normalizedType === 'array' ? '数组' : '对象'}类型的值必须是合法 JSON`);
    }

    if (normalizedType === 'array' && !Array.isArray(parsed)) {
        throw new Error('数组类型的值必须是 JSON 数组');
    }
    if (normalizedType === 'object' && !isPlainObject(parsed)) {
        throw new Error('对象类型的值必须是 JSON 对象');
    }
    return parsed;
}

function convertEditorRawValue(rawValue, fromType, toType) {
    const targetType = normalizeValueType(toType);
    try {
        const parsed = parseEditorValue(rawValue, fromType);
        return serializeEditorValue(parsed, targetType);
    } catch (error) {
        return serializeEditorValue(getDefaultValueForType(targetType), targetType);
    }
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
        stopping: '停止中',
        success: '成功',
        failed: '失败',
        skipped: '已跳过',
        cancelled: '已取消',
    };
    return map[status] || status;
}

function getScheduledRunUiStatus(run) {
    if (!run) return '';
    if (run.status === 'running' && run.stop_requested_at) {
        return 'stopping';
    }
    return run.status || '';
}

function getTaskConfigSchema(taskType) {
    return Array.isArray(TASK_CONFIG_SCHEMAS[taskType])
        ? TASK_CONFIG_SCHEMAS[taskType].map((item) => ({ ...item, default_value: cloneValue(item.default_value) }))
        : [];
}

function getDefaultConfigMetaEntry(schemaEntry) {
    return {
        key_description: schemaEntry.key_description || '',
        value_description: schemaEntry.value_description || '',
        value_type: normalizeValueType(schemaEntry.value_type),
    };
}

function createConfigEntry({
    key = '',
    keyDescription = '',
    rawValue = '',
    valueDescription = '',
    valueType = 'string',
    builtin = false,
    readonlyKey = false,
} = {}) {
    return {
        key,
        keyDescription,
        rawValue,
        valueDescription,
        valueType: normalizeValueType(valueType),
        builtin: Boolean(builtin),
        readonlyKey: Boolean(readonlyKey),
    };
}

function buildConfigMetaFromEntries(entries) {
    const configMeta = {};
    (Array.isArray(entries) ? entries : []).forEach((entry) => {
        const key = String(entry?.key || '').trim();
        if (!key) return;
        configMeta[key] = {
            key_description: String(entry.keyDescription || ''),
            value_description: String(entry.valueDescription || ''),
            value_type: normalizeValueType(entry.valueType),
        };
    });
    return configMeta;
}

function buildConfigEntriesFromConfig(taskType, config = {}, configMeta = {}) {
    const safeConfig = isPlainObject(config) ? config : {};
    const safeMeta = isPlainObject(configMeta) ? configMeta : {};
    const schema = getTaskConfigSchema(taskType);
    const schemaKeys = new Set(schema.map((item) => item.key));
    const entries = [];

    schema.forEach((schemaEntry) => {
        const metaEntry = isPlainObject(safeMeta[schemaEntry.key]) ? safeMeta[schemaEntry.key] : {};
        const valueType = normalizeValueType(metaEntry.value_type || schemaEntry.value_type);
        const hasConfigValue = Object.prototype.hasOwnProperty.call(safeConfig, schemaEntry.key);
        const value = hasConfigValue ? safeConfig[schemaEntry.key] : cloneValue(schemaEntry.default_value);
        entries.push(
            createConfigEntry({
                key: schemaEntry.key,
                keyDescription: metaEntry.key_description ?? schemaEntry.key_description ?? '',
                rawValue: serializeEditorValue(value, valueType),
                valueDescription: metaEntry.value_description ?? schemaEntry.value_description ?? '',
                valueType,
                builtin: true,
                readonlyKey: schemaEntry.readonly_key !== false,
            }),
        );
    });

    Object.entries(safeConfig).forEach(([key, value]) => {
        if (schemaKeys.has(key)) return;
        const metaEntry = isPlainObject(safeMeta[key]) ? safeMeta[key] : {};
        const valueType = normalizeValueType(metaEntry.value_type || inferValueType(value));
        entries.push(
            createConfigEntry({
                key,
                keyDescription: metaEntry.key_description || '',
                rawValue: serializeEditorValue(value, valueType),
                valueDescription: metaEntry.value_description || '',
                valueType,
                builtin: false,
                readonlyKey: false,
            }),
        );
    });

    Object.entries(safeMeta).forEach(([key, metaEntry]) => {
        if (schemaKeys.has(key) || Object.prototype.hasOwnProperty.call(safeConfig, key) || !isPlainObject(metaEntry)) {
            return;
        }
        entries.push(
            createConfigEntry({
                key,
                keyDescription: metaEntry.key_description || '',
                rawValue: serializeEditorValue(getDefaultValueForType(metaEntry.value_type), metaEntry.value_type),
                valueDescription: metaEntry.value_description || '',
                valueType: metaEntry.value_type || 'string',
                builtin: false,
                readonlyKey: false,
            }),
        );
    });

    return entries;
}

function buildConfigPayloadFromEntries(entries) {
    const config = {};
    const configMeta = {};
    const usedKeys = new Set();

    (Array.isArray(entries) ? entries : []).forEach((entry, index) => {
        const key = String(entry?.key || '').trim();
        if (!key) {
            throw new Error(`第 ${index + 1} 行的键不能为空`);
        }
        if (usedKeys.has(key)) {
            throw new Error(`配置键重复: ${key}`);
        }
        usedKeys.add(key);
        config[key] = parseEditorValue(entry.rawValue, entry.valueType);
        configMeta[key] = {
            key_description: String(entry.keyDescription || ''),
            value_description: String(entry.valueDescription || ''),
            value_type: normalizeValueType(entry.valueType),
        };
    });

    return { config, config_meta: configMeta };
}

function extractCustomConfigPayload(taskType, config = {}, configMeta = {}) {
    const schemaKeys = new Set(getTaskConfigSchema(taskType).map((item) => item.key));
    const customConfig = {};
    const customConfigMeta = {};

    Object.entries(isPlainObject(config) ? config : {}).forEach(([key, value]) => {
        if (schemaKeys.has(key)) return;
        customConfig[key] = cloneValue(value);
    });

    Object.entries(isPlainObject(configMeta) ? configMeta : {}).forEach(([key, value]) => {
        if (schemaKeys.has(key)) return;
        customConfigMeta[key] = cloneValue(value);
    });

    return { config: customConfig, config_meta: customConfigMeta };
}

function getDefaultConfigEntries(taskType) {
    return buildConfigEntriesFromConfig(taskType, {}, {});
}

function getDefaultConfigJson(taskType) {
    const payload = buildConfigPayloadFromEntries(getDefaultConfigEntries(taskType));
    return JSON.stringify(payload.config, null, 2);
}

function renderConfigValueInput(entry, index) {
    const safeValue = escapeHtml(entry.rawValue || '');
    const commonAttrs = `data-config-index="${index}" data-config-field="rawValue" style="width:100%;"`;

    switch (normalizeValueType(entry.valueType)) {
        case 'number':
            return `<input type="number" ${commonAttrs} value="${safeValue}" onchange="handleConfigEntryInput(this)">`;
        case 'boolean':
            return `
                <select ${commonAttrs} onchange="handleConfigEntryInput(this)">
                    <option value="true" ${entry.rawValue === 'true' ? 'selected' : ''}>true</option>
                    <option value="false" ${entry.rawValue === 'false' ? 'selected' : ''}>false</option>
                </select>
            `;
        case 'object':
        case 'array':
            return `<textarea rows="4" ${commonAttrs} onchange="handleConfigEntryInput(this)">${safeValue}</textarea>`;
        default:
            return `<input type="text" ${commonAttrs} value="${safeValue}" onchange="handleConfigEntryInput(this)">`;
    }
}

function renderConfigEntries() {
    const tbody = scheduledTaskElements.planConfigEntriesBody;
    if (!tbody) return;

    if (!Array.isArray(currentConfigEntries) || currentConfigEntries.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="6">
                    <div class="empty-state">
                        <div class="empty-state-icon">🧩</div>
                        <div class="empty-state-title">暂无配置项</div>
                    </div>
                </td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = currentConfigEntries
        .map((entry, index) => {
            const typeOptions = CONFIG_VALUE_TYPES
                .map((valueType) => `<option value="${valueType}" ${entry.valueType === valueType ? 'selected' : ''}>${valueType}</option>`)
                .join('');
            return `
                <tr>
                    <td>
                        <input
                            type="text"
                            data-config-index="${index}"
                            data-config-field="key"
                            value="${escapeHtml(entry.key || '')}"
                            ${entry.readonlyKey ? 'readonly' : ''}
                            onchange="handleConfigEntryInput(this)"
                            style="width:100%;"
                        >
                    </td>
                    <td>
                        <input
                            type="text"
                            data-config-index="${index}"
                            data-config-field="keyDescription"
                            value="${escapeHtml(entry.keyDescription || '')}"
                            onchange="handleConfigEntryInput(this)"
                            style="width:100%;"
                        >
                    </td>
                    <td>${renderConfigValueInput(entry, index)}</td>
                    <td>
                        <input
                            type="text"
                            data-config-index="${index}"
                            data-config-field="valueDescription"
                            value="${escapeHtml(entry.valueDescription || '')}"
                            onchange="handleConfigEntryInput(this)"
                            style="width:100%;"
                        >
                    </td>
                    <td>
                        <select
                            data-config-index="${index}"
                            data-config-field="valueType"
                            onchange="handleConfigEntryInput(this)"
                            style="width:100%;"
                        >${typeOptions}</select>
                    </td>
                    <td>
                        ${entry.builtin
                            ? '<span style="color: var(--text-muted); font-size: 12px;">预置</span>'
                            : `
                                <button
                                    type="button"
                                    class="btn btn-secondary btn-sm"
                                    data-config-action="remove"
                                    data-config-index="${index}"
                                    onclick="handleConfigEntryAction(this)"
                                >删除</button>
                            `}
                    </td>
                </tr>
            `;
        })
        .join('');
}

function updateConfigEditorModeUi() {
    const isTableMode = currentConfigEditorMode === CONFIG_EDITOR_MODE_TABLE;

    if (scheduledTaskElements.planConfigEditorPanel) {
        scheduledTaskElements.planConfigEditorPanel.style.display = isTableMode ? '' : 'none';
    }
    if (scheduledTaskElements.planConfigJsonPanel) {
        scheduledTaskElements.planConfigJsonPanel.style.display = isTableMode ? 'none' : '';
    }
    if (scheduledTaskElements.planConfigAddEntryBtn) {
        scheduledTaskElements.planConfigAddEntryBtn.style.display = isTableMode ? '' : 'none';
    }

    if (scheduledTaskElements.planConfigModeTableBtn) {
        scheduledTaskElements.planConfigModeTableBtn.className = `btn ${isTableMode ? 'btn-primary' : 'btn-secondary'} btn-sm`;
    }
    if (scheduledTaskElements.planConfigModeJsonBtn) {
        scheduledTaskElements.planConfigModeJsonBtn.className = `btn ${isTableMode ? 'btn-secondary' : 'btn-primary'} btn-sm`;
    }
}

function syncRawJsonFromConfigEntries() {
    const payload = buildConfigPayloadFromEntries(currentConfigEntries);
    if (scheduledTaskElements.planConfigJsonInput) {
        scheduledTaskElements.planConfigJsonInput.value = JSON.stringify(payload.config, null, 2);
    }
    return payload.config;
}

function syncConfigEntriesFromRawJson() {
    const rawText = (scheduledTaskElements.planConfigJsonInput?.value || '').trim() || '{}';
    let parsedConfig;
    try {
        parsedConfig = JSON.parse(rawText);
    } catch (error) {
        throw new Error('配置 JSON 解析失败');
    }

    if (!isPlainObject(parsedConfig)) {
        throw new Error('配置 JSON 必须是对象');
    }

    currentConfigEntries = buildConfigEntriesFromConfig(
        scheduledTaskElements.planTaskTypeInput?.value,
        parsedConfig,
        buildConfigMetaFromEntries(currentConfigEntries),
    );
    renderConfigEntries();
    return parsedConfig;
}

function prepareConfigEntriesForTaskTypeSwitch(nextTaskType, previousTaskType, entries, rawJsonText = null) {
    let sourceConfig;
    if (typeof rawJsonText === 'string') {
        sourceConfig = JSON.parse(rawJsonText.trim() || '{}');
        if (!isPlainObject(sourceConfig)) {
            throw new Error('配置 JSON 必须是对象');
        }
    } else {
        sourceConfig = buildConfigPayloadFromEntries(entries).config;
    }

    const sourceConfigMeta = buildConfigMetaFromEntries(entries);
    const customPayload = extractCustomConfigPayload(previousTaskType, sourceConfig, sourceConfigMeta);
    return buildConfigEntriesFromConfig(nextTaskType, customPayload.config, customPayload.config_meta);
}

function switchConfigEditorMode(mode) {
    if (mode === currentConfigEditorMode) {
        updateConfigEditorModeUi();
        return;
    }

    if (mode === CONFIG_EDITOR_MODE_JSON) {
        syncRawJsonFromConfigEntries();
        currentConfigEditorMode = CONFIG_EDITOR_MODE_JSON;
        updateConfigEditorModeUi();
        return;
    }

    syncConfigEntriesFromRawJson();
    currentConfigEditorMode = CONFIG_EDITOR_MODE_TABLE;
    updateConfigEditorModeUi();
}

function setConfigEditorState(taskType, config = {}, configMeta = {}) {
    currentConfigTaskType = taskType;
    currentConfigEntries = buildConfigEntriesFromConfig(taskType, config, configMeta);
    currentConfigEditorMode = CONFIG_EDITOR_MODE_TABLE;
    renderConfigEntries();
    syncRawJsonFromConfigEntries();
    updateConfigEditorModeUi();
}

function addCustomConfigEntry() {
    currentConfigEntries.push(
        createConfigEntry({
            key: '',
            keyDescription: '',
            rawValue: '',
            valueDescription: '',
            valueType: 'string',
            builtin: false,
            readonlyKey: false,
        }),
    );
    renderConfigEntries();
}

function handleConfigEntryInput(element) {
    const index = Number.parseInt(element?.dataset?.configIndex || '', 10);
    const field = element?.dataset?.configField;
    const entry = currentConfigEntries[index];
    if (!entry || !field) return;

    if (field === 'valueType') {
        const nextType = normalizeValueType(element.value);
        entry.rawValue = convertEditorRawValue(entry.rawValue, entry.valueType, nextType);
        entry.valueType = nextType;
    } else if (field === 'rawValue') {
        entry.rawValue = element.value;
    } else if (field === 'key') {
        entry.key = element.value;
    } else if (field === 'keyDescription') {
        entry.keyDescription = element.value;
    } else if (field === 'valueDescription') {
        entry.valueDescription = element.value;
    }

    renderConfigEntries();
}

function handleConfigEntryAction(button) {
    const action = button?.dataset?.configAction;
    const index = Number.parseInt(button?.dataset?.configIndex || '', 10);
    if (action !== 'remove' || !Number.isInteger(index) || index < 0 || index >= currentConfigEntries.length) {
        return;
    }
    currentConfigEntries.splice(index, 1);
    renderConfigEntries();
}

function maybeSetDefaultConfigForTaskType() {
    const taskType = scheduledTaskElements.planTaskTypeInput?.value;
    if (!taskType) return;

    try {
        const nextEntries = prepareConfigEntriesForTaskTypeSwitch(
            taskType,
            currentConfigTaskType || taskType,
            currentConfigEntries,
            currentConfigEditorMode === CONFIG_EDITOR_MODE_JSON
                ? (scheduledTaskElements.planConfigJsonInput?.value || '').trim() || '{}'
                : null,
        );
        currentConfigTaskType = taskType;
        currentConfigEntries = nextEntries;
        renderConfigEntries();
        syncRawJsonFromConfigEntries();
        updateConfigEditorModeUi();
    } catch (error) {
        setConfigEditorState(taskType, {}, {});
    }
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
                            <button class="btn btn-secondary btn-sm" data-action="detail" data-plan-id="${plan.id}" onclick="handlePlanAction(this)">详情</button>
                            <button class="btn btn-secondary btn-sm" data-action="logs" data-plan-id="${plan.id}" onclick="handlePlanAction(this)">记录</button>
                            <button class="btn btn-secondary btn-sm" data-action="edit" data-plan-id="${plan.id}" onclick="handlePlanAction(this)">编辑</button>
                            <button class="btn btn-secondary btn-sm" data-action="toggle" data-plan-id="${plan.id}" data-should-enable="${shouldEnable}" onclick="handlePlanAction(this)">${toggleLabel}</button>
                            <button class="btn btn-primary btn-sm" data-action="run-now" data-plan-id="${plan.id}" onclick="handlePlanAction(this)">立即执行</button>
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
    scheduledTaskElements.planEnabledInput.checked = true;
    setConfigEditorState('cpa_cleanup', {}, {});

    await loadCpaServices(true);
    renderCpaServiceOptions();
    scheduledTaskElements.planCpaServiceIdInput.value = scheduledTaskElements.planCpaServiceSelect.value || '';
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
    setConfigEditorState(plan.task_type || 'cpa_cleanup', plan.config || {}, plan.config_meta || {});

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
    const cpaServiceRaw =
        (scheduledTaskElements.planCpaServiceIdInput?.value || '').trim() ||
        (scheduledTaskElements.planCpaServiceSelect?.value || '').trim();
    const cpaServiceId = Number.parseInt(cpaServiceRaw, 10);

    if (!Number.isInteger(cpaServiceId) || cpaServiceId <= 0) {
        throw new Error('CPA 服务 ID 无效');
    }

    let payloadConfig;
    if (currentConfigEditorMode === CONFIG_EDITOR_MODE_JSON) {
        let rawConfig;
        try {
            rawConfig = JSON.parse((scheduledTaskElements.planConfigJsonInput?.value || '').trim() || '{}');
        } catch (error) {
            throw new Error('配置 JSON 解析失败');
        }
        if (!isPlainObject(rawConfig)) {
            throw new Error('配置 JSON 必须是对象');
        }
        const derivedEntries = buildConfigEntriesFromConfig(taskType, rawConfig, buildConfigMetaFromEntries(currentConfigEntries));
        payloadConfig = buildConfigPayloadFromEntries(derivedEntries);
    } else {
        payloadConfig = buildConfigPayloadFromEntries(currentConfigEntries);
    }

    const payload = {
        name,
        task_type: taskType,
        cpa_service_id: cpaServiceId,
        trigger_type: triggerType,
        config: payloadConfig.config,
        config_meta: payloadConfig.config_meta,
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
            <div class="info-item"><span class="label">计划名称</span><span class="value">${escapeHtml(plan.name || '-')}</span></div>
            <div class="info-item"><span class="label">任务类型</span><span class="value">${getTaskTypeText(plan.task_type)}</span></div>
            <div class="info-item"><span class="label">触发方式</span><span class="value">${escapeHtml(getTriggerText(plan))}</span></div>
            <div class="info-item"><span class="label">主 CPA 服务 ID</span><span class="value">${plan.cpa_service_id ?? '-'}</span></div>
            <div class="info-item"><span class="label">下次执行</span><span class="value">${format.date(plan.next_run_at)}</span></div>
            <div class="info-item"><span class="label">最近状态</span><span class="value">${getRunStatusText(plan.last_run_status)}</span></div>
        </div>
    `;

    scheduledTaskElements.planModal.classList.add('active');
}

async function togglePlanEnabled(planId, shouldEnable) {
    const endpoint = shouldEnable ? `/scheduled-plans/${planId}/enable` : `/scheduled-plans/${planId}/disable`;

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
        await loadScheduledRuns();
    } catch (error) {
        toast.error(`触发失败: ${error.message}`);
    }
}

function buildScheduledRunQuery(filters = scheduledRunFilters) {
    const params = new URLSearchParams();
    if (filters.taskType) params.set('task_type', filters.taskType);
    if (filters.status) params.set('status', filters.status);
    if (filters.planId) params.set('plan_id', String(filters.planId));
    if (filters.startedFrom) params.set('started_from', filters.startedFrom);
    if (filters.startedTo) params.set('started_to', filters.startedTo);
    params.set('page', String(filters.page || 1));
    params.set('page_size', String(filters.pageSize || 20));
    const query = params.toString();
    return query ? `?${query}` : '';
}

function syncScheduledRunFiltersToInputs() {
    if (scheduledTaskElements.runFilterTaskTypeInput) {
        scheduledTaskElements.runFilterTaskTypeInput.value = scheduledRunFilters.taskType || '';
    }
    if (scheduledTaskElements.runFilterStatusInput) {
        scheduledTaskElements.runFilterStatusInput.value = scheduledRunFilters.status || '';
    }
    if (scheduledTaskElements.runFilterStartedFromInput) {
        scheduledTaskElements.runFilterStartedFromInput.value = scheduledRunFilters.startedFrom || '';
    }
    if (scheduledTaskElements.runFilterStartedToInput) {
        scheduledTaskElements.runFilterStartedToInput.value = scheduledRunFilters.startedTo || '';
    }
}

function updateScheduledRunFiltersFromInputs() {
    scheduledRunFilters = {
        ...scheduledRunFilters,
        taskType: scheduledTaskElements.runFilterTaskTypeInput?.value || '',
        status: scheduledTaskElements.runFilterStatusInput?.value || '',
        startedFrom: scheduledTaskElements.runFilterStartedFromInput?.value || '',
        startedTo: scheduledTaskElements.runFilterStartedToInput?.value || '',
        page: 1,
    };
}

function getScheduledRunTotalPages(total = scheduledRunPaginationMeta.total, pageSize = scheduledRunFilters.pageSize || 20) {
    const safeTotal = Number.isFinite(Number(total)) ? Math.max(0, Number(total)) : 0;
    const safePageSize = Number.isFinite(Number(pageSize)) ? Math.max(1, Number(pageSize)) : 20;
    return Math.max(1, Math.ceil(safeTotal / safePageSize));
}

function updateScheduledRunPaginationControls() {
    const total = Number.isFinite(Number(scheduledRunPaginationMeta.total))
        ? Math.max(0, Number(scheduledRunPaginationMeta.total))
        : 0;
    const pageSize = Number.isFinite(Number(scheduledRunFilters.pageSize))
        ? Math.max(1, Number(scheduledRunFilters.pageSize))
        : 20;
    const totalPages = getScheduledRunTotalPages(total, pageSize);
    const currentPage = Math.min(Math.max(1, Number(scheduledRunFilters.page) || 1), totalPages);

    scheduledRunFilters = {
        ...scheduledRunFilters,
        page: currentPage,
        pageSize,
    };
    scheduledRunPaginationMeta = {
        ...scheduledRunPaginationMeta,
        total,
        page: currentPage,
        pageSize,
    };

    if (scheduledTaskElements.runPaginationSummary) {
        scheduledTaskElements.runPaginationSummary.textContent = `第 ${currentPage} / ${totalPages} 页 · 共 ${total} 条`;
    }
    if (scheduledTaskElements.runPageJumpInput) {
        scheduledTaskElements.runPageJumpInput.max = String(totalPages);
        if (document.activeElement !== scheduledTaskElements.runPageJumpInput) {
            scheduledTaskElements.runPageJumpInput.value = String(currentPage);
        }
    }
    if (scheduledTaskElements.runPrevPageBtn) {
        scheduledTaskElements.runPrevPageBtn.disabled = currentPage <= 1;
    }
    if (scheduledTaskElements.runNextPageBtn) {
        scheduledTaskElements.runNextPageBtn.disabled = currentPage >= totalPages;
    }
}

function resolveScheduledRunTargetPage(rawValue, {
    currentPage = scheduledRunFilters.page,
    totalPages = getScheduledRunTotalPages(),
} = {}) {
    const normalized = String(rawValue ?? '').trim();
    if (!/^\d+$/.test(normalized)) {
        return { valid: false, page: currentPage, clamped: false };
    }
    const requested = Number.parseInt(normalized, 10);
    const clamped = Math.min(Math.max(requested, 1), totalPages);
    return { valid: true, page: clamped, clamped: clamped !== requested };
}

async function jumpToScheduledRunPage(rawValue, { showWarnings = true } = {}) {
    const totalPages = getScheduledRunTotalPages();
    const result = resolveScheduledRunTargetPage(rawValue, {
        currentPage: scheduledRunFilters.page,
        totalPages,
    });
    if (!result.valid) {
        if (showWarnings) {
            toast.warning('请输入有效页码');
        }
        return;
    }

    if (result.clamped && showWarnings) {
        toast.warning(`页码已自动调整到 ${result.page}`);
    }
    if (result.page === scheduledRunFilters.page) {
        updateScheduledRunPaginationControls();
        return;
    }

    scheduledRunFilters = {
        ...scheduledRunFilters,
        page: result.page,
    };
    await loadScheduledRuns();
}

function toSummaryCount(value) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return 0;
    return Math.max(0, Math.floor(parsed));
}

function formatScheduledRunReason(run) {
    const rawReason = String(run?.error_message || '').trim();
    if (rawReason) {
        return rawReason.length > 32 ? `${rawReason.slice(0, 32)}…` : rawReason;
    }

    const uiStatus = getScheduledRunUiStatus(run);
    if (uiStatus === 'cancelled') return '用户停止';
    if (uiStatus === 'stopping') return '停止中';
    if (uiStatus === 'failed') return '执行失败';
    return '';
}

function buildScheduledRunSummaryBase(run) {
    const summary = isPlainObject(run?.summary) ? run.summary : {};
    const taskType = run?.task_type;

    if (taskType === 'cpa_refill') {
        return `补号 ${toSummaryCount(summary.uploaded_success)}`;
    }

    if (taskType === 'cpa_cleanup') {
        return `检测 ${toSummaryCount(summary.invalid_items_found)} · 清理 ${toSummaryCount(summary.remote_deleted)}`;
    }

    if (taskType === 'account_refresh') {
        return (
            `处理 ${toSummaryCount(summary.processed)} · ` +
            `刷新 ${toSummaryCount(summary.refreshed_success)} · ` +
            `上传 ${toSummaryCount(summary.uploaded_success)}`
        );
    }

    if (run?.summary && Object.keys(run.summary).length > 0) {
        try {
            return JSON.stringify(run.summary);
        } catch (error) {
            return String(run.summary);
        }
    }

    return '-';
}

function summarizeScheduledRun(run) {
    const baseSummary = buildScheduledRunSummaryBase(run);
    const uiStatus = getScheduledRunUiStatus(run);

    if (['failed', 'cancelled', 'stopping'].includes(uiStatus)) {
        const reason = formatScheduledRunReason(run);
        if (reason) {
            return `${baseSummary} · 原因：${reason}`;
        }
    }

    return baseSummary;
}

function updateScheduledRunCacheItem(runId, updater) {
    const targetId = Number(runId);
    scheduledRunsCache = scheduledRunsCache.map((run) => {
        if (Number(run.id) !== targetId) return run;
        return typeof updater === 'function' ? updater(run) : { ...run, ...updater };
    });
    currentRunList = scheduledRunsCache;
}

function focusScheduledRunsCard() {
    if (!scheduledTaskElements.runsCard) return;
    scheduledTaskElements.runsCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderScheduledRuns(runs) {
    const tbody = scheduledTaskElements.runsBody;
    if (!tbody) return;

    const rows = Array.isArray(runs) ? runs : [];
    if (rows.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="9">
                    <div class="empty-state">
                        <div class="empty-state-icon">📭</div>
                        <div class="empty-state-title">暂无运行记录</div>
                    </div>
                </td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = rows.map((run) => {
        const uiStatus = getScheduledRunUiStatus(run);
        const stopAction = run.can_stop
            ? `<button class="btn btn-secondary btn-sm" data-action="stop-run" data-run-id="${run.id}" onclick="handleRunLogAction(this)">停止</button>`
            : '';
        return `
            <tr class="${uiStatus === 'stopping' ? 'stopping' : ''}" data-run-id="${run.id}" data-run-state="${uiStatus}">
                <td>${run.id}</td>
                <td>${escapeHtml(run.plan_name || `计划 #${run.plan_id}`)}</td>
                <td>${getTaskTypeText(run.task_type)}</td>
                <td>${escapeHtml(run.trigger_source || '-')}</td>
                <td><span class="status-badge ${uiStatus}">${getRunStatusText(uiStatus)}</span></td>
                <td>${format.date(run.started_at)}</td>
                <td>${format.date(run.finished_at)}</td>
                <td class="scheduled-run-summary-cell"><div class="scheduled-run-summary">${escapeHtml(summarizeScheduledRun(run))}</div></td>
                <td>
                    <div class="table-actions">
                        <button class="btn btn-secondary btn-sm" data-action="view-run-detail" data-run-id="${run.id}" onclick="handleRunLogAction(this)">详情</button>
                        <button class="btn btn-secondary btn-sm" data-action="view-run-log" data-run-id="${run.id}" onclick="handleRunLogAction(this)">日志</button>
                        <button class="btn btn-secondary btn-sm" data-action="filter-plan-runs" data-plan-id="${run.plan_id}" onclick="handlePlanAction(this)">同计划</button>
                        ${stopAction}
                    </div>
                </td>
            </tr>
        `;
    }).join('');
}

async function loadScheduledRuns() {
    const tbody = scheduledTaskElements.runsBody;
    if (!tbody) return;

    try {
        const data = await api.get(`/scheduled-runs${buildScheduledRunQuery()}`);
        scheduledRunsCache = Array.isArray(data.items) ? data.items : [];
        currentRunList = scheduledRunsCache;
        const total = Number.isFinite(Number(data.total)) ? Math.max(0, Number(data.total)) : 0;
        const pageSize = Number.isFinite(Number(data.page_size))
            ? Math.max(1, Number(data.page_size))
            : Math.max(1, Number(scheduledRunFilters.pageSize) || 20);
        const totalPages = getScheduledRunTotalPages(total, pageSize);
        const pageFromResponse = Number.isFinite(Number(data.page)) ? Number(data.page) : Number(scheduledRunFilters.page) || 1;
        const nextPage = Math.min(Math.max(1, pageFromResponse), totalPages);
        scheduledRunFilters = {
            ...scheduledRunFilters,
            page: nextPage,
            pageSize,
        };
        scheduledRunPaginationMeta = {
            total,
            page: nextPage,
            pageSize,
        };
        renderScheduledRuns(scheduledRunsCache);
        updateScheduledRunPaginationControls();
    } catch (error) {
        scheduledRunPaginationMeta = {
            ...scheduledRunPaginationMeta,
            total: 0,
        };
        updateScheduledRunPaginationControls();
        tbody.innerHTML = `
            <tr>
                <td colspan="9">
                    <div class="empty-state">
                        <div class="empty-state-icon">❌</div>
                        <div class="empty-state-title">运行记录加载失败</div>
                        <div class="empty-state-description">${escapeHtml(error.message || '请求失败')}</div>
                    </div>
                </td>
            </tr>
        `;
    }
}

async function openRunLogs(planId) {
    scheduledRunFilters = {
        ...scheduledRunFilters,
        planId: Number(planId),
        page: 1,
    };
    syncScheduledRunFiltersToInputs();
    await loadScheduledRuns();
    focusScheduledRunsCard();
}

function resetScheduledRunModalState() {
    scheduledRunLogLoadToken += 1;
    scheduledRunLogPollingInFlight = false;
    activeScheduledRunId = null;
    activeScheduledRunDetail = null;
    currentScheduledRunLogOffset = 0;
    stopScheduledRunLogPolling();
    if (scheduledTaskElements.runLogStatusBar) {
        scheduledTaskElements.runLogStatusBar.innerHTML = '<span>未选择运行记录</span>';
    }
    if (scheduledTaskElements.runLogModalBody) {
        scheduledTaskElements.runLogModalBody.innerHTML = '<div style="color: var(--text-muted);">暂无记录</div>';
    }
    if (scheduledTaskElements.runLogStopBtn) {
        scheduledTaskElements.runLogStopBtn.style.display = 'none';
        scheduledTaskElements.runLogStopBtn.textContent = '停止运行';
        scheduledTaskElements.runLogStopBtn.disabled = false;
        scheduledTaskElements.runLogStopBtn.removeAttribute('data-run-id');
        scheduledTaskElements.runLogStopBtn.removeAttribute('data-stopping');
    }
}

function renderScheduledRunStatusBar(detail) {
    if (!scheduledTaskElements.runLogStatusBar) return;
    if (!detail) {
        scheduledTaskElements.runLogStatusBar.innerHTML = '<span>未选择运行记录</span>';
        return;
    }
    const uiStatus = getScheduledRunUiStatus(detail);
    scheduledTaskElements.runLogStatusBar.innerHTML = `
        <div class="scheduled-run-meta-bar">
            <span><strong>Run #${detail.id}</strong> · ${escapeHtml(detail.plan_name || `计划 #${detail.plan_id}`)}</span>
            <span>状态：<strong>${getRunStatusText(uiStatus)}</strong></span>
            <span>最后日志：${format.date(detail.last_log_at)}</span>
        </div>
    `;
}

function renderScheduledRunDetailBody(detail, initialLogs = '') {
    if (!scheduledTaskElements.runLogModalBody) return;
    scheduledTaskElements.runLogModalBody.innerHTML = `
        <div class="scheduled-run-detail-head">
            <strong>运行详情 #${detail.id}</strong>
            <div class="table-actions">
                <button class="btn btn-secondary btn-sm" data-action="back-to-runs" onclick="handleRunLogAction(this)">返回运行中心</button>
            </div>
        </div>
        <div class="info-grid scheduled-run-detail-grid">
            <div class="info-item"><span class="label">计划</span><span class="value">${escapeHtml(detail.plan_name || `计划 #${detail.plan_id}`)}</span></div>
            <div class="info-item"><span class="label">任务类型</span><span class="value">${getTaskTypeText(detail.task_type)}</span></div>
            <div class="info-item"><span class="label">触发源</span><span class="value">${escapeHtml(detail.trigger_source || '-')}</span></div>
            <div class="info-item"><span class="label">状态</span><span class="value">${getRunStatusText(getScheduledRunUiStatus(detail))}</span></div>
            <div class="info-item"><span class="label">开始时间</span><span class="value">${format.date(detail.started_at)}</span></div>
            <div class="info-item"><span class="label">结束时间</span><span class="value">${format.date(detail.finished_at)}</span></div>
            <div class="info-item"><span class="label">错误信息</span><span class="value">${escapeHtml(detail.error_message || '-')}</span></div>
            <div class="info-item scheduled-run-summary"><span class="label">摘要</span><span class="value">${escapeHtml(summarizeScheduledRun(detail))}</span></div>
        </div>
        <div class="scheduled-run-log-panel">
            <div class="scheduled-run-log-title">实时日志</div>
            <pre id="scheduled-run-log-output" class="scheduled-run-log-output">${escapeHtml(initialLogs || '暂无日志')}</pre>
        </div>
    `;
}

function appendScheduledRunLogChunk(chunk, options = {}) {
    const logOutput = document.getElementById('scheduled-run-log-output');
    if (!logOutput) return;
    const reset = options.reset === true;
    const nextText = reset ? String(chunk || '') : `${logOutput.textContent === '暂无日志' ? '' : logOutput.textContent}${chunk || ''}`;
    logOutput.textContent = nextText || '暂无日志';
    if (scheduledTaskElements.runLogAutoScrollInput?.checked) {
        logOutput.scrollTop = logOutput.scrollHeight;
    }
}

function setScheduledRunStopButtonState(detail) {
    const button = scheduledTaskElements.runLogStopBtn;
    if (!button) return;
    if (!detail || !detail.is_running) {
        button.style.display = 'none';
        button.disabled = false;
        button.textContent = '停止运行';
        button.removeAttribute('data-run-id');
        button.removeAttribute('data-stopping');
        return;
    }

    button.style.display = '';
    button.dataset.runId = String(detail.id);
    if (getScheduledRunUiStatus(detail) === 'stopping') {
        button.textContent = '停止中';
        button.disabled = true;
        button.setAttribute('data-stopping', 'true');
        return;
    }

    button.textContent = '停止运行';
    button.disabled = false;
    button.removeAttribute('data-stopping');
}

function stopScheduledRunLogPolling() {
    if (scheduledRunLogPollingTimer) {
        clearTimeout(scheduledRunLogPollingTimer);
        scheduledRunLogPollingTimer = null;
    }
}

function scheduleScheduledRunLogPolling(runId, token) {
    scheduledRunLogPollingTimer = setTimeout(async () => {
        if (!isScheduledRunLogRequestActive(runId, token)) {
            stopScheduledRunLogPolling();
            return;
        }
        if (scheduledRunLogPollingInFlight) {
            return;
        }
        scheduledRunLogPollingInFlight = true;
        try {
            const data = await loadScheduledRunLogChunk(runId, { token });
            if (!data || !isScheduledRunLogRequestActive(runId, token)) {
                return;
            }
            if (data.is_running) {
                scheduleScheduledRunLogPolling(runId, token);
            } else {
                stopScheduledRunLogPolling();
            }
        } catch (error) {
            stopScheduledRunLogPolling();
        } finally {
            scheduledRunLogPollingInFlight = false;
        }
    }, 2000);
}

function startScheduledRunLogPolling(runId, token = scheduledRunLogLoadToken) {
    stopScheduledRunLogPolling();
    scheduleScheduledRunLogPolling(runId, token);
}

async function loadScheduledRunLogChunk(runId, { reset = false, token = scheduledRunLogLoadToken } = {}) {
    const targetId = Number(runId);
    if (!Number.isInteger(targetId) || targetId <= 0) return null;

    let offset = reset ? 0 : currentScheduledRunLogOffset;
    let finalData = null;
    let shouldReset = reset;
    const seenOffsets = new Set();

    for (let attempt = 0; attempt < 50; attempt += 1) {
        if (!isScheduledRunLogRequestActive(targetId, token)) {
            return null;
        }

        const data = await api.get(`/scheduled-runs/${targetId}/logs?offset=${offset}`);
        if (!isScheduledRunLogRequestActive(targetId, token)) {
            return null;
        }

        finalData = data;
        const nextOffset = Number(data.next_offset || 0);
        currentScheduledRunLogOffset = nextOffset;

        if (activeScheduledRunDetail && Number(activeScheduledRunDetail.id) === targetId) {
            activeScheduledRunDetail = {
                ...activeScheduledRunDetail,
                status: data.status,
                stop_requested_at: data.stop_requested_at,
                log_version: data.log_version,
                last_log_at: data.last_log_at,
                is_running: data.is_running,
                can_stop: Boolean(data.is_running) && !data.stop_requested_at,
            };
            renderScheduledRunStatusBar(activeScheduledRunDetail);
            setScheduledRunStopButtonState(activeScheduledRunDetail);
        }

        updateScheduledRunCacheItem(targetId, (run) => ({
            ...run,
            status: data.status,
            stop_requested_at: data.stop_requested_at,
            last_log_at: data.last_log_at,
            can_stop: Boolean(data.is_running) && !data.stop_requested_at,
        }));
        renderScheduledRuns(scheduledRunsCache);
        appendScheduledRunLogChunk(data.chunk, { reset: shouldReset });
        shouldReset = false;

        if (!data.has_more) {
            break;
        }

        if (seenOffsets.has(nextOffset) || nextOffset === offset) {
            break;
        }
        seenOffsets.add(nextOffset);
        offset = nextOffset;
    }

    if (finalData && !finalData.is_running) {
        stopScheduledRunLogPolling();
    }

    return finalData;
}

async function openScheduledRunDetail(runId) {
    resetScheduledRunModalState();
    activeScheduledRunId = Number(runId);
    const token = scheduledRunLogLoadToken;
    if (scheduledTaskElements.runLogModal) {
        scheduledTaskElements.runLogModal.classList.add('active');
    }

    try {
        const detail = await api.get(`/scheduled-runs/${runId}`);
        if (!isScheduledRunLogRequestActive(runId, token)) {
            return;
        }
        activeScheduledRunDetail = detail;
        renderScheduledRunStatusBar(detail);
        renderScheduledRunDetailBody(detail, '');
        setScheduledRunStopButtonState(detail);
        const logChunk = await loadScheduledRunLogChunk(runId, { reset: true, token });
        if (!isScheduledRunLogRequestActive(runId, token)) {
            return;
        }
        if (logChunk?.is_running) {
            startScheduledRunLogPolling(runId, token);
        }
    } catch (error) {
        if (!isScheduledRunLogRequestActive(runId, token)) {
            return;
        }
        renderScheduledRunStatusBar(null);
        scheduledTaskElements.runLogModalBody.innerHTML = `<div style="color: var(--danger-color, #d9534f);">加载运行详情失败：${escapeHtml(error.message || '请求失败')}</div>`;
        toast.error(`加载运行详情失败: ${error.message}`);
    }
}

async function openScheduledRunLog(runId) {
    await openScheduledRunDetail(runId);
}

async function stopScheduledRun(runId) {
    const targetId = Number(runId);
    if (!Number.isInteger(targetId) || targetId <= 0) return;

    try {
        await api.post(`/scheduled-runs/${targetId}/stop`, {});
        toast.success('已发送停止请求');
        const optimisticStopRequestedAt = new Date().toISOString();
        updateScheduledRunCacheItem(targetId, (run) => ({
            ...run,
            stop_requested_at: optimisticStopRequestedAt,
            can_stop: false,
        }));
        renderScheduledRuns(scheduledRunsCache);

        if (activeScheduledRunDetail && Number(activeScheduledRunDetail.id) === targetId) {
            activeScheduledRunDetail = {
                ...activeScheduledRunDetail,
                stop_requested_at: optimisticStopRequestedAt,
                can_stop: false,
            };
            renderScheduledRunStatusBar(activeScheduledRunDetail);
            setScheduledRunStopButtonState(activeScheduledRunDetail);
        }

        await loadScheduledRuns();
    } catch (error) {
        toast.error(`停止失败: ${error.message}`);
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
            case 'filter-plan-runs':
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
        if (action === 'view-run-detail') {
            const runId = Number.parseInt(button?.dataset?.runId || '', 10);
            await openScheduledRunDetail(runId);
            return;
        }

        if (action === 'view-run-log') {
            const runId = Number.parseInt(button?.dataset?.runId || '', 10);
            await openScheduledRunLog(runId);
            return;
        }

        if (action === 'stop-run') {
            const runId = Number.parseInt(button?.dataset?.runId || scheduledTaskElements.runLogStopBtn?.dataset?.runId || '', 10);
            await stopScheduledRun(runId);
            return;
        }

        if (action === 'back-to-runs') {
            closeModal('run-log-modal');
            focusScheduledRunsCard();
        }
    });
}

function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (!modal) return;
    if (modalId === 'run-log-modal') {
        resetScheduledRunModalState();
    }
    modal.classList.remove('active');
}

document.addEventListener('DOMContentLoaded', () => {
    loadPlans();
    loadScheduledRuns();
    loadCpaServices();
    setConfigEditorState('cpa_cleanup', {}, {});

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

    if (scheduledTaskElements.runFilterApplyBtn) {
        scheduledTaskElements.runFilterApplyBtn.addEventListener('click', (event) => {
            updateScheduledRunFiltersFromInputs();
            void withButtonBusy(event.currentTarget, () => loadScheduledRuns());
        });
    }

    if (scheduledTaskElements.runFilterResetBtn) {
        scheduledTaskElements.runFilterResetBtn.addEventListener('click', (event) => {
            scheduledRunFilters = {
                taskType: '',
                status: '',
                startedFrom: '',
                startedTo: '',
                planId: null,
                page: 1,
                pageSize: 20,
            };
            scheduledRunPaginationMeta = {
                total: 0,
                page: 1,
                pageSize: 20,
            };
            syncScheduledRunFiltersToInputs();
            void withButtonBusy(event.currentTarget, () => loadScheduledRuns());
        });
    }

    if (scheduledTaskElements.runPrevPageBtn) {
        scheduledTaskElements.runPrevPageBtn.addEventListener('click', (event) => {
            event.preventDefault();
            void withButtonBusy(event.currentTarget, () => jumpToScheduledRunPage(String((scheduledRunFilters.page || 1) - 1)));
        });
    }

    if (scheduledTaskElements.runNextPageBtn) {
        scheduledTaskElements.runNextPageBtn.addEventListener('click', (event) => {
            event.preventDefault();
            void withButtonBusy(event.currentTarget, () => jumpToScheduledRunPage(String((scheduledRunFilters.page || 1) + 1)));
        });
    }

    if (scheduledTaskElements.runPageJumpBtn) {
        scheduledTaskElements.runPageJumpBtn.addEventListener('click', (event) => {
            event.preventDefault();
            const targetPage = scheduledTaskElements.runPageJumpInput?.value || '';
            void withButtonBusy(event.currentTarget, () => jumpToScheduledRunPage(targetPage));
        });
    }

    if (scheduledTaskElements.runPageJumpInput) {
        scheduledTaskElements.runPageJumpInput.addEventListener('keydown', (event) => {
            if (event.key !== 'Enter') return;
            event.preventDefault();
            const targetPage = scheduledTaskElements.runPageJumpInput?.value || '';
            void jumpToScheduledRunPage(targetPage);
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

    if (scheduledTaskElements.planConfigModeTableBtn) {
        scheduledTaskElements.planConfigModeTableBtn.addEventListener('click', () => {
            try {
                switchConfigEditorMode(CONFIG_EDITOR_MODE_TABLE);
            } catch (error) {
                toast.warning(error.message || '无法切回键值对编辑模式');
            }
        });
    }

    if (scheduledTaskElements.planConfigModeJsonBtn) {
        scheduledTaskElements.planConfigModeJsonBtn.addEventListener('click', () => {
            try {
                switchConfigEditorMode(CONFIG_EDITOR_MODE_JSON);
            } catch (error) {
                toast.warning(error.message || '无法切换到原始 JSON 模式');
            }
        });
    }

    if (scheduledTaskElements.planConfigAddEntryBtn) {
        scheduledTaskElements.planConfigAddEntryBtn.addEventListener('click', (event) => {
            void withButtonBusy(event.currentTarget, () => addCustomConfigEntry());
        });
    }

    if (scheduledTaskElements.planCpaServiceSelect) {
        scheduledTaskElements.planCpaServiceSelect.addEventListener('change', () => {
            if (!scheduledTaskElements.planCpaServiceIdInput) return;
            scheduledTaskElements.planCpaServiceIdInput.value = scheduledTaskElements.planCpaServiceSelect.value || '';
        });
    }

    if (scheduledTaskElements.runLogRefreshBtn) {
        scheduledTaskElements.runLogRefreshBtn.addEventListener('click', (event) => {
            if (!activeScheduledRunId) return;
            void withButtonBusy(event.currentTarget, () => openScheduledRunDetail(activeScheduledRunId));
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
                closeModal(modal.id);
            }
        });
    });
});

window.loadPlans = loadPlans;
window.loadScheduledRuns = loadScheduledRuns;
window.buildScheduledRunQuery = buildScheduledRunQuery;
window.renderScheduledRuns = renderScheduledRuns;
window.openCreatePlanModal = openCreatePlanModal;
window.openEditPlanModal = openEditPlanModal;
window.submitPlanForm = submitPlanForm;
window.openRunLogs = openRunLogs;
window.runPlanNow = runPlanNow;
window.showPlanDetail = showPlanDetail;
window.togglePlanEnabled = togglePlanEnabled;
window.openScheduledRunDetail = openScheduledRunDetail;
window.openScheduledRunLog = openScheduledRunLog;
window.viewRunLog = openScheduledRunLog;
window.startScheduledRunLogPolling = startScheduledRunLogPolling;
window.stopScheduledRunLogPolling = stopScheduledRunLogPolling;
window.appendScheduledRunLogChunk = appendScheduledRunLogChunk;
window.stopScheduledRun = stopScheduledRun;
window.handlePlanAction = handlePlanAction;
window.handleRunLogAction = handleRunLogAction;
window.handleConfigEntryInput = handleConfigEntryInput;
window.handleConfigEntryAction = handleConfigEntryAction;
window.renderConfigEntries = renderConfigEntries;
window.buildConfigPayloadFromEntries = buildConfigPayloadFromEntries;
window.prepareConfigEntriesForTaskTypeSwitch = prepareConfigEntriesForTaskTypeSwitch;
window.syncRawJsonFromConfigEntries = syncRawJsonFromConfigEntries;
window.syncConfigEntriesFromRawJson = syncConfigEntriesFromRawJson;
window.switchConfigEditorMode = switchConfigEditorMode;
