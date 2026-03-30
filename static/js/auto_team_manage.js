
(function () {
    const STATUS_LABEL = {
        active: '可用',
        full: '已满',
        expired: '已过期',
        blocked: '已阻断',
        unknown: '待校验',
        error: '异常',
        banned: '已封禁',
        failed: '失败',
    };

    const COLUMN_KEYS = ['members', 'plan', 'expires'];
    const HIDDEN_COLS_KEY = 'auto_team_manage_hidden_cols_v1';
    const TEAM_INVITER_CACHE_KEY = 'auto_team_inviter_accounts_cache_v1';
    const TEAM_ROWS_CACHE_KEY = 'auto_team_manage_rows_cache_v1';
    const TEAM_MEMBERS_CACHE_KEY = 'auto_team_manage_members_cache_v1';
    const DEFAULT_TEAM_MAX_MEMBERS = 5;

    function esc(value) {
        return String(value || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function fmtDate(raw) {
        if (!raw) return '-';
        const dt = new Date(raw);
        if (Number.isNaN(dt.getTime())) return String(raw);
        const y = dt.getFullYear();
        const m = String(dt.getMonth() + 1).padStart(2, '0');
        const d = String(dt.getDate()).padStart(2, '0');
        const h = String(dt.getHours()).padStart(2, '0');
        const mm = String(dt.getMinutes()).padStart(2, '0');
        return `${y}-${m}-${d} ${h}:${mm}`;
    }

    function statusText(status) {
        const key = String(status || '').toLowerCase();
        return STATUS_LABEL[key] || key || '未知';
    }

    const BLOCKED_INVITER_STATUSES = new Set([
        'failed',
        'banned',
        'deleted',
        'disabled',
        'invalid',
        'inactive',
        'frozen',
        'expired',
        'error',
        'locked',
        'suspended',
    ]);

    function isHardRemoveAuthSource(rawSource) {
        const source = String(rawSource || '').trim().toLowerCase();
        if (!source) return false;
        return (
            source.includes('hard_remove_auth')
            || source.includes('http_401')
            || source.includes('http_403')
            || source.includes('token has been invalidated')
            || source.includes('authentication token has been invalidated')
            || source.includes('please try signing in again')
        );
    }

    function isBlockedInviter(item) {
        const status = String(item?.status || '').trim().toLowerCase();
        const source = String(item?.manager_verify_source || item?.verify_source || '').trim();
        if (BLOCKED_INVITER_STATUSES.has(status)) return true;
        if (isHardRemoveAuthSource(source)) return true;
        return false;
    }

    function normalizePlan(planRaw) {
        const value = String(planRaw || '').trim().toLowerCase();
        if (value.includes('plus') || value.includes('pro')) return 'plus';
        if (value.includes('team') || value.includes('enterprise')) return 'team';
        return 'free';
    }

    function planText(planRaw) {
        const plan = normalizePlan(planRaw);
        if (plan === 'plus') return 'PLUS';
        if (plan === 'team') return 'TEAM';
        return 'FREE';
    }

    function safeError(error) {
        if (!error) return '未知错误';
        if (typeof error === 'string') return error;
        if (error.data && error.data.detail) {
            if (typeof error.data.detail === 'string') return error.data.detail;
            try { return JSON.stringify(error.data.detail); } catch (_e) { return String(error.data.detail); }
        }
        if (error.message) return error.message;
        try { return JSON.stringify(error); } catch (_e) { return '未知错误'; }
    }

    function decodeJwtPayload(token) {
        const raw = String(token || '').trim();
        if (!raw) return {};
        const parts = raw.split('.');
        if (parts.length < 2) return {};
        try {
            const base64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
            const pad = '='.repeat((4 - (base64.length % 4)) % 4);
            const decoded = atob(base64 + pad);
            const text = decodeURIComponent(
                Array.from(decoded).map((ch) => `%${ch.charCodeAt(0).toString(16).padStart(2, '0')}`).join('')
            );
            return JSON.parse(text);
        } catch (_e) {
            return {};
        }
    }

    function extractTokenFields(accessToken) {
        const payload = decodeJwtPayload(accessToken);
        const auth = payload['https://api.openai.com/auth'] || {};
        const profile = payload['https://api.openai.com/profile'] || {};
        const email = String(profile.email || payload.email || '').trim().toLowerCase();
        const accountId = String(auth.chatgpt_account_id || '').trim();
        const clientId = String(payload.client_id || '').trim();
        const planRaw = String(auth.chatgpt_plan_type || '').trim().toLowerCase();
        let plan = planRaw;
        if (plan.includes('team') || plan.includes('enterprise')) plan = 'team';
        else if (plan.includes('plus')) plan = 'plus';
        else if (plan.includes('basic') || plan.includes('free')) plan = 'free';
        return { email, accountId, clientId, plan };
    }

    class TeamManageConsole {
        constructor() {
            this.els = {
                tabManage: document.getElementById('tabManage'),
                panelManage: document.getElementById('panelManage'),
                teamStatTotal: document.getElementById('teamStatTotal'),
                teamStatAvailable: document.getElementById('teamStatAvailable'),
                teamStatusFilter: document.getElementById('teamStatusFilter'),
                teamSearchInput: document.getElementById('teamSearchInput'),
                btnToggleColumnMenu: document.getElementById('btnToggleColumnMenu'),
                teamColumnMenu: document.getElementById('teamColumnMenu'),
                btnImportTeam: document.getElementById('btnImportTeam'),
                btnReloadTeamConsole: document.getElementById('btnReloadTeamConsole'),
                teamSelectAll: document.getElementById('teamSelectAll'),
                teamTableBody: document.getElementById('teamTableBody'),
                teamImportModal: document.getElementById('teamImportModal'),
                btnCloseTeamImportModal: document.getElementById('btnCloseTeamImportModal'),
                btnTeamImportSingleTab: document.getElementById('btnTeamImportSingleTab'),
                btnTeamImportBatchTab: document.getElementById('btnTeamImportBatchTab'),
                teamImportSinglePanel: document.getElementById('teamImportSinglePanel'),
                teamImportBatchPanel: document.getElementById('teamImportBatchPanel'),
                teamImportModalHint: document.getElementById('teamImportModalHint'),
                teamImportAccessToken: document.getElementById('teamImportAccessToken'),
                teamImportRefreshToken: document.getElementById('teamImportRefreshToken'),
                teamImportSessionToken: document.getElementById('teamImportSessionToken'),
                teamImportClientId: document.getElementById('teamImportClientId'),
                teamImportEmail: document.getElementById('teamImportEmail'),
                teamImportAccountId: document.getElementById('teamImportAccountId'),
                teamImportBatchText: document.getElementById('teamImportBatchText'),
                btnSubmitTeamImport: document.getElementById('btnSubmitTeamImport'),

                teamMemberModal: document.getElementById('teamMemberModal'),
                teamMemberModalSub: document.getElementById('teamMemberModalSub'),
                teamMemberModalHint: document.getElementById('teamMemberModalHint'),
                btnCloseTeamMemberModal: document.getElementById('btnCloseTeamMemberModal'),
                teamMemberInviteEmail: document.getElementById('teamMemberInviteEmail'),
                btnInviteMember: document.getElementById('btnInviteMember'),
                btnReloadTeamMembers: document.getElementById('btnReloadTeamMembers'),
                teamJoinedMembersBody: document.getElementById('teamJoinedMembersBody'),
                teamInvitedMembersBody: document.getElementById('teamInvitedMembersBody'),
            };

            this.rows = [];
            this.filteredRows = [];
            this.loaded = false;
            this.teamConsoleUnavailable = false;
            this.hiddenCols = new Set(this.readHiddenCols());
            this.memberAccountId = null;
            this.memberAccountEmail = '';
            this.teamImportMode = 'single';
            this.consoleLoadSeq = 0;
            this.membersCache = this.readJsonCache(TEAM_MEMBERS_CACHE_KEY, {});
            this.bindEvents();
            this.applyColumnVisibility();
            this.restoreRowsFromCache();
            this.renderRows();
        }

        bindEvents() {
            this.els.tabManage?.addEventListener('click', () => {
                if (!this.loaded) this.renderRows();
            });

            this.els.teamStatusFilter?.addEventListener('change', () => this.applyFilters());
            this.els.teamSearchInput?.addEventListener('input', () => this.applyFilters());
            this.els.btnReloadTeamConsole?.addEventListener('click', () => this.loadConsole(true, true));
            this.els.btnImportTeam?.addEventListener('click', () => this.openImportModal());
            this.els.btnCloseTeamImportModal?.addEventListener('click', () => this.closeImportModal());
            this.els.teamImportModal?.addEventListener('click', (e) => {
                if (e.target === this.els.teamImportModal) this.closeImportModal();
            });
            this.els.btnTeamImportSingleTab?.addEventListener('click', () => this.setImportMode('single'));
            this.els.btnTeamImportBatchTab?.addEventListener('click', () => this.setImportMode('batch'));
            this.els.btnSubmitTeamImport?.addEventListener('click', () => this.submitTeamImport());

            this.els.btnToggleColumnMenu?.addEventListener('click', (e) => {
                e.stopPropagation();
                this.els.teamColumnMenu?.classList.toggle('show');
            });
            this.els.teamColumnMenu?.addEventListener('change', (e) => this.onColumnToggle(e));

            this.els.teamSelectAll?.addEventListener('change', () => {
                const checked = !!this.els.teamSelectAll.checked;
                this.els.teamTableBody?.querySelectorAll('input[data-row-select]').forEach((el) => {
                    el.checked = checked;
                });
            });

            this.els.teamTableBody?.addEventListener('click', (e) => this.handleTableAction(e));

            this.els.btnCloseTeamMemberModal?.addEventListener('click', () => this.closeMemberModal());
            this.els.teamMemberModal?.addEventListener('click', (e) => {
                if (e.target === this.els.teamMemberModal) this.closeMemberModal();
            });
            this.els.btnInviteMember?.addEventListener('click', () => this.inviteMember());
            this.els.btnReloadTeamMembers?.addEventListener('click', () => this.loadMembers(true));
            this.els.teamJoinedMembersBody?.addEventListener('click', (e) => this.handleMemberTableAction(e, 'joined'));
            this.els.teamInvitedMembersBody?.addEventListener('click', (e) => this.handleMemberTableAction(e, 'invited'));

            document.addEventListener('click', (e) => {
                if (!this.els.teamColumnMenu || !this.els.btnToggleColumnMenu) return;
                if (!this.els.teamColumnMenu.contains(e.target) && !this.els.btnToggleColumnMenu.contains(e.target)) {
                    this.els.teamColumnMenu.classList.remove('show');
                }
            });
        }
        readHiddenCols() {
            try {
                const raw = localStorage.getItem(HIDDEN_COLS_KEY);
                if (!raw) return [];
                const parsed = JSON.parse(raw);
                return Array.isArray(parsed) ? parsed.filter((x) => COLUMN_KEYS.includes(x)) : [];
            } catch (_e) {
                return [];
            }
        }

        saveHiddenCols() {
            try {
                localStorage.setItem(HIDDEN_COLS_KEY, JSON.stringify([...this.hiddenCols]));
            } catch (_e) {
                // ignore
            }
        }

        readJsonCache(key, fallback) {
            try {
                const raw = localStorage.getItem(key);
                if (!raw) return fallback;
                return JSON.parse(raw);
            } catch (_e) {
                return fallback;
            }
        }

        writeJsonCache(key, value) {
            try {
                localStorage.setItem(key, JSON.stringify(value));
            } catch (_e) {
                // ignore
            }
        }

        persistRowsCache() {
            this.writeJsonCache(TEAM_ROWS_CACHE_KEY, this.rows || []);
        }

        restoreRowsFromCache() {
            const cachedRows = this.readJsonCache(TEAM_ROWS_CACHE_KEY, []);
            if (Array.isArray(cachedRows) && cachedRows.length > 0) {
                this.rows = cachedRows.map((x) => this.normalizeRow(x));
                this.loaded = true;
                this.recomputeStats();
                this.applyFilters();
            }
        }

        syncRowsFromInviters(inviters, options = {}) {
            const sourceRaw = Array.isArray(inviters) ? inviters : [];
            const source = sourceRaw.filter((item) => !isBlockedInviter(item));
            if (!source.length) return;
            const pruneMissing = !!options.pruneMissing;
            const applyStats = options.applyStats !== false;
            const existingMap = new Map((this.rows || []).map((row) => [Number(row.id || 0), this.normalizeRow(row)]));
            const orderedIds = [];
            const nextMap = new Map();

            source.forEach((item) => {
                const incoming = this.fromInviterToRow(item);
                const id = Number(incoming.id || 0);
                if (!id) return;
                orderedIds.push(id);
                const existing = existingMap.get(id);
                if (existing) {
                    const maxMembers = Number(existing.max_members || incoming.max_members || DEFAULT_TEAM_MAX_MEMBERS);
                    const currentMembers = Number(existing.current_members || 0);
                    nextMap.set(id, this.normalizeRow({
                        ...existing,
                        ...incoming,
                        current_members: Number.isFinite(currentMembers) ? currentMembers : 0,
                        max_members: Number.isFinite(maxMembers) && maxMembers > 0 ? maxMembers : DEFAULT_TEAM_MAX_MEMBERS,
                        member_ratio: `${Number.isFinite(currentMembers) ? currentMembers : 0}/${Number.isFinite(maxMembers) && maxMembers > 0 ? maxMembers : DEFAULT_TEAM_MAX_MEMBERS}`,
                    }));
                } else {
                    nextMap.set(id, this.normalizeRow(incoming));
                }
            });

            if (!pruneMissing) {
                existingMap.forEach((row, id) => {
                    if (!nextMap.has(id)) nextMap.set(id, this.normalizeRow(row));
                });
            }

            const nextRows = [];
            orderedIds.forEach((id) => {
                if (nextMap.has(id)) nextRows.push(nextMap.get(id));
            });
            nextMap.forEach((row, id) => {
                if (!orderedIds.includes(id)) nextRows.push(row);
            });

            this.rows = nextRows;
            this.loaded = true;
            this.persistRowsCache();
            if (applyStats) {
                this.recomputeStats();
                this.applyFilters();
            }
        }

        syncRowsFromConsole(consoleRows, options = {}) {
            const source = Array.isArray(consoleRows) ? consoleRows : [];
            if (!source.length) return false;
            const applyStats = options.applyStats !== false;
            const existingMap = new Map((this.rows || []).map((row) => [Number(row.id || 0), this.normalizeRow(row)]));
            const orderedIds = [];
            const nextMap = new Map();

            source.forEach((item) => {
                const incoming = this.normalizeRow(item);
                const id = Number(incoming.id || 0);
                if (!id) return;
                orderedIds.push(id);
                const existing = existingMap.get(id);
                if (existing) {
                    nextMap.set(id, this.normalizeRow({
                        ...existing,
                        ...incoming,
                        current_members: Number.isFinite(Number(incoming.current_members))
                            ? Number(incoming.current_members)
                            : Number(existing.current_members || 0),
                        max_members: Number.isFinite(Number(incoming.max_members)) && Number(incoming.max_members) > 0
                            ? Number(incoming.max_members)
                            : Number(existing.max_members || DEFAULT_TEAM_MAX_MEMBERS),
                        member_ratio: incoming.member_ratio || existing.member_ratio || '0/5',
                    }));
                } else {
                    nextMap.set(id, incoming);
                }
            });

            existingMap.forEach((row, id) => {
                if (!nextMap.has(id)) nextMap.set(id, this.normalizeRow(row));
            });

            const nextRows = [];
            orderedIds.forEach((id) => {
                if (nextMap.has(id)) nextRows.push(nextMap.get(id));
            });
            nextMap.forEach((row, id) => {
                if (!orderedIds.includes(id)) nextRows.push(row);
            });

            this.rows = nextRows;
            this.loaded = true;
            this.persistRowsCache();
            if (applyStats) {
                this.recomputeStats();
                this.applyFilters();
            }
            return true;
        }

        updateRowMemberStats(accountId, joinedCount) {
            const id = Number(accountId || 0);
            if (!id) return;
            const idx = this.rows.findIndex((x) => Number(x.id || 0) === id);
            if (idx < 0) return;
            const row = this.rows[idx] || {};
            const maxMembersRaw = Number(row.max_members || DEFAULT_TEAM_MAX_MEMBERS);
            const maxMembers = Number.isFinite(maxMembersRaw) && maxMembersRaw > 0 ? maxMembersRaw : DEFAULT_TEAM_MAX_MEMBERS;
            const current = Math.max(0, Number(joinedCount || 0));
            this.rows.splice(idx, 1, this.normalizeRow({
                ...row,
                current_members: current,
                max_members: maxMembers,
                member_ratio: `${current}/${maxMembers}`,
            }));
            this.persistRowsCache();
            this.recomputeStats();
            this.applyFilters();
        }

        onColumnToggle(event) {
            const target = event.target;
            if (!target || !target.matches('input[type="checkbox"][data-col-toggle]')) return;
            const key = String(target.dataset.colToggle || '');
            if (!COLUMN_KEYS.includes(key)) return;
            if (target.checked) this.hiddenCols.delete(key);
            else this.hiddenCols.add(key);
            this.saveHiddenCols();
            this.applyColumnVisibility();
        }

        applyColumnVisibility() {
            COLUMN_KEYS.forEach((key) => {
                const hidden = this.hiddenCols.has(key);
                document.querySelectorAll(`.col-${key}`).forEach((el) => {
                    el.style.display = hidden ? 'none' : '';
                });
            });
            this.els.teamColumnMenu?.querySelectorAll('input[type="checkbox"][data-col-toggle]').forEach((el) => {
                const key = String(el.dataset.colToggle || '');
                el.checked = !this.hiddenCols.has(key);
            });
        }

        normalizeRow(row) {
            const currentRaw = Number(row.current_members || row.currentMembers || 0);
            const maxRaw = Number(row.max_members || row.maxMembers || DEFAULT_TEAM_MAX_MEMBERS);
            const max = Number.isFinite(maxRaw) && maxRaw > 0
                ? Math.min(maxRaw, DEFAULT_TEAM_MAX_MEMBERS)
                : DEFAULT_TEAM_MAX_MEMBERS;
            const current = Number.isFinite(currentRaw) ? Math.max(0, Math.min(currentRaw, max)) : 0;
            return {
                id: Number(row.id || 0),
                email: String(row.email || ''),
                account_id: String(row.account_id || row.workspace_id || ''),
                team_name: String(row.team_name || 'MyTeam'),
                current_members: current,
                max_members: max,
                member_ratio: `${current}/${max}`,
                subscription_plan: String(row.subscription_plan || 'chatgptteamplan'),
                expires_at: row.expires_at || null,
                status: String(row.status || 'active').toLowerCase(),
                role_tag: String(row.role_tag || ''),
                pool_state: String(row.pool_state || ''),
                priority: Number(row.priority || 50),
                last_used_at: row.last_used_at || null,
                workspace_id: String(row.workspace_id || ''),
            };
        }

        markRowsSoftUnavailable(rows, status = 'expired') {
            const sourceRows = Array.isArray(rows) ? rows : [];
            const nextStatus = String(status || 'expired').trim().toLowerCase() || 'expired';
            return sourceRows.map((row) => this.normalizeRow({
                ...row,
                status: nextStatus,
            }));
        }

        fromInviterToRow(item) {
            const id = Number(item.id || 0);
            return {
                id: Number.isFinite(id) ? id : 0,
                email: String(item.email || ''),
                account_id: String(item.workspace_id || ''),
                team_name: 'MyTeam',
                current_members: 0,
                max_members: DEFAULT_TEAM_MAX_MEMBERS,
                member_ratio: `0/${DEFAULT_TEAM_MAX_MEMBERS}`,
                subscription_plan: 'chatgptteamplan',
                expires_at: null,
                status: String(item.status || 'active').toLowerCase(),
                role_tag: String(item.role_tag || ''),
                pool_state: String(item.pool_state || ''),
                priority: Number(item.priority || 50),
                last_used_at: item.last_used_at || null,
                workspace_id: String(item.workspace_id || ''),
            };
        }

        async loadRowsFromInviterPool(force = false, seq = null) {
            try {
                const queryParams = new URLSearchParams();
                if (force) queryParams.set('force', '1');
                queryParams.set('local_only', '1');
                const query = `?${queryParams.toString()}`;
                const inviterData = await api.get(`/auto-team/inviter-accounts${query}`, {
                    timeoutMs: 10000,
                    retry: 0,
                    priority: 'high',
                    requestKey: 'auto-team:inviter-accounts',
                    cancelPrevious: true,
                    silentNetworkError: true,
                    silentTimeoutError: true,
                });
                if (seq != null && seq !== this.consoleLoadSeq) {
                    return false;
                }
                const inviters = (Array.isArray(inviterData.accounts) ? inviterData.accounts : [])
                    .filter((item) => !isBlockedInviter(item));
                if (!inviters.length) {
                    return false;
                }
                this.syncRowsFromInviters(inviters, { pruneMissing: false, applyStats: false });
                return true;
            } catch (error) {
                if (error?.name === 'AbortError' && error?.cancelReason === 'request_replaced') {
                    return false;
                }
                return false;
            }
        }

        async refreshConsoleRemote(seq, rowsBeforeLoad, options = {}) {
            const force = !!options.force;
            const withToast = !!options.withToast;
            try {
                const query = force ? '?force=1' : '';
                const data = await api.get(`/auto-team/team-console${query}`, {
                    timeoutMs: 15000,
                    retry: 0,
                    priority: 'high',
                    requestKey: 'auto-team:team-console',
                    cancelPrevious: true,
                    silentNetworkError: true,
                    silentTimeoutError: true,
                });
                if (seq !== this.consoleLoadSeq) return;

                this.teamConsoleUnavailable = false;
                const consoleRows = Array.isArray(data.rows) ? data.rows : [];
                const remoteSynced = this.syncRowsFromConsole(consoleRows, { applyStats: false });

                let usedInviterFallback = false;
                if (!consoleRows.length) {
                    usedInviterFallback = await this.loadRowsFromInviterPool(false, seq);
                }
                if (seq !== this.consoleLoadSeq) return;

                if (!this.rows.length && !usedInviterFallback && rowsBeforeLoad.length) {
                    this.rows = this.markRowsSoftUnavailable(rowsBeforeLoad, 'expired');
                    this.loaded = true;
                    this.recomputeStats();
                    this.persistRowsCache();
                    this.applyFilters();
                    if (withToast) toast.warning('当前无可用 Team 管理账号，已保留历史 Team 列表并标记为过期');
                    return;
                }

                this.loaded = true;
                this.recomputeStats();
                this.persistRowsCache();
                this.applyFilters();
                if (withToast) {
                    if (remoteSynced || usedInviterFallback) {
                        toast.success('Team 控制台已刷新');
                    } else {
                        toast.warning('刷新完成，但当前无可用 Team 管理账号');
                    }
                }
            } catch (error) {
                if (error?.name === 'AbortError' && error?.cancelReason === 'request_replaced') {
                    return;
                }
                const msg = safeError(error);
                const statusCode = Number(error?.response?.status || 0);
                const fallbackOk = await this.loadRowsFromInviterPool(false, seq);
                if (seq !== this.consoleLoadSeq) return;

                this.loaded = true;
                if (fallbackOk) {
                    this.recomputeStats();
                    this.applyFilters();
                    this.persistRowsCache();
                    if (statusCode === 404 || /not found/i.test(msg)) {
                        this.teamConsoleUnavailable = true;
                        if (withToast) toast.success('已切换邀请池模式（team-console 不可用）');
                        return;
                    }
                    if (withToast) toast.warning(`team-console 读取失败，已回退邀请池：${msg}`);
                    return;
                }
                if (rowsBeforeLoad.length) {
                    this.rows = this.markRowsSoftUnavailable(rowsBeforeLoad, 'unknown');
                    this.loaded = true;
                    this.recomputeStats();
                    this.applyFilters();
                    this.persistRowsCache();
                    if (withToast) toast.warning(`team-console 读取失败，已保留当前列表（待校验）：${msg}`);
                    return;
                }

                this.filteredRows = [];
                this.setStats(0, 0);
                this.renderRows();
                if (withToast) toast.error(msg);
            }
        }

        async loadConsole(withToast, force = false) {
            const seq = ++this.consoleLoadSeq;
            const rowsBeforeLoad = Array.isArray(this.rows) ? this.rows.map((x) => this.normalizeRow(x)) : [];
            const applyManualPoolSync = async () => {
                const synced = await this.loadRowsFromInviterPool(true, seq);
                if (seq !== this.consoleLoadSeq) return false;
                if (synced) {
                    this.loaded = true;
                    this.recomputeStats();
                    this.applyFilters();
                    this.persistRowsCache();
                }
                return synced;
            };

            if (this.teamConsoleUnavailable && !force) {
                try {
                    loading.show(this.els.btnReloadTeamConsole, '刷新中...');
                    const fallbackOk = await this.loadRowsFromInviterPool(false, seq);
                    if (seq !== this.consoleLoadSeq) return;
                    this.loaded = true;
                    if (fallbackOk) {
                        this.recomputeStats();
                        this.applyFilters();
                        this.persistRowsCache();
                        if (withToast) toast.success('已按邀请池刷新 Team 列表');
                        return;
                    }
                    if (rowsBeforeLoad.length) {
                        this.rows = this.markRowsSoftUnavailable(rowsBeforeLoad, 'expired');
                        this.loaded = true;
                        this.recomputeStats();
                        this.applyFilters();
                        this.persistRowsCache();
                        if (withToast) toast.warning('当前无可用 Team 管理账号，已保留历史 Team 列表并标记为过期');
                        return;
                    }
                    this.filteredRows = [];
                    this.setStats(0, 0);
                    this.renderRows();
                    if (withToast) toast.warning('当前无可用 Team 管理账号');
                    return;
                } finally {
                    if (seq === this.consoleLoadSeq) {
                        loading.hide(this.els.btnReloadTeamConsole);
                    }
                }
            }

            if (force) {
                try {
                    loading.show(this.els.btnReloadTeamConsole, '刷新中...');
                    const manualSynced = await applyManualPoolSync();
                    if (seq !== this.consoleLoadSeq) return;

                    if (!manualSynced && rowsBeforeLoad.length) {
                        this.rows = this.markRowsSoftUnavailable(rowsBeforeLoad, 'expired');
                        this.loaded = true;
                        this.recomputeStats();
                        this.applyFilters();
                        this.persistRowsCache();
                        if (withToast) toast.warning('本地未匹配到可用管理账号，已保留历史 Team 列表并标记为过期');
                    } else if (!manualSynced && !this.rows.length) {
                        this.filteredRows = [];
                        this.setStats(0, 0);
                        this.renderRows();
                        if (withToast) toast.warning('当前无可用 Team 管理账号');
                    } else if (withToast) {
                        toast.success('已完成本地同步，正在后台校验 Team 状态...');
                    }
                } finally {
                    if (seq === this.consoleLoadSeq) {
                        loading.hide(this.els.btnReloadTeamConsole);
                    }
                }
                void this.refreshConsoleRemote(seq, rowsBeforeLoad, { force: true, withToast: false });
                return;
            }

            try {
                loading.show(this.els.btnReloadTeamConsole, '刷新中...');
                await this.refreshConsoleRemote(seq, rowsBeforeLoad, { force: false, withToast });
            } finally {
                if (seq === this.consoleLoadSeq) {
                    loading.hide(this.els.btnReloadTeamConsole);
                }
            }
        }

        setStats(total, available) {
            if (this.els.teamStatTotal) this.els.teamStatTotal.textContent = String(total || 0);
            if (this.els.teamStatAvailable) this.els.teamStatAvailable.textContent = String(available || 0);
        }

        recomputeStats() {
            const total = this.rows.length;
            const available = this.rows.filter((row) => {
                if (row.status !== 'active') return false;
                if (!row.max_members) return true;
                return row.current_members < row.max_members;
            }).length;
            this.setStats(total, available);
        }

        applyFilters() {
            const status = String(this.els.teamStatusFilter?.value || '').trim().toLowerCase();
            const keyword = String(this.els.teamSearchInput?.value || '').trim().toLowerCase();
            this.filteredRows = this.rows.filter((row) => {
                if (status && row.status !== status) return false;
                if (!keyword) return true;
                return [
                    row.email,
                    row.team_name,
                    row.subscription_plan,
                    row.member_ratio,
                    row.status,
                ].some((x) => String(x || '').toLowerCase().includes(keyword));
            });
            this.renderRows();
            this.applyColumnVisibility();
        }

        renderRows() {
            if (!this.els.teamTableBody) return;
            if (!this.loaded) {
                this.els.teamTableBody.innerHTML = '<tr><td colspan="9" class="empty-tip">首次不自动加载，请点击“刷新”读取 Team 列表</td></tr>';
                return;
            }
            if (!this.filteredRows.length) {
                this.els.teamTableBody.innerHTML = '<tr><td colspan="9" class="empty-tip">暂无 Team 管理账号</td></tr>';
                return;
            }
            this.els.teamTableBody.innerHTML = this.filteredRows.map((row) => `
                <tr>
                    <td><input type="checkbox" data-row-select data-id="${row.id}"></td>
                    <td>${row.id}</td>
                    <td>${esc(row.email)}</td>
                    <td>${esc(row.team_name || '-')}</td>
                    <td class="col-members">${esc(row.member_ratio || '-')}</td>
                    <td class="col-plan">
                        <span class="team-plan-badge ${esc(normalizePlan(row.subscription_plan))}">${esc(planText(row.subscription_plan))}</span>
                    </td>
                    <td class="col-expires">${esc(fmtDate(row.expires_at))}</td>
                    <td><span class="status-pill ${esc(row.status)}">${esc(statusText(row.status))}</span></td>
                    <td>
                        <div class="team-action-group">
                            <button class="team-action-btn" data-action="members" data-id="${row.id}" data-email="${esc(row.email)}" title="成员管理">👥</button>
                            <button class="team-action-btn" data-action="refresh" data-id="${row.id}" title="刷新">↻</button>
                            <button class="team-action-btn danger" data-action="delete" data-id="${row.id}" data-email="${esc(row.email)}" title="删除">🗑</button>
                        </div>
                    </td>
                </tr>
            `).join('');
        }

        setTinyBusy(button, busy) {
            if (!button) return;
            if (busy) {
                if (!button.dataset.originHtml) button.dataset.originHtml = button.innerHTML;
                button.innerHTML = '…';
                button.disabled = true;
                return;
            }
            if (button.dataset.originHtml) {
                button.innerHTML = button.dataset.originHtml;
                delete button.dataset.originHtml;
            }
            button.disabled = false;
        }

        setImportHint(text, isError = false) {
            if (!this.els.teamImportModalHint) return;
            this.els.teamImportModalHint.textContent = text || '-';
            this.els.teamImportModalHint.style.color = isError ? 'var(--danger-color)' : '';
        }

        setImportMode(mode) {
            const next = mode === 'batch' ? 'batch' : 'single';
            this.teamImportMode = next;
            this.els.btnTeamImportSingleTab?.classList.toggle('active', next === 'single');
            this.els.btnTeamImportBatchTab?.classList.toggle('active', next === 'batch');
            this.els.teamImportSinglePanel?.classList.toggle('active', next === 'single');
            this.els.teamImportBatchPanel?.classList.toggle('active', next === 'batch');
        }

        openImportModal() {
            this.setImportMode('single');
            this.setImportHint('填写 Team Token 后点击导入。');
            this.els.teamImportModal?.classList.add('show');
        }

        closeImportModal() {
            this.els.teamImportModal?.classList.remove('show');
        }

        buildImportItemFromRaw(rawItem) {
            const accessToken = String(rawItem.access_token || rawItem.accessToken || '').trim();
            const refreshToken = String(rawItem.refresh_token || rawItem.refreshToken || '').trim();
            const sessionToken = String(rawItem.session_token || rawItem.sessionToken || '').trim();
            const clientIdInput = String(rawItem.client_id || rawItem.clientId || '').trim();
            const emailInput = String(rawItem.email || '').trim().toLowerCase();
            const accountIdInput = String(rawItem.account_id || rawItem.accountId || '').trim();
            const tokenFields = extractTokenFields(accessToken);
            const email = emailInput || tokenFields.email;
            const accountId = accountIdInput || tokenFields.accountId;
            const clientId = clientIdInput || tokenFields.clientId;
            const plan = String(tokenFields.plan || '').toLowerCase();

            if (!accessToken) {
                throw new Error('缺少 access_token');
            }
            if (!email) {
                throw new Error('缺少 email，且无法从 AT 中提取');
            }

            return {
                email,
                password: String(rawItem.password || '').trim() || null,
                email_service: 'manual',
                status: 'active',
                client_id: clientId || null,
                account_id: accountId || null,
                workspace_id: accountId || null,
                access_token: accessToken,
                refresh_token: refreshToken || null,
                session_token: sessionToken || null,
                source: 'team_import',
                role_tag: 'parent',
                account_label: 'mother',
                subscription_type: (plan === 'plus' || plan === 'team') ? plan : 'team',
                metadata: {
                    imported_from: 'team_manage_modal',
                    imported_at: new Date().toISOString(),
                },
            };
        }

        parseBatchImportText(raw) {
            const text = String(raw || '').trim();
            if (!text) throw new Error('请先填写批量导入文本');
            if (text.startsWith('[')) {
                const arr = JSON.parse(text);
                if (!Array.isArray(arr)) throw new Error('JSON 数组格式不正确');
                return arr;
            }

            const rows = text.split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
            const out = [];
            rows.forEach((line, idx) => {
                try {
                    out.push(JSON.parse(line));
                } catch (_e) {
                    throw new Error(`第 ${idx + 1} 行 JSON 解析失败`);
                }
            });
            return out;
        }

        async submitTeamImport() {
            try {
                this.setImportHint('导入中...');
                loading.show(this.els.btnSubmitTeamImport, '导入中...');
                let rawItems = [];

                if (this.teamImportMode === 'single') {
                    rawItems = [{
                        access_token: this.els.teamImportAccessToken?.value,
                        refresh_token: this.els.teamImportRefreshToken?.value,
                        session_token: this.els.teamImportSessionToken?.value,
                        client_id: this.els.teamImportClientId?.value,
                        email: this.els.teamImportEmail?.value,
                        account_id: this.els.teamImportAccountId?.value,
                    }];
                } else {
                    rawItems = this.parseBatchImportText(this.els.teamImportBatchText?.value || '');
                }

                const accounts = rawItems.map((item, idx) => {
                    try {
                        return this.buildImportItemFromRaw(item || {});
                    } catch (e) {
                        throw new Error(`第 ${idx + 1} 条: ${e.message || e}`);
                    }
                });

                const data = await api.post('/accounts/import', {
                    accounts,
                    overwrite: true,
                });
                const msg = `完成：创建 ${data.created || 0}，更新 ${data.updated || 0}，跳过 ${data.skipped || 0}，失败 ${data.failed || 0}`;
                this.setImportHint(msg, false);
                toast.success('导入完成');
                await this.loadConsole(false);
                if (!Number(data.failed || 0)) {
                    this.closeImportModal();
                }
            } catch (error) {
                const msg = safeError(error);
                this.setImportHint(msg, true);
                toast.error(msg);
            } finally {
                loading.hide(this.els.btnSubmitTeamImport);
            }
        }

        async handleTableAction(event) {
            const btn = event.target.closest('button[data-action]');
            if (!btn) return;
            const action = String(btn.dataset.action || '');
            const accountId = Number(btn.dataset.id || 0);
            const email = String(btn.dataset.email || '');
            if (!accountId) return;

            if (action === 'members') {
                await this.openMemberModal(accountId, email);
                return;
            }

            if (action === 'refresh') {
                try {
                    this.setTinyBusy(btn, true);
                    await this.refreshOne(accountId);
                    toast.success('刷新成功');
                } catch (error) {
                    if (error?.name === 'AbortError' && error?.cancelReason === 'request_replaced') return;
                    toast.error(safeError(error));
                } finally {
                    this.setTinyBusy(btn, false);
                }
                return;
            }

            if (action === 'delete') {
                if (!confirm(`确定删除 Team 账号 ${email || accountId} 吗？`)) return;
                try {
                    this.setTinyBusy(btn, true);
                    await api.delete(`/accounts/${accountId}`);
                    this.rows = this.rows.filter((x) => x.id !== accountId);
                    delete this.membersCache[String(accountId)];
                    this.writeJsonCache(TEAM_MEMBERS_CACHE_KEY, this.membersCache);
                    this.persistRowsCache();
                    this.applyFilters();
                    this.recomputeStats();
                    toast.success('删除成功');
                } catch (error) {
                    toast.error(safeError(error));
                } finally {
                    this.setTinyBusy(btn, false);
                }
            }
        }

        async refreshOne(accountId) {
            const data = await api.post(`/auto-team/team-accounts/${accountId}/refresh`, {}, {
                timeoutMs: 15000,
                retry: 0,
                priority: 'high',
                requestKey: `auto-team:refresh-one:${accountId}`,
                cancelPrevious: true,
                silentNetworkError: true,
                silentTimeoutError: true,
            });
            const row = this.normalizeRow(data.row || {});
            const idx = this.rows.findIndex((x) => x.id === accountId);
            if (idx >= 0) this.rows.splice(idx, 1, row);
            else this.rows.unshift(row);
            this.persistRowsCache();
            this.applyFilters();
            this.recomputeStats();
        }
        async openMemberModal(accountId, email) {
            this.memberAccountId = accountId;
            this.memberAccountEmail = email || '-';
            if (this.els.teamMemberModalSub) this.els.teamMemberModalSub.textContent = this.memberAccountEmail;
            if (this.els.teamMemberInviteEmail) this.els.teamMemberInviteEmail.value = '';
            const cacheKey = String(accountId);
            const cached = this.membersCache && typeof this.membersCache === 'object' ? this.membersCache[cacheKey] : null;
            const cachedJoined = Array.isArray(cached?.joined_members)
                ? cached.joined_members
                : (Array.isArray(cached?.joined) ? cached.joined : []);
            const cachedInvited = Array.isArray(cached?.invited_members)
                ? cached.invited_members
                : (Array.isArray(cached?.invited) ? cached.invited : []);
            if (cached) {
                this.renderJoined(cachedJoined);
                this.renderInvited(cachedInvited);
                this.updateRowMemberStats(accountId, cachedJoined.length);
                if (this.els.teamMemberModalHint) {
                    this.els.teamMemberModalHint.textContent = `workspace: ${cached.workspace_id || '-'} | 已加入 ${cachedJoined.length} | 邀请中 ${cachedInvited.length}（缓存）`;
                }
            } else {
                if (this.els.teamJoinedMembersBody) this.els.teamJoinedMembersBody.innerHTML = '<tr><td colspan="4" class="empty-tip">加载中...</td></tr>';
                if (this.els.teamInvitedMembersBody) this.els.teamInvitedMembersBody.innerHTML = '<tr><td colspan="4" class="empty-tip">加载中...</td></tr>';
            }
            this.els.teamMemberModal?.classList.add('show');
            await this.loadMembers(false, { preserveExisting: true });
        }

        closeMemberModal() {
            this.els.teamMemberModal?.classList.remove('show');
            this.memberAccountId = null;
            this.memberAccountEmail = '';
        }

        async loadMembers(withToast, options = {}) {
            const accountId = Number(this.memberAccountId || 0);
            if (!accountId) return;
            const preserveExisting = options.preserveExisting !== false;
            const cacheKey = String(accountId);
            const cached = this.membersCache && typeof this.membersCache === 'object' ? this.membersCache[cacheKey] : null;
            const hasCached = !!cached;
            if (!preserveExisting || !hasCached) {
                if (this.els.teamJoinedMembersBody) this.els.teamJoinedMembersBody.innerHTML = '<tr><td colspan="4" class="empty-tip">加载中...</td></tr>';
                if (this.els.teamInvitedMembersBody) this.els.teamInvitedMembersBody.innerHTML = '<tr><td colspan="4" class="empty-tip">加载中...</td></tr>';
            }
            try {
                loading.show(this.els.btnReloadTeamMembers, '刷新中...');
                const data = await api.get(`/auto-team/team-accounts/${accountId}/members`, {
                    timeoutMs: 15000,
                    retry: 0,
                    priority: 'high',
                    requestKey: `auto-team:members:${accountId}`,
                    cancelPrevious: true,
                    silentNetworkError: true,
                    silentTimeoutError: true,
                });
                const joined = Array.isArray(data.joined_members) ? data.joined_members : [];
                const invited = Array.isArray(data.invited_members) ? data.invited_members : [];
                this.renderJoined(joined);
                this.renderInvited(invited);
                this.membersCache[cacheKey] = {
                    workspace_id: data.workspace_id || '',
                    joined_members: joined,
                    invited_members: invited,
                    updated_at: new Date().toISOString(),
                };
                this.writeJsonCache(TEAM_MEMBERS_CACHE_KEY, this.membersCache);
                this.updateRowMemberStats(accountId, joined.length);
                if (this.els.teamMemberModalHint) {
                    this.els.teamMemberModalHint.textContent = `workspace: ${data.workspace_id || '-'} | 已加入 ${joined.length} | 邀请中 ${invited.length}`;
                }
                if (withToast) toast.success('成员已刷新');
            } catch (error) {
                if (error?.name === 'AbortError' && error?.cancelReason === 'request_replaced') return;
                const msg = safeError(error);
                if (preserveExisting && hasCached) {
                    if (this.els.teamMemberModalHint) this.els.teamMemberModalHint.textContent = `读取失败，已显示缓存: ${msg}`;
                    if (withToast) toast.warning(`读取失败，已显示缓存：${msg}`);
                    return;
                }
                if (this.els.teamJoinedMembersBody) this.els.teamJoinedMembersBody.innerHTML = `<tr><td colspan="4" class="empty-tip">${esc(msg)}</td></tr>`;
                if (this.els.teamInvitedMembersBody) this.els.teamInvitedMembersBody.innerHTML = '<tr><td colspan="4" class="empty-tip">-</td></tr>';
                if (this.els.teamMemberModalHint) this.els.teamMemberModalHint.textContent = `读取失败: ${msg}`;
                if (withToast) toast.error(msg);
            } finally {
                loading.hide(this.els.btnReloadTeamMembers);
            }
        }

        renderJoined(rows) {
            if (!this.els.teamJoinedMembersBody) return;
            if (!rows.length) {
                this.els.teamJoinedMembersBody.innerHTML = '<tr><td colspan="4" class="empty-tip">暂无已加入成员</td></tr>';
                return;
            }
            this.els.teamJoinedMembersBody.innerHTML = rows.map((item) => `
                <tr>
                    <td>${esc(item.email || '-')}</td>
                    <td><span class="member-role-badge">${esc(item.role || 'standard-user')}</span></td>
                    <td>${esc(fmtDate(item.added_at))}</td>
                    <td><button class="btn btn-sm btn-danger" data-member-action="remove" data-user-id="${esc(item.user_id || '')}" data-email="${esc(item.email || '')}">移除</button></td>
                </tr>
            `).join('');
        }

        renderInvited(rows) {
            if (!this.els.teamInvitedMembersBody) return;
            if (!rows.length) {
                this.els.teamInvitedMembersBody.innerHTML = '<tr><td colspan="4" class="empty-tip">暂无邀请中成员</td></tr>';
                return;
            }
            this.els.teamInvitedMembersBody.innerHTML = rows.map((item) => `
                <tr>
                    <td>${esc(item.email || '-')}</td>
                    <td><span class="member-role-badge">${esc(item.role || 'standard-user')}</span></td>
                    <td>${esc(fmtDate(item.added_at))}</td>
                    <td><button class="btn btn-sm btn-secondary" data-member-action="revoke" data-email="${esc(item.email || '')}">撤回</button></td>
                </tr>
            `).join('');
        }

        async inviteMember() {
            const accountId = Number(this.memberAccountId || 0);
            if (!accountId) return;
            const email = String(this.els.teamMemberInviteEmail?.value || '').trim().toLowerCase();
            const emailRe = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
            if (!emailRe.test(email)) {
                toast.warning('请输入正确邮箱');
                return;
            }
            try {
                loading.show(this.els.btnInviteMember, '添加中...');
                const data = await api.post(`/auto-team/team-accounts/${accountId}/members/invite`, { email });
                toast.success(data?.message || '邀请已提交');
                if (this.els.teamMemberInviteEmail) this.els.teamMemberInviteEmail.value = '';
                await this.loadMembers(false);
                await this.refreshOne(accountId);
            } catch (error) {
                toast.error(safeError(error));
            } finally {
                loading.hide(this.els.btnInviteMember);
            }
        }

        async handleMemberTableAction(event, type) {
            const btn = event.target.closest('button[data-member-action]');
            if (!btn) return;
            const action = String(btn.dataset.memberAction || '');
            const accountId = Number(this.memberAccountId || 0);
            if (!accountId) return;

            try {
                this.setTinyBusy(btn, true);
                if (type === 'invited' && action === 'revoke') {
                    const email = String(btn.dataset.email || '').trim().toLowerCase();
                    if (!email) return;
                    await api.post(`/auto-team/team-accounts/${accountId}/members/revoke`, { email });
                    toast.success('邀请已撤回');
                } else if (type === 'joined' && action === 'remove') {
                    const userId = String(btn.dataset.userId || '').trim();
                    if (!userId) return;
                    await api.post(`/auto-team/team-accounts/${accountId}/members/remove`, { user_id: userId });
                    toast.success('成员已移除');
                } else {
                    return;
                }
                await this.loadMembers(false);
                await this.refreshOne(accountId);
            } catch (error) {
                toast.error(safeError(error));
            } finally {
                this.setTinyBusy(btn, false);
            }
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        window.teamManageConsole = new TeamManageConsole();
    });
})();
