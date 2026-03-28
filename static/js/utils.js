/**
 * 通用工具库
 * 包含 Toast 通知、主题切换、工具函数等
 */

// ============================================
// Toast 通知系统
// ============================================

class ToastManager {
    constructor() {
        this.container = null;
        this.init();
    }

    init() {
        this.container = document.createElement('div');
        this.container.className = 'toast-container';
        document.body.appendChild(this.container);
    }

    show(message, type = 'info', duration = 4000) {
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;

        const icon = this.getIcon(type);
        toast.innerHTML = `
            <span class="toast-icon">${icon}</span>
            <span class="toast-message">${this.escapeHtml(message)}</span>
            <button class="toast-close" onclick="this.parentElement.remove()">&times;</button>
        `;

        this.container.appendChild(toast);

        // 自动移除
        setTimeout(() => {
            toast.style.animation = 'slideOut 0.3s ease forwards';
            setTimeout(() => toast.remove(), 300);
        }, duration);

        return toast;
    }

    getIcon(type) {
        const icons = {
            success: '✓',
            error: '✕',
            warning: '⚠',
            info: 'ℹ'
        };
        return icons[type] || icons.info;
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    success(message, duration) {
        return this.show(message, 'success', duration);
    }

    error(message, duration) {
        return this.show(message, 'error', duration);
    }

    warning(message, duration) {
        return this.show(message, 'warning', duration);
    }

    info(message, duration) {
        return this.show(message, 'info', duration);
    }
}

// 全局 Toast 实例
const toast = new ToastManager();

// ============================================
// 主题管理
// ============================================

class ThemeManager {
    constructor() {
        this.theme = this.loadTheme();
        this.applyTheme();
    }

    loadTheme() {
        return localStorage.getItem('theme') || 'light';
    }

    saveTheme(theme) {
        localStorage.setItem('theme', theme);
    }

    applyTheme() {
        document.documentElement.setAttribute('data-theme', this.theme);
        this.updateToggleButtons();
    }

    toggle() {
        this.theme = this.theme === 'light' ? 'dark' : 'light';
        this.saveTheme(this.theme);
        this.applyTheme();
    }

    setTheme(theme) {
        this.theme = theme;
        this.saveTheme(theme);
        this.applyTheme();
    }

    updateToggleButtons() {
        const buttons = document.querySelectorAll('.theme-toggle');
        buttons.forEach(btn => {
            btn.innerHTML = this.theme === 'light' ? '🌙' : '☀️';
            btn.title = this.theme === 'light' ? '切换到暗色模式' : '切换到亮色模式';
        });
    }
}

// 全局主题实例
const theme = new ThemeManager();

// ============================================
// 加载状态管理
// ============================================

class LoadingManager {
    constructor() {
        this.activeLoaders = new Set();
    }

    show(element, text = '加载中...') {
        if (typeof element === 'string') {
            element = document.getElementById(element);
        }
        if (!element) return;

        element.classList.add('loading');
        element.dataset.originalText = element.innerHTML;
        element.innerHTML = `<span class="loading-spinner"></span> ${text}`;
        element.disabled = true;
        this.activeLoaders.add(element);
    }

    hide(element) {
        if (typeof element === 'string') {
            element = document.getElementById(element);
        }
        if (!element) return;

        element.classList.remove('loading');
        if (element.dataset.originalText) {
            element.innerHTML = element.dataset.originalText;
            delete element.dataset.originalText;
        }
        element.disabled = false;
        this.activeLoaders.delete(element);
    }

    hideAll() {
        this.activeLoaders.forEach(element => this.hide(element));
    }
}

const loading = new LoadingManager();

// ============================================
// API 请求封装
// ============================================

class ApiClient {
    constructor(baseUrl = '/api') {
        this.baseUrl = baseUrl;
    }

