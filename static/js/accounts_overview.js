/**
 * 账号总览页面脚本
 * 上半区：原有总览布局
 * 下半区：卡片管理布局（按设计图增强）
 */

const VIEW_MODE_STORAGE_KEY = 'accounts_overview_view_mode';
const AUTO_REFRESH_SCOPE_STORAGE_KEY = 'accounts_overview_auto_refresh_scope_v1';

const overviewApi = {
    loadStats() {
        return api.get('/accounts/stats/overview', {
            requestKey: 'overview:stats',
            cancelPrevious: true,
            retry: 1,
        });
    },
    loadCards() {
        return api.get('/accounts/overview/cards', {
            requestKey: 'overview:cards',
            cancelPrevious: true,
            retry: 1,
            timeoutMs: 15000,
        });
    },
    startRefreshTask(payload) {
        return api.post('/accounts/overview/refresh/async', payload, {
            timeoutMs: 20000,
            retry: 0,
            requestKey: 'overview:refresh-task',
            cancelPrevious: true,
        });
    },
    refreshSingle(payload) {
        return api.post('/accounts/overview/refresh', payload, {
            timeoutMs: 60000,
            retry: 0,
            requestKey: `overview:refresh-single:${Number(payload?.ids?.[0] || 0)}`,
            cancelPrevious: true,
        });
    },
    loadSelectable() {
        return api.get('/accounts/overview/cards/selectable', {
            requestKey: 'overview:addable',
            cancelPrevious: true,
            retry: 1,
        });
    },
    removeCards(payload) {
        return api.post('/accounts/overview/cards/remove', payload, {
            timeoutMs: 20000,
            retry: 0,
        });
    },
    attachCard(id) {
        return api.post(`/accounts/overview/cards/${id}/attach`, {}, {
            timeoutMs: 15000,
            retry: 0,
        });
    },
};

const overviewState = {
    summary: null,
    cards: [],
    filteredCards: [],
    selectedCardIds: new Set(),
    addableCards: [],
    viewMode: storage.get(VIEW_MODE_STORAGE_KEY, 'grid') || 'grid',
    planFilter: 'all',
    sortMode: 'created_desc',
    cardRefreshIntervalMin: 7,
    cardRefreshTimer: null,
    cardCountdownTimer: null,
    cardNextRefreshAt: null,
    isBulkRefreshing: false,
    cardAutoRefreshScope: storage.get(AUTO_REFRESH_SCOPE_STORAGE_KEY, 'stale_failed') || 'stale_failed',
};

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

async function watchOverviewTask(taskId, onUpdate, maxWaitMs = 20 * 60 * 1000) {
    const startedAt = Date.now();
    while (Date.now() - startedAt < maxWaitMs) {
        const task = await api.get(`/accounts/tasks/${taskId}`, {
            requestKey: `overview:task:${taskId}`,
            cancelPrevious: true,
            retry: 0,
            timeoutMs: 30000,
        });

        if (typeof onUpdate === 'function') {
            onUpdate(task);
        }

        const status = String(task?.status || '').toLowerCase();
        if (['completed', 'failed', 'cancelled'].includes(status)) {
            return task;
        }
        await sleep(1200);
    }
    throw new Error('任务等待超时，请稍后刷新查看结果');
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text ?? '';
    return div.innerHTML;
}

function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = format.number(value || 0);
}

function toSortedEntries(data) {
    return Object.entries(data || {}).sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0));
}

const SERVICE_DIST_PALETTE = [
    '#3b82f6', // blue
    '#2563eb', // deep blue
    '#4f46e5', // indigo
    '#8b5cf6', // violet
    '#06b6d4', // cyan
    '#2f80ff', // azure
    '#7c3aed', // purple
];

function hashText(text) {
    const str = String(text || '');
    let hash = 0;
    for (let i = 0; i < str.length; i += 1) {
        hash = ((hash << 5) - hash) + str.charCodeAt(i);
        hash |= 0;
    }
    return Math.abs(hash);
}

function getDistributionBarColor(containerId, key, index) {
    const value = String(key || '').trim().toLowerCase();

    if (containerId === 'dist-subscription') {
        if (value.includes('team')) return '#8b5cf6'; // team: purple
        if (value.includes('plus')) return '#2f80ff';
        if (value.includes('pro')) return '#0ea5e9';
        if (value.includes('free')) return '#94a3b8';
    }

    if (containerId === 'dist-status') {
        if (value === 'active') return '#f59e0b'; // active: orange
        if (value === 'failed' || value === 'banned') return '#ef4444';
        if (value === 'expired') return '#64748b';
        return '#3b82f6';
    }

    if (containerId === 'dist-service') {
        const paletteIndex = hashText(value || index) % SERVICE_DIST_PALETTE.length;
        return SERVICE_DIST_PALETTE[paletteIndex];
    }

    if (containerId === 'dist-source') {
        if (value === 'register') return '#3b82f6';
        if (value === 'login') return '#8b5cf6';
    }

    return '#3b82f6';
}

