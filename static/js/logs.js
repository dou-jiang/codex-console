/**
 * 后台日志页面
 */

const logsState = {
    page: 1,
    pageSize: 100,
    total: 0,
    autoRefreshSeconds: 10,
    timer: null,
    filters: {
        level: "",
        logger_name: "",
        keyword: "",
        since_minutes: "",
    },
};

const el = {
    filterLevel: document.getElementById("filter-level"),
    filterLogger: document.getElementById("filter-logger"),
    filterKeyword: document.getElementById("filter-keyword"),
    filterSinceMinutes: document.getElementById("filter-since-minutes"),
    autoRefreshSeconds: document.getElementById("auto-refresh-seconds"),
    pageSizeSelect: document.getElementById("page-size-select"),
    refreshBtn: document.getElementById("refresh-logs-btn"),
    clearFiltersBtn: document.getElementById("clear-filters-btn"),
    cleanupBtn: document.getElementById("cleanup-logs-btn"),
    clearLogsBtn: document.getElementById("clear-logs-btn"),
    cleanupRetentionDays: document.getElementById("cleanup-retention-days"),
    cleanupMaxRows: document.getElementById("cleanup-max-rows"),
    logConsole: document.getElementById("log-console"),
    summary: document.getElementById("logs-summary"),
    pageInfo: document.getElementById("page-info"),
    prevPageBtn: document.getElementById("prev-page-btn"),
    nextPageBtn: document.getElementById("next-page-btn"),
    latestLogTime: document.getElementById("latest-log-time"),
    statTotal: document.getElementById("stat-total"),
    statInfo: document.getElementById("stat-info"),
    statWarning: document.getElementById("stat-warning"),
    statError: document.getElementById("stat-error"),
    statCritical: document.getElementById("stat-critical"),
};

function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = String(value ?? "");
    return div.innerHTML;
}

function getLevelClass(level) {
    const text = String(level || "").toUpperCase();
    return `log-level ${text}`;
}

function setLogsSummary() {
    const from = logsState.total === 0 ? 0 : ((logsState.page - 1) * logsState.pageSize + 1);
    const to = Math.min(logsState.total, logsState.page * logsState.pageSize);
    el.summary.textContent = `共 ${logsState.total} 条，当前 ${from}-${to}`;
    el.pageInfo.textContent = `第 ${logsState.page} 页`;
    el.prevPageBtn.disabled = logsState.page <= 1;
    el.nextPageBtn.disabled = to >= logsState.total;
}

function renderLogs(rows) {
    if (!Array.isArray(rows) || rows.length === 0) {
        el.logConsole.innerHTML = `
            <div class="log-line">
                <span class="log-time">-</span>
                <span class="log-level INFO">INFO</span>
                <span class="log-logger">system</span>
                <span class="log-message">暂无日志</span>
            </div>
        `;
        return;
    }

    el.logConsole.innerHTML = rows.map((row) => {
        const level = String(row.level || "INFO").toUpperCase();
        const msg = row.exception ? `${row.message || ""}\n${row.exception}` : (row.message || "");
        return `
            <div class="log-line">
                <span class="log-time">${escapeHtml(format.date(row.created_at))}</span>
                <span class="${getLevelClass(level)}">${escapeHtml(level)}</span>
                <span class="log-logger" title="${escapeHtml(row.logger || "")}">${escapeHtml(row.logger || "-")}</span>
                <span class="log-message">${escapeHtml(msg || "-")}</span>
            </div>
        `;
    }).join("");
}

function collectFilters() {
    logsState.filters.level = String(el.filterLevel.value || "").trim().toUpperCase();
    logsState.filters.logger_name = String(el.filterLogger.value || "").trim();
    logsState.filters.keyword = String(el.filterKeyword.value || "").trim();
    logsState.filters.since_minutes = String(el.filterSinceMinutes.value || "").trim();
}

function buildLogParams() {
    collectFilters();
    const params = new URLSearchParams({
        page: String(logsState.page),
        page_size: String(logsState.pageSize),
    });
    if (logsState.filters.level) params.set("level", logsState.filters.level);
    if (logsState.filters.logger_name) params.set("logger_name", logsState.filters.logger_name);
    if (logsState.filters.keyword) params.set("keyword", logsState.filters.keyword);
    if (logsState.filters.since_minutes) params.set("since_minutes", logsState.filters.since_minutes);
    return params.toString();
}

async function loadLogs(silent = false) {
    if (!silent) {
        loading.show(el.refreshBtn, "加载中...");
    }
    try {
        const query = buildLogParams();
        const data = await api.get(`/logs?${query}`);
        logsState.total = Number(data.total || 0);
        renderLogs(data.logs || []);
        setLogsSummary();
    } catch (error) {
        toast.error(`加载日志失败: ${error?.message || error}`);
    } finally {
        if (!silent) {
            loading.hide(el.refreshBtn);
        }
    }
}

