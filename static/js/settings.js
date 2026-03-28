/**
 * 设置页面 JavaScript
 * 使用 utils.js 中的工具库
 */

// DOM 元素
const elements = {
    tabs: document.querySelectorAll('.tab-btn'),
    tabContents: document.querySelectorAll('.tab-content'),
    registrationForm: document.getElementById('registration-settings-form'),
    backupBtn: document.getElementById('backup-btn'),
    importDbBtn: document.getElementById('import-db-btn'),
    dbImportFile: document.getElementById('db-import-file'),
    cleanupBtn: document.getElementById('cleanup-btn'),
    addEmailServiceBtn: document.getElementById('add-email-service-btn'),
    addServiceModal: document.getElementById('add-service-modal'),
    addServiceForm: document.getElementById('add-service-form'),
    closeServiceModal: document.getElementById('close-service-modal'),
    cancelAddService: document.getElementById('cancel-add-service'),
    serviceType: document.getElementById('service-type'),
    serviceConfigFields: document.getElementById('service-config-fields'),
    emailServicesTable: document.getElementById('email-services-table'),
    // Outlook 导入
    toggleImportBtn: document.getElementById('toggle-import-btn'),
    outlookImportBody: document.getElementById('outlook-import-body'),
    outlookImportBtn: document.getElementById('outlook-import-btn'),
    clearImportBtn: document.getElementById('clear-import-btn'),
    outlookImportData: document.getElementById('outlook-import-data'),
    importResult: document.getElementById('import-result'),
    // 批量操作
    selectAllServices: document.getElementById('select-all-services'),
    // 代理列表
    proxiesTable: document.getElementById('proxies-table'),
    addProxyBtn: document.getElementById('add-proxy-btn'),
    proxyBatchImportBtn: document.getElementById('proxy-batch-import-btn'),
    testAllProxiesBtn: document.getElementById('test-all-proxies-btn'),
    addProxyModal: document.getElementById('add-proxy-modal'),
    proxyItemForm: document.getElementById('proxy-item-form'),
    closeProxyModal: document.getElementById('close-proxy-modal'),
    cancelProxyBtn: document.getElementById('cancel-proxy-btn'),
    proxyModalTitle: document.getElementById('proxy-modal-title'),
    proxyBatchImportModal: document.getElementById('proxy-batch-import-modal'),
    closeProxyBatchImportModal: document.getElementById('close-proxy-batch-import-modal'),
    cancelProxyBatchImportBtn: document.getElementById('cancel-proxy-batch-import-btn'),
    proxyBatchImportForm: document.getElementById('proxy-batch-import-form'),
    proxyBatchImportText: document.getElementById('proxy-batch-import-text'),
    proxyBatchImportResult: document.getElementById('proxy-batch-import-result'),
    // 动态代理设置
    dynamicProxyForm: document.getElementById('dynamic-proxy-form'),
    testDynamicProxyBtn: document.getElementById('test-dynamic-proxy-btn'),
    // CPA 服务管理
    addCpaServiceBtn: document.getElementById('add-cpa-service-btn'),
    cpaServicesTable: document.getElementById('cpa-services-table'),
    cpaServiceEditModal: document.getElementById('cpa-service-edit-modal'),
    closeCpaServiceModal: document.getElementById('close-cpa-service-modal'),
    cancelCpaServiceBtn: document.getElementById('cancel-cpa-service-btn'),
    cpaServiceForm: document.getElementById('cpa-service-form'),
    cpaServiceModalTitle: document.getElementById('cpa-service-modal-title'),
    testCpaServiceBtn: document.getElementById('test-cpa-service-btn'),
    // Sub2API 服务管理
    addSub2ApiServiceBtn: document.getElementById('add-sub2api-service-btn'),
    sub2ApiServicesTable: document.getElementById('sub2api-services-table'),
    sub2ApiServiceEditModal: document.getElementById('sub2api-service-edit-modal'),
    closeSub2ApiServiceModal: document.getElementById('close-sub2api-service-modal'),
    cancelSub2ApiServiceBtn: document.getElementById('cancel-sub2api-service-btn'),
    sub2ApiServiceForm: document.getElementById('sub2api-service-form'),
    sub2ApiServiceModalTitle: document.getElementById('sub2api-service-modal-title'),
    testSub2ApiServiceBtn: document.getElementById('test-sub2api-service-btn'),
    // Team Manager 服务管理
    addTmServiceBtn: document.getElementById('add-tm-service-btn'),
    tmServicesTable: document.getElementById('tm-services-table'),
    tmServiceEditModal: document.getElementById('tm-service-edit-modal'),
    closeTmServiceModal: document.getElementById('close-tm-service-modal'),
    cancelTmServiceBtn: document.getElementById('cancel-tm-service-btn'),
    tmServiceForm: document.getElementById('tm-service-form'),
    tmServiceModalTitle: document.getElementById('tm-service-modal-title'),
    testTmServiceBtn: document.getElementById('test-tm-service-btn'),
    // 验证码设置
    emailCodeForm: document.getElementById('email-code-form'),
    // Outlook 设置
    outlookSettingsForm: document.getElementById('outlook-settings-form'),
    // 系统设置（端口 + 访问控制）
    systemSettingsForm: document.getElementById('system-settings-form')
};

// 选中的服务 ID
let selectedServiceIds = new Set();

// 初始化
document.addEventListener('DOMContentLoaded', () => {
    initTabs();
    loadSettings();
    loadEmailServices();
    loadDatabaseInfo();
    loadProxies();
    loadCpaServices();
    loadSub2ApiServices();
    loadTmServices();
    initEventListeners();
});

document.addEventListener('click', () => {
    document.querySelectorAll('.dropdown-menu.active').forEach(m => m.classList.remove('active'));
});

// 初始化标签页
function initTabs() {
    elements.tabs.forEach(btn => {
        btn.addEventListener('click', () => {
            const tab = btn.dataset.tab;

            elements.tabs.forEach(b => b.classList.remove('active'));
            elements.tabContents.forEach(c => c.classList.remove('active'));

            btn.classList.add('active');
            document.getElementById(`${tab}-tab`).classList.add('active');
        });
    });
}