function renderDistribution(containerId, data, labelFormatter) {
    const container = document.getElementById(containerId);
    if (!container) return;

    const entries = toSortedEntries(data);
    if (!entries.length) {
        container.innerHTML = '<div class="empty-state" style="padding: 16px 0;">暂无数据</div>';
        return;
    }

    const maxValue = Math.max(...entries.map(([, count]) => Number(count || 0)), 1);
    container.innerHTML = entries.map(([key, count], index) => {
        const value = Number(count || 0);
        const width = Math.max((value / maxValue) * 100, 2);
        const label = labelFormatter ? labelFormatter(key) : key;
        const barColor = getDistributionBarColor(containerId, key, index);
        return `
            <div class="distribution-row">
                <div class="distribution-label" title="${escapeHtml(label)}">${escapeHtml(label)}</div>
                <div class="distribution-bar-wrap"><div class="distribution-bar" style="width:${width}%;background:${barColor};"></div></div>
                <div class="distribution-value">${format.number(value)}</div>
            </div>
        `;
    }).join('');
}

function formatSource(value) {
    const sourceMap = {
        register: '注册',
        login: '登录',
        unknown: '未知',
    };
    return sourceMap[value] || value || '-';
}

function formatSubscription(value) {
    const key = (value || 'free').toLowerCase();
    const map = {
        free: '免费',
        plus: 'Plus',
        team: 'Team',
        pro: 'Pro',
    };
    return map[key] || key;
}

function normalizePlan(planType) {
    const value = String(planType || '').toLowerCase();
    if (value.includes('team')) return 'team';
    if (value.includes('plus')) return 'plus';
    if (value.includes('pro')) return 'pro';
    if (value.includes('free') || value.includes('basic')) return 'free';
    return 'free';
}

function getPlanText(planType) {
    const plan = normalizePlan(planType);
    if (plan === 'team') return 'TEAM';
    if (plan === 'plus') return 'PLUS';
    if (plan === 'pro') return 'PRO';
    return 'FREE';
}

function getPlanClass(planType) {
    const plan = normalizePlan(planType);
    if (plan === 'team') return 'team';
    if (plan === 'plus' || plan === 'pro') return 'plus';
    return 'free';
}

function parsePercent(quota) {
    const raw = quota?.percentage;
    if (raw === null || raw === undefined || Number.isNaN(Number(raw))) return null;
    return Math.max(0, Math.min(100, Number(raw)));
}

function getProgressTone(percentage) {
    if (percentage === null) {
        return { valueClass: 'tone-gray', barClass: 'tone-gray-bar' };
    }
    if (percentage < 10) {
        return { valueClass: 'tone-red', barClass: 'tone-red-bar' };
    }
    if (percentage >= 90) {
        return { valueClass: 'tone-green', barClass: 'tone-green-bar' };
    }
    return { valueClass: 'tone-orange', barClass: 'tone-orange-bar' };
}

function parseTimeMs(value) {
    const ms = Date.parse(value || '');
    return Number.isNaN(ms) ? null : ms;
}

function formatMinuteCountdown(targetMs) {
    if (!targetMs) return '-';
    const diffMs = targetMs - Date.now();
    if (diffMs <= 0) return '0分';

    const totalMinutes = Math.max(1, Math.ceil(diffMs / 60000));
    const days = Math.floor(totalMinutes / 1440);
    const hours = Math.floor((totalMinutes % 1440) / 60);
    const mins = totalMinutes % 60;

    if (days > 0) return `${days}天${hours}小时${mins}分`;
    if (hours > 0) return `${hours}小时${mins}分`;
    return `${mins}分`;
}

function formatQuotaResetText(quota) {
    const resetAt = parseTimeMs(quota?.reset_at);
    const percent = parsePercent(quota);
    if (resetAt) {
        const absolute = format.date(new Date(resetAt).toISOString());
        if (percent !== null && percent <= 0) {
            return `已用尽，下次 ${absolute}`;
        }
        return `${formatMinuteCountdown(resetAt)} (${absolute})`;
    }
    return quota?.reset_in_text || '-';
}

function renderQuotaItem(title, quota) {
    const percent = parsePercent(quota);
    const tone = getProgressTone(percent);
    const showPercent = percent === null ? '--' : `${Math.round(percent)}%`;
    const width = percent === null ? 0 : percent;
    const resetText = formatQuotaResetText(quota);

    return `
        <div class="quota-item">
            <div class="quota-row">
                <span>${escapeHtml(title)}</span>
                <span class="quota-value ${tone.valueClass}">${showPercent}</span>
            </div>
            <div class="quota-bar"><span class="${tone.barClass}" style="width:${width}%"></span></div>
            <div class="quota-reset">重置: ${escapeHtml(resetText)}</div>
        </div>
    `;
}

