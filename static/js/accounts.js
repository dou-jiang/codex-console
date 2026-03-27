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
let selectAllPages = false;  // 是否选中了全部页
let currentFilters = { status: '', email_service: '', search: '' };  // 当前筛选条件

// DOM 元素
const elements = {
    table: document.getElementById('accounts-table'),
    totalAccounts: document.getElementById('total-accounts'),
    activeAccounts: document.getElementById('active-accounts'),
    expiredAccounts: document.getElementById('expired-accounts'),
    failedAccounts: document.getElementById('failed-accounts'),
    filterStatus: document.getElementById('filter-status'),
    filterService: document.getElementById('filter-service'),
    searchInput: document.getElementById('search-input'),
    refreshBtn: document.getElementById('refresh-btn'),
    batchRefreshBtn: document.getElementById('batch-refresh-btn'),
    batchValidateBtn: document.getElementById('batch-validate-btn'),
    batchUploadBtn: document.getElementById('batch-upload-btn'),
    batchCheckSubBtn: document.getElementById('batch-check-sub-btn'),
    batchDeleteBtn: document.getElementById('batch-delete-btn'),
    exportBtn: document.getElementById('export-btn'),
    exportMenu: document.getElementById('export-menu'),
    selectAll: document.getElementById('select-all'),
    prevPage: document.getElementById('prev-page'),
    nextPage: document.getElementById('next-page'),
    pageInfo: document.getElementById('page-info'),
    detailModal: document.getElementById('detail-modal'),
    modalBody: document.getElementById('modal-body'),
    closeModal: document.getElementById('close-modal')
};

