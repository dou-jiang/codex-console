/**
 * 账号管理页面 JavaScript
 * 使用 utils.js 中的工具库
 */

// 状态
let currentPage = 1;
let pageSize = 20;
let totalAccounts = 0;
let selectedAccounts = new Set();
let isLoading = false;
let isBatchRefreshing = false;
let isBatchValidating = false;
let isBatchCheckingSubscription = false;
let isOverviewRefreshing = false;
let isQuickWorkflowRunning = false;
let quickWorkflowStepLabel = '';
let selectAllPages = false;  // 是否选中了全部页
let currentFilters = { status: '', email_service: '', role_tag: '', search: '' };  // 当前筛选条件
let autoQuickRefreshSettings = null;
let autoQuickRefreshFormDirty = false;
let isTaskPausing = false;
let isTaskResuming = false;
let pendingAccountListRefresh = null;
let pendingAccountStatsRefresh = null;
const TASK_TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled']);
const activeBatchTasks = {
    refresh: null,
    validate: null,
    subscription: null,
    overview: null,
};

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

function delay(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

function normalizeTaskState(taskRef = {}) {
    const status = String(taskRef?.status || '').trim().toLowerCase();
    const paused = Boolean(taskRef?.paused) || status === 'paused';
    return { ...taskRef, status, paused };
}

function trackBatchTask(key, taskRef = null) {
    if (!Object.prototype.hasOwnProperty.call(activeBatchTasks, key)) return;
    activeBatchTasks[key] = taskRef ? normalizeTaskState(taskRef) : null;
    updateBatchButtons();
}

function patchBatchTask(key, patch = {}) {
    if (!Object.prototype.hasOwnProperty.call(activeBatchTasks, key)) return;
    const current = activeBatchTasks[key];
    if (!current) return;
    activeBatchTasks[key] = normalizeTaskState({ ...current, ...(patch || {}) });
    updateBatchButtons();
}

function getRunningBatchTasks() {
    return Object.entries(activeBatchTasks)
        .map(([key, task]) => ({ key, ...(task || {}) }))
        .filter((task) => task.id && !TASK_TERMINAL_STATUSES.has(String(task.status || '').toLowerCase()));
}

function getPausableBatchTasks() {
    return getRunningBatchTasks().filter((task) => !Boolean(task.paused));
}

function getResumableBatchTasks() {
    return getRunningBatchTasks().filter((task) => Boolean(task.paused));
}

async function watchDomainTask(fetchTask, onUpdate, maxWaitMs = 20 * 60 * 1000, options = {}) {
    const startedAt = Date.now();
    const poller = createAdaptivePoller({
        baseIntervalMs: Number(options.baseIntervalMs || 1200),
        maxIntervalMs: Number(options.maxIntervalMs || 12000),
    });
    let lastError = null;

    while (Date.now() - startedAt < maxWaitMs) {
        try {
            const task = await fetchTask();
            poller.recordSuccess();

            if (typeof onUpdate === 'function') {
                onUpdate(task);
            }

            const status = String(task?.status || '').toLowerCase();
            if (TASK_TERMINAL_STATUSES.has(status)) {
                return task;
            }
        } catch (error) {
            lastError = error;
            const statusCode = Number(error?.response?.status || 0);
            if (statusCode === 404) {
                throw error;
            }
            poller.recordError();
        }

        await sleep(poller.nextDelay({ forceSlow: !api.networkOnline }));
    }

    if (lastError && lastError.message) {
        throw new Error(`任务等待超时: ${lastError.message}`);
    }
    throw new Error('任务等待超时，请稍后刷新查看结果');
}

async function watchAccountTask(taskId, onUpdate, maxWaitMs = 20 * 60 * 1000) {
    return watchDomainTask(
        () => api.get(`/accounts/tasks/${taskId}`, {
            requestKey: `accounts:task:${taskId}`,
            cancelPrevious: true,
            retry: 0,
            timeoutMs: 30000,
            silentNetworkError: true,
            silentTimeoutError: true,
            priority: 'low',
        }),
        onUpdate,
        maxWaitMs,
        { baseIntervalMs: 1200, maxIntervalMs: 12000 },
    );
}

async function watchPaymentTask(taskId, onUpdate, maxWaitMs = 20 * 60 * 1000) {
    return watchDomainTask(
        () => api.get(`/payment/ops/tasks/${taskId}`, {
            requestKey: `payment:task:${taskId}`,
            cancelPrevious: true,
            retry: 0,
            timeoutMs: 30000,
            silentNetworkError: true,
            silentTimeoutError: true,
            priority: 'low',
        }),
        onUpdate,
        maxWaitMs,
        { baseIntervalMs: 1200, maxIntervalMs: 12000 },
    );
}

function replaceAccountRowStatus(accountId, nextStatus) {
    const normalizedId = Number(accountId || 0);
    const normalizedStatus = String(nextStatus || '').trim().toLowerCase();
    if (normalizedId <= 0 || !normalizedStatus) return false;

    const row = elements.table?.querySelector(`tr[data-id="${normalizedId}"]`);
    if (!row) return false;

    const statusCell = row.children?.[5];
    if (!statusCell) return false;

    statusCell.innerHTML = renderAccountStatusDot(normalizedStatus, normalizedId);
    return true;
}

function collectValidatedStatusMap(taskOrResult) {
    const detailRows = Array.isArray(taskOrResult?.details)
        ? taskOrResult.details
        : (Array.isArray(taskOrResult?.result?.details) ? taskOrResult.result.details : []);
    const statusMap = new Map();

    detailRows.forEach((detail) => {
        const accountId = Number(detail?.id || 0);
        const status = String(detail?.status || '').trim().toLowerCase();
        if (accountId > 0 && status) {
            statusMap.set(accountId, status);
        }
    });

    return statusMap;
}

function applyValidatedStatuses(taskOrResult) {
    const statusMap = collectValidatedStatusMap(taskOrResult);
    let updatedCount = 0;
    statusMap.forEach((status, accountId) => {
        if (replaceAccountRowStatus(accountId, status)) {
            updatedCount += 1;
        }
    });
    return updatedCount;
}

async function refreshAccountsView(options = {}) {
    const refreshStats = options.refreshStats !== false;
    const refreshList = options.refreshList !== false;
    const settleDelayMs = Math.max(0, Number(options.settleDelayMs || 0));
    const tasks = [];

    if (settleDelayMs > 0) {
        await delay(settleDelayMs);
    }

    if (refreshStats) {
        if (!pendingAccountStatsRefresh) {
            pendingAccountStatsRefresh = loadStats().finally(() => {
                pendingAccountStatsRefresh = null;
            });
        }
        tasks.push(pendingAccountStatsRefresh);
    }

    if (refreshList) {
        if (!pendingAccountListRefresh) {
            pendingAccountListRefresh = loadAccounts().finally(() => {
                pendingAccountListRefresh = null;
            });
        }
        tasks.push(pendingAccountListRefresh);
    }

    if (tasks.length > 0) {
        await Promise.all(tasks);
    }
}

// DOM 元素
const elements = {
    table: document.getElementById('accounts-table'),
    totalAccounts: document.getElementById('total-accounts'),
    activeAccounts: document.getElementById('active-accounts'),
    expiredAccounts: document.getElementById('expired-accounts'),
    failedAccounts: document.getElementById('failed-accounts'),
    motherAccounts: document.getElementById('mother-accounts'),
    childAccounts: document.getElementById('child-accounts'),
    filterStatus: document.getElementById('filter-status'),
    filterService: document.getElementById('filter-service'),
    filterRoleTag: document.getElementById('filter-role-tag'),
    searchInput: document.getElementById('search-input'),
    quickRefreshBtn: document.getElementById('quick-refresh-btn'),
    autoQuickRefreshSettingsBtn: document.getElementById('auto-quick-refresh-settings-btn'),
    batchRefreshBtn: document.getElementById('batch-refresh-btn'),
    batchValidateBtn: document.getElementById('batch-validate-btn'),
    batchUploadBtn: document.getElementById('batch-upload-btn'),
    batchCheckSubBtn: document.getElementById('batch-check-sub-btn'),
    batchPauseBtn: document.getElementById('batch-pause-btn'),
    batchResumeBtn: document.getElementById('batch-resume-btn'),
    batchDeleteBtn: document.getElementById('batch-delete-btn'),
    exportBtn: document.getElementById('export-btn'),
    exportMenu: document.getElementById('export-menu'),
    selectAll: document.getElementById('select-all'),
    prevPage: document.getElementById('prev-page'),
    nextPage: document.getElementById('next-page'),
    pageInfo: document.getElementById('page-info'),
    detailModal: document.getElementById('detail-modal'),
    modalBody: document.getElementById('modal-body'),
    closeModal: document.getElementById('close-modal'),
    autoQuickRefreshModal: document.getElementById('auto-quick-refresh-modal'),
    autoQuickRefreshEnabled: document.getElementById('auto-quick-refresh-enabled'),
    autoQuickRefreshInterval: document.getElementById('auto-quick-refresh-interval'),
    autoQuickRefreshRetry: document.getElementById('auto-quick-refresh-retry'),
    autoQuickRefreshRunNow: document.getElementById('auto-quick-refresh-run-now'),
    autoQuickRefreshRuntime: document.getElementById('auto-quick-refresh-runtime'),
    closeAutoQuickRefreshModalBtn: document.getElementById('close-auto-quick-refresh-modal'),
    cancelAutoQuickRefreshBtn: document.getElementById('cancel-auto-quick-refresh-btn'),
    saveAutoQuickRefreshBtn: document.getElementById('save-auto-quick-refresh-btn'),
};

// 初始化
document.addEventListener('DOMContentLoaded', () => {
    loadStats();
    loadAccounts();
    loadAutoQuickRefreshSettings({ silent: true });
    setInterval(() => {
        loadAutoQuickRefreshSettings({ silent: true });
    }, 30000);
    initEventListeners();
    updateBatchButtons();  // 初始化按钮状态
    renderSelectAllBanner();
});

// 事件监听
function initEventListeners() {
    // 筛选
    elements.filterStatus.addEventListener('change', () => {
        currentPage = 1;
        resetSelectAllPages();
        loadAccounts();
    });

    elements.filterService.addEventListener('change', () => {
        currentPage = 1;
        resetSelectAllPages();
        loadAccounts();
    });

    elements.filterRoleTag?.addEventListener('change', () => {
        currentPage = 1;
        resetSelectAllPages();
        loadAccounts();
    });

    // 搜索（防抖）
    elements.searchInput.addEventListener('input', debounce(() => {
        currentPage = 1;
        resetSelectAllPages();
        loadAccounts();
    }, 300));

    // 快捷键聚焦搜索
    elements.searchInput.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            elements.searchInput.blur();
            elements.searchInput.value = '';
            resetSelectAllPages();
            loadAccounts();
        }
    });

    // 批量刷新Token
    elements.batchRefreshBtn.addEventListener('click', handleBatchRefresh);
    elements.autoQuickRefreshSettingsBtn?.addEventListener('click', openAutoQuickRefreshModal);

    // 批量验证Token
    elements.batchValidateBtn.addEventListener('click', handleBatchValidate);

    // 批量检测订阅
    elements.batchCheckSubBtn.addEventListener('click', handleBatchCheckSubscription);
    elements.batchPauseBtn?.addEventListener('click', pauseActiveBatchTasks);
    elements.batchResumeBtn?.addEventListener('click', resumeActiveBatchTasks);

    // 上传下拉菜单
    const uploadMenu = document.getElementById('upload-menu');
    elements.batchUploadBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        uploadMenu.classList.toggle('active');
    });
    document.getElementById('batch-upload-cpa-item').addEventListener('click', (e) => { e.preventDefault(); uploadMenu.classList.remove('active'); handleBatchUploadCpa(); });
    document.getElementById('batch-upload-sub2api-item').addEventListener('click', (e) => { e.preventDefault(); uploadMenu.classList.remove('active'); handleBatchUploadSub2Api(); });
    document.getElementById('batch-upload-tm-item').addEventListener('click', (e) => { e.preventDefault(); uploadMenu.classList.remove('active'); handleBatchUploadTm(); });

    // 批量删除
    elements.batchDeleteBtn.addEventListener('click', handleBatchDelete);

    // 全选（当前页）
    elements.selectAll.addEventListener('change', (e) => {
        const checkboxes = elements.table.querySelectorAll('input[type="checkbox"][data-id]');
        checkboxes.forEach(cb => {
            cb.checked = e.target.checked;
            const id = parseInt(cb.dataset.id);
            if (e.target.checked) {
                selectedAccounts.add(id);
            } else {
                selectedAccounts.delete(id);
            }
        });
        if (!e.target.checked) {
            selectAllPages = false;
        }
        updateBatchButtons();
        renderSelectAllBanner();
    });

    // 分页
    elements.prevPage.addEventListener('click', () => {
        if (currentPage > 1 && !isLoading) {
            currentPage--;
            loadAccounts();
        }
    });

    elements.nextPage.addEventListener('click', () => {
        const totalPages = Math.ceil(totalAccounts / pageSize);
        if (currentPage < totalPages && !isLoading) {
            currentPage++;
            loadAccounts();
        }
    });

    // 导出
    elements.exportBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        elements.exportMenu.classList.toggle('active');
    });

    delegate(elements.exportMenu, 'click', '.dropdown-item', (e, target) => {
        e.preventDefault();
        const format = target.dataset.format;
        exportAccounts(format);
        elements.exportMenu.classList.remove('active');
    });

    // 关闭模态框
    elements.closeModal.addEventListener('click', () => {
        elements.detailModal.classList.remove('active');
    });

    elements.detailModal.addEventListener('click', (e) => {
        if (e.target === elements.detailModal) {
            elements.detailModal.classList.remove('active');
        }
    });

    elements.closeAutoQuickRefreshModalBtn?.addEventListener('click', closeAutoQuickRefreshModal);
    elements.cancelAutoQuickRefreshBtn?.addEventListener('click', closeAutoQuickRefreshModal);
    elements.saveAutoQuickRefreshBtn?.addEventListener('click', saveAutoQuickRefreshSettings);
    [
        elements.autoQuickRefreshEnabled,
        elements.autoQuickRefreshInterval,
        elements.autoQuickRefreshRetry,
        elements.autoQuickRefreshRunNow,
    ].forEach((input) => {
        input?.addEventListener('change', () => {
            autoQuickRefreshFormDirty = true;
        });
        input?.addEventListener('input', () => {
            autoQuickRefreshFormDirty = true;
        });
    });
    elements.autoQuickRefreshModal?.addEventListener('click', (e) => {
        if (e.target === elements.autoQuickRefreshModal) {
            closeAutoQuickRefreshModal();
        }
    });

    // 点击其他地方关闭下拉菜单
    document.addEventListener('click', () => {
        elements.exportMenu.classList.remove('active');
        uploadMenu.classList.remove('active');
        document.querySelectorAll('#accounts-table .dropdown-menu.active').forEach(m => m.classList.remove('active'));
    });
}