function renderRecentAccounts(accounts) {
    const tbody = document.getElementById('recent-accounts-table');
    if (!tbody) return;

    if (!accounts || !accounts.length) {
        tbody.innerHTML = `
            <tr>
                <td colspan="8"><div class="empty-state">暂无账号数据</div></td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = accounts.map((item) => `
        <tr>
            <td>${item.id || '-'}</td>
            <td>${escapeHtml(item.email || '-')}</td>
            <td><span class="status-badge ${getStatusClass('account', item.status)}">${escapeHtml(getStatusText('account', item.status) || '-')}</span></td>
            <td>${escapeHtml(getServiceTypeText(item.email_service || '') || '-')}</td>
            <td>${escapeHtml(formatSource(item.source))}</td>
            <td>${escapeHtml(formatSubscription(item.subscription_type))}</td>
            <td>${format.date(item.created_at)}</td>
            <td>${format.date(item.last_refresh)}</td>
        </tr>
    `).join('');
}

function setToolbarRefreshLoading(loading) {
    const button = document.getElementById('card-refresh-btn');
    if (!button) return;
    button.disabled = Boolean(loading);
    button.classList.toggle('is-loading', Boolean(loading));
    button.setAttribute('aria-busy', loading ? 'true' : 'false');
}

async function loadLegacyOverview() {
    try {
        const data = await overviewApi.loadStats();
        overviewState.summary = data;

        setText('ov-total', data.total);
        setText('ov-active', data.active_count);
        setText('ov-access-token', data.token_stats?.with_access_token || 0);
        setText('ov-cpa-uploaded', data.cpa_uploaded_count);

        renderDistribution('dist-status', data.by_status, (status) => getStatusText('account', status));
        renderDistribution('dist-service', data.by_email_service, (service) => getServiceTypeText(service));
        renderDistribution('dist-subscription', data.by_subscription, formatSubscription);
        renderDistribution('dist-source', data.by_source, formatSource);
        renderRecentAccounts(data.recent_accounts || []);
    } catch (error) {
        toast.error(`加载总览统计失败: ${error.message || '未知错误'}`);
    }
}

function updatePlanFilterOptions() {
    const select = document.getElementById('card-plan-filter');
    if (!select) return;

    const sourceCards = overviewState.cards;
    const total = sourceCards.length;
    const freeCount = sourceCards.filter((item) => normalizePlan(item.plan_type) === 'free').length;
    const plusCount = sourceCards.filter((item) => normalizePlan(item.plan_type) === 'plus').length;
    const teamCount = sourceCards.filter((item) => normalizePlan(item.plan_type) === 'team').length;
    const currentCount = sourceCards.filter((item) => Boolean(item.current)).length;

    const currentValue = overviewState.planFilter;
    select.innerHTML = `
        <option value="all">全部 (${total})</option>
        <option value="free">Free (${freeCount})</option>
        <option value="plus">Plus (${plusCount})</option>
        <option value="team">Team (${teamCount})</option>
        <option value="current">当前账号 (${currentCount})</option>
    `;
    select.value = ['all', 'free', 'plus', 'team', 'current'].includes(currentValue) ? currentValue : 'all';
}

function syncSelectedCardIds() {
    const existing = new Set(
        overviewState.cards
            .map((item) => Number(item.id))
            .filter((id) => Number.isFinite(id))
    );
    const next = new Set();
    for (const id of overviewState.selectedCardIds) {
        if (existing.has(id)) next.add(id);
    }
    overviewState.selectedCardIds = next;
}

function updateSelectionInfo() {
    const info = document.getElementById('card-selection-info');
    if (!info) return;
    const selected = overviewState.selectedCardIds.size;
    const total = overviewState.filteredCards.length;
    info.textContent = `已选择 ${selected} 个账号 / 当前列表 ${total} 个`;
}

function parseDateScore(value) {
    const ts = Date.parse(value || '');
    return Number.isNaN(ts) ? 0 : ts;
}

function sortCards(cards) {
    const rows = [...cards];
    const sortMode = overviewState.sortMode;

    rows.sort((a, b) => {
        if (sortMode === 'created_asc') {
            return parseDateScore(a.created_at) - parseDateScore(b.created_at);
        }
        if (sortMode === 'hourly_desc') {
            return (parsePercent(b.hourly_quota) ?? -1) - (parsePercent(a.hourly_quota) ?? -1);
        }
        if (sortMode === 'weekly_desc') {
            return (parsePercent(b.weekly_quota) ?? -1) - (parsePercent(a.weekly_quota) ?? -1);
        }
        if (sortMode === 'email_asc') {
            return String(a.email || '').localeCompare(String(b.email || ''), 'zh-CN');
        }
        return parseDateScore(b.created_at) - parseDateScore(a.created_at);
    });

    return rows;
}

function applyCardFilters() {
    const keyword = (document.getElementById('card-search-input')?.value || '').trim().toLowerCase();
    const planFilter = overviewState.planFilter;

    let rows = [...overviewState.cards];

    if (keyword) {
        rows = rows.filter((item) => String(item.email || '').toLowerCase().includes(keyword));
    }

    if (planFilter === 'free') {
        rows = rows.filter((item) => normalizePlan(item.plan_type) === 'free');
    } else if (planFilter === 'plus') {
        rows = rows.filter((item) => normalizePlan(item.plan_type) === 'plus');
    } else if (planFilter === 'team') {
        rows = rows.filter((item) => normalizePlan(item.plan_type) === 'team');
    } else if (planFilter === 'current') {
        rows = rows.filter((item) => Boolean(item.current));
    }

    overviewState.filteredCards = sortCards(rows);
    renderPlanCards();
    updateSelectionInfo();
}

function setViewMode(mode) {
    overviewState.viewMode = mode === 'list' ? 'list' : 'grid';
    storage.set(VIEW_MODE_STORAGE_KEY, overviewState.viewMode);

    const listBtn = document.getElementById('view-list-btn');
    const gridBtn = document.getElementById('view-grid-btn');
    const container = document.getElementById('plan-cards-container');
    if (listBtn) listBtn.classList.toggle('active', overviewState.viewMode === 'list');
    if (gridBtn) gridBtn.classList.toggle('active', overviewState.viewMode === 'grid');
    if (container) container.classList.toggle('view-list', overviewState.viewMode === 'list');
}

function renderPlanCards() {
    const container = document.getElementById('plan-cards-container');
    const empty = document.getElementById('plan-cards-empty');
    if (!container || !empty) return;

    setViewMode(overviewState.viewMode);

    if (!overviewState.filteredCards.length) {
        container.innerHTML = '';
        empty.style.display = 'block';
        return;
    }

    empty.style.display = 'none';
    container.innerHTML = overviewState.filteredCards.map((account) => {
        const accountId = Number(account.id);
        const checked = overviewState.selectedCardIds.has(accountId) ? 'checked' : '';
        const codeReviewQuota = account.code_review_quota || null;
        const hasCodeReviewQuota = Boolean(
            codeReviewQuota &&
            (
                codeReviewQuota.status === 'ok' ||
                codeReviewQuota.percentage !== null && codeReviewQuota.percentage !== undefined ||
                codeReviewQuota.reset_at ||
                (codeReviewQuota.reset_in_text && codeReviewQuota.reset_in_text !== '-')
            )
        );
        const codeReviewHtml = hasCodeReviewQuota
            ? renderQuotaItem('Code Review', codeReviewQuota)
            : '';

        return `
            <article class="quota-card ${account.current ? 'is-current' : ''}">
                <div class="quota-card-head">
                    <label class="card-check">
                        <input type="checkbox" data-role="select-card" data-id="${accountId}" ${checked}>
                    </label>
                    <div class="quota-card-email" title="${escapeHtml(account.email || '-')}">${escapeHtml(account.email || '-')}</div>
                    <div class="quota-badges">
                        ${account.current ? '<span class="status-pill current">当前</span>' : ''}
                        <span class="plan-badge ${getPlanClass(account.plan_type)}">${getPlanText(account.plan_type)}</span>
                    </div>
                </div>

                ${renderQuotaItem('5小时配额', account.hourly_quota)}
                ${renderQuotaItem('周配额', account.weekly_quota)}
                ${codeReviewHtml}

                <div class="quota-divider"></div>
                <div class="quota-card-foot">
                    <span class="quota-time">${format.date(account.created_at)}</span>
                    <div class="card-actions">
                        <button class="card-action-btn" data-action="refresh" data-id="${accountId}" title="刷新配额">⟳</button>
                        <button class="card-action-btn" data-action="export" data-id="${accountId}" title="导出该卡片配置">⇪</button>
                        <button class="card-action-btn danger" data-action="remove" data-id="${accountId}" title="从卡片删除">🗑</button>
                    </div>
                </div>
            </article>
        `;
    }).join('');
}

async function loadPlanCards(forceRefresh = false) {
    void forceRefresh;
    try {
        const data = await overviewApi.loadCards();
        overviewState.cards = Array.isArray(data?.accounts) ? data.accounts : [];
        syncSelectedCardIds();
        updatePlanFilterOptions();
        applyCardFilters();
    } catch (error) {
        overviewState.cards = [];
        overviewState.filteredCards = [];
        overviewState.selectedCardIds = new Set();
        updatePlanFilterOptions();
        renderPlanCards();
        updateSelectionInfo();
        toast.error(`加载订阅卡片失败: ${error?.message || '未知错误'}`);
    }
}

function getCardsForRefresh(mode = 'visible') {
    const source = Array.isArray(overviewState.filteredCards) && overviewState.filteredCards.length
        ? overviewState.filteredCards
        : overviewState.cards;
    if (mode !== 'stale_failed') return source;
    return source.filter((item) => {
        const stale = Boolean(item?.overview_stale);
        const hasError = Boolean(item?.overview_error);
        const hourlyUnknown = String(item?.hourly_quota?.status || '').toLowerCase() === 'unknown';
        const weeklyUnknown = String(item?.weekly_quota?.status || '').toLowerCase() === 'unknown';
        return stale || hasError || (hourlyUnknown && weeklyUnknown);
    });
}

function pickRefreshTargetIds(options = {}) {
    const mode = String(options?.targetMode || 'visible');
    const preferSelected = options?.preferSelected !== false;
    const selectedIds = Array.from(overviewState.selectedCardIds);
    if (preferSelected && selectedIds.length) {
        return selectedIds.filter((id) => Number.isFinite(Number(id)));
    }
    return getCardsForRefresh(mode)
        .map((item) => Number(item.id))
        .filter((id) => Number.isFinite(id));
}

async function refreshSelectedOrAll(force = true, silent = false, options = {}) {
    if (overviewState.isBulkRefreshing) {
        if (!silent) toast.info('刷新任务仍在执行，请稍候');
        return;
    }

    const targetIds = pickRefreshTargetIds(options);

    overviewState.isBulkRefreshing = true;
    setToolbarRefreshLoading(true);
    try {
        if (!targetIds.length) {
            await loadPlanCards(false);
            if (!silent) toast.info('当前没有可刷新的卡片');
            return;
        }
        const task = await overviewApi.startRefreshTask({
            ids: targetIds,
            force,
            select_all: false,
        });
        const taskId = task?.id;
        if (!taskId) {
            throw new Error('任务创建失败：未返回任务 ID');
        }

        if (!silent) toast.info(`总览刷新任务已启动（${taskId.slice(0, 8)}）`);
        const finalTask = await watchOverviewTask(taskId, (progressTask) => {
            const progress = progressTask?.progress || {};
            const completed = Number(progress.completed || 0);
            const total = Number(progress.total || targetIds.length);
            if (!silent && total > 0) {
                const text = `刷新中 ${completed}/${total}`;
                const el = document.getElementById('card-selection-info');
                if (el) el.textContent = text;
            }
        });

        const status = String(finalTask?.status || '').toLowerCase();
        const result = finalTask?.result || {};
        await loadPlanCards(false);
        updateSelectionInfo();
        if (!silent) {
            if (status === 'completed') {
                toast.success(`刷新完成：成功 ${result.success_count || 0}，失败 ${result.failed_count || 0}`);
            } else if (status === 'cancelled') {
                toast.warning(`任务已取消（成功 ${result.success_count || 0}，失败 ${result.failed_count || 0}）`, 5000);
            } else {
                toast.error(`刷新失败: ${finalTask?.error || finalTask?.message || '未知错误'}`);
            }
        }
    } catch (error) {
        if (!silent) toast.error(`刷新失败: ${error.message || '未知错误'}`);
    } finally {
        overviewState.isBulkRefreshing = false;
        setToolbarRefreshLoading(false);
    }
}

function setCardRefreshLoading(button, loading) {
    if (!button) return;
    button.classList.toggle('is-loading', Boolean(loading));
    button.disabled = Boolean(loading);
    button.setAttribute('aria-busy', loading ? 'true' : 'false');
    if (!loading) button.blur();
}

async function refreshSingleCard(accountId, button = null) {
    if (!Number.isFinite(Number(accountId))) return;
    if (button?.classList.contains('is-loading')) return;
    if (overviewState.isBulkRefreshing) {
        toast.info('批量刷新进行中，请稍后再刷新单卡');
        return;
    }
    setCardRefreshLoading(button, true);
    toast.info(`正在刷新账号 #${accountId} 配额...`, 1500);
    try {
        const result = await overviewApi.refreshSingle({
            ids: [accountId],
            force: true,
            select_all: false,
        });

        const details = Array.isArray(result?.details) ? result.details : [];
        const currentDetail = details.find((item) => Number(item?.id) === Number(accountId));
        if (currentDetail && currentDetail.success === false) {
            toast.warning(
                `刷新完成但未拿到新配额: ${currentDetail.error || '未知原因'}`,
                4500
            );
        } else {
            toast.success(`账号 #${accountId} 配额刷新完成`);
        }
        await loadPlanCards(false);
    } catch (error) {
        toast.error(`刷新失败: ${error?.message || '未知错误'}`);
    } finally {
        setCardRefreshLoading(button, false);
    }
}

async function removeSingleCard(accountId) {
    const id = Number(accountId);
    if (!Number.isFinite(id)) return;
    const ok = await confirm('确认从卡片列表删除该账号吗？（不会删除账号管理数据）', '删除卡片');
    if (!ok) return;
    await overviewApi.removeCards({
        ids: [id],
        select_all: false,
    });
    overviewState.selectedCardIds.delete(id);
    await loadPlanCards(false);
    await loadAddableAccounts();
    toast.success('卡片已删除，可在“添加账号”里重新添加');
}

async function loadAddableAccounts() {
    try {
        const data = await overviewApi.loadSelectable();
        overviewState.addableCards = Array.isArray(data?.accounts) ? data.accounts : [];
    } catch (error) {
        overviewState.addableCards = [];
        console.warn('load addable cards failed', error);
    }
    renderAddableAccounts();
}

function renderAddableAccounts() {
    const select = document.getElementById('overview-add-existing-select');
    if (!select) return;
    const options = overviewState.addableCards.map((item) => {
        const plan = getPlanText(item.subscription_type || 'free');
        const tokenTag = item.has_access_token ? '有Token' : '无Token';
        return `<option value="${Number(item.id)}">${escapeHtml(item.email || '-')} (${plan}/${tokenTag})</option>`;
    });
    select.innerHTML = `<option value="">请选择账号</option>${options.join('')}`;
}

function getSelectedExistingAccount() {
    const select = document.getElementById('overview-add-existing-select');
    const id = Number(select?.value || 0);
    if (!Number.isFinite(id) || id <= 0) return null;
    return overviewState.addableCards.find((item) => Number(item.id) === id) || null;
}

function fillAddFormFromExistingSelection() {
    const selected = getSelectedExistingAccount();
    if (!selected) return;

    setFieldValue('overview-add-email', selected.email || '');
    setFieldValue('overview-add-password', selected.password || '');
    setFieldValue('overview-add-email-service', selected.email_service || 'manual');
    setFieldValue('overview-add-subscription', normalizePlan(selected.subscription_type || 'free'));
    setFieldValue('overview-add-client-id', selected.client_id || '');
    setFieldValue('overview-add-account-id', selected.account_id || '');
    setFieldValue('overview-add-workspace-id', selected.workspace_id || '');
    setFieldValue('overview-add-status', selected.status || 'active');
}

async function restoreSelectedAddableAccount() {
    const select = document.getElementById('overview-add-existing-select');
    const id = Number(select?.value || 0);
    if (!Number.isFinite(id) || id <= 0) {
        toast.warning('请先选择一个账号');
        return;
    }
    await overviewApi.attachCard(id);
    await loadAddableAccounts();
    await loadPlanCards(false);
    await loadLegacyOverview();
    toast.success('账号已添加到卡片');
}

function extractFilenameFromDisposition(contentDisposition) {
    const raw = String(contentDisposition || '');
    const match = raw.match(/filename="?([^"]+)"?/i);
    return match ? match[1] : '';
}