    async request(path, options = {}) {
        const url = `${this.baseUrl}${path}`;

        const defaultOptions = {
            headers: {
                'Content-Type': 'application/json',
            },
        };

        const finalOptions = { ...defaultOptions, ...options };

        if (finalOptions.body && typeof finalOptions.body === 'object') {
            finalOptions.body = JSON.stringify(finalOptions.body);
        }

        try {
            const response = await fetch(url, finalOptions);
            const data = await response.json().catch(() => ({}));

            if (!response.ok) {
                const error = new Error(data.detail || `HTTP ${response.status}`);
                error.response = response;
                error.data = data;
                throw error;
            }

            return data;
        } catch (error) {
            // 网络错误处理
            if (!error.response) {
                toast.error('网络连接失败，请检查网络');
            }
            throw error;
        }
    }

    get(path, options = {}) {
        return this.request(path, { ...options, method: 'GET' });
    }

    post(path, body, options = {}) {
        return this.request(path, { ...options, method: 'POST', body });
    }

    put(path, body, options = {}) {
        return this.request(path, { ...options, method: 'PUT', body });
    }

    patch(path, body, options = {}) {
        return this.request(path, { ...options, method: 'PATCH', body });
    }

    delete(path, options = {}) {
        return this.request(path, { ...options, method: 'DELETE' });
    }
}

const api = new ApiClient();

// ============================================
// 事件委托助手
// ============================================

function delegate(element, eventType, selector, handler) {
    element.addEventListener(eventType, (e) => {
        const target = e.target.closest(selector);
        if (target && element.contains(target)) {
            handler.call(target, e, target);
        }
    });
}

// ============================================
// 防抖和节流
// ============================================

function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

function throttle(func, limit) {
    let inThrottle;
    return function executedFunction(...args) {
        if (!inThrottle) {
            func(...args);
            inThrottle = true;
            setTimeout(() => inThrottle = false, limit);
        }
    };
}

// ============================================
// 格式化工具
// ============================================

const format = {
    date(dateStr) {
        if (!dateStr) return '-';
        const date = new Date(dateStr);
        return date.toLocaleString('zh-CN', {
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit'
        });
    },

    dateShort(dateStr) {
        if (!dateStr) return '-';
        const date = new Date(dateStr);
        return date.toLocaleDateString('zh-CN');
    },

    relativeTime(dateStr) {
        if (!dateStr) return '-';
        const date = new Date(dateStr);
        const now = new Date();
        const diff = now - date;
        const seconds = Math.floor(diff / 1000);
        const minutes = Math.floor(seconds / 60);
        const hours = Math.floor(minutes / 60);
        const days = Math.floor(hours / 24);

        if (seconds < 60) return '刚刚';
        if (minutes < 60) return `${minutes} 分钟前`;
        if (hours < 24) return `${hours} 小时前`;
        if (days < 7) return `${days} 天前`;
        return this.dateShort(dateStr);
    },

    bytes(bytes) {
        if (bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    },

    number(num) {
        if (num === null || num === undefined) return '-';
        return num.toLocaleString('zh-CN');
    }
};

// ============================================
// 状态映射
// ============================================

const statusMap = {
    account: {
        active: { text: '活跃', class: 'active' },
        expired: { text: '过期', class: 'expired' },
        banned: { text: '封禁', class: 'banned' },
        failed: { text: '失败', class: 'failed' }
    },
    task: {
        pending: { text: '等待中', class: 'pending' },
        running: { text: '运行中', class: 'running' },
        completed: { text: '已完成', class: 'completed' },
        failed: { text: '失败', class: 'failed' },
        cancelled: { text: '已取消', class: 'disabled' }
    },
    service: {
        tempmail: 'Tempmail.lol',
        yyds_mail: 'YYDS Mail',
        outlook: 'Outlook',
        moe_mail: 'MoeMail',
        temp_mail: 'Temp-Mail（自部署）',
        duck_mail: 'DuckMail',
        freemail: 'Freemail',
        imap_mail: 'IMAP 邮箱'
    }
};

function getStatusText(type, status) {
    return statusMap[type]?.[status]?.text || status;
}

function getStatusClass(type, status) {
    return statusMap[type]?.[status]?.class || '';
}

function getServiceTypeText(type) {
    return statusMap.service[type] || type;
}

const accountStatusIconMap = {
    active:  { icon: '🟢', title: '活跃' },
    expired: { icon: '🟡', title: '过期' },
    banned:  { icon: '🔴', title: '封禁' },
    failed:  { icon: '❌', title: '失败' },
};

function getStatusIcon(status) {
    const s = accountStatusIconMap[status];
    if (!s) return `<span title="${status}">⚪</span>`;
    return `<span title="${s.title}">${s.icon}</span>`;
}

// ============================================
// 确认对话框
// ============================================

function confirm(message, title = '确认操作') {
    return new Promise((resolve) => {
        const modal = document.createElement('div');
        modal.className = 'modal active';
        modal.innerHTML = `
            <div class="modal-content" style="max-width: 400px;">
                <div class="modal-header">
                    <h3>${title}</h3>
                </div>
                <div class="modal-body">
                    <p style="margin-bottom: var(--spacing-lg);">${message}</p>
                    <div class="form-actions" style="margin-top: 0; padding-top: 0; border-top: none;">
                        <button class="btn btn-secondary" id="confirm-cancel">取消</button>
                        <button class="btn btn-danger" id="confirm-ok">确认</button>
                    </div>
                </div>
            </div>
        `;

        document.body.appendChild(modal);

        const cancelBtn = modal.querySelector('#confirm-cancel');
        const okBtn = modal.querySelector('#confirm-ok');

        cancelBtn.onclick = () => {
            modal.remove();
            resolve(false);
        };

        okBtn.onclick = () => {
            modal.remove();
            resolve(true);
        };

        modal.onclick = (e) => {
            if (e.target === modal) {
                modal.remove();
                resolve(false);
            }
        };
    });
}

// ============================================
// 复制到剪贴板
// ============================================

async function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
        try {
            await navigator.clipboard.writeText(text);
            toast.success('已复制到剪贴板');
            return true;
        } catch (err) {
            // 降级到 execCommand
        }
    }
    try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none;';
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        const ok = document.execCommand('copy');
        document.body.removeChild(ta);
        if (ok) {
            toast.success('已复制到剪贴板');
            return true;
        }
        throw new Error('execCommand failed');
    } catch (err) {
        toast.error('复制失败');
        return false;
    }
}