function formatSchedulerTime(value) {
    if (!value) return '-';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '-';
    return date.toLocaleString('zh-CN');
}

function renderAutoQuickRefreshRuntime(runtime) {
    const info = runtime || {};
    const logs = Array.isArray(info.logs) ? info.logs : [];
    if (logs.length === 0) {
        return `
            <div class="auto-quick-refresh-log-title">执行日志</div>
            <div class="auto-quick-refresh-log-empty">暂无执行日志</div>
        `;
    }
    const rows = logs
        .slice(-20)
        .reverse()
        .map((item) => {
            const levelRaw = String(item?.level || 'info').trim().toLowerCase();
            const level = ['success', 'warning', 'error', 'info'].includes(levelRaw) ? levelRaw : 'info';
            const timeText = formatSchedulerTime(item?.time);
            const message = String(item?.message || '').trim() || '-';
            return `
                <div class="auto-quick-refresh-log-item level-${level}">
                    <span class="log-time">${escapeHtml(timeText)}</span>
                    <span class="log-level">${escapeHtml(level)}</span>
                    <span class="log-message">${escapeHtml(message)}</span>
                </div>
            `;
        })
        .join('');
    return `
        <div class="auto-quick-refresh-log-title">执行日志</div>
        <div class="auto-quick-refresh-log-list">${rows}</div>
    `;
}

function updateAutoQuickRefreshButton() {
    const btn = elements.autoQuickRefreshSettingsBtn;
    if (!btn) return;
    const enabled = Boolean(autoQuickRefreshSettings?.enabled);
    const interval = Number(autoQuickRefreshSettings?.interval_minutes || 0);
    const runtime = autoQuickRefreshSettings?.runtime || {};
    if (runtime.running) {
        btn.textContent = '⚙️ 运行中';
        btn.title = '定时自动一键刷新正在执行';
        return;
    }
    if (enabled && interval > 0) {
        btn.textContent = `⚙️ 定时(${interval}m)`;
        btn.title = `定时自动一键刷新已启用，每 ${interval} 分钟执行`;
        return;
    }
    btn.textContent = '⚙️ 定时';
    btn.title = '定时自动一键刷新设置';
}

function fillAutoQuickRefreshForm(options = {}) {
    if (!autoQuickRefreshSettings) return;
    let syncSettings = options.syncSettings !== false;
    const syncRuntime = options.syncRuntime !== false;
    const force = options.force === true;
    const modalActive = elements.autoQuickRefreshModal?.classList.contains('active');
    if (!force && modalActive && autoQuickRefreshFormDirty) {
        syncSettings = false;
    }

    if (syncSettings && elements.autoQuickRefreshEnabled) {
        elements.autoQuickRefreshEnabled.checked = Boolean(autoQuickRefreshSettings.enabled);
    }
    if (syncSettings && elements.autoQuickRefreshInterval) {
        elements.autoQuickRefreshInterval.value = String(autoQuickRefreshSettings.interval_minutes || 30);
    }
    if (syncSettings && elements.autoQuickRefreshRetry) {
        elements.autoQuickRefreshRetry.value = String(autoQuickRefreshSettings.retry_limit || 2);
    }
    if (syncSettings && elements.autoQuickRefreshRunNow) {
        elements.autoQuickRefreshRunNow.checked = false;
    }
    if (syncRuntime && elements.autoQuickRefreshRuntime) {
        elements.autoQuickRefreshRuntime.innerHTML = renderAutoQuickRefreshRuntime(autoQuickRefreshSettings.runtime || {});
    }
}

async function loadAutoQuickRefreshSettings(options = {}) {
    const silent = options.silent === true;
    try {
        const data = await api.get('/settings/auto-quick-refresh', {
            requestKey: 'settings:auto-quick-refresh',
            cancelPrevious: true,
            retry: 1,
            timeoutMs: 15000,
        });
        autoQuickRefreshSettings = data || {};
        updateAutoQuickRefreshButton();
        if (elements.autoQuickRefreshModal?.classList.contains('active')) {
            fillAutoQuickRefreshForm({
                syncSettings: !autoQuickRefreshFormDirty,
                syncRuntime: true,
            });
        }
    } catch (error) {
        if (!silent) {
            toast.error('加载定时设置失败: ' + error.message);
        }
    }
}

async function openAutoQuickRefreshModal() {
    if (!elements.autoQuickRefreshModal) return;
    await loadAutoQuickRefreshSettings({ silent: false });
    autoQuickRefreshFormDirty = false;
    fillAutoQuickRefreshForm({ force: true, syncSettings: true, syncRuntime: true });
    elements.autoQuickRefreshModal.classList.add('active');
}

function closeAutoQuickRefreshModal() {
    autoQuickRefreshFormDirty = false;
    elements.autoQuickRefreshModal?.classList.remove('active');
}