async function loadStats(silent = false) {
    try {
        const data = await api.get("/logs/stats");
        const levels = data.levels || {};
        el.statTotal.textContent = format.number(data.total || 0);
        el.statInfo.textContent = format.number(levels.INFO || 0);
        el.statWarning.textContent = format.number(levels.WARNING || 0);
        el.statError.textContent = format.number(levels.ERROR || 0);
        el.statCritical.textContent = format.number(levels.CRITICAL || 0);
        el.latestLogTime.textContent = `最新日志: ${data.latest_at ? format.date(data.latest_at) : "-"}`;
    } catch (error) {
        if (!silent) {
            toast.error(`加载统计失败: ${error?.message || error}`);
        }
    }
}

function resetFilters() {
    el.filterLevel.value = "";
    el.filterLogger.value = "";
    el.filterKeyword.value = "";
    el.filterSinceMinutes.value = "";
    logsState.page = 1;
    loadLogs();
}

async function cleanupLogs() {
    const retentionDays = Number(el.cleanupRetentionDays.value || 30);
    const maxRows = Number(el.cleanupMaxRows.value || 50000);
    const ok = await confirm(
        `确认清理日志吗？\n保留天数=${retentionDays}，最大条数=${maxRows}`,
        "清理后台日志"
    );
    if (!ok) return;

    try {
        loading.show(el.cleanupBtn, "清理中...");
        const data = await api.post("/logs/cleanup", {
            retention_days: retentionDays,
            max_rows: maxRows,
        });
        toast.success(`清理完成：删除 ${data.deleted_total || 0} 条，剩余 ${data.remaining || 0} 条`);
        logsState.page = 1;
        await Promise.all([loadLogs(true), loadStats(true)]);
        setLogsSummary();
    } catch (error) {
        toast.error(`清理失败: ${error?.message || error}`);
    } finally {
        loading.hide(el.cleanupBtn);
    }
}

async function clearAllLogs() {
    const ok = await confirm("确认清空全部后台日志吗？该操作不可恢复。", "清空后台日志");
    if (!ok) return;

    try {
        loading.show(el.clearLogsBtn, "清空中...");
        const data = await api.delete("/logs?confirm=true");
        toast.success(`清空完成：删除 ${data.deleted_total || 0} 条`);
        logsState.page = 1;
        await Promise.all([loadLogs(true), loadStats(true)]);
        setLogsSummary();
    } catch (error) {
        toast.error(`清空失败: ${error?.message || error}`);
    } finally {
        loading.hide(el.clearLogsBtn);
    }
}

function restartAutoRefresh() {
    if (logsState.timer) {
        clearInterval(logsState.timer);
        logsState.timer = null;
    }
    if (!logsState.autoRefreshSeconds) return;
    logsState.timer = setInterval(() => {
        loadLogs(true);
        loadStats(true);
    }, logsState.autoRefreshSeconds * 1000);
}

function bindEvents() {
    el.refreshBtn.addEventListener("click", () => {
        loadLogs();
        loadStats();
    });
    el.clearFiltersBtn.addEventListener("click", resetFilters);
    el.cleanupBtn.addEventListener("click", cleanupLogs);
    el.clearLogsBtn?.addEventListener("click", clearAllLogs);

    el.pageSizeSelect.addEventListener("change", () => {
        logsState.pageSize = Number(el.pageSizeSelect.value || 100);
        logsState.page = 1;
        loadLogs();
    });
    el.prevPageBtn.addEventListener("click", () => {
        if (logsState.page <= 1) return;
        logsState.page -= 1;
        loadLogs();
    });
    el.nextPageBtn.addEventListener("click", () => {
        const maxPage = Math.max(1, Math.ceil(logsState.total / logsState.pageSize));
        if (logsState.page >= maxPage) return;
        logsState.page += 1;
        loadLogs();
    });

    [
        el.filterLevel,
        el.filterLogger,
        el.filterKeyword,
        el.filterSinceMinutes,
    ].forEach((node) => {
        node.addEventListener("change", () => {
            logsState.page = 1;
            loadLogs();
        });
    });
    el.filterLogger.addEventListener("input", debounce(() => {
        logsState.page = 1;
        loadLogs(true);
    }, 300));
    el.filterKeyword.addEventListener("input", debounce(() => {
        logsState.page = 1;
        loadLogs(true);
    }, 300));

    el.autoRefreshSeconds.addEventListener("change", () => {
        logsState.autoRefreshSeconds = Number(el.autoRefreshSeconds.value || 0);
        restartAutoRefresh();
    });
}

document.addEventListener("DOMContentLoaded", async () => {
    bindEvents();
    await Promise.all([loadLogs(), loadStats()]);
    logsState.autoRefreshSeconds = Number(el.autoRefreshSeconds.value || 0);
    restartAutoRefresh();

    window.addEventListener("beforeunload", () => {
        if (logsState.timer) {
            clearInterval(logsState.timer);
            logsState.timer = null;
        }
    });
});