// 事件监听
function initEventListeners() {
    // 注册配置表单
    if (elements.registrationForm) {
        elements.registrationForm.addEventListener('submit', handleSaveRegistration);
    }

    // 备份数据库
    if (elements.backupBtn) {
        elements.backupBtn.addEventListener('click', handleBackup);
    }
    // 导入数据库
    if (elements.importDbBtn && elements.dbImportFile) {
        elements.importDbBtn.addEventListener('click', () => {
            elements.dbImportFile.value = '';
            elements.dbImportFile.click();
        });
        elements.dbImportFile.addEventListener('change', handleImportDatabase);
    }

    // 清理数据
    if (elements.cleanupBtn) {
        elements.cleanupBtn.addEventListener('click', handleCleanup);
    }

    // 添加邮箱服务
    if (elements.addEmailServiceBtn) {
        elements.addEmailServiceBtn.addEventListener('click', () => {
            elements.addServiceModal.classList.add('active');
            loadServiceConfigFields(elements.serviceType.value);
        });
    }

    if (elements.closeServiceModal) {
        elements.closeServiceModal.addEventListener('click', () => {
            elements.addServiceModal.classList.remove('active');
        });
    }

    if (elements.cancelAddService) {
        elements.cancelAddService.addEventListener('click', () => {
            elements.addServiceModal.classList.remove('active');
        });
    }

    if (elements.addServiceModal) {
        elements.addServiceModal.addEventListener('click', (e) => {
            if (e.target === elements.addServiceModal) {
                elements.addServiceModal.classList.remove('active');
            }
        });
    }

    // 服务类型切换
    if (elements.serviceType) {
        elements.serviceType.addEventListener('change', (e) => {
            loadServiceConfigFields(e.target.value);
        });
    }

    // 添加服务表单
    if (elements.addServiceForm) {
        elements.addServiceForm.addEventListener('submit', handleAddService);
    }

    // Outlook 批量导入展开/折叠
    if (elements.toggleImportBtn) {
        elements.toggleImportBtn.addEventListener('click', () => {
            const isHidden = elements.outlookImportBody.style.display === 'none';
            elements.outlookImportBody.style.display = isHidden ? 'block' : 'none';
            elements.toggleImportBtn.textContent = isHidden ? '收起' : '展开';
        });
    }

    // Outlook 批量导入
    if (elements.outlookImportBtn) {
        elements.outlookImportBtn.addEventListener('click', handleOutlookBatchImport);
    }

    // 清空导入数据
    if (elements.clearImportBtn) {
        elements.clearImportBtn.addEventListener('click', () => {
            elements.outlookImportData.value = '';
            elements.importResult.style.display = 'none';
        });
    }

    // 全选/取消全选
    if (elements.selectAllServices) {
        elements.selectAllServices.addEventListener('change', (e) => {
            const checkboxes = document.querySelectorAll('.service-checkbox');
            checkboxes.forEach(cb => cb.checked = e.target.checked);
            updateSelectedServices();
        });
    }

    // 代理列表相关
    if (elements.addProxyBtn) {
        elements.addProxyBtn.addEventListener('click', () => openProxyModal());
    }
    if (elements.proxyBatchImportBtn) {
        elements.proxyBatchImportBtn.addEventListener('click', openProxyBatchImportModal);
    }

    if (elements.testAllProxiesBtn) {
        elements.testAllProxiesBtn.addEventListener('click', handleTestAllProxies);
    }

    if (elements.closeProxyModal) {
        elements.closeProxyModal.addEventListener('click', closeProxyModal);
    }

    if (elements.cancelProxyBtn) {
        elements.cancelProxyBtn.addEventListener('click', closeProxyModal);
    }

    if (elements.addProxyModal) {
        elements.addProxyModal.addEventListener('click', (e) => {
            if (e.target === elements.addProxyModal) {
                closeProxyModal();
            }
        });
    }

    if (elements.proxyItemForm) {
        elements.proxyItemForm.addEventListener('submit', handleSaveProxyItem);
    }
    if (elements.closeProxyBatchImportModal) {
        elements.closeProxyBatchImportModal.addEventListener('click', closeProxyBatchImportModal);
    }
    if (elements.cancelProxyBatchImportBtn) {
        elements.cancelProxyBatchImportBtn.addEventListener('click', closeProxyBatchImportModal);
    }
    if (elements.proxyBatchImportModal) {
        elements.proxyBatchImportModal.addEventListener('click', (e) => {
            if (e.target === elements.proxyBatchImportModal) {
                closeProxyBatchImportModal();
            }
        });
    }
    if (elements.proxyBatchImportForm) {
        elements.proxyBatchImportForm.addEventListener('submit', handleProxyBatchImport);
    }

    // 动态代理设置
    if (elements.dynamicProxyForm) {
        elements.dynamicProxyForm.addEventListener('submit', handleSaveDynamicProxy);
    }
    if (elements.testDynamicProxyBtn) {
        elements.testDynamicProxyBtn.addEventListener('click', handleTestDynamicProxy);
    }

    // 验证码设置
    if (elements.emailCodeForm) {
        elements.emailCodeForm.addEventListener('submit', handleSaveEmailCode);
    }

    // Outlook 设置
    if (elements.outlookSettingsForm) {
        elements.outlookSettingsForm.addEventListener('submit', handleSaveOutlookSettings);
    }

    if (elements.systemSettingsForm) {
        elements.systemSettingsForm.addEventListener('submit', handleSaveSystemSettings);
    }
    // Team Manager 服务管理
    if (elements.addTmServiceBtn) {
        elements.addTmServiceBtn.addEventListener('click', () => openTmServiceModal());
    }
    if (elements.closeTmServiceModal) {
        elements.closeTmServiceModal.addEventListener('click', closeTmServiceModal);
    }
    if (elements.cancelTmServiceBtn) {
        elements.cancelTmServiceBtn.addEventListener('click', closeTmServiceModal);
    }
    if (elements.tmServiceEditModal) {
        elements.tmServiceEditModal.addEventListener('click', (e) => {
            if (e.target === elements.tmServiceEditModal) closeTmServiceModal();
        });
    }
    if (elements.tmServiceForm) {
        elements.tmServiceForm.addEventListener('submit', handleSaveTmService);
    }
    if (elements.testTmServiceBtn) {
        elements.testTmServiceBtn.addEventListener('click', handleTestTmService);
    }

    // CPA 服务管理
    if (elements.addCpaServiceBtn) {
        elements.addCpaServiceBtn.addEventListener('click', () => openCpaServiceModal());
    }
    if (elements.closeCpaServiceModal) {
        elements.closeCpaServiceModal.addEventListener('click', closeCpaServiceModal);
    }
    if (elements.cancelCpaServiceBtn) {
        elements.cancelCpaServiceBtn.addEventListener('click', closeCpaServiceModal);
    }
    if (elements.cpaServiceEditModal) {
        elements.cpaServiceEditModal.addEventListener('click', (e) => {
            if (e.target === elements.cpaServiceEditModal) closeCpaServiceModal();
        });
    }
    if (elements.cpaServiceForm) {
        elements.cpaServiceForm.addEventListener('submit', handleSaveCpaService);
    }
    if (elements.testCpaServiceBtn) {
        elements.testCpaServiceBtn.addEventListener('click', handleTestCpaService);
    }

    // Sub2API 服务管理
    if (elements.addSub2ApiServiceBtn) {
        elements.addSub2ApiServiceBtn.addEventListener('click', () => openSub2ApiServiceModal());
    }
    if (elements.closeSub2ApiServiceModal) {
        elements.closeSub2ApiServiceModal.addEventListener('click', closeSub2ApiServiceModal);
    }
    if (elements.cancelSub2ApiServiceBtn) {
        elements.cancelSub2ApiServiceBtn.addEventListener('click', closeSub2ApiServiceModal);
    }
    if (elements.sub2ApiServiceEditModal) {
        elements.sub2ApiServiceEditModal.addEventListener('click', (e) => {
            if (e.target === elements.sub2ApiServiceEditModal) closeSub2ApiServiceModal();
        });
    }
    if (elements.sub2ApiServiceForm) {
        elements.sub2ApiServiceForm.addEventListener('submit', handleSaveSub2ApiService);
    }
    if (elements.testSub2ApiServiceBtn) {
        elements.testSub2ApiServiceBtn.addEventListener('click', handleTestSub2ApiService);
    }
}