async function saveAutoQuickRefreshSettings() {
    if (!elements.saveAutoQuickRefreshBtn) return;
    const enabled = Boolean(elements.autoQuickRefreshEnabled?.checked);
    const interval = Math.max(5, Math.min(1440, Number(elements.autoQuickRefreshInterval?.value || 30)));
    const retryLimit = Math.max(0, Math.min(5, Number(elements.autoQuickRefreshRetry?.value || 2)));
    const runNow = enabled && Boolean(elements.autoQuickRefreshRunNow?.checked);

    const btn = elements.saveAutoQuickRefreshBtn;
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = '保存中...';

    try {
        await api.post('/settings/auto-quick-refresh', {
            enabled,
            interval_minutes: interval,
            retry_limit: retryLimit,
            run_now: runNow,
        }, {
            requestKey: 'settings:auto-quick-refresh:update',
            cancelPrevious: true,
            timeoutMs: 20000,
            retry: 0,
        });
        toast.success(runNow ? '设置已保存，已触发一次立即执行' : '定时自动一键刷新设置已保存');
        autoQuickRefreshFormDirty = false;
        closeAutoQuickRefreshModal();
        await loadAutoQuickRefreshSettings({ silent: true });
    } catch (error) {
        toast.error('保存定时设置失败: ' + error.message);
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

// 加载统计信息
async function loadStats() {
    try {
        const data = await api.get('/accounts/stats/summary', {
            requestKey: 'accounts:stats',
            cancelPrevious: true,
            retry: 1,
        });

        elements.totalAccounts.textContent = format.number(data.total || 0);
        elements.activeAccounts.textContent = format.number(data.by_status?.active || 0);
        elements.expiredAccounts.textContent = format.number(data.by_status?.expired || 0);
        elements.failedAccounts.textContent = format.number(data.by_status?.failed || 0);
        const parentCount = Number(data.tagged_role_counts?.parent ?? data.by_role_tag?.parent ?? 0);
        const childCount = Number(data.tagged_role_counts?.child ?? data.by_role_tag?.child ?? 0);
        if (elements.motherAccounts) {
            elements.motherAccounts.textContent = format.number(parentCount);
        }
        if (elements.childAccounts) {
            elements.childAccounts.textContent = format.number(childCount);
        }

        // 添加动画效果
        animateValue(elements.totalAccounts, data.total || 0);
    } catch (error) {
        console.error('加载统计信息失败:', error);
    }
}

// 数字动画
function animateValue(element, value) {
    element.style.transition = 'transform 0.2s ease';
    element.style.transform = 'scale(1.1)';
    setTimeout(() => {
        element.style.transform = 'scale(1)';
    }, 200);
}

// 加载账号列表
async function loadAccounts() {
    if (isLoading) return;
    isLoading = true;

    // 显示加载状态
    elements.table.innerHTML = `
        <tr>
            <td colspan="9">
                <div class="empty-state">
                    <div class="skeleton skeleton-text" style="width: 60%;"></div>
                    <div class="skeleton skeleton-text" style="width: 80%;"></div>
                    <div class="skeleton skeleton-text" style="width: 40%;"></div>
                </div>
            </td>
        </tr>
    `;

    // 记录当前筛选条件
    currentFilters = filterProtocol.normalize({
        status: elements.filterStatus.value,
        email_service: elements.filterService.value,
        role_tag: elements.filterRoleTag?.value || '',
        search: elements.searchInput.value.trim(),
    });

    const params = filterProtocol.toQuery({
        page: currentPage,
        page_size: pageSize,
        status: currentFilters.status,
        email_service: currentFilters.email_service,
        role_tag: currentFilters.role_tag,
        search: currentFilters.search,
    });
    const queryText = params.toString();

    try {
        const data = await api.get(`/accounts${queryText ? `?${queryText}` : ''}`, {
            requestKey: 'accounts:list',
            cancelPrevious: true,
            retry: 1,
        });
        totalAccounts = data.total;
        renderAccounts(data.accounts);
        updatePagination();
    } catch (error) {
        console.error('加载账号列表失败:', error);
        elements.table.innerHTML = `
            <tr>
                <td colspan="9">
                    <div class="empty-state">
                        <div class="empty-state-icon">❌</div>
                        <div class="empty-state-title">加载失败</div>
                        <div class="empty-state-description">请检查网络连接后重试</div>
                    </div>
                </td>
            </tr>
        `;
    } finally {
        isLoading = false;
        updateBatchButtons();
    }
}

// 渲染账号列表
function renderAccounts(accounts) {
    if (accounts.length === 0) {
        elements.table.innerHTML = `
            <tr>
                <td colspan="9">
                    <div class="empty-state">
                        <div class="empty-state-icon">📭</div>
                        <div class="empty-state-title">暂无数据</div>
                        <div class="empty-state-description">没有找到符合条件的账号记录</div>
                    </div>
                </td>
            </tr>
        `;
        return;
    }

    elements.table.innerHTML = accounts.map(account => `
        <tr data-id="${account.id}">
            <td>
                <input type="checkbox" data-id="${account.id}"
                    ${selectedAccounts.has(account.id) ? 'checked' : ''}>
            </td>
            <td>${account.id}</td>
            <td>
                <span style="display:inline-flex;align-items:center;gap:4px;">
                    <span class="email-cell" title="${escapeHtml(account.email)}">${escapeHtml(account.email)}</span>
                    ${renderAccountLabelBadge(account.account_label)}
                    <button class="btn-copy-icon copy-email-btn" data-email="${escapeHtml(account.email)}" title="复制邮箱">📋</button>
                </span>
            </td>
            <td class="password-cell">
                ${account.password
                    ? `<span style="display:inline-flex;align-items:center;gap:4px;">
                        <span class="password-hidden" data-pwd="${escapeHtml(account.password)}" onclick="togglePassword(this, this.dataset.pwd)" title="点击查看">${escapeHtml(account.password.substring(0, 4) + '****')}</span>
                        <button class="btn-copy-icon copy-pwd-btn" data-pwd="${escapeHtml(account.password)}" title="复制密码">📋</button>
                       </span>`
                    : '-'}
            </td>
            <td>${getServiceTypeText(account.email_service)}</td>
            <td>${renderAccountStatusDot(account.status, account.id)}</td>
            <td>
                <div class="cpa-status">
                    ${account.cpa_uploaded
                        ? `<span class="cpa-status-dot" title="已上传于 ${format.date(account.cpa_uploaded_at)}"></span>`
                        : ``}
                </div>
            </td>
            <td>
                ${renderSubscriptionStatus(account.subscription_type)}
            </td>
            <td>${format.date(account.last_refresh) || '-'}</td>
            <td>
                <div style="display:flex;gap:4px;align-items:center;white-space:nowrap;">
                    <button class="btn btn-secondary btn-sm" onclick="viewAccount(${account.id})">详情</button>
                    <button class="btn btn-secondary btn-sm" onclick="checkInboxCode(${account.id})">收件箱</button>
                    <div class="dropdown" style="position:relative;">
                        <button class="btn btn-secondary btn-sm" onclick="event.stopPropagation();toggleMoreMenu(this)">更多</button>
                        <div class="dropdown-menu" style="min-width:100px;">
                            <a href="#" class="dropdown-item" onclick="event.preventDefault();closeMoreMenu(this);refreshToken(${account.id})">刷新</a>
                            <a href="#" class="dropdown-item" onclick="event.preventDefault();closeMoreMenu(this);uploadAccount(${account.id})">上传</a>
                            <a href="#" class="dropdown-item" onclick="event.preventDefault();closeMoreMenu(this);markAccountLabel(${account.id}, '${escapeHtml(account.account_label || account.role_tag || 'none')}')">标号</a>
                            <a href="#" class="dropdown-item" onclick="event.preventDefault();closeMoreMenu(this);markSubscription(${account.id})">标记</a>
                        </div>
                    </div>
                    <button class="btn btn-danger btn-sm" onclick="deleteAccount(${account.id}, '${escapeHtml(account.email)}')">删除</button>
                </div>
            </td>
        </tr>
    `).join('');

    // 绑定复选框事件
    elements.table.querySelectorAll('input[type="checkbox"][data-id]').forEach(cb => {
        cb.addEventListener('change', (e) => {
            const id = parseInt(e.target.dataset.id);
            if (e.target.checked) {
                selectedAccounts.add(id);
            } else {
                selectedAccounts.delete(id);
                selectAllPages = false;
            }
            // 同步全选框状态
            const allChecked = elements.table.querySelectorAll('input[type="checkbox"][data-id]');
            const checkedCount = elements.table.querySelectorAll('input[type="checkbox"][data-id]:checked').length;
            elements.selectAll.checked = allChecked.length > 0 && checkedCount === allChecked.length;
            elements.selectAll.indeterminate = checkedCount > 0 && checkedCount < allChecked.length;
            updateBatchButtons();
            renderSelectAllBanner();
        });
    });

    // 绑定复制邮箱按钮
    elements.table.querySelectorAll('.copy-email-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            copyToClipboard(btn.dataset.email);
        });
    });

    // 绑定复制密码按钮
    elements.table.querySelectorAll('.copy-pwd-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            copyToClipboard(btn.dataset.pwd);
        });
    });

    // 渲染后同步全选框状态
    const allCbs = elements.table.querySelectorAll('input[type="checkbox"][data-id]');
    const checkedCbs = elements.table.querySelectorAll('input[type="checkbox"][data-id]:checked');
    elements.selectAll.checked = allCbs.length > 0 && checkedCbs.length === allCbs.length;
    elements.selectAll.indeterminate = checkedCbs.length > 0 && checkedCbs.length < allCbs.length;
    renderSelectAllBanner();
}

function normalizeSubscriptionType(subscriptionType) {
    const raw = String(subscriptionType || '').trim().toLowerCase();
    if (!raw) return '';
    if (raw.includes('team') || raw.includes('enterprise')) return 'team';
    if (raw.includes('plus') || raw.includes('pro')) return 'plus';
    if (raw.includes('free') || raw.includes('basic')) return 'free';
    return raw;
}

function hasActiveSubscription(subscriptionType) {
    const normalized = normalizeSubscriptionType(subscriptionType);
    return normalized === 'plus' || normalized === 'team';
}

function renderAccountStatusDot(status, accountId) {
    const normalized = String(status || '').trim().toLowerCase();
    const dotClass = ['active', 'expired', 'banned', 'failed'].includes(normalized)
        ? normalized
        : 'unknown';
    const title = getStatusText('account', normalized) || normalized || '-';
    return `
        <div class="account-status-cell" title="${escapeHtml(title)}">
            <span class="account-status-dot ${dotClass}"></span>
        </div>
    `;
}

function renderSubscriptionStatus(subscriptionType) {
    const normalized = normalizeSubscriptionType(subscriptionType);
    const variant = (normalized === 'plus' || normalized === 'team') ? normalized : 'free';
    const label = variant.toUpperCase();
    const title = variant === 'free'
        ? '未检测到 Plus/Team 订阅'
        : `已订阅: ${variant}`;
    return `
        <div class="subscription-status ${variant}" title="${escapeHtml(title)}">
            <span class="dot"></span>
            <span class="label">${escapeHtml(label)}</span>
        </div>
    `;
}