// 初始化
document.addEventListener('DOMContentLoaded', () => {
    loadStats();
    loadAccounts();
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

    // 刷新
    elements.refreshBtn.addEventListener('click', () => {
        loadStats();
        loadAccounts();
        toast.info('已刷新');
    });

    // 批量刷新Token
    elements.batchRefreshBtn.addEventListener('click', handleBatchRefresh);

    // 批量验证Token
    elements.batchValidateBtn.addEventListener('click', handleBatchValidate);

    // 批量检测订阅
    elements.batchCheckSubBtn.addEventListener('click', handleBatchCheckSubscription);

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

    // 点击其他地方关闭下拉菜单
    document.addEventListener('click', () => {
        elements.exportMenu.classList.remove('active');
        uploadMenu.classList.remove('active');
        document.querySelectorAll('#accounts-table .dropdown-menu.active').forEach(m => m.classList.remove('active'));
    });
}

// 加载统计信息
async function loadStats() {
    try {
        const data = await api.get('/accounts/stats/summary');

        elements.totalAccounts.textContent = format.number(data.total || 0);
        elements.activeAccounts.textContent = format.number(data.by_status?.active || 0);
        elements.expiredAccounts.textContent = format.number(data.by_status?.expired || 0);
        elements.failedAccounts.textContent = format.number(data.by_status?.failed || 0);

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
    currentFilters.status = elements.filterStatus.value;
    currentFilters.email_service = elements.filterService.value;
    currentFilters.search = elements.searchInput.value.trim();

    const params = new URLSearchParams({
        page: currentPage,
        page_size: pageSize,
    });

    if (currentFilters.status) {
        params.append('status', currentFilters.status);
    }

    if (currentFilters.email_service) {
        params.append('email_service', currentFilters.email_service);
    }

    if (currentFilters.search) {
        params.append('search', currentFilters.search);
    }

    try {
        const data = await api.get(`/accounts?${params}`);
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
            <td>${renderAccountStatusDot(account.status)}</td>
            <td>
                <div class="cpa-status">
                    ${account.cpa_uploaded
                        ? `<span class="badge uploaded" title="已上传于 ${format.date(account.cpa_uploaded_at)}">✓</span>`
                        : `<span class="badge pending">-</span>`}
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

function renderAccountStatusDot(status) {
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
    const subscribed = hasActiveSubscription(normalized);
    const dotClass = subscribed ? 'subscribed' : 'unsubscribed';
    const label = subscribed ? normalized.toUpperCase() : 'FREE';
    const title = subscribed
        ? `已订阅: ${normalized}`
        : '未检测到 Plus/Team 订阅';
    return `
        <div class="subscription-status ${dotClass}" title="${escapeHtml(title)}">
            <span class="dot ${dotClass}"></span>
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
    if (selectAllPages) {
        return {
            ids: [],
            select_all: true,
            status_filter: currentFilters.status || null,
            email_service_filter: currentFilters.email_service || null,
            search_filter: currentFilters.search || null,
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

// 更新批量操作按钮
function updateBatchButtons() {
    const count = getEffectiveCount();
    elements.batchDeleteBtn.disabled = count === 0;
    elements.batchRefreshBtn.disabled = count === 0;
    elements.batchValidateBtn.disabled = count === 0;
    elements.batchUploadBtn.disabled = count === 0;
    elements.batchCheckSubBtn.disabled = count === 0;
    elements.exportBtn.disabled = count === 0;

    elements.batchDeleteBtn.textContent = count > 0 ? `🗑️ 删除 (${count})` : '🗑️ 批量删除';
    elements.batchRefreshBtn.textContent = count > 0 ? `🔄 刷新 (${count})` : '🔄 刷新Token';
    elements.batchValidateBtn.textContent = count > 0 ? `✅ 验证 (${count})` : '✅ 验证Token';
    elements.batchUploadBtn.textContent = count > 0 ? `☁️ 上传 (${count})` : '☁️ 上传';
    elements.batchCheckSubBtn.textContent = count > 0 ? `🔍 检测 (${count})` : '🔍 检测订阅';
}

// 刷新单个账号Token
async function refreshToken(id) {
    try {
        toast.info('正在刷新Token...');
        const result = await api.post(`/accounts/${id}/refresh`);

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

// 批量刷新Token
async function handleBatchRefresh() {
    const count = getEffectiveCount();
    if (count === 0) return;

    const confirmed = await confirm(`确定要刷新选中的 ${count} 个账号的Token吗？`);
    if (!confirmed) return;

    elements.batchRefreshBtn.disabled = true;
    elements.batchRefreshBtn.textContent = '刷新中...';

    try {
        const result = await api.post('/accounts/batch-refresh', buildBatchPayload());
        toast.success(`成功刷新 ${result.success_count} 个，失败 ${result.failed_count} 个`);
        loadAccounts();
    } catch (error) {
        toast.error('批量刷新失败: ' + error.message);
    } finally {
        updateBatchButtons();
    }
}

// 批量验证Token
async function handleBatchValidate() {
    if (getEffectiveCount() === 0) return;

    elements.batchValidateBtn.disabled = true;
    elements.batchValidateBtn.textContent = '验证中...';

    try {
        const result = await api.post('/accounts/batch-validate', buildBatchPayload());
        toast.info(`有效: ${result.valid_count}，无效: ${result.invalid_count}`);
        loadAccounts();
    } catch (error) {
        toast.error('批量验证失败: ' + error.message);
    } finally {
        updateBatchButtons();
    }
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
    if (count === 0) return;
    const confirmed = await confirm(`确定要检测选中的 ${count} 个账号的订阅状态吗？`);
    if (!confirmed) return;

    elements.batchCheckSubBtn.disabled = true;
    elements.batchCheckSubBtn.textContent = '检测中...';

    try {
        const result = await api.post('/payment/accounts/batch-check-subscription', buildBatchPayload());
        let message = `成功: ${result.success_count}`;
        if (result.failed_count > 0) message += `, 失败: ${result.failed_count}`;
        toast.success(message);
        loadAccounts();
    } catch (e) {
        toast.error('批量检测失败: ' + e.message);
    } finally {
        updateBatchButtons();
    }
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