// 加载设置
async function loadSettings() {
    try {
        const data = await api.get('/settings');

        // 动态代理设置
        document.getElementById('dynamic-proxy-enabled').checked = data.proxy?.dynamic_enabled || false;
        document.getElementById('dynamic-proxy-api-url').value = data.proxy?.dynamic_api_url || '';
        document.getElementById('dynamic-proxy-api-key-header').value = data.proxy?.dynamic_api_key_header || 'X-API-Key';
        document.getElementById('dynamic-proxy-result-field').value = data.proxy?.dynamic_result_field || '';

        // 注册配置
        document.getElementById('max-retries').value = data.registration?.max_retries || 3;
        document.getElementById('timeout').value = data.registration?.timeout || 120;
        document.getElementById('password-length').value = data.registration?.default_password_length || 12;
        const entryFlowRaw = String(data.registration?.entry_flow || 'native').toLowerCase();
        const entryFlow = entryFlowRaw === 'abcard' ? 'abcard' : 'native';
        document.getElementById('registration-entry-flow').value = entryFlow;
        document.getElementById('sleep-min').value = data.registration?.sleep_min || 5;
        document.getElementById('sleep-max').value = data.registration?.sleep_max || 30;

        // 验证码等待配置
        if (data.email_code) {
            document.getElementById('email-code-timeout').value = data.email_code.timeout || 120;
            document.getElementById('email-code-poll-interval').value = data.email_code.poll_interval || 3;
        }

        // 加载 Outlook 设置
        loadOutlookSettings();

        // 系统设置（端口 + 访问密码）
        const webuiPortInput = document.getElementById('webui-port');
        if (webuiPortInput) {
            webuiPortInput.value = data.webui?.port || 1455;
        }
        const passwordInput = document.getElementById('webui-access-password');
        if (passwordInput) {
            if (data.webui?.has_access_password) {
                passwordInput.value = '';
                passwordInput.placeholder = '已配置，留空保持不变';
            } else {
                passwordInput.value = '';
                passwordInput.placeholder = '未配置，留空表示不修改';
            }
        }

    } catch (error) {
        console.error('加载设置失败:', error);
        toast.error('加载设置失败');
    }
}

// 保存系统设置（端口 + 访问控制）
async function handleSaveSystemSettings(e) {
    e.preventDefault();

    const portRaw = document.getElementById('webui-port')?.value;
    const parsedPort = parseInt(portRaw, 10);
    const accessPassword = document.getElementById('webui-access-password').value;
    const payload = {};
    if (Number.isFinite(parsedPort) && parsedPort > 0 && parsedPort <= 65535) {
        payload.port = parsedPort;
    }
    if (accessPassword) {
        payload.access_password = accessPassword;
    }

    try {
        await api.post('/settings/webui', payload);
        toast.success('系统设置已更新');
        document.getElementById('webui-access-password').value = '';
        await loadSettings();
    } catch (error) {
        console.error('保存系统设置失败:', error);
        toast.error('保存系统设置失败');
    }
}

// 加载邮箱服务
async function loadEmailServices() {
    // 检查元素是否存在
    if (!elements.emailServicesTable) return;

    try {
        const data = await api.get('/email-services');
        renderEmailServices(data.services);
    } catch (error) {
        console.error('加载邮箱服务失败:', error);
        if (elements.emailServicesTable) {
            elements.emailServicesTable.innerHTML = `
                <tr>
                    <td colspan="7">
                        <div class="empty-state">
                            <div class="empty-state-icon">❌</div>
                            <div class="empty-state-title">加载失败</div>
                        </div>
                    </td>
                </tr>
            `;
        }
    }
}

// 渲染邮箱服务
function renderEmailServices(services) {
    // 检查元素是否存在
    if (!elements.emailServicesTable) return;

    if (services.length === 0) {
        elements.emailServicesTable.innerHTML = `
            <tr>
                <td colspan="7">
                    <div class="empty-state">
                        <div class="empty-state-icon">📭</div>
                        <div class="empty-state-title">暂无配置</div>
                        <div class="empty-state-description">点击上方"添加服务"按钮添加邮箱服务</div>
                    </div>
                </td>
            </tr>
        `;
        return;
    }

    elements.emailServicesTable.innerHTML = services.map(service => `
        <tr data-service-id="${service.id}">
            <td>
                <input type="checkbox" class="service-checkbox" data-id="${service.id}"
                    onchange="updateSelectedServices()">
            </td>
            <td>${escapeHtml(service.name)}</td>
            <td>${getServiceTypeText(service.service_type)}</td>
            <td title="${service.enabled ? '已启用' : '已禁用'}">${service.enabled ? '✅' : '⭕'}</td>
            <td>${service.priority}</td>
            <td>${format.date(service.last_used)}</td>
            <td>
                <div class="action-buttons">
                    <button class="btn btn-ghost btn-sm" onclick="testService(${service.id})" title="测试">
                        🔌
                    </button>
                    <button class="btn btn-ghost btn-sm" onclick="toggleService(${service.id}, ${!service.enabled})" title="${service.enabled ? '禁用' : '启用'}">
                        ${service.enabled ? '🔒' : '🔓'}
                    </button>
                    <button class="btn btn-ghost btn-sm" onclick="deleteService(${service.id})" title="删除">
                        🗑️
                    </button>
                </div>
            </td>
        </tr>
    `).join('');
}

// 加载数据库信息
async function loadDatabaseInfo() {
    try {
        const data = await api.get('/settings/database');

        document.getElementById('db-size').textContent = `${data.database_size_mb} MB`;
        document.getElementById('db-accounts').textContent = format.number(data.accounts_count);
        document.getElementById('db-services').textContent = format.number(data.email_services_count);
        document.getElementById('db-tasks').textContent = format.number(data.tasks_count);

    } catch (error) {
        console.error('加载数据库信息失败:', error);
    }
}

// 保存注册配置
async function handleSaveRegistration(e) {
    e.preventDefault();

    const data = {
        max_retries: parseInt(document.getElementById('max-retries').value),
        timeout: parseInt(document.getElementById('timeout').value),
        default_password_length: parseInt(document.getElementById('password-length').value),
        entry_flow: document.getElementById('registration-entry-flow').value || 'native',
        sleep_min: parseInt(document.getElementById('sleep-min').value),
        sleep_max: parseInt(document.getElementById('sleep-max').value),
    };

    try {
        await api.post('/settings/registration', data);
        toast.success('注册配置已保存');
    } catch (error) {
        toast.error('保存失败: ' + error.message);
    }
}

// 保存验证码等待配置
async function handleSaveEmailCode(e) {
    e.preventDefault();

    const timeout = parseInt(document.getElementById('email-code-timeout').value);
    const pollInterval = parseInt(document.getElementById('email-code-poll-interval').value);

    // 客户端验证
    if (timeout < 30 || timeout > 600) {
        toast.error('等待超时必须在 30-600 秒之间');
        return;
    }
    if (pollInterval < 1 || pollInterval > 30) {
        toast.error('轮询间隔必须在 1-30 秒之间');
        return;
    }

    const data = {
        timeout: timeout,
        poll_interval: pollInterval
    };

    try {
        await api.post('/settings/email-code', data);
        toast.success('验证码配置已保存');
    } catch (error) {
        toast.error('保存失败: ' + error.message);
    }
}

// 备份数据库
async function handleBackup() {
    elements.backupBtn.disabled = true;
    elements.backupBtn.innerHTML = '<span class="loading-spinner"></span> 备份中...';

    try {
        const data = await api.post('/settings/database/backup');
        toast.success(`备份成功: ${data.backup_path}`);
    } catch (error) {
        toast.error('备份失败: ' + error.message);
    } finally {
        elements.backupBtn.disabled = false;
        elements.backupBtn.textContent = '💾 备份数据库';
    }
}