async function downloadAccountsExportJson(payload, fallbackNamePrefix) {
    const resp = await fetch('/api/accounts/export/json', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });

    if (!resp.ok) {
        let detail = '';
        try {
            const errData = await resp.json();
            detail = errData?.detail || '';
        } catch {
            detail = '';
        }
        throw new Error(detail || `HTTP ${resp.status}`);
    }

    const blob = await resp.blob();
    const contentDisposition = resp.headers.get('Content-Disposition') || '';
    const filename =
        extractFilenameFromDisposition(contentDisposition) ||
        `${fallbackNamePrefix}_${Date.now()}.json`;
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
}

function getSelectedIds() {
    return Array.from(overviewState.selectedCardIds).filter((id) => Number.isFinite(Number(id)));
}

function openModalById(id) {
    const modal = document.getElementById(id);
    if (modal) modal.classList.add('active');
}

function closeModalById(id) {
    const modal = document.getElementById(id);
    if (modal) modal.classList.remove('active');
}

function resetAddModalFields() {
    setFieldValue('overview-add-email', '');
    setFieldValue('overview-add-password', '');
    setFieldValue('overview-add-email-service', 'manual');
    setFieldValue('overview-add-subscription', '');
    setFieldValue('overview-add-client-id', '');
    setFieldValue('overview-add-account-id', '');
    setFieldValue('overview-add-workspace-id', '');
    setFieldValue('overview-add-status', 'active');
    setFieldValue('overview-add-existing-select', '');
}