// 切换密码显示
function togglePassword(element, password) {
    if (element.dataset.revealed === 'true') {
        element.textContent = password.substring(0, 4) + '****';
        element.classList.add('password-hidden');
        element.dataset.revealed = 'false';
    } else {
        element.textContent = password;
        element.classList.remove('password-hidden');
        element.dataset.revealed = 'true';
    }
}

// 更新分页
function updatePagination() {
    const totalPages = Math.max(1, Math.ceil(totalAccounts / pageSize));

    elements.prevPage.disabled = currentPage <= 1;
    elements.nextPage.disabled = currentPage >= totalPages;

    elements.pageInfo.textContent = `第 ${currentPage} 页 / 共 ${totalPages} 页`;
}

// 重置全选所有页状态
function resetSelectAllPages() {
    selectAllPages = false;
    selectedAccounts.clear();
    updateBatchButtons();
    renderSelectAllBanner();
}

// 构建批量请求体（含 select_all 和筛选参数）
function buildBatchPayload(extraFields = {}) {
    const filterPayload = filterProtocol.toPayload({
        status_filter: currentFilters.status,
        email_service_filter: currentFilters.email_service,
        search_filter: currentFilters.search,
    });
    if (selectAllPages) {
        return {
            ids: [],
            select_all: true,
            ...filterPayload,
            ...extraFields
        };
    }
    return { ids: Array.from(selectedAccounts), ...extraFields };
}

// 获取有效选中数量（select_all 时用总数）
function getEffectiveCount() {
    return selectAllPages ? totalAccounts : selectedAccounts.size;
}

// 渲染全选横幅
function renderSelectAllBanner() {
    let banner = document.getElementById('select-all-banner');
    const totalPages = Math.ceil(totalAccounts / pageSize);
    const currentPageSize = elements.table.querySelectorAll('input[type="checkbox"][data-id]').length;
    const checkedOnPage = elements.table.querySelectorAll('input[type="checkbox"][data-id]:checked').length;
    const allPageSelected = currentPageSize > 0 && checkedOnPage === currentPageSize;

    // 只在全选了当前页且有多页时显示横幅
    if (!allPageSelected || totalPages <= 1 || totalAccounts <= pageSize) {
        if (banner) banner.remove();
        return;
    }

    if (!banner) {
        banner = document.createElement('div');
        banner.id = 'select-all-banner';
        banner.style.cssText = 'background:var(--primary-light,#e8f0fe);color:var(--primary-color,#1a73e8);padding:8px 16px;text-align:center;font-size:0.875rem;border-bottom:1px solid var(--border-color);';
        const tableContainer = document.querySelector('.table-container');
        if (tableContainer) tableContainer.insertAdjacentElement('beforebegin', banner);
    }

    if (selectAllPages) {
        banner.innerHTML = `已选中全部 <strong>${totalAccounts}</strong> 条记录。<button onclick="resetSelectAllPages()" style="margin-left:8px;color:var(--primary-color,#1a73e8);background:none;border:none;cursor:pointer;text-decoration:underline;">取消全选</button>`;
    } else {
        banner.innerHTML = `当前页已全选 <strong>${checkedOnPage}</strong> 条。<button onclick="selectAllPagesAction()" style="margin-left:8px;color:var(--primary-color,#1a73e8);background:none;border:none;cursor:pointer;text-decoration:underline;">选择全部 ${totalAccounts} 条</button>`;
    }
}

// 选中所有页
function selectAllPagesAction() {
    selectAllPages = true;
    updateBatchButtons();
    renderSelectAllBanner();
}

async function pauseActiveBatchTasks() {
    const tasks = getPausableBatchTasks();
    if (!tasks.length || isTaskPausing) return;

    isTaskPausing = true;
    updateBatchButtons();
    try {
        const results = await Promise.allSettled(
            tasks.map((task) => api.post(`/tasks/${task.domain}/${task.id}/pause`, {}, {
                timeoutMs: 15000,
                retry: 0,
                priority: 'high',
            })),
        );
        let successCount = 0;
        let failedCount = 0;
        results.forEach((item, index) => {
            if (item.status === 'fulfilled') {
                successCount += 1;
                patchBatchTask(tasks[index].key, {
                    status: 'paused',
                    paused: true,
                    pause_requested: true,
                });
                return;
            }
            failedCount += 1;
        });
        if (successCount > 0) {
            toast.success(`已暂停 ${successCount} 个任务`);
        }
        if (failedCount > 0) {
            toast.warning(`${failedCount} 个任务暂停失败`);
        }
    } catch (error) {
        toast.error(`暂停任务失败: ${error.message}`);
    } finally {
        isTaskPausing = false;
        updateBatchButtons();
    }
}

async function resumeActiveBatchTasks() {
    const tasks = getResumableBatchTasks();
    if (!tasks.length || isTaskResuming) return;

    isTaskResuming = true;
    updateBatchButtons();
    try {
        const results = await Promise.allSettled(
            tasks.map((task) => api.post(`/tasks/${task.domain}/${task.id}/resume`, {}, {
                timeoutMs: 15000,
                retry: 0,
                priority: 'high',
            })),
        );
        let successCount = 0;
        let failedCount = 0;
        results.forEach((item, index) => {
            if (item.status === 'fulfilled') {
                successCount += 1;
                patchBatchTask(tasks[index].key, {
                    status: 'running',
                    paused: false,
                    pause_requested: false,
                });
                return;
            }
            failedCount += 1;
        });
        if (successCount > 0) {
            toast.success(`已继续 ${successCount} 个任务`);
        }
        if (failedCount > 0) {
            toast.warning(`${failedCount} 个任务继续失败`);
        }
    } catch (error) {
        toast.error(`继续任务失败: ${error.message}`);
    } finally {
        isTaskResuming = false;
        updateBatchButtons();
    }
}

// 更新批量操作按钮
function updateBatchButtons() {
    const count = getEffectiveCount();
    const baseDisabled = count === 0 || isQuickWorkflowRunning || isTaskPausing || isTaskResuming;
    elements.batchDeleteBtn.disabled = baseDisabled;
    elements.batchRefreshBtn.disabled = baseDisabled || isBatchRefreshing;
    elements.batchValidateBtn.disabled = baseDisabled || isBatchValidating;
    elements.batchUploadBtn.disabled = baseDisabled;
    elements.batchCheckSubBtn.disabled = baseDisabled || isBatchCheckingSubscription;
    elements.exportBtn.disabled = count === 0;
    if (elements.quickRefreshBtn) {
        elements.quickRefreshBtn.disabled = true;
        elements.quickRefreshBtn.textContent = '⚡ 一键刷新(已禁用)';
    }

    elements.batchDeleteBtn.textContent = count > 0 ? `🗑️ 删除 (${count})` : '🗑️ 批量删除';
    elements.batchRefreshBtn.textContent = count > 0 ? `🔄 刷新 (${count})` : '🔄 刷新Token';
    elements.batchValidateBtn.textContent = count > 0 ? `✅ 验证 (${count})` : '✅ 验证Token';
    elements.batchUploadBtn.textContent = count > 0 ? `☁️ 上传 (${count})` : '☁️ 上传';
    elements.batchCheckSubBtn.textContent = count > 0 ? `🔍 检测 (${count})` : '🔍 检测订阅';

    const pausableCount = getPausableBatchTasks().length;
    const resumableCount = getResumableBatchTasks().length;
    if (elements.batchPauseBtn) {
        elements.batchPauseBtn.disabled = pausableCount === 0 || isTaskPausing;
        elements.batchPauseBtn.textContent = isTaskPausing
            ? '⏸️ 暂停中...'
            : (pausableCount > 0 ? `⏸️ 暂停(${pausableCount})` : '⏸️ 暂停');
    }
    if (elements.batchResumeBtn) {
        elements.batchResumeBtn.disabled = resumableCount === 0 || isTaskResuming;
        elements.batchResumeBtn.textContent = isTaskResuming
            ? '▶️ 继续中...'
            : (resumableCount > 0 ? `▶️ 继续(${resumableCount})` : '▶️ 继续');
    }
}

// 刷新单个账号Token
async function refreshToken(id) {
    try {
        toast.info('正在刷新Token...');
        const result = await api.post(`/accounts/${id}/refresh`, {}, {
            timeoutMs: 60000,
            retry: 0,
        });

        if (result.success) {
            toast.success('Token刷新成功');
            loadAccounts();
        } else {
            toast.error('刷新失败: ' + (result.error || '未知错误'));
        }
    } catch (error) {
        toast.error('刷新失败: ' + error.message);
    }
}

async function validateToken(id) {
    try {
        toast.info('正在验证Token...');
        const result = await api.post(`/accounts/${id}/validate`, {}, {
            timeoutMs: 30000,
            retry: 0,
        });

        const nextStatus = String(result?.status || '').trim().toLowerCase();
        if (nextStatus) {
            replaceAccountRowStatus(id, nextStatus);
        }

        if (result.valid) {
            toast.success('Token 验证通过');
        } else {
            toast.warning(`Token 无效: ${result.error || '未知错误'}`, 5000);
        }

        await refreshAccountsView({ settleDelayMs: 80 });
    } catch (error) {
        toast.error('验证失败: ' + error.message);
    }
}