// 导入数据库
async function handleImportDatabase() {
    const file = elements.dbImportFile?.files?.[0];
    if (!file) return;

    const confirmed = await confirm(
        `确定导入数据库文件「${file.name}」吗？系统会先自动备份当前数据库，再执行覆盖导入。`
    );
    if (!confirmed) {
        elements.dbImportFile.value = '';
        return;
    }

    const originText = elements.importDbBtn.textContent;
    elements.importDbBtn.disabled = true;
    elements.importDbBtn.innerHTML = '<span class="loading-spinner"></span> 导入中...';

    try {
        const formData = new FormData();
        formData.append('file', file);

        const resp = await fetch('/api/settings/database/import', {
            method: 'POST',
            body: formData
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw new Error(data.detail || `HTTP ${resp.status}`);
        }

        const backupText = data.backup_path ? `，备份: ${data.backup_path}` : '';
        toast.success(`导入成功，已覆盖数据库${backupText}`);
        await loadDatabaseInfo();
    } catch (error) {
        toast.error('导入失败: ' + error.message);
    } finally {
        elements.dbImportFile.value = '';
        elements.importDbBtn.disabled = false;
        elements.importDbBtn.textContent = originText || '📥 导入数据';
    }
}

// 清理数据
async function handleCleanup() {
    const confirmed = await confirm('确定要清理过期数据吗？此操作不可恢复。');
    if (!confirmed) return;

    elements.cleanupBtn.disabled = true;
    elements.cleanupBtn.innerHTML = '<span class="loading-spinner"></span> 清理中...';

    try {
        const data = await api.post('/settings/database/cleanup?days=30');
        toast.success(data.message);
        loadDatabaseInfo();
    } catch (error) {
        toast.error('清理失败: ' + error.message);
    } finally {
        elements.cleanupBtn.disabled = false;
        elements.cleanupBtn.textContent = '🧹 清理过期数据';
    }
}

// 加载服务配置字段
async function loadServiceConfigFields(serviceType) {
    try {
        const data = await api.get('/email-services/types');
        const typeInfo = data.types.find(t => t.value === serviceType);

        if (!typeInfo) {
            elements.serviceConfigFields.innerHTML = '';
            return;
        }

        elements.serviceConfigFields.innerHTML = typeInfo.config_fields.map(field => `
            <div class="form-group">
                <label for="config-${field.name}">${field.label}</label>
                <input type="${field.name.includes('password') || field.name.includes('token') ? 'password' : 'text'}"
                       id="config-${field.name}"
                       name="${field.name}"
                       value="${field.default || ''}"
                       placeholder="${field.label}"
                       ${field.required ? 'required' : ''}>
            </div>
        `).join('');

    } catch (error) {
        console.error('加载配置字段失败:', error);
    }
}

// 添加邮箱服务
async function handleAddService(e) {
    e.preventDefault();

    const formData = new FormData(elements.addServiceForm);
    const config = {};

    elements.serviceConfigFields.querySelectorAll('input').forEach(input => {
        config[input.name] = input.value;
    });

    const data = {
        service_type: formData.get('service_type'),
        name: formData.get('name'),
        config: config,
        enabled: true,
        priority: 0,
    };

    try {
        await api.post('/email-services', data);
        toast.success('邮箱服务已添加');
        elements.addServiceModal.classList.remove('active');
        elements.addServiceForm.reset();
        loadEmailServices();
    } catch (error) {
        toast.error('添加失败: ' + error.message);
    }
}

// 测试服务
async function testService(id) {
    try {
        const data = await api.post(`/email-services/${id}/test`);
        if (data.success) {
            toast.success('服务连接正常');
        } else {
            toast.warning('服务连接失败: ' + data.message);
        }
    } catch (error) {
        toast.error('测试失败: ' + error.message);
    }
}

// 切换服务状态
async function toggleService(id, enabled) {
    try {
        const endpoint = enabled ? 'enable' : 'disable';
        await api.post(`/email-services/${id}/${endpoint}`);
        toast.success(enabled ? '服务已启用' : '服务已禁用');
        loadEmailServices();
    } catch (error) {
        toast.error('操作失败: ' + error.message);
    }
}

// 删除服务
async function deleteService(id) {
    const confirmed = await confirm('确定要删除此邮箱服务配置吗？');
    if (!confirmed) return;

    try {
        await api.delete(`/email-services/${id}`);
        toast.success('服务已删除');
        loadEmailServices();
    } catch (error) {
        toast.error('删除失败: ' + error.message);
    }
}

// 更新选中的服务
function updateSelectedServices() {
    selectedServiceIds.clear();
    document.querySelectorAll('.service-checkbox:checked').forEach(cb => {
        selectedServiceIds.add(parseInt(cb.dataset.id));
    });
}

// Outlook 批量导入
async function handleOutlookBatchImport() {
    const data = elements.outlookImportData.value.trim();
    if (!data) {
        toast.warning('请输入要导入的数据');
        return;
    }

    const enabled = document.getElementById('outlook-import-enabled').checked;
    const priority = parseInt(document.getElementById('outlook-import-priority').value) || 0;

    // 解析数据
    const lines = data.split('\n').filter(line => line.trim() && !line.trim().startsWith('#'));
    const accounts = [];
    const errors = [];

    lines.forEach((line, index) => {
        const parts = line.split('----').map(p => p.trim());
        if (parts.length < 2) {
            errors.push(`第 ${index + 1} 行格式错误`);
            return;
        }

        const account = {
            email: parts[0],
            password: parts[1],
            client_id: parts[2] || null,
            refresh_token: parts[3] || null,
            enabled: enabled,
            priority: priority
        };

        if (!account.email.includes('@')) {
            errors.push(`第 ${index + 1} 行邮箱格式错误: ${account.email}`);
            return;
        }

        accounts.push(account);
    });

    if (errors.length > 0) {
        elements.importResult.style.display = 'block';
        elements.importResult.innerHTML = `
            <div class="import-errors">${errors.map(e => `<div>${e}</div>`).join('')}</div>
        `;
        return;
    }

    elements.outlookImportBtn.disabled = true;
    elements.outlookImportBtn.innerHTML = '<span class="loading-spinner"></span> 导入中...';

    let successCount = 0;
    let failCount = 0;

    try {
        for (const account of accounts) {
            try {
                await api.post('/email-services', {
                    service_type: 'outlook',
                    name: account.email,
                    config: {
                        email: account.email,
                        password: account.password,
                        client_id: account.client_id,
                        refresh_token: account.refresh_token
                    },
                    enabled: account.enabled,
                    priority: account.priority
                });
                successCount++;
            } catch {
                failCount++;
            }
        }

        elements.importResult.style.display = 'block';
        elements.importResult.innerHTML = `
            <div class="import-stats">
                <span>✅ 成功: ${successCount}</span>
                <span>❌ 失败: ${failCount}</span>
            </div>
        `;

        toast.success(`导入完成，成功 ${successCount} 个`);
        loadEmailServices();

    } catch (error) {
        toast.error('导入失败: ' + error.message);
    } finally {
        elements.outlookImportBtn.disabled = false;
        elements.outlookImportBtn.textContent = '📥 开始导入';
    }
}

// HTML 转义
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}


// ============================================================================
// 代理列表管理
// ============================================================================

// 加载代理列表
async function loadProxies() {
    try {
        const data = await api.get('/settings/proxies');
        renderProxies(data.proxies);
    } catch (error) {
        console.error('加载代理列表失败:', error);
        elements.proxiesTable.innerHTML = `
            <tr>
                <td colspan="8">
                    <div class="empty-state">
                        <div class="empty-state-icon">❌</div>
                        <div class="empty-state-title">加载失败</div>
                    </div>
                </td>
            </tr>
        `;
    }
}