function setFieldValue(id, value) {
    const node = document.getElementById(id);
    if (!node) return;
    node.value = value ?? '';
}

async function submitAddAccount() {
    const selectedExisting = getSelectedExistingAccount();
    if (selectedExisting) {
        await overviewApi.attachCard(Number(selectedExisting.id));
        closeModalById('overview-add-modal');
        resetAddModalFields();
        await loadAddableAccounts();
        await loadPlanCards(false);
        await loadLegacyOverview();
        toast.success('已从账号管理直接添加到卡片');
        return;
    }

    const email = (document.getElementById('overview-add-email')?.value || '').trim();
    const password = (document.getElementById('overview-add-password')?.value || '').trim();
    const emailService = (document.getElementById('overview-add-email-service')?.value || 'manual').trim() || 'manual';
    const subscriptionType = (document.getElementById('overview-add-subscription')?.value || '').trim();
    const clientId = (document.getElementById('overview-add-client-id')?.value || '').trim();
    const accountId = (document.getElementById('overview-add-account-id')?.value || '').trim();
    const workspaceId = (document.getElementById('overview-add-workspace-id')?.value || '').trim();
    const status = (document.getElementById('overview-add-status')?.value || 'active').trim() || 'active';

    if (!email || !password) {
        toast.warning('邮箱和密码为必填项');
        return;
    }

    const payload = {
        email,
        password,
        email_service: emailService,
        subscription_type: subscriptionType || null,
        client_id: clientId || null,
        account_id: accountId || null,
        workspace_id: workspaceId || null,
        status,
        source: 'manual',
    };
    await api.post('/accounts', payload);
    closeModalById('overview-add-modal');
    resetAddModalFields();
    await loadAddableAccounts();
    await loadPlanCards(false);
    await loadLegacyOverview();
    toast.success('账号已添加');
}