// ============================================
// 本地存储助手
// ============================================

const storage = {
    get(key, defaultValue = null) {
        try {
            const value = localStorage.getItem(key);
            return value ? JSON.parse(value) : defaultValue;
        } catch {
            return defaultValue;
        }
    },

    set(key, value) {
        try {
            localStorage.setItem(key, JSON.stringify(value));
            return true;
        } catch {
            return false;
        }
    },

    remove(key) {
        localStorage.removeItem(key);
    }
};

// ============================================
// 页面初始化
// ============================================

document.addEventListener('DOMContentLoaded', () => {
    // 初始化主题
    theme.applyTheme();

    // 全局键盘快捷键
    document.addEventListener('keydown', (e) => {
        // Ctrl/Cmd + K: 聚焦搜索
        if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
            e.preventDefault();
            const searchInput = document.querySelector('#search-input, [type="search"]');
            if (searchInput) searchInput.focus();
        }

        // Escape: 关闭模态框
        if (e.key === 'Escape') {
            const activeModal = document.querySelector('.modal.active');
            if (activeModal) activeModal.classList.remove('active');
        }
    });
});

// 导出全局对象
window.toast = toast;
window.theme = theme;
window.loading = loading;
window.api = api;
window.format = format;
window.confirm = confirm;
window.copyToClipboard = copyToClipboard;
window.storage = storage;
window.delegate = delegate;
window.debounce = debounce;
window.throttle = throttle;
window.getStatusText = getStatusText;
window.getStatusClass = getStatusClass;
window.getServiceTypeText = getServiceTypeText;
window.getStatusIcon = getStatusIcon;