// 渲染代理列表
function renderProxies(proxies) {
    if (!proxies || proxies.length === 0) {
        elements.proxiesTable.innerHTML = `
            <tr>
                <td colspan="8">
                    <div class="empty-state">
                        <div class="empty-state-icon">🌐</div>
                        <div class="empty-state-title">暂无代理</div>
                        <div class="empty-state-description">点击"添加代理"按钮添加代理服务器</div>
                    </div>
                </td>
            </tr>
        `;
        return;
    }

    elements.proxiesTable.innerHTML = proxies.map(proxy => `
        <tr data-proxy-id="${proxy.id}">
            <td>${proxy.id}</td>
            <td>${escapeHtml(proxy.name)}</td>
            <td><span class="badge">${proxy.type.toUpperCase()}</span></td>
            <td><code>${escapeHtml(proxy.host)}:${proxy.port}</code></td>
            <td>
                ${proxy.is_default
                    ? '<span class="status-badge active">默认</span>'
                    : `<button class="btn btn-ghost btn-sm" onclick="handleSetProxyDefault(${proxy.id})" title="设为默认">设默认</button>`
                }
            </td>
            <td title="${proxy.enabled ? '已启用' : '已禁用'}">${proxy.enabled ? '✅' : '⭕'}</td>
            <td>${format.date(proxy.last_used)}</td>
            <td>
                <div style="display:flex;gap:4px;align-items:center;white-space:nowrap;">
                    <button class="btn btn-secondary btn-sm" onclick="editProxyItem(${proxy.id})">编辑</button>
                    <div class="dropdown" style="position:relative;">
                        <button class="btn btn-secondary btn-sm" onclick="event.stopPropagation();toggleSettingsMoreMenu(this)">更多</button>
                        <div class="dropdown-menu" style="min-width:80px;">
                            <a href="#" class="dropdown-item" onclick="event.preventDefault();closeSettingsMoreMenu(this);testProxyItem(${proxy.id})">测试</a>
                            <a href="#" class="dropdown-item" onclick="event.preventDefault();closeSettingsMoreMenu(this);toggleProxyItem(${proxy.id}, ${!proxy.enabled})">${proxy.enabled ? '禁用' : '启用'}</a>
                            ${!proxy.is_default ? `<a href="#" class="dropdown-item" onclick="event.preventDefault();closeSettingsMoreMenu(this);handleSetProxyDefault(${proxy.id})">设为默认</a>` : ''}
                        </div>
                    </div>
                    <button class="btn btn-danger btn-sm" onclick="deleteProxyItem(${proxy.id})">删除</button>
                </div>
            </td>
        </tr>
    `).join('');
}

function toggleSettingsMoreMenu(btn) {
    const menu = btn.nextElementSibling;
    const isActive = menu.classList.contains('active');
    document.querySelectorAll('.dropdown-menu.active').forEach(m => m.classList.remove('active'));
    if (!isActive) menu.classList.add('active');
}

function closeSettingsMoreMenu(el) {
    const menu = el.closest('.dropdown-menu');
    if (menu) menu.classList.remove('active');
}

// 设为默认代理
async function handleSetProxyDefault(id) {
    try {
        await api.post(`/settings/proxies/${id}/set-default`);
        toast.success('已设为默认代理');
        loadProxies();
    } catch (error) {
        toast.error('操作失败: ' + error.message);
    }
}

// 打开代理模态框
function openProxyModal(proxy = null) {
    elements.proxyModalTitle.textContent = proxy ? '编辑代理' : '添加代理';
    elements.proxyItemForm.reset();

    document.getElementById('proxy-item-id').value = proxy ? proxy.id : '';

    if (proxy) {
        document.getElementById('proxy-item-name').value = proxy.name || '';
        document.getElementById('proxy-item-type').value = proxy.type || 'http';
        document.getElementById('proxy-item-host').value = proxy.host || '';
        document.getElementById('proxy-item-port').value = proxy.port || '';
        document.getElementById('proxy-item-username').value = proxy.username || '';
        document.getElementById('proxy-item-password').value = '';
    }

    elements.addProxyModal.classList.add('active');
}

// 关闭代理模态框
function closeProxyModal() {
    elements.addProxyModal.classList.remove('active');
    elements.proxyItemForm.reset();
}

function openProxyBatchImportModal() {
    if (!elements.proxyBatchImportModal) return;
    if (elements.proxyBatchImportForm) {
        elements.proxyBatchImportForm.reset();
    }
    if (elements.proxyBatchImportResult) {
        elements.proxyBatchImportResult.style.display = 'none';
        elements.proxyBatchImportResult.innerHTML = '';
    }
    elements.proxyBatchImportModal.classList.add('active');
}

function closeProxyBatchImportModal() {
    if (!elements.proxyBatchImportModal) return;
    elements.proxyBatchImportModal.classList.remove('active');
}

function renderProxyBatchImportResult(result) {
    if (!elements.proxyBatchImportResult) return;
    const errors = Array.isArray(result?.errors) ? result.errors : [];
    const preview = errors.slice(0, 20).map(item => {
        const line = Number(item?.line || 0);
        const raw = escapeHtml(String(item?.raw || ''));
        const error = escapeHtml(String(item?.error || '解析失败'));
        return `<li>第 ${line} 行：${error}${raw ? `（${raw}）` : ''}</li>`;
    }).join('');
    elements.proxyBatchImportResult.innerHTML = `
        <div style="display:flex;gap:12px;flex-wrap:wrap;">
            <span>✅ 新增: <strong>${result.created || 0}</strong></span>
            <span>🔄 更新: <strong>${result.updated || 0}</strong></span>
            <span>⏭️ 跳过: <strong>${result.skipped || 0}</strong></span>
            <span>❌ 失败: <strong>${result.failed || 0}</strong></span>
        </div>
        ${preview ? `<div style="margin-top:8px;"><strong>错误明细（最多 20 条）：</strong><ul style="margin:6px 0 0 18px;">${preview}</ul></div>` : ''}
    `;
    elements.proxyBatchImportResult.style.display = '';
}

async function handleProxyBatchImport(e) {
    e.preventDefault();
    const content = String(elements.proxyBatchImportText?.value || '').trim();
    if (!content) {
        toast.warning('请先粘贴代理文本');
        return;
    }

    const submitBtn = document.getElementById('submit-proxy-batch-import-btn');
    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="loading-spinner"></span> 导入中...';
    }

    try {
        const payload = {
            content,
            default_type: String(document.getElementById('proxy-batch-default-type')?.value || 'http'),
            enabled: Boolean(document.getElementById('proxy-batch-import-enabled')?.checked),
            overwrite_existing: Boolean(document.getElementById('proxy-batch-overwrite-existing')?.checked),
        };
        const result = await api.post('/settings/proxies/batch-import', payload);
        renderProxyBatchImportResult(result);
        await loadProxies();
        if ((result.failed || 0) > 0) {
            toast.warning(`导入完成：新增 ${result.created || 0}，更新 ${result.updated || 0}，失败 ${result.failed || 0}`);
        } else {
            toast.success(`导入完成：新增 ${result.created || 0}，更新 ${result.updated || 0}，跳过 ${result.skipped || 0}`);
        }
    } catch (error) {
        toast.error('批量导入失败: ' + error.message);
    } finally {
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = '📥 开始导入';
        }
    }
}

// 保存代理
async function handleSaveProxyItem(e) {
    e.preventDefault();

    const proxyId = document.getElementById('proxy-item-id').value;
    const data = {
        name: document.getElementById('proxy-item-name').value,
        type: document.getElementById('proxy-item-type').value,
        host: document.getElementById('proxy-item-host').value,
        port: parseInt(document.getElementById('proxy-item-port').value),
        username: document.getElementById('proxy-item-username').value || null,
        password: document.getElementById('proxy-item-password').value || null,
        enabled: true
    };

    try {
        if (proxyId) {
            await api.patch(`/settings/proxies/${proxyId}`, data);
            toast.success('代理已更新');
        } else {
            await api.post('/settings/proxies', data);
            toast.success('代理已添加');
        }
        closeProxyModal();
        loadProxies();
    } catch (error) {
        toast.error('保存失败: ' + error.message);
    }
}