async function runBatchRefreshTask(payload, count, sourceLabel, options = {}) {
    const showToast = options.showToast !== false;
    const reloadAfter = options.reloadAfter !== false;
    const onProgress = typeof options.onProgress === 'function' ? options.onProgress : null;
    isBatchRefreshing = true;
    updateBatchButtons();

    try {
        const task = await api.post('/accounts/batch-refresh/async', payload, {
            timeoutMs: 20000,
            retry: 0,
            requestKey: 'accounts:batch-refresh',
            cancelPrevious: true,
        });
        const taskId = task?.id;
        if (!taskId) {
            throw new Error('任务创建失败：未返回任务 ID');
        }
        trackBatchTask('refresh', {
            id: taskId,
            domain: 'accounts',
            status: task?.status || 'pending',
            paused: Boolean(task?.paused),
        });

        if (showToast) {
            toast.info(`${sourceLabel}任务已启动（${taskId.slice(0, 8)}）`);
        }
        const finalTask = await watchAccountTask(taskId, (progressTask) => {
            patchBatchTask('refresh', {
                status: progressTask?.status || 'running',
                paused: Boolean(progressTask?.paused),
                pause_requested: Boolean(progressTask?.pause_requested),
            });
            const progress = progressTask?.progress || {};
            const completed = Number(progress.completed || 0);
            const total = Number(progress.total || count);
            const paused = Boolean(progressTask?.paused) || String(progressTask?.status || '').toLowerCase() === 'paused';
            elements.batchRefreshBtn.textContent = paused ? `已暂停 ${completed}/${total}` : `刷新中 ${completed}/${total}`;
            if (elements.quickRefreshBtn && !isQuickWorkflowRunning) {
                elements.quickRefreshBtn.textContent = paused ? `⚡ 已暂停 ${completed}/${total}` : `⚡ 刷新中 ${completed}/${total}`;
            }
            if (onProgress) {
                onProgress({ completed, total, task: progressTask });
            }
        });
        patchBatchTask('refresh', {
            status: finalTask?.status || 'completed',
            paused: false,
            pause_requested: false,
        });
        const result = finalTask?.result || {};
        const status = String(finalTask?.status || '').toLowerCase();
        if (status === 'completed') {
            if (showToast) {
                toast.success(`成功刷新 ${result.success_count || 0} 个，失败 ${result.failed_count || 0} 个`);
            }
        } else if (status === 'cancelled') {
            if (showToast) {
                toast.warning(`任务已取消（成功 ${result.success_count || 0}，失败 ${result.failed_count || 0}）`, 5000);
            }
        } else {
            if (showToast) {
                toast.error(`任务执行失败: ${finalTask?.error || finalTask?.message || '未知错误'}`);
            }
        }
        if (reloadAfter) {
            await refreshAccountsView({ settleDelayMs: 80 });
        }
        return {
            ok: status === 'completed',
            status,
            result,
            task: finalTask,
            error: status === 'failed' ? (finalTask?.error || finalTask?.message || '未知错误') : null,
        };
    } catch (error) {
        if (showToast) {
            toast.error(`${sourceLabel}失败: ${error.message}`);
        }
        return {
            ok: false,
            status: 'failed',
            result: null,
            task: null,
            error: error.message,
        };
    } finally {
        trackBatchTask('refresh', null);
        isBatchRefreshing = false;
        updateBatchButtons();
    }
}

function buildQuickRefreshPayload() {
    return {
        ids: [],
        select_all: true,
        ...filterProtocol.toPayload({
            status_filter: currentFilters.status,
            email_service_filter: currentFilters.email_service,
            search_filter: currentFilters.search,
        }),
    };
}

async function runBatchValidateTask(payload, count, sourceLabel, options = {}) {
    const showToast = options.showToast !== false;
    const reloadAfter = options.reloadAfter !== false;
    const onProgress = typeof options.onProgress === 'function' ? options.onProgress : null;
    isBatchValidating = true;
    updateBatchButtons();
    elements.batchValidateBtn.textContent = '验证中...';

    try {
        const task = await api.post('/accounts/batch-validate/async', payload, {
            timeoutMs: 20000,
            retry: 0,
            requestKey: 'accounts:batch-validate:async',
            cancelPrevious: true,
        });
        const taskId = task?.id;
        if (!taskId) {
            throw new Error('任务创建失败：未返回任务 ID');
        }
        trackBatchTask('validate', {
            id: taskId,
            domain: 'accounts',
            status: task?.status || 'pending',
            paused: Boolean(task?.paused),
        });

        if (showToast) {
            toast.info(`${sourceLabel}任务已启动（${taskId.slice(0, 8)}）`);
        }

        const finalTask = await watchAccountTask(taskId, (progressTask) => {
            patchBatchTask('validate', {
                status: progressTask?.status || 'running',
                paused: Boolean(progressTask?.paused),
                pause_requested: Boolean(progressTask?.pause_requested),
            });
            const progress = progressTask?.progress || {};
            const completed = Number(progress.completed || 0);
            const total = Number(progress.total || count);
            const paused = Boolean(progressTask?.paused) || String(progressTask?.status || '').toLowerCase() === 'paused';
            elements.batchValidateBtn.textContent = paused ? `已暂停 ${completed}/${total}` : `验证中 ${completed}/${total}`;
            applyValidatedStatuses(progressTask);
            if (onProgress) {
                onProgress({ completed, total, task: progressTask });
            }
        });
        patchBatchTask('validate', {
            status: finalTask?.status || 'completed',
            paused: false,
            pause_requested: false,
        });

        const result = finalTask?.result || {};
        const status = String(finalTask?.status || '').toLowerCase();
        if (status === 'completed') {
            if (showToast) {
                const workers = Number(result.worker_count || 0);
                const retries = Number(result.retry_count || 0);
                const durationMs = Number(result.duration_ms || 0);
                let message = `有效: ${result.valid_count || 0}，无效: ${result.invalid_count || 0}`;
                if (workers > 0) message += `，并发: ${workers}`;
                if (retries > 0) message += `，重试: ${retries}`;
                if (durationMs > 0) message += `，耗时: ${durationMs}ms`;
                toast.success(message);
            }
        } else if (status === 'cancelled') {
            if (showToast) {
                toast.warning(`任务已取消（有效 ${result.valid_count || 0}，无效 ${result.invalid_count || 0}）`, 5000);
            }
        } else if (showToast) {
            toast.error(`任务执行失败: ${finalTask?.error || finalTask?.message || '未知错误'}`);
        }

        if (reloadAfter) {
            applyValidatedStatuses(finalTask);
            await refreshAccountsView({ settleDelayMs: 80 });
        }
        return {
            ok: status === 'completed',
            status,
            result,
            task: finalTask,
            error: status === 'failed' ? (finalTask?.error || finalTask?.message || '未知错误') : null,
        };
    } catch (error) {
        if (showToast) {
            toast.error(`${sourceLabel}失败: ${error.message}`);
        }
        return {
            ok: false,
            status: 'failed',
            result: null,
            task: null,
            error: error.message,
        };
    } finally {
        trackBatchTask('validate', null);
        isBatchValidating = false;
        updateBatchButtons();
    }
}

async function runBatchCheckSubscriptionTask(payload, count, sourceLabel, options = {}) {
    const showToast = options.showToast !== false;
    const reloadAfter = options.reloadAfter !== false;
    const onProgress = typeof options.onProgress === 'function' ? options.onProgress : null;
    isBatchCheckingSubscription = true;
    updateBatchButtons();
    elements.batchCheckSubBtn.textContent = '检测中...';

    try {
        const task = await api.post('/payment/accounts/batch-check-subscription/async', payload, {
            timeoutMs: 20000,
            retry: 0,
            requestKey: 'payment:batch-check-subscription',
            cancelPrevious: true,
        });
        const taskId = task?.id;
        if (!taskId) {
            throw new Error('任务创建失败：未返回任务 ID');
        }
        trackBatchTask('subscription', {
            id: taskId,
            domain: 'payment',
            status: task?.status || 'pending',
            paused: Boolean(task?.paused),
        });

        if (showToast) {
            toast.info(`${sourceLabel}任务已启动（${taskId.slice(0, 8)}）`);
        }
        const finalTask = await watchPaymentTask(taskId, (progressTask) => {
            patchBatchTask('subscription', {
                status: progressTask?.status || 'running',
                paused: Boolean(progressTask?.paused),
                pause_requested: Boolean(progressTask?.pause_requested),
            });
            const progress = progressTask?.progress || {};
            const completed = Number(progress.completed || 0);
            const total = Number(progress.total || count);
            const paused = Boolean(progressTask?.paused) || String(progressTask?.status || '').toLowerCase() === 'paused';
            elements.batchCheckSubBtn.textContent = paused ? `已暂停 ${completed}/${total}` : `检测中 ${completed}/${total}`;
            if (onProgress) {
                onProgress({ completed, total, task: progressTask });
            }
        });
        patchBatchTask('subscription', {
            status: finalTask?.status || 'completed',
            paused: false,
            pause_requested: false,
        });

        const result = finalTask?.result || {};
        const status = String(finalTask?.status || '').toLowerCase();
        if (status === 'completed') {
            if (showToast) {
                let message = `成功: ${result.success_count || 0}`;
                if ((result.failed_count || 0) > 0) message += `, 失败: ${result.failed_count || 0}`;
                toast.success(message);
            }
        } else if (status === 'cancelled') {
            if (showToast) {
                toast.warning(`任务已取消（成功 ${result.success_count || 0}，失败 ${result.failed_count || 0}）`, 5000);
            }
        } else if (showToast) {
            toast.error(`任务执行失败: ${finalTask?.error || finalTask?.message || '未知错误'}`);
        }

        if (reloadAfter) {
            await refreshAccountsView({ refreshStats: false, settleDelayMs: 80 });
        }
        return {
            ok: status === 'completed',
            status,
            result,
            task: finalTask,
            error: status === 'failed' ? (finalTask?.error || finalTask?.message || '未知错误') : null,
        };
    } catch (error) {
        if (showToast) {
            toast.error(`${sourceLabel}失败: ${error.message}`);
        }
        return {
            ok: false,
            status: 'failed',
            result: null,
            task: null,
            error: error.message,
        };
    } finally {
        trackBatchTask('subscription', null);
        isBatchCheckingSubscription = false;
        updateBatchButtons();
    }
}