async function submitImportAccounts() {
    const input = (document.getElementById('overview-import-json')?.value || '').trim();
    const overwrite = Boolean(document.getElementById('overview-import-overwrite')?.checked);
    if (!input) {
        toast.warning('请先粘贴 JSON');
        return;
    }

    let parsed;
    try {
        parsed = JSON.parse(input);
    } catch (error) {
        toast.error(`JSON 解析失败: ${error.message || '格式错误'}`);
        return;
    }
    const accounts = Array.isArray(parsed)
        ? parsed
        : (Array.isArray(parsed?.accounts) ? parsed.accounts : []);
    if (!accounts.length) {
        toast.warning('JSON 必须是非空数组');
        return;
    }

    const result = await api.post('/accounts/import', {
        accounts,
        overwrite,
    });
    closeModalById('overview-import-modal');
    toast.success(
        `导入完成：新增 ${result?.created || 0}，更新 ${result?.updated || 0}，跳过 ${result?.skipped || 0}，失败 ${result?.failed || 0}`
    );
    await loadPlanCards(false);
    await loadLegacyOverview();
    await loadAddableAccounts();
}

async function removeSelectedAccounts() {
    let ids = getSelectedIds();
    if (!ids.length) {
        ids = overviewState.filteredCards
            .map((item) => Number(item.id))
            .filter((id) => Number.isFinite(id));
        if (!ids.length) {
            toast.warning('当前没有可删除的账号');
            return;
        }
    }

    const ok = await confirm(`确认从卡片列表删除 ${ids.length} 个账号吗？（不会删除账号管理数据）`, '批量删除卡片');
    if (!ok) return;

    const result = await overviewApi.removeCards({ ids, select_all: false });
    const removedCount = Number(result?.removed_count || 0);
    toast.success(`删除完成：${removedCount} 个卡片`);
    overviewState.selectedCardIds.clear();
    await loadAddableAccounts();
    await loadPlanCards(false);
}