// 编辑代理
async function editProxyItem(id) {
    try {
        const proxy = await api.get(`/settings/proxies/${id}`);
        openProxyModal(proxy);
    } catch (error) {
        toast.error('获取代理信息失败');
    }
}

// 测试单个代理
async function testProxyItem(id) {
    try {
        const result = await api.post(`/settings/proxies/${id}/test`);
        if (result.success) {
            toast.success(result.message);
        } else {
            toast.error(result.message);
        }
    } catch (error) {
        toast.error('测试失败: ' + error.message);
    }
}

// 切换代理状态
async function toggleProxyItem(id, enabled) {
    try {
        const endpoint = enabled ? 'enable' : 'disable';
        await api.post(`/settings/proxies/${id}/${endpoint}`);
        toast.success(enabled ? '代理已启用' : '代理已禁用');
        loadProxies();
    } catch (error) {
        toast.error('操作失败: ' + error.message);
    }
}

// 删除代理
async function deleteProxyItem(id) {
    const confirmed = await confirm('确定要删除此代理吗？');
    if (!confirmed) return;

    try {
        await api.delete(`/settings/proxies/${id}`);
        toast.success('代理已删除');
        loadProxies();
    } catch (error) {
        toast.error('删除失败: ' + error.message);
    }
}

// 测试所有代理
async function handleTestAllProxies() {
    elements.testAllProxiesBtn.disabled = true;
    elements.testAllProxiesBtn.innerHTML = '<span class="loading-spinner"></span> 测试中...';

    try {
        const result = await api.post('/settings/proxies/test-all');
        toast.info(`测试完成: 成功 ${result.success}, 失败 ${result.failed}`);
        loadProxies();
    } catch (error) {
        toast.error('测试失败: ' + error.message);
    } finally {
        elements.testAllProxiesBtn.disabled = false;
        elements.testAllProxiesBtn.textContent = '🔌 测试全部';
    }
}


// ============================================================================
// Outlook 设置管理
// ============================================================================

// 加载 Outlook 设置
async function loadOutlookSettings() {
    try {
        const data = await api.get('/settings/outlook');
        const el = document.getElementById('outlook-default-client-id');
        if (el) el.value = data.default_client_id || '';
    } catch (error) {
        console.error('加载 Outlook 设置失败:', error);
    }
}

// 保存 Outlook 设置
async function handleSaveOutlookSettings(e) {
    e.preventDefault();
    const data = {
        default_client_id: document.getElementById('outlook-default-client-id').value
    };
    try {
        await api.post('/settings/outlook', data);
        toast.success('Outlook 设置已保存');
    } catch (error) {
        toast.error('保存失败: ' + error.message);
    }
}

// ============== 动态代理设置 ==============

async function handleSaveDynamicProxy(e) {
    e.preventDefault();
    const data = {
        enabled: document.getElementById('dynamic-proxy-enabled').checked,
        api_url: document.getElementById('dynamic-proxy-api-url').value.trim(),
        api_key: document.getElementById('dynamic-proxy-api-key').value || null,
        api_key_header: document.getElementById('dynamic-proxy-api-key-header').value.trim() || 'X-API-Key',
        result_field: document.getElementById('dynamic-proxy-result-field').value.trim()
    };
    try {
        await api.post('/settings/proxy/dynamic', data);
        toast.success('动态代理设置已保存');
        document.getElementById('dynamic-proxy-api-key').value = '';
    } catch (error) {
        toast.error('保存失败: ' + error.message);
    }
}

async function handleTestDynamicProxy() {
    const apiUrl = document.getElementById('dynamic-proxy-api-url').value.trim();
    if (!apiUrl) {
        toast.warning('请先填写动态代理 API 地址');
        return;
    }
    const btn = elements.testDynamicProxyBtn;
    btn.disabled = true;
    btn.textContent = '测试中...';
    try {
        const result = await api.post('/settings/proxy/dynamic/test', {
            api_url: apiUrl,
            api_key: document.getElementById('dynamic-proxy-api-key').value || null,
            api_key_header: document.getElementById('dynamic-proxy-api-key-header').value.trim() || 'X-API-Key',
            result_field: document.getElementById('dynamic-proxy-result-field').value.trim()
        });
        if (result.success) {
            toast.success(result.message);
        } else {
            toast.error(result.message);
        }
    } catch (error) {
        toast.error('测试失败: ' + error.message);
    } finally {
        btn.disabled = false;
        btn.textContent = '🔌 测试动态代理';
    }
}

// ============== Team Manager 服务管理 ==============

async function loadTmServices() {
    if (!elements.tmServicesTable) return;
    try {
        const services = await api.get('/tm-services');
        renderTmServicesTable(services);
    } catch (e) {
        elements.tmServicesTable.innerHTML = `<tr><td colspan="5" style="text-align:center;color:var(--danger-color);">${e.message}</td></tr>`;
    }
}

function renderTmServicesTable(services) {
    if (!services || services.length === 0) {
        elements.tmServicesTable.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-muted);padding:20px;">暂无 Team Manager 服务，点击「添加服务」新增</td></tr>';
        return;
    }
    elements.tmServicesTable.innerHTML = services.map(s => `
        <tr>
            <td>${escapeHtml(s.name)}</td>
            <td style="font-size:0.85rem;color:var(--text-muted);">${escapeHtml(s.api_url)}</td>
            <td style="text-align:center;" title="${s.enabled ? '已启用' : '已禁用'}">${s.enabled ? '✅' : '⭕'}</td>
            <td style="text-align:center;">${s.priority}</td>
            <td style="white-space:nowrap;">
                <button class="btn btn-secondary btn-sm" onclick="editTmService(${s.id})">编辑</button>
                <button class="btn btn-secondary btn-sm" onclick="testTmServiceById(${s.id})">测试</button>
                <button class="btn btn-danger btn-sm" onclick="deleteTmService(${s.id}, '${escapeHtml(s.name)}')">删除</button>
            </td>
        </tr>
    `).join('');
}

function openTmServiceModal(service = null) {
    document.getElementById('tm-service-id').value = service ? service.id : '';
    document.getElementById('tm-service-name').value = service ? service.name : '';
    document.getElementById('tm-service-url').value = service ? service.api_url : '';
    document.getElementById('tm-service-key').value = '';
    document.getElementById('tm-service-priority').value = service ? service.priority : 0;
    document.getElementById('tm-service-enabled').checked = service ? service.enabled : true;
    if (service) {
        document.getElementById('tm-service-key').placeholder = service.has_key ? '已配置，留空保持不变' : '请输入 API Key';
    } else {
        document.getElementById('tm-service-key').placeholder = '请输入 API Key';
    }
    elements.tmServiceModalTitle.textContent = service ? '编辑 Team Manager 服务' : '添加 Team Manager 服务';
    elements.tmServiceEditModal.classList.add('active');
}

function closeTmServiceModal() {
    elements.tmServiceEditModal.classList.remove('active');
}

async function editTmService(id) {
    try {
        const service = await api.get(`/tm-services/${id}`);
        openTmServiceModal(service);
    } catch (e) {
        toast.error('获取服务信息失败: ' + e.message);
    }
}