async function runOverviewRefreshTask(payload, count, sourceLabel, options = {}) {
    const showToast = options.showToast !== false;
    const reloadAfter = options.reloadAfter !== false;
    const onProgress = typeof options.onProgress === 'function' ? options.onProgress : null;
    isOverviewRefreshing = true;
    updateBatchButtons();

    try {
        const task = await api.post('/accounts/overview/refresh/async', payload, {
            timeoutMs: 20000,
            retry: 0,
            requestKey: 'accounts:overview-refresh',
            cancelPrevious: true,
        });
        const taskId = task?.id;
        if (!taskId) {
            throw new Error('任务创建失败：未返回任务 ID');
        }
        trackBatchTask('overview', {
            id: taskId,
            domain: 'accounts',
            status: task?.status || 'pending',
            paused: Boolean(task?.paused),
        });
        if (showToast) {
            toast.info(`${sourceLabel}任务已启动（${taskId.slice(0, 8)}）`);
        }

        const finalTask = await watchAccountTask(taskId, (progressTask) => {
            patchBatchTask('overview', {
                status: progressTask?.status || 'running',
                paused: Boolean(progressTask?.paused),
                pause_requested: Boolean(progressTask?.pause_requested),
            });
            const progress = progressTask?.progress || {};
            const completed = Number(progress.completed || 0);
            const total = Number(progress.total || count);
            if (onProgress) {
                onProgress({ completed, total, task: progressTask });
            }
        });
        patchBatchTask('overview', {
            status: finalTask?.status || 'completed',
            paused: false,
            pause_requested: false,
        });
        const result = finalTask?.result || {};
        const status = String(finalTask?.status || '').toLowerCase();

        if (status === 'completed') {
            if (showToast) {
                toast.success(`总览刷新完成：成功 ${result.success_count || 0}，失败 ${result.failed_count || 0}`);
            }
        } else if (status === 'cancelled') {
            if (showToast) {
                toast.warning(`任务已取消（成功 ${result.success_count || 0}，失败 ${result.failed_count || 0}）`, 5000);
            }
        } else if (showToast) {
            toast.error(`任务执行失败: ${finalTask?.error || finalTask?.message || '未知错误'}`);
        }

        if (reloadAfter) {
            await refreshAccountsView({ refreshStats: false, settleDelayMs: 80 });
        }
        return {
            ok: status === 'completed',
            status,
            result,
            task: finalTask,
            error: status === 'failed' ? (finalTask?.error || finalTask?.message || '未知错误') : null,
        };
    } catch (error) {
        if (showToast) {
            toast.error(`${sourceLabel}失败: ${error.message}`);
        }
        return {
            ok: false,
            status: 'failed',
            result: null,
            task: null,
            error: error.message,
        };
    } finally {
        trackBatchTask('overview', null);
        isOverviewRefreshing = false;
        updateBatchButtons();
    }
}

async function handleQuickRefreshAll() {
    toast.warning('一键刷新功能已屏蔽，请使用批量验证与批量检测订阅', 4000);
    return;
}

// 批量刷新Token
async function handleBatchRefresh() {
    const count = getEffectiveCount();
    if (count === 0 || isBatchRefreshing) return;

    const confirmed = await confirm(`确定要刷新选中的 ${count} 个账号的Token吗？`);
    if (!confirmed) return;

    await runBatchRefreshTask(buildBatchPayload(), count, '批量刷新');
}

// 批量验证Token
async function handleBatchValidate() {
    const count = getEffectiveCount();
    if (count === 0 || isBatchValidating) return;
    await runBatchValidateTask(buildBatchPayload(), count, '批量验证');
}

// 查看账号详情
async function viewAccount(id) {
    try {
        const account = await api.get(`/accounts/${id}`);
        const tokens = await api.get(`/accounts/${id}/tokens`);

        elements.modalBody.innerHTML = `
            <div class="info-grid">
                <div class="info-item">
                    <span class="label">邮箱</span>
                    <span class="value">
                        ${escapeHtml(account.email)}
                        <button class="btn btn-ghost btn-sm" onclick="copyToClipboard('${escapeHtml(account.email)}')" title="复制">
                            📋
                        </button>
                    </span>
                </div>
                <div class="info-item">
                    <span class="label">密码</span>
                    <span class="value">
                        ${account.password
                            ? `<code style="font-size: 0.75rem;">${escapeHtml(account.password)}</code>
                               <button class="btn btn-ghost btn-sm" onclick="copyToClipboard('${escapeHtml(account.password)}')" title="复制">📋</button>`
                            : '-'}
                    </span>
                </div>
                <div class="info-item">
                    <span class="label">邮箱服务</span>
                    <span class="value">${getServiceTypeText(account.email_service)}</span>
                </div>
                <div class="info-item">
                    <span class="label">账号标签</span>
                    <span class="value">${getAccountLabelText(account.account_label)}</span>
                </div>
                <div class="info-item">
                    <span class="label">状态</span>
                    <span class="value">
                        <span class="status-badge ${getStatusClass('account', account.status)}">
                            ${getStatusText('account', account.status)}
                        </span>
                    </span>
                </div>
                <div class="info-item">
                    <span class="label">注册时间</span>
                    <span class="value">${format.date(account.registered_at)}</span>
                </div>
                <div class="info-item">
                    <span class="label">最后刷新</span>
                    <span class="value">${format.date(account.last_refresh) || '-'}</span>
                </div>
                <div class="info-item" style="grid-column: span 2;">
                    <span class="label">Account ID</span>
                    <span class="value" style="font-size: 0.75rem; word-break: break-all;">
                        ${escapeHtml(account.account_id || '-')}
                    </span>
                </div>
                <div class="info-item" style="grid-column: span 2;">
                    <span class="label">Workspace ID</span>
                    <span class="value" style="font-size: 0.75rem; word-break: break-all;">
                        ${escapeHtml(account.workspace_id || '-')}
                    </span>
                </div>
                <div class="info-item" style="grid-column: span 2;">
                    <span class="label">Client ID</span>
                    <span class="value" style="font-size: 0.75rem; word-break: break-all;">
                        ${escapeHtml(account.client_id || '-')}
                    </span>
                </div>
                <div class="info-item" style="grid-column: span 2;">
                    <span class="label">Access Token</span>
                    <div class="value" style="font-size: 0.7rem; word-break: break-all; font-family: var(--font-mono); background: var(--surface-hover); padding: 8px; border-radius: 4px;">
                        ${escapeHtml(tokens.access_token || '-')}
                        ${tokens.access_token ? `<button class="btn btn-ghost btn-sm" onclick="copyToClipboard('${escapeHtml(tokens.access_token)}')" style="margin-left: 8px;">📋</button>` : ''}
                    </div>
                </div>
                <div class="info-item" style="grid-column: span 2;">
                    <span class="label">Refresh Token</span>
                    <div class="value" style="font-size: 0.7rem; word-break: break-all; font-family: var(--font-mono); background: var(--surface-hover); padding: 8px; border-radius: 4px;">
                        ${escapeHtml(tokens.refresh_token || '-')}
                        ${tokens.refresh_token ? `<button class="btn btn-ghost btn-sm" onclick="copyToClipboard('${escapeHtml(tokens.refresh_token)}')" style="margin-left: 8px;">📋</button>` : ''}
                    </div>
                </div>
                <div class="info-item" style="grid-column: span 2;">
                    <span class="label">Session Token</span>
                    <div class="value" style="font-size: 0.7rem; word-break: break-all; font-family: var(--font-mono); background: var(--surface-hover); padding: 8px; border-radius: 4px;">
                        ${escapeHtml(tokens.session_token || account.session_token || '-')}
                        ${(tokens.session_token || account.session_token)
                            ? `<button class="btn btn-ghost btn-sm" onclick="copyToClipboard('${escapeHtml(tokens.session_token || account.session_token)}')" style="margin-left: 8px;">📋</button>`
                            : ''
                        }
                        <button class="btn btn-ghost btn-sm" onclick="editSessionToken(${id}, '${escapeHtml(tokens.session_token || account.session_token || '')}')" style="margin-left: 8px;" title="修改 Session Token">✏️</button>
                        ${tokens.session_token_source ? `<span style="margin-left:8px;color:var(--text-muted);font-size:0.72rem;">来源: ${escapeHtml(tokens.session_token_source)}</span>` : ''}
                    </div>
                </div>
                <div class="info-item" style="grid-column: span 2;">
                    <span class="label">Device ID</span>
                    <div class="value" style="font-size: 0.75rem; word-break: break-all; font-family: var(--font-mono); background: var(--surface-hover); padding: 8px; border-radius: 4px;">
                        ${escapeHtml(tokens.device_id || account.device_id || '-')}
                        ${(tokens.device_id || account.device_id) ? `<button class="btn btn-ghost btn-sm" onclick="copyToClipboard('${escapeHtml(tokens.device_id || account.device_id)}')" style="margin-left: 8px;">📋</button>` : ''}
                    </div>
                </div>
                <div class="info-item" style="grid-column: span 2;">
                    <span class="label">Cookies（支付用）</span>
                    <div class="value">
                        <textarea id="cookies-input-${id}" rows="3"
                            style="width:100%;font-size:0.7rem;font-family:var(--font-mono);background:var(--surface-hover);border:1px solid var(--border);border-radius:4px;padding:6px;color:var(--text-primary);resize:vertical;"
                            placeholder="粘贴完整 cookie 字符串，留空则清除">${escapeHtml(account.cookies || '')}</textarea>
                        <button class="btn btn-secondary btn-sm" style="margin-top:4px" onclick="saveCookies(${id})">
                            保存 Cookies
                        </button>
                    </div>
                </div>
            </div>
            <div style="margin-top: var(--spacing-lg); display: flex; gap: var(--spacing-sm);">
                <button class="btn btn-primary" onclick="refreshToken(${id}); elements.detailModal.classList.remove('active');">
                    🔄 刷新Token
                </button>
            </div>
        `;

        elements.detailModal.classList.add('active');
    } catch (error) {
        toast.error('加载账号详情失败: ' + error.message);
    }
}

async function bootstrapSessionToken(id) {
    try {
        const result = await api.post(`/payment/accounts/${id}/session-bootstrap`, {});
        if (result && result.success) {
            toast.success('Session Token 补全成功');
        } else {
            toast.warning(result?.message || '未补全到 Session Token');
        }
    } catch (error) {
        toast.error('补全 Session Token 失败: ' + error.message);
    } finally {
        await viewAccount(id);
        loadAccounts();
    }
}

async function editSessionToken(id, currentToken = '') {
    const current = String(currentToken || '');
    const nextToken = window.prompt('请输入新的 Session Token（留空将清空）', current);
    if (nextToken === null) return;
    try {
        await api.patch(`/accounts/${id}`, { session_token: String(nextToken).trim() });
        toast.success('Session Token 已更新');
    } catch (error) {
        toast.error('更新 Session Token 失败: ' + error.message);
    } finally {
        await viewAccount(id);
        loadAccounts();
    }
}

// 复制邮箱
function copyEmail(email) {
    copyToClipboard(email);
}

// 删除账号
async function deleteAccount(id, email) {
    const confirmed = await confirm(`确定要删除账号 ${email} 吗？此操作不可恢复。`);
    if (!confirmed) return;

    try {
        await api.delete(`/accounts/${id}`);
        toast.success('账号已删除');
        selectedAccounts.delete(id);
        loadStats();
        loadAccounts();
    } catch (error) {
        toast.error('删除失败: ' + error.message);
    }
}