async function exportAllVisibleAccounts() {
    const ids = overviewState.filteredCards
        .map((item) => Number(item.id))
        .filter((id) => Number.isFinite(id));
    if (!ids.length) {
        toast.warning('当前没有可导出的账号');
        return;
    }
    await downloadAccountsExportJson({ ids, select_all: false }, 'overview_all_accounts');
    toast.success(`已导出 ${ids.length} 个账号`);
}

async function exportSingleCard(accountId) {
    const id = Number(accountId);
    if (!Number.isFinite(id)) return;
    await downloadAccountsExportJson({ ids: [id], select_all: false }, `overview_account_${id}`);
    toast.success(`账号 #${id} 配置已导出`);
}

function updateCardNextRefreshText() {
    const el = document.getElementById('card-next-refresh');
    if (!el) return;
    if (!overviewState.cardNextRefreshAt) {
        el.textContent = '下次刷新 --';
        return;
    }
    const remainSec = Math.max(0, Math.floor((overviewState.cardNextRefreshAt - Date.now()) / 1000));
    const min = Math.floor(remainSec / 60);
    const sec = remainSec % 60;
    el.textContent = `下次刷新 ${min}:${String(sec).padStart(2, '0')}`;
}

function restartCardAutoRefresh() {
    if (overviewState.cardRefreshTimer) {
        clearInterval(overviewState.cardRefreshTimer);
        overviewState.cardRefreshTimer = null;
    }
    if (overviewState.cardCountdownTimer) {
        clearInterval(overviewState.cardCountdownTimer);
        overviewState.cardCountdownTimer = null;
    }

    const intervalMs = overviewState.cardRefreshIntervalMin * 60 * 1000;
    overviewState.cardNextRefreshAt = Date.now() + intervalMs;
    updateCardNextRefreshText();

    overviewState.cardRefreshTimer = setInterval(async () => {
        if (document.hidden) return;
        await refreshSelectedOrAll(true, true, {
            targetMode: overviewState.cardAutoRefreshScope,
            preferSelected: false,
        });
        overviewState.cardNextRefreshAt = Date.now() + intervalMs;
        updateCardNextRefreshText();
    }, intervalMs);

    let ticks = 0;
    overviewState.cardCountdownTimer = setInterval(() => {
        updateCardNextRefreshText();
        ticks += 1;
        if (ticks % 30 === 0 && overviewState.filteredCards.length) {
            renderPlanCards();
        }
    }, 1000);
}