async function handleSaveTmService(e) {
    e.preventDefault();
    const id = document.getElementById('tm-service-id').value;
    const name = document.getElementById('tm-service-name').value.trim();
    const apiUrl = document.getElementById('tm-service-url').value.trim();
    const apiKey = document.getElementById('tm-service-key').value.trim();
    const priority = parseInt(document.getElementById('tm-service-priority').value) || 0;
    const enabled = document.getElementById('tm-service-enabled').checked;

    if (!name || !apiUrl) {
        toast.error('名称和 API URL 不能为空');
        return;
    }
    if (!id && !apiKey) {
        toast.error('新增服务时 API Key 不能为空');
        return;
    }

    try {
        const payload = { name, api_url: apiUrl, priority, enabled };
        if (apiKey) payload.api_key = apiKey;

        if (id) {
            await api.patch(`/tm-services/${id}`, payload);
            toast.success('服务已更新');
        } else {
            payload.api_key = apiKey;
            await api.post('/tm-services', payload);
            toast.success('服务已添加');
        }
        closeTmServiceModal();
        loadTmServices();
    } catch (e) {
        toast.error('保存失败: ' + e.message);
    }
}

async function deleteTmService(id, name) {
    const confirmed = await confirm(`确定要删除 Team Manager 服务「${name}」吗？`);
    if (!confirmed) return;
    try {
        await api.delete(`/tm-services/${id}`);
        toast.success('已删除');
        loadTmServices();
    } catch (e) {
        toast.error('删除失败: ' + e.message);
    }
}

async function testTmServiceById(id) {
    try {
        const result = await api.post(`/tm-services/${id}/test`);
        if (result.success) {
            toast.success(result.message);
        } else {
            toast.error(result.message);
        }
    } catch (e) {
        toast.error('测试失败: ' + e.message);
    }
}

async function handleTestTmService() {
    const apiUrl = document.getElementById('tm-service-url').value.trim();
    const apiKey = document.getElementById('tm-service-key').value.trim();
    const id = document.getElementById('tm-service-id').value;

    if (!apiUrl) {
        toast.error('请先填写 API URL');
        return;
    }
    if (!id && !apiKey) {
        toast.error('请先填写 API Key');
        return;
    }

    elements.testTmServiceBtn.disabled = true;
    elements.testTmServiceBtn.textContent = '测试中...';

    try {
        let result;
        if (id && !apiKey) {
            result = await api.post(`/tm-services/${id}/test`);
        } else {
            result = await api.post('/tm-services/test-connection', { api_url: apiUrl, api_key: apiKey });
        }
        if (result.success) {
            toast.success(result.message);
        } else {
            toast.error(result.message);
        }
    } catch (e) {
        toast.error('测试失败: ' + e.message);
    } finally {
        elements.testTmServiceBtn.disabled = false;
        elements.testTmServiceBtn.textContent = '🔌 测试连接';
    }
}


// ============== CPA 服务管理 ==============

async function loadCpaServices() {
    if (!elements.cpaServicesTable) return;
    try {
        const services = await api.get('/cpa-services');
        renderCpaServicesTable(services);
    } catch (e) {
        elements.cpaServicesTable.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--danger-color);">${e.message}</td></tr>`;
    }
}

function renderCpaServicesTable(services) {
    if (!services || services.length === 0) {
        elements.cpaServicesTable.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-muted);padding:20px;">暂无 CPA 服务，点击「添加服务」新增</td></tr>';
        return;
    }
    elements.cpaServicesTable.innerHTML = services.map(s => `
        <tr>
            <td>${escapeHtml(s.name)}</td>
            <td style="font-size:0.85rem;color:var(--text-muted);">${escapeHtml(s.api_url)}</td>
            <td style="font-size:0.85rem;color:var(--text-muted);">${escapeHtml(s.proxy_url || '-')}</td>
            <td style="text-align:center;" title="${s.enabled ? '已启用' : '已禁用'}">${s.enabled ? '✅' : '⭕'}</td>
            <td style="text-align:center;">${s.priority}</td>
            <td style="white-space:nowrap;">
                <button class="btn btn-secondary btn-sm" onclick="editCpaService(${s.id})">编辑</button>
                <button class="btn btn-secondary btn-sm" onclick="testCpaServiceById(${s.id})">测试</button>
                <button class="btn btn-danger btn-sm" onclick="deleteCpaService(${s.id}, '${escapeHtml(s.name)}')">删除</button>
            </td>
        </tr>
    `).join('');
}

function openCpaServiceModal(service = null) {
    document.getElementById('cpa-service-id').value = service ? service.id : '';
    document.getElementById('cpa-service-name').value = service ? service.name : '';
    document.getElementById('cpa-service-url').value = service ? service.api_url : '';
    document.getElementById('cpa-service-token').value = '';
    document.getElementById('cpa-service-proxy-url').value = service ? (service.proxy_url || '') : '';
    document.getElementById('cpa-service-priority').value = service ? service.priority : 0;
    document.getElementById('cpa-service-enabled').checked = service ? service.enabled : true;
    elements.cpaServiceModalTitle.textContent = service ? '编辑 CPA 服务' : '添加 CPA 服务';
    elements.cpaServiceEditModal.classList.add('active');
}

function closeCpaServiceModal() {
    elements.cpaServiceEditModal.classList.remove('active');
}

async function editCpaService(id) {
    try {
        const service = await api.get(`/cpa-services/${id}`);
        openCpaServiceModal(service);
    } catch (e) {
        toast.error('获取服务信息失败: ' + e.message);
    }
}

async function handleSaveCpaService(e) {
    e.preventDefault();
    const id = document.getElementById('cpa-service-id').value;
    const name = document.getElementById('cpa-service-name').value.trim();
    const apiUrl = document.getElementById('cpa-service-url').value.trim();
    const apiToken = document.getElementById('cpa-service-token').value.trim();
    const proxyUrl = document.getElementById('cpa-service-proxy-url').value.trim();
    const priority = parseInt(document.getElementById('cpa-service-priority').value) || 0;
    const enabled = document.getElementById('cpa-service-enabled').checked;

    if (!name || !apiUrl) {
        toast.error('名称和 API URL 不能为空');
        return;
    }
    if (!id && !apiToken) {
        toast.error('新增服务时 API Token 不能为空');
        return;
    }

    try {
        const payload = { name, api_url: apiUrl, proxy_url: proxyUrl, priority, enabled };
        if (apiToken) payload.api_token = apiToken;

        if (id) {
            await api.patch(`/cpa-services/${id}`, payload);
            toast.success('服务已更新');
        } else {
            payload.api_token = apiToken;
            await api.post('/cpa-services', payload);
            toast.success('服务已添加');
        }
        closeCpaServiceModal();
        loadCpaServices();
    } catch (e) {
        toast.error('保存失败: ' + e.message);
    }
}

async function deleteCpaService(id, name) {
    const confirmed = await confirm(`确定要删除 CPA 服务「${name}」吗？`);
    if (!confirmed) return;
    try {
        await api.delete(`/cpa-services/${id}`);
        toast.success('已删除');
        loadCpaServices();
    } catch (e) {
        toast.error('删除失败: ' + e.message);
    }
}

async function testCpaServiceById(id) {
    try {
        const result = await api.post(`/cpa-services/${id}/test`);
        if (result.success) {
            toast.success(result.message);
        } else {
            toast.error(result.message);
        }
    } catch (e) {
        toast.error('测试失败: ' + e.message);
    }
}