// 批量删除
async function handleBatchDelete() {
    const count = getEffectiveCount();
    if (count === 0) return;

    const confirmed = await confirm(`确定要删除选中的 ${count} 个账号吗？此操作不可恢复。`);
    if (!confirmed) return;

    try {
        const result = await api.post('/accounts/batch-delete', buildBatchPayload());
        toast.success(`成功删除 ${result.deleted_count} 个账号`);
        selectedAccounts.clear();
        selectAllPages = false;
        loadStats();
        loadAccounts();
    } catch (error) {
        toast.error('删除失败: ' + error.message);
    }
}

// 导出账号
async function exportAccounts(format) {
    const count = getEffectiveCount();
    if (count === 0) {
        toast.warning('请先选择要导出的账号');
        return;
    }

    toast.info(`正在导出 ${count} 个账号...`);

    try {
        const response = await fetch('/api/accounts/export/' + format, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(buildBatchPayload())
        });

        if (!response.ok) {
            throw new Error(`导出失败: HTTP ${response.status}`);
        }

        // 获取文件内容
        const blob = await response.blob();

        // 从 Content-Disposition 获取文件名
        const disposition = response.headers.get('Content-Disposition');
        let filename = `accounts_${Date.now()}.${(format === 'cpa' || format === 'sub2api') ? 'json' : (format === 'codex' ? 'jsonl' : format)}`;
        if (disposition) {
            const match = disposition.match(/filename=(.+)/);
            if (match) {
                filename = match[1];
            }
        }

        // 创建下载链接
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        a.remove();

        toast.success('导出成功');
    } catch (error) {
        console.error('导出失败:', error);
        toast.error('导出失败: ' + error.message);
    }
}

// HTML 转义
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ============== CPA 服务选择 ==============

// 弹出 CPA 服务选择框，返回 Promise<{cpa_service_id: number|null}|null>
// null 表示用户取消，{cpa_service_id: null} 表示使用全局配置
function selectCpaService() {
    return new Promise(async (resolve) => {
        const modal = document.getElementById('cpa-service-modal');
        const listEl = document.getElementById('cpa-service-list');
        const closeBtn = document.getElementById('close-cpa-modal');
        const cancelBtn = document.getElementById('cancel-cpa-modal-btn');
        const globalBtn = document.getElementById('cpa-use-global-btn');

        // 加载服务列表
        listEl.innerHTML = '<div style="text-align:center;color:var(--text-muted)">加载中...</div>';
        modal.classList.add('active');

        let services = [];
        try {
            services = await api.get('/cpa-services?enabled=true');
        } catch (e) {
            services = [];
        }

        if (services.length === 0) {
            listEl.innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:12px;">暂无已启用的 CPA 服务，将使用全局配置</div>';
        } else {
            listEl.innerHTML = services.map(s => `
                <div class="cpa-service-item" data-id="${s.id}" style="
                    padding: 10px 14px;
                    border: 1px solid var(--border);
                    border-radius: 8px;
                    cursor: pointer;
                    transition: background 0.15s;
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                ">
                    <div>
                        <div style="font-weight:500;">${escapeHtml(s.name)}</div>
                        <div style="font-size:0.8rem;color:var(--text-muted);">${escapeHtml(s.api_url)}</div>
                    </div>
                    <span class="badge" style="background:var(--success-color);color:#fff;font-size:0.7rem;padding:2px 8px;border-radius:10px;">选择</span>
                </div>
            `).join('');

            listEl.querySelectorAll('.cpa-service-item').forEach(item => {
                item.addEventListener('mouseenter', () => item.style.background = 'var(--surface-hover)');
                item.addEventListener('mouseleave', () => item.style.background = '');
                item.addEventListener('click', () => {
                    cleanup();
                    resolve({ cpa_service_id: parseInt(item.dataset.id) });
                });
            });
        }

        function cleanup() {
            modal.classList.remove('active');
            closeBtn.removeEventListener('click', onCancel);
            cancelBtn.removeEventListener('click', onCancel);
            globalBtn.removeEventListener('click', onGlobal);
        }
        function onCancel() { cleanup(); resolve(null); }
        function onGlobal() { cleanup(); resolve({ cpa_service_id: null }); }

        closeBtn.addEventListener('click', onCancel);
        cancelBtn.addEventListener('click', onCancel);
        globalBtn.addEventListener('click', onGlobal);
    });
}

function normalizeAccountLabel(value) {
    const text = String(value || '').trim().toLowerCase();
    if (text === 'mother' || text === 'parent' || text === 'manager') return 'mother';
    if (text === 'child' || text === 'member') return 'child';
    if (text === '普通' || text === 'normal') return 'none';
    return 'none';
}

function getAccountLabelText(value) {
    const normalized = normalizeAccountLabel(value);
    if (normalized === 'mother') return '母号';
    if (normalized === 'child') return '子号';
    return '普通';
}

function renderAccountLabelBadge(value) {
    const normalized = normalizeAccountLabel(value);
    if (normalized === 'none') return '';
    return `<span class="account-label-badge ${normalized}" title="${getAccountLabelText(normalized)}">${getAccountLabelText(normalized)}</span>`;
}

// 统一上传入口：弹出目标选择
async function uploadAccount(id) {
    const targets = [
        { label: '☁️ 上传到 CPA', value: 'cpa' },
        { label: '🔗 上传到 Sub2API', value: 'sub2api' },
        { label: '🚀 上传到 Team Manager', value: 'tm' },
    ];

    const choice = await new Promise((resolve) => {
        const modal = document.createElement('div');
        modal.className = 'modal active';
        modal.innerHTML = `
            <div class="modal-content" style="max-width:360px;">
                <div class="modal-header">
                    <h3>☁️ 选择上传目标</h3>
                    <button class="modal-close" id="_upload-close">&times;</button>
                </div>
                <div class="modal-body" style="display:flex;flex-direction:column;gap:8px;">
                    ${targets.map(t => `
                        <button class="btn btn-secondary" data-val="${t.value}" style="text-align:left;">${t.label}</button>
                    `).join('')}
                </div>
            </div>`;
        document.body.appendChild(modal);
        modal.querySelector('#_upload-close').addEventListener('click', () => { modal.remove(); resolve(null); });
        modal.addEventListener('click', (e) => { if (e.target === modal) { modal.remove(); resolve(null); } });
        modal.querySelectorAll('button[data-val]').forEach(btn => {
            btn.addEventListener('click', () => { modal.remove(); resolve(btn.dataset.val); });
        });
    });

    if (!choice) return;
    if (choice === 'cpa') return uploadToCpa(id);
    if (choice === 'sub2api') return uploadToSub2Api(id);
    if (choice === 'tm') return uploadToTm(id);
}

// 上传单个账号到CPA
async function uploadToCpa(id) {
    const choice = await selectCpaService();
    if (choice === null) return;  // 用户取消

    try {
        toast.info('正在上传到CPA...');
        const payload = {};
        if (choice.cpa_service_id != null) payload.cpa_service_id = choice.cpa_service_id;
        const result = await api.post(`/accounts/${id}/upload-cpa`, payload);

        if (result.success) {
            toast.success('上传成功');
            loadAccounts();
        } else {
            toast.error('上传失败: ' + (result.error || '未知错误'));
        }
    } catch (error) {
        toast.error('上传失败: ' + error.message);
    }
}

// 批量上传到CPA
async function handleBatchUploadCpa() {
    const count = getEffectiveCount();
    if (count === 0) return;

    const choice = await selectCpaService();
    if (choice === null) return;  // 用户取消

    const confirmed = await confirm(`确定要将选中的 ${count} 个账号上传到CPA吗？`);
    if (!confirmed) return;

    elements.batchUploadBtn.disabled = true;
    elements.batchUploadBtn.textContent = '上传中...';

    try {
        const payload = buildBatchPayload();
        if (choice.cpa_service_id != null) payload.cpa_service_id = choice.cpa_service_id;
        const result = await api.post('/accounts/batch-upload-cpa', payload);

        let message = `成功: ${result.success_count}`;
        if (result.failed_count > 0) message += `, 失败: ${result.failed_count}`;
        if (result.skipped_count > 0) message += `, 跳过: ${result.skipped_count}`;

        toast.success(message);
        loadAccounts();
    } catch (error) {
        toast.error('批量上传失败: ' + error.message);
    } finally {
        updateBatchButtons();
    }
}

// ============== 订阅状态 ==============

function accountLabelToRoleTag(value) {
    const normalized = normalizeAccountLabel(value);
    if (normalized === 'mother') return 'parent';
    if (normalized === 'child') return 'child';
    return 'none';
}

// 手动标记账号标签
async function markAccountLabel(id, currentLabel = 'none') {
    const current = normalizeAccountLabel(currentLabel);
    const defaultValue = current === 'mother' ? 'mother' : (current === 'child' ? 'child' : 'none');
    const input = prompt('请输入账号标号（mother=母号 / child=子号 / none=普通）:', defaultValue);
    if (input === null) return;

    const normalized = normalizeAccountLabel(input);
    const roleTag = accountLabelToRoleTag(normalized);
    const nextText = getAccountLabelText(normalized);

    const confirmed = await confirm(`确认将账号标号修改为「${nextText}」吗？`);
    if (!confirmed) return;

    try {
        await api.patch(`/accounts/${id}`, { role_tag: roleTag });
        toast.success(`账号标号已更新为 ${nextText}`);
        loadAccounts();
    } catch (e) {
        toast.error('更新标号失败: ' + e.message);
    }
}

// 手动标记订阅类型
async function markSubscription(id) {
    const type = prompt('请输入订阅类型 (plus / team / free):', 'plus');
    if (!type) return;
    if (!['plus', 'team', 'free'].includes(type.trim().toLowerCase())) {
        toast.error('无效的订阅类型，请输入 plus、team 或 free');
        return;
    }
    try {
        await api.post(`/payment/accounts/${id}/mark-subscription`, {
            subscription_type: type.trim().toLowerCase()
        });
        toast.success('订阅状态已更新');
        loadAccounts();
    } catch (e) {
        toast.error('标记失败: ' + e.message);
    }
}

// 批量检测订阅状态
async function handleBatchCheckSubscription() {
    const count = getEffectiveCount();
    if (count === 0 || isBatchCheckingSubscription) return;
    const confirmed = await confirm(`确定要检测选中的 ${count} 个账号的订阅状态吗？`);
    if (!confirmed) return;
    await runBatchCheckSubscriptionTask(buildBatchPayload(), count, '批量检测订阅');
}

