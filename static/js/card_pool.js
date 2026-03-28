(function () {
    "use strict";

    const STORAGE_KEY = "card_pool.redeem_codes.v1";
    const REDEEM_CODE_REGEX = /^UK(?:-[A-Z0-9]{5}){5}$/;
    const VALID_STATUSES = new Set(["unused", "used", "expired"]);
    const BUILTIN_SUPPLIERS = ["EFun"];
    const fp = window.filterProtocol || {
        normalizeValue(value) {
            if (value === null || value === undefined) return null;
            const text = String(value).trim();
            return text ? text : null;
        },
        pickSort(value, allowed = [], fallback = "") {
            const candidate = String(value || "").trim();
            return allowed.includes(candidate) ? candidate : fallback;
        },
    };

    const state = {
        activeTab: "redeem",
        statusFilter: "",
        supplierFilter: "",
        sortOrder: "created_desc",
        search: "",
        page: 1,
        pageSize: 50,
        codes: [],
        selectedCodes: new Set(),
        pageCodes: [],
        editingCode: "",
    };

    function getNormalizedFilters() {
        const normalized = {
            status: fp.normalizeValue(state.statusFilter) || "",
            supplier: fp.normalizeValue(state.supplierFilter) || "",
            sort: fp.pickSort(state.sortOrder, ["created_desc", "created_asc"], "created_desc"),
            search: String(fp.normalizeValue(state.search) || ""),
        };
        return normalized;
    }

    function safeJsonParse(raw, fallback) {
        try {
            const parsed = JSON.parse(raw);
            return parsed ?? fallback;
        } catch (_) {
            return fallback;
        }
    }

    function normalizeCode(input) {
        const compact = String(input || "")
            .toUpperCase()
            .replace(/[^A-Z0-9]/g, "");
        if (!compact.startsWith("UK")) {
            return "";
        }
        const body = compact.slice(2);
        if (body.length !== 25) {
            return "";
        }
        return `UK-${body.slice(0, 5)}-${body.slice(5, 10)}-${body.slice(10, 15)}-${body.slice(15, 20)}-${body.slice(20, 25)}`;
    }

    function normalizeSupplierKey(value) {
        return String(value || "")
            .trim()
            .toLowerCase()
            .replace(/[\s_-]+/g, "");
    }

    function normalizeSupplier(value) {
        const text = String(value || "").trim();
        if (!text) {
            return "";
        }
        const key = normalizeSupplierKey(text);
        if (key === "efun" || key === "efuncard") {
            return "EFun";
        }
        return text;
    }

    function getResolvedStatus(item) {
        if (!item || item.status === "used") {
            return "used";
        }
        if (item.expires_at) {
            const expiresAt = Date.parse(item.expires_at);
            if (Number.isFinite(expiresAt) && expiresAt <= Date.now()) {
                return "expired";
            }
        }
        if (item.status === "expired") {
            return "expired";
        }
        return "unused";
    }

    function sanitizeRecord(record) {
        const code = normalizeCode(record?.code || "");
        if (!REDEEM_CODE_REGEX.test(code)) {
            return null;
        }
        const status = VALID_STATUSES.has(record?.status) ? String(record.status) : "unused";
        const createdAt = record?.created_at && Number.isFinite(Date.parse(record.created_at))
            ? record.created_at
            : new Date().toISOString();
        const expiresAt = record?.expires_at && Number.isFinite(Date.parse(record.expires_at))
            ? record.expires_at
            : null;
        const usedAt = record?.used_at && Number.isFinite(Date.parse(record.used_at))
            ? record.used_at
            : null;
        return {
            code,
            status,
            supplier: normalizeSupplier(record?.supplier),
            created_at: createdAt,
            expires_at: expiresAt,
            used_by_email: String(record?.used_by_email || "").trim(),
            used_at: usedAt,
        };
    }

    function loadCodes() {
        const parsed = safeJsonParse(localStorage.getItem(STORAGE_KEY) || "[]", []);
        if (!Array.isArray(parsed)) {
            return [];
        }
        const dedup = new Map();
        parsed.forEach((item) => {
            const sanitized = sanitizeRecord(item);
            if (sanitized) {
                dedup.set(sanitized.code, sanitized);
            }
        });
        return Array.from(dedup.values()).sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
    }

    function persistCodes() {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(state.codes));
    }

    function escapeHtml(value) {
        const div = document.createElement("div");
        div.textContent = String(value ?? "");
        return div.innerHTML;
    }

    function formatDateTime(iso) {
        if (!iso) {
            return "-";
        }
        const date = new Date(iso);
        if (!Number.isFinite(date.getTime())) {
            return "-";
        }
        const y = date.getFullYear();
        const m = String(date.getMonth() + 1).padStart(2, "0");
        const d = String(date.getDate()).padStart(2, "0");
        const hh = String(date.getHours()).padStart(2, "0");
        const mm = String(date.getMinutes()).padStart(2, "0");
        return `${y}-${m}-${d} ${hh}:${mm}`;
    }

    function toLocalDateTimeInputValue(iso) {
        if (!iso) {
            return "";
        }
        const date = new Date(iso);
        if (!Number.isFinite(date.getTime())) {
            return "";
        }
        const y = date.getFullYear();
        const m = String(date.getMonth() + 1).padStart(2, "0");
        const d = String(date.getDate()).padStart(2, "0");
        const hh = String(date.getHours()).padStart(2, "0");
        const mm = String(date.getMinutes()).padStart(2, "0");
        return `${y}-${m}-${d}T${hh}:${mm}`;
    }

    function parseLocalDateTimeToIso(raw) {
        const value = String(raw || "").trim();
        if (!value) {
            return null;
        }
        const date = new Date(value);
        if (!Number.isFinite(date.getTime())) {
            return null;
        }
        return date.toISOString();
    }

    function formatStatusText(status) {
        if (status === "unused") return "未使用";
        if (status === "used") return "已使用";
        return "已过期";
    }

    function getSupplierList() {
        const values = Array.from(
            new Set(
                [...BUILTIN_SUPPLIERS, ...state.codes
                    .map((item) => normalizeSupplier(item.supplier))
                    .filter(Boolean)]
            )
        );
        values.sort((a, b) => {
            if (a === "EFun" && b !== "EFun") return -1;
            if (b === "EFun" && a !== "EFun") return 1;
            return a.localeCompare(b, "zh-Hans-CN");
        });
        return values;
    }

    function renderSupplierOptions() {
        const supplierSelect = document.getElementById("supplier-filter");
        const supplierDatalist = document.getElementById("supplier-options");
        const importSupplierSelect = document.getElementById("import-supplier-input");
        if (!supplierSelect || !supplierDatalist) {
            return;
        }
        const current = state.supplierFilter;
        const list = getSupplierList();
        const hasEmptySupplier = state.codes.some((item) => !normalizeSupplier(item.supplier));

        const selectOptions = ['<option value="">全部供应商</option>'];
        list.forEach((name) => {
            selectOptions.push(`<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`);
        });
        if (hasEmptySupplier) {
            selectOptions.push('<option value="__EMPTY__">未设置供应商</option>');
        }
        supplierSelect.innerHTML = selectOptions.join("");
        if (current && Array.from(supplierSelect.options).some((option) => option.value === current)) {
            supplierSelect.value = current;
        } else {
            state.supplierFilter = "";
            supplierSelect.value = "";
        }

        supplierDatalist.innerHTML = list
            .map((name) => `<option value="${escapeHtml(name)}"></option>`)
            .join("");

        if (importSupplierSelect && importSupplierSelect.tagName.toLowerCase() === "select") {
            const currentImport = String(importSupplierSelect.value || "").trim();
            const importOptions = ['<option value="">未设置供应商</option>']
                .concat(list.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`));
            importSupplierSelect.innerHTML = importOptions.join("");
            if (currentImport && Array.from(importSupplierSelect.options).some((option) => option.value === currentImport)) {
                importSupplierSelect.value = currentImport;
            } else {
                importSupplierSelect.value = "";
            }
        }
    }

    function getRowsByFilter() {
        const filters = getNormalizedFilters();
        const keyword = String(filters.search || "").toUpperCase();
        const rows = state.codes
            .map((item) => ({
                ...item,
                supplier: normalizeSupplier(item.supplier),
                resolvedStatus: getResolvedStatus(item),
            }))
            .filter((item) => {
                if (filters.status && item.resolvedStatus !== filters.status) {
                    return false;
                }
                if (filters.supplier) {
                    if (filters.supplier === "__EMPTY__") {
                        if (item.supplier) {
                            return false;
                        }
                    } else if (item.supplier !== filters.supplier) {
                        return false;
                    }
                }
                if (keyword) {
                    const haystack = [
                        item.code,
                        item.supplier || "",
                        item.used_by_email || "",
                        formatStatusText(item.resolvedStatus),
                    ]
                        .join(" ")
                        .toUpperCase();
                    if (!haystack.includes(keyword)) {
                        return false;
                    }
                }
                return true;
            });

        rows.sort((a, b) => {
            const ta = Date.parse(a.created_at) || 0;
            const tb = Date.parse(b.created_at) || 0;
            if (filters.sort === "created_asc") {
                if (ta !== tb) return ta - tb;
                return a.code.localeCompare(b.code);
            }
            if (tb !== ta) return tb - ta;
            return b.code.localeCompare(a.code);
        });

        return rows;
    }

    function updateStats() {
        let total = 0;
        let unused = 0;
        let used = 0;
        let expired = 0;
        state.codes.forEach((item) => {
            total += 1;
            const status = getResolvedStatus(item);
            if (status === "unused") unused += 1;
            if (status === "used") used += 1;
            if (status === "expired") expired += 1;
        });
        document.getElementById("stat-total").textContent = String(total);
        document.getElementById("stat-unused").textContent = String(unused);
        document.getElementById("stat-used").textContent = String(used);
        document.getElementById("stat-expired").textContent = String(expired);
    }

    function renderTable() {
        const tbody = document.getElementById("redeem-codes-body");
        const rows = getRowsByFilter();
        const totalPages = Math.max(1, Math.ceil(rows.length / state.pageSize));
        if (state.page > totalPages) {
            state.page = totalPages;
        }
        const start = (state.page - 1) * state.pageSize;
        const pageRows = rows.slice(start, start + state.pageSize);
        state.pageCodes = pageRows.map((item) => item.code);

        if (!pageRows.length) {
            tbody.innerHTML = '<tr><td colspan="9" class="empty-row">暂无匹配数据。</td></tr>';
        } else {
            tbody.innerHTML = pageRows
                .map((item) => {
                    const status = item.resolvedStatus;
                    return `
                        <tr data-code="${escapeHtml(item.code)}">
                            <td><input type="checkbox" class="code-select" data-code="${escapeHtml(item.code)}" ${state.selectedCodes.has(item.code) ? "checked" : ""}></td>
                            <td>
                                <span class="code-cell">
                                    <span>🎫</span>
                                    <span>${escapeHtml(item.code)}</span>
                                </span>
                            </td>
                            <td>${item.supplier ? escapeHtml(item.supplier) : "-"}</td>
                            <td><span class="status-badge-pill ${status}">${formatStatusText(status)}</span></td>
                            <td>${escapeHtml(formatDateTime(item.created_at))}</td>
                            <td>${item.expires_at ? escapeHtml(formatDateTime(item.expires_at)) : "永久有效"}</td>
                            <td>${item.used_by_email ? escapeHtml(item.used_by_email) : "-"}</td>
                            <td>${item.used_at ? escapeHtml(formatDateTime(item.used_at)) : "-"}</td>
                            <td style="text-align: right;">
                                <div class="card-pool-actions">
                                    <button type="button" class="action-btn" data-action="copy" title="复制">⧉</button>
                                    <button type="button" class="action-btn" data-action="edit" title="修改">✎</button>
                                    <button type="button" class="action-btn danger" data-action="delete" title="删除">🗑</button>
                                </div>
                            </td>
                        </tr>
                    `;
                })
                .join("");
        }

        const pageInfo = document.getElementById("page-info");
        const prevBtn = document.getElementById("page-prev");
        const nextBtn = document.getElementById("page-next");
        pageInfo.textContent = `第 ${state.page} 页 / 共 ${totalPages} 页`;
        prevBtn.disabled = state.page <= 1;
        nextBtn.disabled = state.page >= totalPages;

        const allChecked = state.pageCodes.length > 0 && state.pageCodes.every((code) => state.selectedCodes.has(code));
        document.getElementById("select-all-codes").checked = allChecked;
        document.getElementById("btn-delete-selected").disabled = state.selectedCodes.size === 0;
    }

    function render() {
        renderSupplierOptions();
        const filters = getNormalizedFilters();
        const sortSelect = document.getElementById("sort-order");
        if (sortSelect && sortSelect.value !== filters.sort) {
            sortSelect.value = filters.sort;
        }
        updateStats();
        renderTable();
    }

    function setActiveTab(tab) {
        state.activeTab = tab === "credit" ? "credit" : "redeem";
        document.getElementById("pool-tab-redeem").classList.toggle("active", state.activeTab === "redeem");
        document.getElementById("pool-tab-credit").classList.toggle("active", state.activeTab === "credit");
        document.getElementById("panel-redeem").classList.toggle("active", state.activeTab === "redeem");
        document.getElementById("panel-credit").classList.toggle("active", state.activeTab === "credit");
    }

    function setStatusFilter(status) {
        state.statusFilter = String(fp.normalizeValue(status) || "");
        state.page = 1;
        document.querySelectorAll(".status-chip").forEach((btn) => {
            btn.classList.toggle("active", (btn.dataset.status || "") === state.statusFilter);
        });
        render();
    }

    function parseImportInput(rawText) {
        const tokens = String(rawText || "")
            .split(/[\s,，;；]+/)
            .map((item) => item.trim())
            .filter(Boolean);
        const valid = [];
        const invalid = [];
        tokens.forEach((item) => {
            const code = normalizeCode(item);
            if (REDEEM_CODE_REGEX.test(code)) {
                valid.push(code);
            } else {
                invalid.push(item);
            }
        });
        return {
            valid: Array.from(new Set(valid)),
            invalidCount: invalid.length,
        };
    }

    function applySearch() {
        const input = document.getElementById("redeem-search");
        state.search = String(fp.normalizeValue(input?.value) || "");
        state.page = 1;
        render();
    }

    function clearImportInputs() {
        document.getElementById("import-codes-input").value = "";
        document.getElementById("import-expire-days").value = "";
        document.getElementById("import-supplier-input").value = "";
    }

    function closeImportModal() {
        document.getElementById("import-modal").classList.remove("active");
    }

    function openImportModal() {
        renderSupplierOptions();
        document.getElementById("import-modal").classList.add("active");
        const input = document.getElementById("import-codes-input");
        if (input) {
            input.focus();
        }
    }

    function closeEditModal() {
        document.getElementById("edit-modal").classList.remove("active");
        state.editingCode = "";
    }

    function openEditModal(code) {
        const target = state.codes.find((item) => item.code === code);
        if (!target) {
            toast.warning("未找到对应兑换码");
            return;
        }
        renderSupplierOptions();
        state.editingCode = code;
        document.getElementById("edit-code-display").value = target.code;
        document.getElementById("edit-supplier-input").value = normalizeSupplier(target.supplier);
        document.getElementById("edit-status-select").value = VALID_STATUSES.has(target.status) ? target.status : "unused";
        document.getElementById("edit-used-email").value = String(target.used_by_email || "").trim();
        document.getElementById("edit-expires-at").value = toLocalDateTimeInputValue(target.expires_at);
        document.getElementById("edit-modal").classList.add("active");
        document.getElementById("edit-status-select").focus();
    }

    function handleEditConfirm() {
        const code = String(state.editingCode || "").trim();
        if (!code) {
            closeEditModal();
            return;
        }
        const target = state.codes.find((item) => item.code === code);
        if (!target) {
            closeEditModal();
            toast.warning("未找到对应兑换码");
            return;
        }

        const nextStatus = String(document.getElementById("edit-status-select").value || "unused");
        if (!VALID_STATUSES.has(nextStatus)) {
            toast.warning("状态无效");
            return;
        }
        const nextSupplier = normalizeSupplier(document.getElementById("edit-supplier-input").value);
        const nextEmail = String(document.getElementById("edit-used-email").value || "").trim();
        const nextExpiresIso = parseLocalDateTimeToIso(document.getElementById("edit-expires-at").value);
        const rawExpires = String(document.getElementById("edit-expires-at").value || "").trim();
        if (rawExpires && !nextExpiresIso) {
            toast.warning("过期时间格式无效");
            return;
        }

        target.status = nextStatus;
        target.supplier = nextSupplier;
        target.used_by_email = nextEmail;
        target.expires_at = nextExpiresIso;
        if (nextStatus === "used") {
            if (!target.used_at) {
                target.used_at = new Date().toISOString();
            }
        } else {
            target.used_at = null;
        }

        persistCodes();
        closeEditModal();
        render();
        toast.success("修改成功");
    }

    function parseExpireDays() {
        const raw = String(document.getElementById("import-expire-days").value || "").trim();
        if (!raw) {
            return null;
        }
        const value = Number(raw);
        if (!Number.isInteger(value) || value <= 0) {
            return NaN;
        }
        return value;
    }

    function buildExpiresAt(days) {
        if (!Number.isInteger(days) || days <= 0) {
            return null;
        }
        const target = new Date();
        target.setDate(target.getDate() + days);
        target.setHours(23, 59, 59, 999);
        return target.toISOString();
    }

    function handleImportConfirm() {
        const text = document.getElementById("import-codes-input").value;
        const supplier = normalizeSupplier(document.getElementById("import-supplier-input").value);
        const parsed = parseImportInput(text);
        if (!parsed.valid.length) {
            toast.warning("没有可导入的兑换码");
            return;
        }

        const expireDays = parseExpireDays();
        if (Number.isNaN(expireDays)) {
            toast.warning("有效期必须是大于 0 的整数");
            return;
        }

        const existingCodes = new Set(state.codes.map((item) => item.code));
        const nowIso = new Date().toISOString();
        const expiresAt = buildExpiresAt(expireDays);
        let added = 0;
        let duplicate = 0;

        parsed.valid.forEach((code) => {
            if (existingCodes.has(code)) {
                duplicate += 1;
                return;
            }
            state.codes.push({
                code,
                status: "unused",
                supplier,
                created_at: nowIso,
                expires_at: expiresAt,
                used_by_email: "",
                used_at: null,
            });
            existingCodes.add(code);
            added += 1;
        });

        state.codes.sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
        persistCodes();
        state.page = 1;
        closeImportModal();
        clearImportInputs();
        render();

        const fragments = [`成功导入 ${added} 条`];
        if (duplicate > 0) fragments.push(`重复跳过 ${duplicate} 条`);
        if (parsed.invalidCount > 0) fragments.push(`格式错误 ${parsed.invalidCount} 条`);
        toast.success(fragments.join("，"));
    }

    function csvEscape(value) {
        const raw = String(value ?? "");
        if (/[",\n]/.test(raw)) {
            return `"${raw.replace(/"/g, "\"\"")}"`;
        }
        return raw;
    }

    function exportCurrentRows() {
        const rows = getRowsByFilter();
        if (!rows.length) {
            toast.warning("暂无可导出的兑换码");
            return;
        }
        const header = ["兑换码", "供应商", "状态", "创建时间", "过期时间", "使用者邮箱", "使用时间"];
        const csvRows = [header];
        rows.forEach((item) => {
            csvRows.push([
                item.code,
                item.supplier || "-",
                formatStatusText(item.resolvedStatus),
                formatDateTime(item.created_at),
                item.expires_at ? formatDateTime(item.expires_at) : "永久有效",
                item.used_by_email || "-",
                item.used_at ? formatDateTime(item.used_at) : "-",
            ]);
        });
        const content = csvRows.map((line) => line.map(csvEscape).join(",")).join("\n");
        const blob = new Blob([content], { type: "text/csv;charset=utf-8;" });
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = `redeem_codes_${Date.now()}.csv`;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(link.href);
    }

    async function copyText(text) {
        const value = String(text || "");
        if (!value) return;
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(value);
            return;
        }
        const textArea = document.createElement("textarea");
        textArea.value = value;
        textArea.style.position = "fixed";
        textArea.style.opacity = "0";
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        document.execCommand("copy");
        document.body.removeChild(textArea);
    }

    function removeCode(code) {
        const before = state.codes.length;
        state.codes = state.codes.filter((item) => item.code !== code);
        state.selectedCodes.delete(code);
        if (state.codes.length !== before) {
            persistCodes();
            render();
        }
    }

    function handleTableAction(event) {
        const button = event.target.closest("button[data-action]");
        if (!button) return;
        const row = event.target.closest("tr[data-code]");
        if (!row) return;
        const code = row.dataset.code || "";
        const action = button.dataset.action || "";

        if (action === "copy") {
            copyText(code)
                .then(() => toast.success("兑换码已复制"))
                .catch(() => toast.error("复制失败"));
            return;
        }
        if (action === "delete") {
            if (!confirm(`确认删除兑换码 ${code} 吗？`)) return;
            removeCode(code);
            toast.success("已删除兑换码");
            return;
        }
        if (action === "edit") {
            openEditModal(code);
            return;
        }
    }

    function syncSelectAllState() {
        const allChecked = state.pageCodes.length > 0 && state.pageCodes.every((code) => state.selectedCodes.has(code));
        document.getElementById("select-all-codes").checked = allChecked;
        document.getElementById("btn-delete-selected").disabled = state.selectedCodes.size === 0;
    }

    function deleteSelectedCodes() {
        const selected = Array.from(state.selectedCodes);
        if (!selected.length) {
            return;
        }
        if (!confirm(`确认删除已选中的 ${selected.length} 条兑换码吗？`)) {
            return;
        }
        const selectedSet = new Set(selected);
        state.codes = state.codes.filter((item) => !selectedSet.has(item.code));
        state.selectedCodes.clear();
        persistCodes();
        render();
        toast.success(`已删除 ${selected.length} 条兑换码`);
    }

    function bindEvents() {
        document.getElementById("pool-tab-redeem").addEventListener("click", () => setActiveTab("redeem"));
        document.getElementById("pool-tab-credit").addEventListener("click", () => setActiveTab("credit"));

        document.querySelectorAll(".status-chip").forEach((button) => {
            button.addEventListener("click", () => setStatusFilter(button.dataset.status || ""));
        });

        document.getElementById("supplier-filter").addEventListener("change", (event) => {
            state.supplierFilter = String(fp.normalizeValue(event.target.value) || "");
            state.page = 1;
            render();
        });

        document.getElementById("sort-order").addEventListener("change", (event) => {
            const value = String(event.target.value || "created_desc");
            state.sortOrder = fp.pickSort(value, ["created_asc", "created_desc"], "created_desc");
            state.page = 1;
            render();
        });

        document.getElementById("redeem-search-btn").addEventListener("click", applySearch);
        document.getElementById("redeem-search").addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                event.preventDefault();
                applySearch();
            }
        });

        document.getElementById("page-size").addEventListener("change", (event) => {
            const value = Number(event.target.value);
            state.pageSize = Number.isInteger(value) && value > 0 ? value : 50;
            state.page = 1;
            render();
        });

        document.getElementById("page-prev").addEventListener("click", () => {
            if (state.page <= 1) return;
            state.page -= 1;
            render();
        });

        document.getElementById("page-next").addEventListener("click", () => {
            state.page += 1;
            render();
        });

        document.getElementById("redeem-codes-body").addEventListener("change", (event) => {
            const checkbox = event.target.closest(".code-select");
            if (!checkbox) return;
            const code = checkbox.dataset.code || "";
            if (!code) return;
            if (checkbox.checked) {
                state.selectedCodes.add(code);
            } else {
                state.selectedCodes.delete(code);
            }
            syncSelectAllState();
        });

        document.getElementById("select-all-codes").addEventListener("change", (event) => {
            const checked = Boolean(event.target.checked);
            state.pageCodes.forEach((code) => {
                if (checked) {
                    state.selectedCodes.add(code);
                } else {
                    state.selectedCodes.delete(code);
                }
            });
            render();
        });

        document.getElementById("redeem-codes-body").addEventListener("click", handleTableAction);

        document.getElementById("btn-import-codes").addEventListener("click", openImportModal);
        document.getElementById("btn-export-codes").addEventListener("click", exportCurrentRows);
        document.getElementById("btn-delete-selected").addEventListener("click", deleteSelectedCodes);

        document.getElementById("import-modal-close").addEventListener("click", closeImportModal);
        document.getElementById("import-modal-cancel").addEventListener("click", closeImportModal);
        document.getElementById("import-modal-confirm").addEventListener("click", handleImportConfirm);

        document.getElementById("import-modal").addEventListener("click", (event) => {
            if (event.target.id === "import-modal") {
                closeImportModal();
            }
        });

        document.getElementById("edit-modal-close").addEventListener("click", closeEditModal);
        document.getElementById("edit-modal-cancel").addEventListener("click", closeEditModal);
        document.getElementById("edit-modal-confirm").addEventListener("click", handleEditConfirm);
        document.getElementById("edit-modal").addEventListener("click", (event) => {
            if (event.target.id === "edit-modal") {
                closeEditModal();
            }
        });

        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                closeImportModal();
                closeEditModal();
            }
        });
    }

    function init() {
        state.codes = loadCodes();
        bindEvents();
        render();
    }

    document.addEventListener("DOMContentLoaded", init);
})();
