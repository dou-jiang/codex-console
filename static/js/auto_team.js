(function () {
    const fp = window.filterProtocol || {
        normalizeValue(value) {
            if (value === null || value === undefined) return null;
            const text = String(value).trim();
            return text ? text : null;
        },
        toQuery(filters = {}) {
            const params = new URLSearchParams();
            Object.entries(filters || {}).forEach(([key, raw]) => {
                if (!key) return;
                if (raw === null || raw === undefined || raw === "") return;
                params.set(String(key), String(raw));
            });
            return params;
        },
    };

    function nowTime() {
        return new Date().toLocaleTimeString("zh-CN", { hour12: false });
    }

    function escapeHtml(text) {
        return String(text || "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");
    }

    function safeError(error) {
        if (!error) return "未知错误";
        if (typeof error === "string") return error;
        if (error.data && error.data.detail) {
            if (typeof error.data.detail === "string") return error.data.detail;
            return JSON.stringify(error.data.detail);
        }
        if (error.message) return error.message;
        try {
            return JSON.stringify(error);
        } catch (_e) {
            return "未知错误";
        }
    }

    function normalizePlanType(rawPlan) {
        const value = String(rawPlan || "").trim().toLowerCase();
        if (value.includes("plus") || value.includes("pro")) return "plus";
        if (value.includes("team") || value.includes("enterprise")) return "team";
        return "free";
    }

    function getPlanBadgeText(rawPlan) {
        const plan = normalizePlanType(rawPlan);
        if (plan === "plus") return "PLUS";
        if (plan === "team") return "TEAM";
        return "FREE";
    }

    const BLOCKED_INVITER_STATUSES = new Set([
        "failed",
        "banned",
        "deleted",
        "disabled",
        "invalid",
        "inactive",
        "frozen",
        "expired",
        "error",
        "locked",
        "suspended",
    ]);

    function isHardRemoveAuthSource(rawSource) {
        const source = String(rawSource || "").trim().toLowerCase();
        if (!source) return false;
        return (
            source.includes("hard_remove_auth")
            || source.includes("http_401")
            || source.includes("http_403")
            || source.includes("token has been invalidated")
            || source.includes("authentication token has been invalidated")
            || source.includes("please try signing in again")
        );
    }

    const INVITER_CACHE_KEY = "auto_team_inviter_accounts_cache_v1";
    const TEAM_GROUP_CACHE_KEY = "auto_team_groups_cache_v1";

    class AutoTeamPage {
        constructor() {
            this.els = {
                targetEmail: document.getElementById("targetEmail"),
                inviterAccount: document.getElementById("inviterAccount"),
                inviterList: document.getElementById("inviterList"),
                managerList: document.getElementById("managerList"),
                memberList: document.getElementById("memberList"),
                tabInvite: document.getElementById("tabInvite"),
                tabManage: document.getElementById("tabManage"),
                panelInvite: document.getElementById("panelInvite"),
                panelManage: document.getElementById("panelManage"),
                btnPickTargetEmail: document.getElementById("btnPickTargetEmail"),
                btnReloadInviterList: document.getElementById("btnReloadInviterList"),
                btnManualPullInviter: document.getElementById("btnManualPullInviter"),
                targetModal: document.getElementById("targetModal"),
                targetModalList: document.getElementById("targetModalList"),
                targetModalSearch: document.getElementById("targetModalSearch"),
                targetModalSelectedInfo: document.getElementById("targetModalSelectedInfo"),
                btnCloseTargetModal: document.getElementById("btnCloseTargetModal"),
                btnTargetSelectAll: document.getElementById("btnTargetSelectAll"),
                btnTargetClearAll: document.getElementById("btnTargetClearAll"),
                btnAddSelectedTargets: document.getElementById("btnAddSelectedTargets"),
                manualInviterModal: document.getElementById("manualInviterModal"),
                manualInviterList: document.getElementById("manualInviterList"),
                manualInviterSearch: document.getElementById("manualInviterSearch"),
                manualInviterSelectedInfo: document.getElementById("manualInviterSelectedInfo"),
                btnCloseManualInviterModal: document.getElementById("btnCloseManualInviterModal"),
                btnManualInviterSelectAll: document.getElementById("btnManualInviterSelectAll"),
                btnManualInviterClearAll: document.getElementById("btnManualInviterClearAll"),
                btnSubmitManualInviter: document.getElementById("btnSubmitManualInviter"),
                btnInvite: document.getElementById("btnInvite"),
                btnReloadAccounts: document.getElementById("btnReloadAccounts"),
                btnReloadTeamGroups: document.getElementById("btnReloadTeamGroups"),
                btnClearLog: document.getElementById("btnClearLog"),
                resultBox: document.getElementById("resultBox"),
                logBox: document.getElementById("autoTeamLog"),
            };
            this.targetAccounts = [];
            this.selectedTargetIds = new Set();
            this.manualInviterCandidates = [];
            this.selectedManualInviterIds = new Set();
            this.inviterAccounts = [];
            this.teamManagers = [];
            this.teamMembers = [];
            this.teamGroupsLoaded = false;
            this.teamGroupsLoading = false;
            this.inviterLoaded = false;
            this.manualLoadMode = true;
            this.inviterBackgroundSeq = 0;
            this.bindEvents();
            this.bootstrap();
        }

        bindEvents() {
            this.els.tabInvite?.addEventListener("click", () => this.switchTab("invite"));
            this.els.tabManage?.addEventListener("click", () => this.switchTab("manage"));
            this.els.btnPickTargetEmail?.addEventListener("click", () => this.openTargetModal());
            this.els.btnReloadInviterList?.addEventListener("click", () => this.loadInviterAccounts(true, this.els.btnReloadInviterList, true));
            this.els.btnManualPullInviter?.addEventListener("click", () => this.openManualInviterModal());
            this.els.btnCloseTargetModal?.addEventListener("click", () => this.closeTargetModal());
            this.els.btnTargetSelectAll?.addEventListener("click", () => this.selectVisibleTargets());
            this.els.btnTargetClearAll?.addEventListener("click", () => this.clearSelectedTargets());
            this.els.btnAddSelectedTargets?.addEventListener("click", () => this.addSelectedTargetsToInput());
            this.els.targetModalSearch?.addEventListener("input", () => this.renderTargetModalList());
            this.els.targetModal?.addEventListener("click", (e) => {
                if (e.target === this.els.targetModal) {
                    this.closeTargetModal();
                }
            });
            this.els.targetModalList?.addEventListener("change", (e) => {
                const el = e.target;
                if (!el || !el.matches('input[type="checkbox"][data-target-id]')) return;
                const id = String(el.dataset.targetId || "");
                if (!id) return;
                if (el.checked) this.selectedTargetIds.add(id);
                else this.selectedTargetIds.delete(id);
                this.updateTargetSelectedInfo();
            });
            this.els.manualInviterModal?.addEventListener("click", (e) => {
                if (e.target === this.els.manualInviterModal) {
                    this.closeManualInviterModal();
                }
            });
            this.els.btnCloseManualInviterModal?.addEventListener("click", () => this.closeManualInviterModal());
            this.els.btnManualInviterSelectAll?.addEventListener("click", () => this.selectVisibleManualInviters());
            this.els.btnManualInviterClearAll?.addEventListener("click", () => this.clearSelectedManualInviters());
            this.els.btnSubmitManualInviter?.addEventListener("click", () => this.submitManualInviterSelection());
            this.els.manualInviterSearch?.addEventListener("input", () => this.renderManualInviterList());
            this.els.manualInviterList?.addEventListener("change", (e) => {
                const el = e.target;
                if (!el || !el.matches('input[type="checkbox"][data-inviter-id]')) return;
                const id = String(el.dataset.inviterId || "");
                if (!id) return;
                if (el.checked) this.selectedManualInviterIds.add(id);
                else this.selectedManualInviterIds.delete(id);
                this.updateManualInviterSelectedInfo();
            });
            this.els.btnInvite?.addEventListener("click", () => this.handleInvite());
            this.els.btnReloadAccounts?.addEventListener("click", () => this.loadInviterAccounts(true, this.els.btnReloadAccounts, true));
            this.els.btnReloadTeamGroups?.addEventListener("click", () => this.loadTeamGroups(true, true));
            this.els.btnClearLog?.addEventListener("click", () => this.clearLogs());
        }

        switchTab(tab) {
            const inviteActive = tab === "invite";
            this.els.tabInvite?.classList.toggle("active", inviteActive);
            this.els.tabManage?.classList.toggle("active", !inviteActive);
            this.els.panelInvite?.classList.toggle("active", inviteActive);
            this.els.panelManage?.classList.toggle("active", !inviteActive);
        }

        async bootstrap() {
            this.log("team页面已加载，已关闭首次自动刷新。请手动点击“刷新”加载 Team 管理账号列表。");
            const cachedInvitersRaw = this.readCache(INVITER_CACHE_KEY, []);
            const cachedInviters = this.pruneInviterAccounts(
                Array.isArray(cachedInvitersRaw) ? cachedInvitersRaw : [],
                "本地缓存",
            );
            this.inviterAccounts = cachedInviters;
            this.inviterLoaded = true;
            this.fillSelect(this.inviterAccounts);
            this.renderInviters(this.inviterAccounts);
            if (this.inviterAccounts.length > 0) {
                this.log(`已载入本地缓存管理账号: ${this.inviterAccounts.length} 个`);
            } else {
                this.log("当前无本地缓存管理账号，请手动刷新加载。");
            }

            const cachedGroups = this.readCache(TEAM_GROUP_CACHE_KEY, {});
            const cachedManagers = Array.isArray(cachedGroups?.managers) ? cachedGroups.managers : [];
            const cachedMembers = Array.isArray(cachedGroups?.members) ? cachedGroups.members : [];
            this.teamManagers = cachedManagers;
            this.teamMembers = cachedMembers;
            if (cachedManagers.length > 0 || cachedMembers.length > 0) {
                this.teamGroupsLoaded = true;
                this.renderTeamGroupList(this.els.managerList, this.teamManagers, true);
                this.renderTeamGroupList(this.els.memberList, this.teamMembers, false);
                this.log(`已载入本地缓存分类: 母号=${this.teamManagers.length} 子号=${this.teamMembers.length}`);
            } else {
                this.renderTeamGroupList(this.els.managerList, [], true);
                this.renderTeamGroupList(this.els.memberList, [], false);
            }
        }

        readCache(key, fallback) {
            try {
                const raw = localStorage.getItem(key);
                if (!raw) return fallback;
                return JSON.parse(raw);
            } catch (_e) {
                return fallback;
            }
        }

        writeCache(key, value) {
            try {
                localStorage.setItem(key, JSON.stringify(value));
            } catch (_e) {
                // ignore
            }
        }

        pruneInviterAccounts(list, stage = "") {
            const source = Array.isArray(list) ? list : [];
            const filtered = source.filter((item) => {
                const status = String(item?.status || "").trim().toLowerCase();
                const verifySource = String(item?.manager_verify_source || item?.verify_source || "").trim();
                if (BLOCKED_INVITER_STATUSES.has(status)) return false;
                if (isHardRemoveAuthSource(verifySource)) return false;
                return true;
            });
            const sorted = [...filtered].sort((a, b) => {
                const aRole = String(a?.role_tag || "").trim().toLowerCase();
                const bRole = String(b?.role_tag || "").trim().toLowerCase();
                const aParent = aRole === "parent" || aRole === "mother" ? 0 : 1;
                const bParent = bRole === "parent" || bRole === "mother" ? 0 : 1;
                if (aParent !== bParent) return aParent - bParent;
                const aPriority = Number.isFinite(Number(a?.priority)) ? Number(a.priority) : 50;
                const bPriority = Number.isFinite(Number(b?.priority)) ? Number(b.priority) : 50;
                if (aPriority !== bPriority) return aPriority - bPriority;
                const aId = Number.isFinite(Number(a?.id)) ? Number(a.id) : 0;
                const bId = Number.isFinite(Number(b?.id)) ? Number(b.id) : 0;
                return bId - aId;
            });
            const removed = source.length - filtered.length;
            if (removed > 0 && stage) {
                this.log(`${stage}剔除失效管理账号: ${removed} 个`);
            }
            return sorted;
        }

        emitInviterSync() {}

        async refreshInviterAccountsInBackground(force = false) {
            const seq = ++this.inviterBackgroundSeq;
            try {
                const queryParams = new URLSearchParams();
                if (force) queryParams.set("force", "1");
                queryParams.set("local_only", "0");
                const query = `?${queryParams.toString()}`;
                const data = await api.get(`/auto-team/inviter-accounts${query}`, {
                    timeoutMs: 15000,
                    retry: 0,
                    cancelPrevious: true,
                    requestKey: "auto-team:inviter-accounts:bg",
                    silentNetworkError: true,
                    silentTimeoutError: true,
                });
                if (seq !== this.inviterBackgroundSeq) return;
                const accountsRaw = Array.isArray(data.accounts) ? data.accounts : [];
                const verified = this.pruneInviterAccounts(accountsRaw, "后台校验");
                if (!verified.length && this.inviterAccounts.length > 0) {
                    this.log("后台校验返回空，保留当前本地管理账号列表。");
                    return;
                }
                this.inviterAccounts = verified;
                this.writeCache(INVITER_CACHE_KEY, this.inviterAccounts);
                this.fillSelect(this.inviterAccounts);
                this.renderInviters(this.inviterAccounts);
                if (verified.length > 0) {
                    this.log(`后台校验完成：可用 Team 管理账号 ${verified.length} 个`);
                } else {
                    this.log("后台校验完成：当前无可用 Team 管理账号");
                }
            } catch (error) {
                const msg = safeError(error);
                this.log(`后台校验失败（已保留当前列表）: ${msg}`);
            }
        }

        clearLogs() {
            this.els.logBox.innerHTML = "";
            this.log("日志已清空。");
        }

        log(message) {
            const line = document.createElement("div");
            line.className = "line";
            line.textContent = `[${nowTime()}] ${message}`;
            this.els.logBox.appendChild(line);
            this.els.logBox.scrollTop = this.els.logBox.scrollHeight;
        }

        setReloadButtonLoading(button, isLoading) {
            const btn = button || this.els.btnReloadInviterList || this.els.btnReloadAccounts;
            if (!btn) return;
            if (isLoading) {
                if (!btn.dataset.originalLabel) {
                    btn.dataset.originalLabel = String(btn.textContent || "").trim() || "刷新";
                }
                btn.disabled = true;
                btn.classList.add("btn-refresh-loading");
                btn.innerHTML = '<span class="btn-refresh-icon" aria-hidden="true">⟳</span>';
                return;
            }
            btn.disabled = false;
            btn.classList.remove("btn-refresh-loading");
            const label = btn.dataset.originalLabel || "刷新";
            btn.textContent = label;
            delete btn.dataset.originalLabel;
        }

        setResult(type, title, content, extra) {
            const box = this.els.resultBox;
            box.style.display = "block";
            let color = "var(--text-secondary)";
            if (type === "success") color = "var(--success-color)";
            if (type === "error") color = "var(--danger-color)";
            if (type === "warning") color = "var(--warning-color)";

            let html = `<div class="result-title" style="color:${color};">${title}</div>`;
            if (content) {
                html += `<div>${String(content).replace(/</g, "&lt;").replace(/>/g, "&gt;")}</div>`;
            }
            if (extra) {
                html += `<pre style="margin-top:8px;padding:8px;border-radius:8px;background:var(--surface);border:1px solid var(--border);overflow:auto;white-space:pre-wrap;">${JSON.stringify(extra, null, 2)}</pre>`;
            }
            box.innerHTML = html;
        }

        getInviterId() {
            const rawId = this.els.inviterAccount.value;
            return rawId ? Number(rawId) : null;
        }

        parseTargetEmails() {
            const raw = String(this.els.targetEmail?.value || "");
            const tokens = raw
                .split(/[\n,;，；\s]+/)
                .map((x) => x.trim().toLowerCase())
                .filter(Boolean);

            const unique = [...new Set(tokens)];
            const emailRe = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
            const valid = [];
            const invalid = [];
            unique.forEach((email) => {
                if (emailRe.test(email)) valid.push(email);
                else invalid.push(email);
            });
            return { valid, invalid };
        }

        renderInviters(list) {
            if (!this.els.inviterList) return;
            if (!Array.isArray(list) || list.length === 0) {
                if (this.manualLoadMode && !this.inviterLoaded) {
                    this.els.inviterList.innerHTML = '<div class="empty-tip">首次不自动加载，请点击“刷新”读取 Team 管理账号。</div>';
                    return;
                }
                this.els.inviterList.innerHTML = '<div class="empty-tip">暂无可用 Team 管理账号（需 team + token + workspace）。</div>';
                return;
            }

            this.els.inviterList.innerHTML = list.map((item) => {
                const email = escapeHtml(item.email || "");
                const workspace = escapeHtml(item.workspace_id || "-");
                const status = escapeHtml(item.status || "-");
                const roleTag = escapeHtml(item.role_tag || "-");
                const poolState = escapeHtml(item.pool_state || "-");
                return `
                    <div class="inviter-item">
                        <div class="inviter-email">${email}</div>
                        <div class="inviter-meta">
                            <span class="badge-team">TEAM</span>
                            <span>ID: ${item.id}</span>
                            <span>状态: ${status}</span>
                            <span>标签: ${roleTag}</span>
                            <span>池: ${poolState}</span>
                        </div>
                        <div class="inviter-meta" style="margin-top:4px;">
                            <span>workspace: ${workspace}</span>
                        </div>
                    </div>
                `;
            }).join("");
        }

        fillSelect(list) {
            const current = this.els.inviterAccount.value;
            this.els.inviterAccount.innerHTML = '<option value="">自动选择（默认第一个可用 Team 管理账号）</option>';
            list.forEach((item) => {
                const option = document.createElement("option");
                option.value = String(item.id);
                option.textContent = `${item.email} (ID=${item.id})`;
                this.els.inviterAccount.appendChild(option);
            });
            if (current && list.some((x) => String(x.id) === current)) {
                this.els.inviterAccount.value = current;
            }
        }

        renderTeamGroupList(container, list, isManager) {
            if (!container) return;
            if (!Array.isArray(list) || list.length === 0) {
                if (this.manualLoadMode && !this.teamGroupsLoaded) {
                    container.innerHTML = `<div class="empty-tip">首次不自动加载，请点击“刷新”读取${isManager ? "母号" : "子号"}账号</div>`;
                    return;
                }
                container.innerHTML = `<div class="empty-tip">暂无${isManager ? "母号" : "子号"}账号</div>`;
                return;
            }
            container.innerHTML = list.map((item) => {
                const email = escapeHtml(item.email || "");
                const workspace = escapeHtml(item.workspace_id || "-");
                const status = escapeHtml(item.status || "-");
                return `
                    <div class="inviter-item">
                        <div class="inviter-email">${email}</div>
                        <div class="inviter-meta">
                            <span class="${isManager ? "badge-team" : "badge-member"}">${isManager ? "母号" : "子号"}</span>
                            <span>ID: ${item.id}</span>
                            <span>状态: ${status}</span>
                        </div>
                        <div class="inviter-meta" style="margin-top:4px;">
                            <span>workspace: ${workspace}</span>
                        </div>
                    </div>
                `;
            }).join("");
        }

        async loadTeamGroups(withToast, force = false) {
            try {
                this.teamGroupsLoading = true;
                if (this.els.btnReloadTeamGroups) {
                    loading.show(this.els.btnReloadTeamGroups, "刷新中...");
                }
                const queryParams = fp.toQuery({ force: force ? 1 : null });
                const queryText = queryParams.toString();
                const query = queryText ? `?${queryText}` : "";
                const data = await api.get(`/auto-team/team-accounts${query}`, {
                    timeoutMs: 12000,
                    retry: 0,
                    silentNetworkError: true,
                    silentTimeoutError: true,
                });
                this.teamGroupsLoaded = true;
                const managers = Array.isArray(data.managers) ? data.managers : [];
                const members = Array.isArray(data.members) ? data.members : [];
                const hasExistingGroups = (Array.isArray(this.teamManagers) && this.teamManagers.length > 0)
                    || (Array.isArray(this.teamMembers) && this.teamMembers.length > 0);
                if (managers.length === 0 && members.length === 0 && hasExistingGroups) {
                    this.renderTeamGroupList(this.els.managerList, this.teamManagers, true);
                    this.renderTeamGroupList(this.els.memberList, this.teamMembers, false);
                    this.log("team分类刷新返回空，已保留当前母号/子号列表（避免误清空）");
                    if (withToast) {
                        toast.warning("返回空结果，已保留当前 Team 分类列表");
                    }
                    return;
                }
                this.teamManagers = managers;
                this.teamMembers = members;
                this.writeCache(TEAM_GROUP_CACHE_KEY, {
                    managers: this.teamManagers,
                    members: this.teamMembers,
                });
                this.renderTeamGroupList(this.els.managerList, this.teamManagers, true);
                this.renderTeamGroupList(this.els.memberList, this.teamMembers, false);
                this.log(`team分类加载完成: 母号=${managers.length} 子号=${members.length}`);
                if (withToast) {
                    toast.success(`已刷新：母号 ${managers.length}，子号 ${members.length}`);
                }
            } catch (error) {
                const msg = safeError(error);
                this.teamGroupsLoaded = true;
                if (this.teamManagers.length > 0 || this.teamMembers.length > 0) {
                    this.renderTeamGroupList(this.els.managerList, this.teamManagers, true);
                    this.renderTeamGroupList(this.els.memberList, this.teamMembers, false);
                    this.log(`team分类加载失败，保留当前入池显示: ${msg}`);
                } else {
                    this.renderTeamGroupList(this.els.managerList, [], true);
                    this.renderTeamGroupList(this.els.memberList, [], false);
                    this.log(`team分类加载失败: ${msg}`);
                }
                if (withToast) {
                    toast.error(`加载失败: ${msg}`);
                }
            } finally {
                this.teamGroupsLoading = false;
                if (this.els.btnReloadTeamGroups) {
                    loading.hide(this.els.btnReloadTeamGroups);
                }
            }
        }

        async loadTargetAccounts(withToast) {
            try {
                loading.show(this.els.btnPickTargetEmail, "...");
                const data = await api.get("/auto-team/target-accounts");
                const accounts = data.accounts || [];
                const lockedTotal = Number(data.locked_total || 0);
                this.targetAccounts = accounts;
                this.renderTargetModalList();
                this.log(`目标邮箱候选账号已加载: ${accounts.length} 个（邀请锁定 ${lockedTotal}）`);
                if (withToast) {
                    toast.success(`可选子号 ${accounts.length} 个`);
                }
            } catch (error) {
                const msg = safeError(error);
                this.log(`读取目标账号失败: ${msg}`);
                if (withToast) {
                    toast.error(msg);
                }
            } finally {
                loading.hide(this.els.btnPickTargetEmail);
            }
        }

        getFilteredTargetAccounts() {
            const q = String(fp.normalizeValue(this.els.targetModalSearch?.value) || "").toLowerCase();
            if (!q) return this.targetAccounts;
            return this.targetAccounts.filter((item) => {
                const email = String(item.email || "").toLowerCase();
                const idText = String(item.id || "");
                return email.includes(q) || idText.includes(q);
            });
        }

        updateTargetSelectedInfo() {
            if (!this.els.targetModalSelectedInfo) return;
            this.els.targetModalSelectedInfo.textContent = `已选 ${this.selectedTargetIds.size} 个`;
        }

        renderTargetModalList() {
            const container = this.els.targetModalList;
            if (!container) return;
            const list = this.getFilteredTargetAccounts();
            if (!list.length) {
                container.innerHTML = '<div class="empty-tip">暂无可选账号（仅 free 且非红色状态）</div>';
                this.updateTargetSelectedInfo();
                return;
            }

            container.innerHTML = list.map((item) => {
                const id = String(item.id);
                const checked = this.selectedTargetIds.has(id) ? "checked" : "";
                const planClass = normalizePlanType(item.plan || "free");
                const planText = getPlanBadgeText(item.plan || "free");
                return `
                    <label class="target-modal-item">
                        <input type="checkbox" data-target-id="${id}" ${checked}>
                        <span class="email">${escapeHtml(item.email || "")}</span>
                        <span class="target-plan-badge ${escapeHtml(planClass)}">${escapeHtml(planText)}</span>
                        <span class="meta">ID=${id}</span>
                    </label>
                `;
            }).join("");
            this.updateTargetSelectedInfo();
        }

        async openTargetModal() {
            await this.loadTargetAccounts(false);
            this.els.targetModal?.classList.add("show");
        }

        closeTargetModal() {
            this.els.targetModal?.classList.remove("show");
        }

        selectVisibleTargets() {
            const list = this.getFilteredTargetAccounts();
            list.forEach((item) => this.selectedTargetIds.add(String(item.id)));
            this.renderTargetModalList();
        }

        clearSelectedTargets() {
            this.selectedTargetIds.clear();
            this.renderTargetModalList();
        }

        addSelectedTargetsToInput() {
            const selectedItems = this.targetAccounts.filter((x) => this.selectedTargetIds.has(String(x.id)));
            if (!selectedItems.length) {
                toast.warning("请先勾选账号");
                return;
            }

            const existing = this.parseTargetEmails().valid;
            const merged = [...new Set([
                ...existing,
                ...selectedItems.map((x) => String(x.email || "").trim().toLowerCase()).filter(Boolean),
            ])];
            this.els.targetEmail.value = merged.join("\n");
            this.log(`已批量添加目标邮箱: ${selectedItems.length} 个`);
            toast.success(`已添加 ${selectedItems.length} 个邮箱`);
            this.closeTargetModal();
        }

        async openManualInviterModal() {
            this.els.manualInviterModal?.classList.add("show");
            if (Array.isArray(this.manualInviterCandidates) && this.manualInviterCandidates.length) {
                this.renderManualInviterList();
            } else if (this.els.manualInviterList) {
                this.els.manualInviterList.innerHTML = '<div class="empty-tip">加载候选账号中...</div>';
                this.updateManualInviterSelectedInfo();
            }
            // 弹窗先打开，候选列表异步加载，避免按钮点击后“卡一下”。
            void this.loadManualInviterCandidates(false);
        }

        closeManualInviterModal() {
            this.els.manualInviterModal?.classList.remove("show");
        }

        async loadManualInviterCandidates(withToast = false) {
            try {
                loading.show(this.els.btnManualPullInviter, "...");
                const data = await api.get("/auto-team/inviter-candidates?force=1", {
                    timeoutMs: 12000,
                    retry: 0,
                    silentNetworkError: true,
                    silentTimeoutError: true,
                });
                this.manualInviterCandidates = Array.isArray(data.accounts) ? data.accounts : [];
                this.renderManualInviterList();
                this.log(`手动拉入候选已加载: ${this.manualInviterCandidates.length} 个`);
                if (withToast) {
                    toast.success(`候选账号 ${this.manualInviterCandidates.length} 个`);
                }
            } catch (error) {
                const msg = safeError(error);
                this.log(`加载手动拉入候选失败: ${msg}`);
                if (withToast) {
                    toast.error(msg);
                }
            } finally {
                loading.hide(this.els.btnManualPullInviter);
            }
        }

        getFilteredManualInviterCandidates() {
            const q = String(fp.normalizeValue(this.els.manualInviterSearch?.value) || "").toLowerCase();
            if (!q) return this.manualInviterCandidates;
            return this.manualInviterCandidates.filter((item) => {
                const email = String(item.email || "").toLowerCase();
                const idText = String(item.id || "");
                const roleTag = String(item.role_tag || "");
                const poolState = String(item.pool_state || "");
                return email.includes(q) || idText.includes(q) || roleTag.includes(q) || poolState.includes(q);
            });
        }

        updateManualInviterSelectedInfo() {
            if (!this.els.manualInviterSelectedInfo) return;
            this.els.manualInviterSelectedInfo.textContent = `已选 ${this.selectedManualInviterIds.size} 个`;
        }

        renderManualInviterList() {
            const container = this.els.manualInviterList;
            if (!container) return;
            const list = this.getFilteredManualInviterCandidates();
            if (!list.length) {
                container.innerHTML = '<div class="empty-tip">暂无可拉入账号（仅母号/普通）</div>';
                this.updateManualInviterSelectedInfo();
                return;
            }
            container.innerHTML = list.map((item) => {
                const id = String(item.id || "");
                const checked = this.selectedManualInviterIds.has(id) ? "checked" : "";
                const roleText = String(item.role_tag || "none");
                const poolText = String(item.pool_state || "candidate_pool");
                return `
                    <label class="target-modal-item">
                        <input type="checkbox" data-inviter-id="${id}" ${checked}>
                        <span class="email">${escapeHtml(item.email || "")}</span>
                        <span class="meta">ID=${id} | role=${escapeHtml(roleText)} | pool=${escapeHtml(poolText)}</span>
                    </label>
                `;
            }).join("");
            this.updateManualInviterSelectedInfo();
        }

        selectVisibleManualInviters() {
            const list = this.getFilteredManualInviterCandidates();
            list.forEach((item) => this.selectedManualInviterIds.add(String(item.id)));
            this.renderManualInviterList();
        }

        clearSelectedManualInviters() {
            this.selectedManualInviterIds.clear();
            this.renderManualInviterList();
        }

        async submitManualInviterSelection() {
            const ids = [...this.selectedManualInviterIds].map((x) => Number(x)).filter((x) => Number.isFinite(x) && x > 0);
            if (!ids.length) {
                toast.warning("请先选择要拉入的账号");
                return;
            }
            try {
                loading.show(this.els.btnSubmitManualInviter, "处理中...");
                const data = await api.post("/auto-team/inviter-pool/add", {
                    account_ids: ids,
                }, {
                    timeoutMs: 15000,
                    retry: 0,
                    priority: "high",
                });
                const added = Array.isArray(data.added) ? data.added.length : 0;
                const skipped = Array.isArray(data.skipped) ? data.skipped.length : 0;
                const invalid = Array.isArray(data.invalid) ? data.invalid.length : 0;
                this.log(`手动拉入完成: added=${added} skipped=${skipped} invalid=${invalid}`);
                toast.success(`拉入完成：新增 ${added}，跳过 ${skipped}，无效 ${invalid}`);
                this.closeManualInviterModal();
                this.selectedManualInviterIds.clear();
                await this.loadInviterAccounts(false, this.els.btnReloadInviterList, true);
                await this.loadTeamGroups(false, true);
            } catch (error) {
                const msg = safeError(error);
                this.log(`手动拉入失败: ${msg}`);
                toast.error(msg);
            } finally {
                loading.hide(this.els.btnSubmitManualInviter);
            }
        }

        async loadInviterAccounts(withToast, loadingBtn = null, force = false) {
            const queryParams = fp.toQuery({
                force: force ? 1 : null,
                local_only: 1,
            });
            const queryText = queryParams.toString();
            const query = queryText ? `?${queryText}` : "";
            const btn = loadingBtn || this.els.btnReloadAccounts || this.els.btnReloadInviterList;
            try {
                this.setReloadButtonLoading(btn, true);
                const data = await api.get(`/auto-team/inviter-accounts${query}`, {
                    timeoutMs: 12000,
                    retry: 0,
                    silentNetworkError: true,
                    silentTimeoutError: true,
                });
                this.inviterLoaded = true;
                const accountsRaw = Array.isArray(data.accounts) ? data.accounts : [];
                const accounts = this.pruneInviterAccounts(accountsRaw, "刷新结果");
                this.inviterAccounts = accounts;
                this.writeCache(INVITER_CACHE_KEY, this.inviterAccounts);
                this.fillSelect(this.inviterAccounts);
                this.renderInviters(this.inviterAccounts);
                if (accounts.length > 0) {
                    this.log(`读取可用 Team 邀请账号完成: ${accounts.length} 个（自动按管理号入池）`);
                } else {
                    this.log("读取可用 Team 邀请账号完成: 0 个（已按最新规则清空不符合账号）");
                }
                if (withToast) {
                    if (accounts.length > 0) {
                        toast.success(`已刷新，可用账号 ${accounts.length} 个`);
                    } else {
                        toast.warning("已刷新：当前无符合条件的 Team 管理账号");
                    }
                }
                void this.refreshInviterAccountsInBackground(force);
            } catch (error) {
                let msg = safeError(error);
                this.inviterLoaded = true;
                const abortLike = String(msg || "").toLowerCase().includes("abort") || String(error?.name || "").toLowerCase() === "aborterror";
                if (abortLike) {
                    try {
                        const retryData = await api.get(`/auto-team/inviter-accounts${query}`, {
                            timeoutMs: 12000,
                            retry: 0,
                            cancelPrevious: false,
                            requestKey: `auto-team:inviter-accounts:retry:${Date.now()}`,
                            silentNetworkError: true,
                            silentTimeoutError: true,
                        });
                        const retryRaw = Array.isArray(retryData.accounts) ? retryData.accounts : [];
                        const retryAccounts = this.pruneInviterAccounts(retryRaw, "中断重试");
                        this.inviterAccounts = retryAccounts;
                        this.writeCache(INVITER_CACHE_KEY, this.inviterAccounts);
                        this.fillSelect(this.inviterAccounts);
                        this.renderInviters(this.inviterAccounts);
                        this.log(`读取邀请账号中断后重试成功: ${retryAccounts.length} 个`);
                        if (withToast) {
                            toast.success(`重试成功，可用账号 ${retryAccounts.length} 个`);
                        }
                        return;
                    } catch (retryError) {
                        const retryMsg = safeError(retryError);
                        msg = `${msg} | retry=${retryMsg}`;
                    }
                }
                if (this.inviterAccounts.length > 0) {
                    const kept = this.pruneInviterAccounts(this.inviterAccounts, "失败保留");
                    this.inviterAccounts = kept;
                    this.writeCache(INVITER_CACHE_KEY, this.inviterAccounts);
                    this.fillSelect(this.inviterAccounts);
                    this.renderInviters(this.inviterAccounts);
                    if (this.inviterAccounts.length > 0) {
                        this.log(`读取邀请账号失败，保留当前入池显示: ${msg}`);
                    } else {
                        this.log(`读取邀请账号失败，且本地保留后无可用账号: ${msg}`);
                    }
                } else {
                    this.renderInviters([]);
                    this.log(`读取邀请账号失败: ${msg}`);
                }
                if (withToast) {
                    toast.error(`读取失败: ${msg}`);
                }
            } finally {
                this.setReloadButtonLoading(btn, false);
            }
        }

        async runSilentPrecheck(sampleEmail, inviterAccountId, totalEmails, invalidCount) {
            let lastError = null;
            const maxAttempts = 2;
            for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
                try {
                    const data = await api.post("/auto-team/preview", {
                        target_email: sampleEmail,
                        inviter_account_id: inviterAccountId,
                    });
                    const tip = totalEmails > 1
                        ? `自动预检通过。当前 ${totalEmails} 个目标邮箱（示例: ${sampleEmail}）。`
                        : "自动预检通过，可以执行 Team 自动邀请。";
                    if (invalidCount > 0) {
                        this.log(`自动预检提示: 发现无效邮箱 ${invalidCount} 个，执行时将自动跳过。`);
                    }
                    this.log(`自动预检成功: inviter=${data.inviter?.email || "-"} | ${tip}`);
                    return data;
                } catch (error) {
                    lastError = error;
                    const msg = String(safeError(error) || "");
                    const lowerMsg = msg.toLowerCase();
                    const retryable = [
                        "timeout",
                        "timed out",
                        "connection",
                        "network",
                        "502",
                        "503",
                        "504",
                    ].some((k) => lowerMsg.includes(k));
                    if (retryable && attempt < maxAttempts) {
                        this.log(`自动预检网络波动，第 ${attempt}/${maxAttempts} 次重试中...`);
                        await new Promise((resolve) => setTimeout(resolve, 800 * attempt));
                        continue;
                    }
                    if (retryable) {
                        this.log(`自动预检网络异常，已跳过预检直接执行邀请: ${msg}`);
                        return null;
                    }
                    throw error;
                }
            }
            throw lastError || new Error("自动预检失败");
        }

        async handleInvite() {
            const { valid, invalid } = this.parseTargetEmails();
            const inviter_account_id = this.getInviterId();
            if (!valid.length) {
                toast.error("请先填写有效目标邮箱");
                return;
            }

            try {
                loading.show(this.els.btnInvite, "邀请中...");
                this.log(`开始执行team邀请流程（共 ${valid.length} 个目标邮箱）。`);
                if (invalid.length) {
                    this.log(`检测到无效邮箱 ${invalid.length} 个，已自动跳过。`);
                }

                await this.runSilentPrecheck(valid[0], inviter_account_id, valid.length, invalid.length);

                let successCount = 0;
                let failedCount = 0;
                const successItems = [];
                const failedItems = [];
                const successfulEmails = new Set();

                for (let i = 0; i < valid.length; i++) {
                    const email = valid[i];
                    this.log(`执行邀请 ${i + 1}/${valid.length}: ${email}`);
                    try {
                        const data = await api.post("/auto-team/invite", {
                            target_email: email,
                            inviter_account_id,
                        });
                        successCount += 1;
                        successItems.push({
                            email,
                            inviter: data?.inviter?.email || "-",
                            message: data?.message || "邀请已提交",
                        });
                        successfulEmails.add(String(email || "").trim().toLowerCase());
                        this.log(`邀请成功: ${email} <- ${data?.inviter?.email || "-"}`);
                    } catch (error) {
                        failedCount += 1;
                        const msg = safeError(error);
                        failedItems.push({ email, error: msg });
                        this.log(`邀请失败: ${email} | ${msg}`);
                    }
                }

                const summary = `执行完成：成功 ${successCount}，失败 ${failedCount}，跳过无效 ${invalid.length}`;
                const resultType = failedCount > 0 ? (successCount > 0 ? "warning" : "error") : "success";
                this.setResult(
                    resultType,
                    "自动邀请结果",
                    summary,
                    { success: successItems, failed: failedItems, invalid },
                );
                if (failedCount > 0) {
                    toast.warning(summary);
                } else {
                    toast.success(summary);
                }
                if (successfulEmails.size) {
                    this.targetAccounts = this.targetAccounts.filter((item) => {
                        const email = String(item?.email || "").trim().toLowerCase();
                        return !successfulEmails.has(email);
                    });
                    this.selectedTargetIds = new Set(
                        [...this.selectedTargetIds].filter((id) => {
                            const item = this.targetAccounts.find((x) => String(x.id) === String(id));
                            return !!item;
                        })
                    );
                    this.renderTargetModalList();
                }
                await this.loadTeamGroups(false, true);
            } catch (error) {
                const msg = safeError(error);
                this.log(`邀请失败: ${msg}`);
                this.setResult("error", "邀请失败", msg);
                toast.error(msg);
            } finally {
                loading.hide(this.els.btnInvite);
            }
        }
    }

    document.addEventListener("DOMContentLoaded", () => {
        window.autoTeamPage = new AutoTeamPage();
    });
})();