// ============== Sub2API 上传 ==============

// 弹出 Sub2API 服务选择框，返回 Promise<{service_id: number|null}|null>
// null 表示用户取消，{service_id: null} 表示自动选择
function selectSub2ApiService() {
    return new Promise(async (resolve) => {
        const modal = document.getElementById('sub2api-service-modal');
        const listEl = document.getElementById('sub2api-service-list');
        const closeBtn = document.getElementById('close-sub2api-modal');
        const cancelBtn = document.getElementById('cancel-sub2api-modal-btn');
        const autoBtn = document.getElementById('sub2api-use-auto-btn');

        listEl.innerHTML = '<div style="text-align:center;color:var(--text-muted)">加载中...</div>';
        modal.classList.add('active');

        let services = [];
        try {
            services = await api.get('/sub2api-services?enabled=true');
        } catch (e) {
            services = [];
        }

        if (services.length === 0) {
            listEl.innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:12px;">暂无已启用的 Sub2API 服务，将自动选择第一个</div>';
        } else {
            listEl.innerHTML = services.map(s => `
                <div class="sub2api-service-item" data-id="${s.id}" style="
                    padding: 10px 14px;
                    border: 1px solid var(--border);
                    border-radius: 8px;
                    cursor: pointer;
                    transition: background 0.15s;
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                ">
                    <div>
                        <div style="font-weight:500;">${escapeHtml(s.name)}</div>
                        <div style="font-size:0.8rem;color:var(--text-muted);">${escapeHtml(s.api_url)}</div>
                    </div>
                    <span class="badge" style="background:var(--primary);color:#fff;font-size:0.7rem;padding:2px 8px;border-radius:10px;">选择</span>
                </div>
            `).join('');

            listEl.querySelectorAll('.sub2api-service-item').forEach(item => {
                item.addEventListener('mouseenter', () => item.style.background = 'var(--surface-hover)');
                item.addEventListener('mouseleave', () => item.style.background = '');
                item.addEventListener('click', () => {
                    cleanup();
                    resolve({ service_id: parseInt(item.dataset.id) });
                });
            });
        }

        function cleanup() {
            modal.classList.remove('active');
            closeBtn.removeEventListener('click', onCancel);
            cancelBtn.removeEventListener('click', onCancel);
            autoBtn.removeEventListener('click', onAuto);
        }
        function onCancel() { cleanup(); resolve(null); }
        function onAuto() { cleanup(); resolve({ service_id: null }); }

        closeBtn.addEventListener('click', onCancel);
        cancelBtn.addEventListener('click', onCancel);
        autoBtn.addEventListener('click', onAuto);
    });
}

// 批量上传到 Sub2API
async function handleBatchUploadSub2Api() {
    const count = getEffectiveCount();
    if (count === 0) return;

    const choice = await selectSub2ApiService();
    if (choice === null) return;  // 用户取消

    const confirmed = await confirm(`确定要将选中的 ${count} 个账号上传到 Sub2API 吗？`);
    if (!confirmed) return;

    elements.batchUploadBtn.disabled = true;
    elements.batchUploadBtn.textContent = '上传中...';

    try {
        const payload = buildBatchPayload();
        if (choice.service_id != null) payload.service_id = choice.service_id;
        const result = await api.post('/accounts/batch-upload-sub2api', payload);

        let message = `成功: ${result.success_count}`;
        if (result.failed_count > 0) message += `, 失败: ${result.failed_count}`;
        if (result.skipped_count > 0) message += `, 跳过: ${result.skipped_count}`;

        toast.success(message);
        loadAccounts();
    } catch (error) {
        toast.error('批量上传失败: ' + error.message);
    } finally {
        updateBatchButtons();
    }
}

// ============== Team Manager 上传 ==============

// 上传单账号到 Sub2API
async function uploadToSub2Api(id) {
    const choice = await selectSub2ApiService();
    if (choice === null) return;
    try {
        toast.info('正在上传到 Sub2API...');
        const payload = {};
        if (choice.service_id != null) payload.service_id = choice.service_id;
        const result = await api.post(`/accounts/${id}/upload-sub2api`, payload);
        if (result.success) {
            toast.success('上传成功');
            loadAccounts();
        } else {
            toast.error('上传失败: ' + (result.error || result.message || '未知错误'));
        }
    } catch (e) {
        toast.error('上传失败: ' + e.message);
    }
}

// 弹出 Team Manager 服务选择框，返回 Promise<{service_id: number|null}|null>
// null 表示用户取消，{service_id: null} 表示自动选择
function selectTmService() {
    return new Promise(async (resolve) => {
        const modal = document.getElementById('tm-service-modal');
        const listEl = document.getElementById('tm-service-list');
        const closeBtn = document.getElementById('close-tm-modal');
        const cancelBtn = document.getElementById('cancel-tm-modal-btn');
        const autoBtn = document.getElementById('tm-use-auto-btn');

        listEl.innerHTML = '<div style="text-align:center;color:var(--text-muted)">加载中...</div>';
        modal.classList.add('active');

        let services = [];
        try {
            services = await api.get('/tm-services?enabled=true');
        } catch (e) {
            services = [];
        }

        if (services.length === 0) {
            listEl.innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:12px;">暂无已启用的 Team Manager 服务，将自动选择第一个</div>';
        } else {
            listEl.innerHTML = services.map(s => `
                <div class="tm-service-item" data-id="${s.id}" style="
                    padding: 10px 14px;
                    border: 1px solid var(--border);
                    border-radius: 8px;
                    cursor: pointer;
                    transition: background 0.15s;
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                ">
                    <div>
                        <div style="font-weight:500;">${escapeHtml(s.name)}</div>
                        <div style="font-size:0.8rem;color:var(--text-muted);">${escapeHtml(s.api_url)}</div>
                    </div>
                    <span class="badge" style="background:var(--primary);color:#fff;font-size:0.7rem;padding:2px 8px;border-radius:10px;">选择</span>
                </div>
            `).join('');

            listEl.querySelectorAll('.tm-service-item').forEach(item => {
                item.addEventListener('mouseenter', () => item.style.background = 'var(--surface-hover)');
                item.addEventListener('mouseleave', () => item.style.background = '');
                item.addEventListener('click', () => {
                    cleanup();
                    resolve({ service_id: parseInt(item.dataset.id) });
                });
            });
        }

        function cleanup() {
            modal.classList.remove('active');
            closeBtn.removeEventListener('click', onCancel);
            cancelBtn.removeEventListener('click', onCancel);
            autoBtn.removeEventListener('click', onAuto);
        }
        function onCancel() { cleanup(); resolve(null); }
        function onAuto() { cleanup(); resolve({ service_id: null }); }

        closeBtn.addEventListener('click', onCancel);
        cancelBtn.addEventListener('click', onCancel);
        autoBtn.addEventListener('click', onAuto);
    });
}

// 上传单账号到 Team Manager
async function uploadToTm(id) {
    const choice = await selectTmService();
    if (choice === null) return;
    try {
        toast.info('正在上传到 Team Manager...');
        const payload = {};
        if (choice.service_id != null) payload.service_id = choice.service_id;
        const result = await api.post(`/accounts/${id}/upload-tm`, payload);
        if (result.success) {
            toast.success('上传成功');
        } else {
            toast.error('上传失败: ' + (result.message || '未知错误'));
        }
    } catch (e) {
        toast.error('上传失败: ' + e.message);
    }
}

// 批量上传到 Team Manager
async function handleBatchUploadTm() {
    const count = getEffectiveCount();
    if (count === 0) return;

    const choice = await selectTmService();
    if (choice === null) return;  // 用户取消

    const confirmed = await confirm(`确定要将选中的 ${count} 个账号上传到 Team Manager 吗？`);
    if (!confirmed) return;

    elements.batchUploadBtn.disabled = true;
    elements.batchUploadBtn.textContent = '上传中...';

    try {
        const payload = buildBatchPayload();
        if (choice.service_id != null) payload.service_id = choice.service_id;
        const result = await api.post('/accounts/batch-upload-tm', payload);
        let message = `成功: ${result.success_count}`;
        if (result.failed_count > 0) message += `, 失败: ${result.failed_count}`;
        if (result.skipped_count > 0) message += `, 跳过: ${result.skipped_count}`;
        toast.success(message);
        loadAccounts();
    } catch (e) {
        toast.error('批量上传失败: ' + e.message);
    } finally {
        updateBatchButtons();
    }
}

// 更多菜单切换
function toggleMoreMenu(btn) {
    const menu = btn.nextElementSibling;
    const isActive = menu.classList.contains('active');
    // 关闭所有其他更多菜单
    document.querySelectorAll('.dropdown-menu.active').forEach(m => m.classList.remove('active'));
    if (!isActive) menu.classList.add('active');
}

function closeMoreMenu(el) {
    const menu = el.closest('.dropdown-menu');
    if (menu) menu.classList.remove('active');
}

// 保存账号 Cookies
async function saveCookies(id) {
    const textarea = document.getElementById(`cookies-input-${id}`);
    if (!textarea) return;
    const cookiesValue = textarea.value.trim();
    try {
        await api.patch(`/accounts/${id}`, { cookies: cookiesValue });
        toast.success('Cookies 已保存');
    } catch (e) {
        toast.error('保存 Cookies 失败: ' + e.message);
    }
}

// 查询收件箱验证码
async function checkInboxCode(id) {
    toast.info('正在查询收件箱...');
    try {
        const result = await api.post(`/accounts/${id}/inbox-code`);
        if (result.success) {
            showInboxCodeResult(result.code, result.email);
        } else {
            toast.error('查询失败: ' + (result.error || '未收到验证码'));
        }
    } catch (error) {
        toast.error('查询失败: ' + error.message);
    }
}

function showInboxCodeResult(code, email) {
    elements.modalBody.innerHTML = `
        <div style="text-align:center; padding:24px 16px;">
            <div style="font-size:13px;color:var(--text-muted);margin-bottom:12px;">
                ${escapeHtml(email)} 最新验证码
            </div>
            <div style="font-size:36px;font-weight:700;letter-spacing:8px;
                        color:var(--primary);font-family:monospace;margin-bottom:20px;">
                ${escapeHtml(code)}
            </div>
            <button class="btn btn-primary" onclick="copyToClipboard('${escapeHtml(code)}')">复制验证码</button>
        </div>
    `;
    elements.detailModal.classList.add('active');
}