async function handleTestCpaService() {
    const apiUrl = document.getElementById('cpa-service-url').value.trim();
    const apiToken = document.getElementById('cpa-service-token').value.trim();
    const id = document.getElementById('cpa-service-id').value;

    if (!apiUrl) {
        toast.error('请先填写 API URL');
        return;
    }
    // 新增时必须有 token，编辑时 token 可为空（用已保存的）
    if (!id && !apiToken) {
        toast.error('请先填写 API Token');
        return;
    }

    elements.testCpaServiceBtn.disabled = true;
    elements.testCpaServiceBtn.textContent = '测试中...';

    try {
        let result;
        if (id && !apiToken) {
            // 编辑时未填 token，直接测试已保存的服务
            result = await api.post(`/cpa-services/${id}/test`);
        } else {
            result = await api.post('/cpa-services/test-connection', { api_url: apiUrl, api_token: apiToken });
        }
        if (result.success) {
            toast.success(result.message);
        } else {
            toast.error(result.message);
        }
    } catch (e) {
        toast.error('测试失败: ' + e.message);
    } finally {
        elements.testCpaServiceBtn.disabled = false;
        elements.testCpaServiceBtn.textContent = '🔌 测试连接';
    }
}

// ============================================================================
// Sub2API 服务管理
// ============================================================================

let _sub2apiEditingId = null;

async function loadSub2ApiServices() {
    try {
        const services = await api.get('/sub2api-services');
        renderSub2ApiServices(services);
    } catch (e) {
        if (elements.sub2ApiServicesTable) {
            elements.sub2ApiServicesTable.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-muted);padding:20px;">加载失败</td></tr>';
        }
    }
}

function renderSub2ApiServices(services) {
    if (!elements.sub2ApiServicesTable) return;
    if (!services || services.length === 0) {
        elements.sub2ApiServicesTable.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-muted);padding:20px;">暂无 Sub2API 服务，点击「添加服务」新增</td></tr>';
        return;
    }
    elements.sub2ApiServicesTable.innerHTML = services.map(s => `
        <tr>
            <td>${escapeHtml(s.name)}</td>
            <td style="font-size:0.85rem;color:var(--text-muted);">${escapeHtml(s.api_url)}</td>
            <td style="text-align:center;">${String(s.target_type || 'sub2api').toLowerCase() === 'newapi' ? 'newApi' : 'Sub2Api'}</td>
            <td style="text-align:center;" title="${s.enabled ? '已启用' : '已禁用'}">${s.enabled ? '✅' : '⭕'}</td>
            <td style="text-align:center;">${s.priority}</td>
            <td style="white-space:nowrap;">
                <button class="btn btn-secondary btn-sm" onclick="editSub2ApiService(${s.id})">编辑</button>
                <button class="btn btn-secondary btn-sm" onclick="testSub2ApiServiceById(${s.id})">测试</button>
                <button class="btn btn-danger btn-sm" onclick="deleteSub2ApiService(${s.id}, '${escapeHtml(s.name)}')">删除</button>
            </td>
        </tr>
    `).join('');
}

function openSub2ApiServiceModal(svc = null) {
    _sub2apiEditingId = svc ? svc.id : null;
    elements.sub2ApiServiceModalTitle.textContent = svc ? '编辑 Sub2API 服务' : '添加 Sub2API 服务';
    elements.sub2ApiServiceForm.reset();
    document.getElementById('sub2api-service-id').value = svc ? svc.id : '';
    if (svc) {
        document.getElementById('sub2api-service-name').value = svc.name || '';
        document.getElementById('sub2api-service-url').value = svc.api_url || '';
        document.getElementById('sub2api-service-priority').value = svc.priority ?? 0;
        document.getElementById('sub2api-service-enabled').checked = svc.enabled !== false;
        document.getElementById('sub2api-service-target-type').value = String(svc.target_type || 'sub2api').toLowerCase() === 'newapi' ? 'newapi' : 'sub2api';
        document.getElementById('sub2api-service-key').placeholder = svc.has_key ? '已配置，留空保持不变' : '请输入 API Key';
    } else {
        document.getElementById('sub2api-service-target-type').value = 'sub2api';
    }
    elements.sub2ApiServiceEditModal.classList.add('active');
}

function closeSub2ApiServiceModal() {
    elements.sub2ApiServiceEditModal.classList.remove('active');
    elements.sub2ApiServiceForm.reset();
    _sub2apiEditingId = null;
}

async function editSub2ApiService(id) {
    try {
        const svc = await api.get(`/sub2api-services/${id}`);
        openSub2ApiServiceModal(svc);
    } catch (e) {
        toast.error('加载失败: ' + e.message);
    }
}

async function deleteSub2ApiService(id, name) {
    const confirmed = await confirm(`确认删除 Sub2API 服务「${name}」吗？`, "删除 Sub2API 服务");
    if (!confirmed) return;
    try {
        await api.delete(`/sub2api-services/${id}`);
        toast.success('服务已删除');
        loadSub2ApiServices();
    } catch (e) {
        toast.error('删除失败: ' + e.message);
    }
}

async function handleSaveSub2ApiService(e) {
    e.preventDefault();
    const id = document.getElementById('sub2api-service-id').value;
    const data = {
        name: document.getElementById('sub2api-service-name').value,
        api_url: document.getElementById('sub2api-service-url').value,
        api_key: document.getElementById('sub2api-service-key').value || undefined,
        target_type: document.getElementById('sub2api-service-target-type').value || 'sub2api',
        priority: parseInt(document.getElementById('sub2api-service-priority').value) || 0,
        enabled: document.getElementById('sub2api-service-enabled').checked,
    };
    if (!id && !data.api_key) {
        toast.error('请填写 API Key');
        return;
    }
    if (!data.api_key) delete data.api_key;

    try {
        if (id) {
            await api.patch(`/sub2api-services/${id}`, data);
            toast.success('服务已更新');
        } else {
            await api.post('/sub2api-services', data);
            toast.success('服务已添加');
        }
        closeSub2ApiServiceModal();
        loadSub2ApiServices();
    } catch (e) {
        toast.error('保存失败: ' + e.message);
    }
}

async function testSub2ApiServiceById(id) {
    try {
        const result = await api.post(`/sub2api-services/${id}/test`);
        if (result.success) {
            toast.success(result.message);
        } else {
            toast.error(result.message);
        }
    } catch (e) {
        toast.error('测试失败: ' + e.message);
    }
}

async function handleTestSub2ApiService() {
    const apiUrl = document.getElementById('sub2api-service-url').value.trim();
    const apiKey = document.getElementById('sub2api-service-key').value.trim();
    const id = document.getElementById('sub2api-service-id').value;

    if (!apiUrl) {
        toast.error('请先填写 API URL');
        return;
    }
    if (!id && !apiKey) {
        toast.error('请先填写 API Key');
        return;
    }

    elements.testSub2ApiServiceBtn.disabled = true;
    elements.testSub2ApiServiceBtn.textContent = '测试中...';

    try {
        let result;
        if (id && !apiKey) {
            result = await api.post(`/sub2api-services/${id}/test`);
        } else {
            result = await api.post('/sub2api-services/test-connection', { api_url: apiUrl, api_key: apiKey });
        }
        if (result.success) {
            toast.success(result.message);
        } else {
            toast.error(result.message);
        }
    } catch (e) {
        toast.error('测试失败: ' + e.message);
    } finally {
        elements.testSub2ApiServiceBtn.disabled = false;
        elements.testSub2ApiServiceBtn.textContent = '🔌 测试连接';
    }
}

function escapeHtml(text) {
    if (!text) return '';
    const d = document.createElement('div');
    d.textContent = text;
    return d.innerHTML;
}