function bindEvents() {
    const cardSearchInput = document.getElementById('card-search-input');
    if (cardSearchInput) {
        cardSearchInput.addEventListener('input', debounce(applyCardFilters, 240));
    }

    const planFilter = document.getElementById('card-plan-filter');
    if (planFilter) {
        planFilter.addEventListener('change', () => {
            overviewState.planFilter = planFilter.value || 'all';
            applyCardFilters();
        });
    }

    const sortMode = document.getElementById('card-sort-mode');
    if (sortMode) {
        sortMode.addEventListener('change', () => {
            overviewState.sortMode = sortMode.value || 'created_desc';
            applyCardFilters();
        });
    }

    const viewListBtn = document.getElementById('view-list-btn');
    const viewGridBtn = document.getElementById('view-grid-btn');
    if (viewListBtn) {
        viewListBtn.addEventListener('click', () => {
            setViewMode('list');
            renderPlanCards();
        });
    }
    if (viewGridBtn) {
        viewGridBtn.addEventListener('click', () => {
            setViewMode('grid');
            renderPlanCards();
        });
    }

    const cardRefreshSelect = document.getElementById('card-refresh-interval');
    if (cardRefreshSelect) {
        cardRefreshSelect.value = String(overviewState.cardRefreshIntervalMin);
        cardRefreshSelect.addEventListener('change', () => {
            const value = Number(cardRefreshSelect.value || 7);
            overviewState.cardRefreshIntervalMin = [5, 7, 10].includes(value) ? value : 7;
            restartCardAutoRefresh();
        });
    }
    const cardRefreshScope = document.getElementById('card-refresh-scope');
    if (cardRefreshScope) {
        const nextScope = ['stale_failed', 'visible'].includes(overviewState.cardAutoRefreshScope)
            ? overviewState.cardAutoRefreshScope
            : 'stale_failed';
        overviewState.cardAutoRefreshScope = nextScope;
        cardRefreshScope.value = nextScope;
        cardRefreshScope.addEventListener('change', () => {
            const value = ['stale_failed', 'visible'].includes(cardRefreshScope.value)
                ? cardRefreshScope.value
                : 'stale_failed';
            overviewState.cardAutoRefreshScope = value;
            storage.set(AUTO_REFRESH_SCOPE_STORAGE_KEY, value);
            restartCardAutoRefresh();
        });
    }

    const addBtn = document.getElementById('card-add-btn');
    if (addBtn) {
        addBtn.addEventListener('click', async () => {
            resetAddModalFields();
            await loadAddableAccounts();
            openModalById('overview-add-modal');
        });
    }

    const toolbarRefreshBtn = document.getElementById('card-refresh-btn');
    if (toolbarRefreshBtn) {
        toolbarRefreshBtn.addEventListener('click', async () => {
            try {
                await refreshSelectedOrAll(true, true, {
                    targetMode: 'visible',
                    preferSelected: true,
                });
                await Promise.all([
                    loadLegacyOverview(),
                    loadAddableAccounts(),
                ]);
                toast.success('账号总览已刷新');
            } catch (error) {
                toast.error(`刷新失败: ${error?.message || '未知错误'}`);
            }
        });
    }
    const toolbarRefreshFailedBtn = document.getElementById('card-refresh-failed-btn');
    if (toolbarRefreshFailedBtn) {
        toolbarRefreshFailedBtn.addEventListener('click', async () => {
            try {
                await refreshSelectedOrAll(true, false, {
                    targetMode: 'stale_failed',
                    preferSelected: false,
                });
                await Promise.all([
                    loadLegacyOverview(),
                    loadAddableAccounts(),
                ]);
            } catch (error) {
                toast.error(`刷新失败: ${error?.message || '未知错误'}`);
            }
        });
    }

    const importBtn = document.getElementById('card-import-btn');
    if (importBtn) {
        importBtn.addEventListener('click', () => openModalById('overview-import-modal'));
    }

    const deleteBtn = document.getElementById('card-delete-btn');
    if (deleteBtn) {
        deleteBtn.addEventListener('click', async () => {
            try {
                await removeSelectedAccounts();
            } catch (error) {
                toast.error(`删除失败: ${error?.message || '未知错误'}`);
            }
        });
    }

    const exportAllBtn = document.getElementById('card-export-all-btn');
    if (exportAllBtn) {
        exportAllBtn.addEventListener('click', async () => {
            try {
                await exportAllVisibleAccounts();
            } catch (error) {
                toast.error(`导出失败: ${error?.message || '未知错误'}`);
            }
        });
    }

    const addModalIds = ['overview-add-modal', 'overview-import-modal'];
    addModalIds.forEach((modalId) => {
        const modal = document.getElementById(modalId);
        if (!modal) return;
        modal.addEventListener('click', (event) => {
            if (event.target === modal) {
                closeModalById(modalId);
            }
        });
    });

    document.getElementById('overview-add-close')?.addEventListener('click', () => closeModalById('overview-add-modal'));
    document.getElementById('overview-add-cancel')?.addEventListener('click', () => closeModalById('overview-add-modal'));
    document.getElementById('overview-add-existing-refresh')?.addEventListener('click', async () => {
        try {
            await loadAddableAccounts();
            toast.success('已刷新账号管理列表');
        } catch (error) {
            toast.error(`刷新失败: ${error?.message || '未知错误'}`);
        }
    });
    document.getElementById('overview-add-existing-select')?.addEventListener('change', () => {
        fillAddFormFromExistingSelection();
    });
    document.getElementById('overview-add-existing-submit')?.addEventListener('click', async () => {
        try {
            await restoreSelectedAddableAccount();
        } catch (error) {
            toast.error(`恢复失败: ${error?.message || '未知错误'}`);
        }
    });
    document.getElementById('overview-add-submit')?.addEventListener('click', async () => {
        try {
            await submitAddAccount();
        } catch (error) {
            toast.error(`添加失败: ${error?.message || '未知错误'}`);
        }
    });

    document.getElementById('overview-import-close')?.addEventListener('click', () => closeModalById('overview-import-modal'));
    document.getElementById('overview-import-cancel')?.addEventListener('click', () => closeModalById('overview-import-modal'));
    document.getElementById('overview-import-submit')?.addEventListener('click', async () => {
        try {
            await submitImportAccounts();
        } catch (error) {
            toast.error(`导入失败: ${error?.message || '未知错误'}`);
        }
    });

    const cardContainer = document.getElementById('plan-cards-container');
    if (cardContainer) {
        cardContainer.addEventListener('change', (event) => {
            const checkbox = event.target.closest('input[data-role="select-card"]');
            if (!checkbox) return;
            const id = Number(checkbox.dataset.id);
            if (!Number.isFinite(id)) return;
            if (checkbox.checked) {
                overviewState.selectedCardIds.add(id);
            } else {
                overviewState.selectedCardIds.delete(id);
            }
            updateSelectionInfo();
        });

        cardContainer.addEventListener('click', async (event) => {
            const button = event.target.closest('button[data-action]');
            if (!button) return;
            const action = button.dataset.action;
            const accountId = Number(button.dataset.id);
            if (!Number.isFinite(accountId)) return;

            if (action === 'refresh') {
                await refreshSingleCard(accountId, button);
                return;
            }
            if (action === 'export') {
                await exportSingleCard(accountId);
                return;
            }
            if (action === 'remove') {
                await removeSingleCard(accountId);
            }
        });
    }
}

document.addEventListener('DOMContentLoaded', async () => {
    const sortMode = document.getElementById('card-sort-mode');
    if (sortMode) sortMode.value = overviewState.sortMode;
    setViewMode(overviewState.viewMode);
    bindEvents();
    const initResults = await Promise.allSettled([
        loadLegacyOverview(),
        loadPlanCards(false),
        loadAddableAccounts(),
    ]);
    initResults.forEach((item, index) => {
        if (item.status === 'rejected') {
            const target = index === 0 ? '总览统计' : (index === 1 ? '卡片列表' : '可选账号');
            toast.warning(`${target}初始化失败，已降级显示`);
        }
    });
    restartCardAutoRefresh();
});
