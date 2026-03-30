/**
 * 支付页面 JavaScript
 * 支付页面：半自动 + 全自动绑卡任务管理 + 用户完成后自动验订阅
 */

const COUNTRY_CURRENCY_MAP = {
    US: "USD",
    GB: "GBP",
    CA: "CAD",
    AU: "AUD",
    SG: "SGD",
    HK: "HKD",
    JP: "JPY",
    TR: "TRY",
    IN: "INR",
    BR: "BRL",
    MX: "MXN",
    DE: "EUR",
    FR: "EUR",
    IT: "EUR",
    ES: "EUR",
    EU: "EUR",
};

const BILLING_STORAGE_KEY = "payment.billing_profile_non_sensitive";
const BILLING_TEMPLATE_STORAGE_KEY = "payment.billing_templates_v1";
const THIRD_PARTY_BIND_URL_STORAGE_KEY = "payment.third_party_bind_api_url";
const BIND_MODE_STORAGE_KEY = "payment.bind_mode";
const VENDOR_REDEEM_STORAGE_KEY = "payment.vendor_redeem_code";
const VENDOR_CHECKOUT_STORAGE_KEY = "payment.vendor_checkout_url";
const EFUN_BASE_URL_STORAGE_KEY = "payment.efun_base_url";
const EFUN_API_KEY_STORAGE_KEY = "payment.efun_api_key";
const EFUN_CODE_STORAGE_KEY = "payment.efun_code";
const EFUN_MINUTES_STORAGE_KEY = "payment.efun_minutes";
const CARD_POOL_REDEEM_STORAGE_KEY = "card_pool.redeem_codes.v1";
const CARD_POOL_EFUN_SUPPLIER_KEY = "efun";
const CARD_POOL_EFUN_SUPPLIER_LABEL = "EFun";
const ALLOWED_BIND_MODES = new Set(["semi_auto", "local_auto", "vendor_efun"]);
const THIRD_PARTY_BIND_DEFAULT_URL = "https://twilight-river-f148.482091502.workers.dev/";
const EFUN_BASE_URL_DEFAULT = "https://card.aimizy.com/api/v1/bindcard";
const VENDOR_REDEEM_CODE_REGEX = /^[A-Z0-9]{4}(?:-[A-Z0-9]{4}){3}$/;
const EFUN_CODE_REGEX = /^UK(?:-[A-Z0-9]{5}){5}$/i;
const BILLING_TEMPLATE_MAX = 200;
const BILLING_COUNTRY_CURRENCY_MAP = {
    US: "USD",
    GB: "GBP",
    CA: "CAD",
    AU: "AUD",
    SG: "SGD",
    HK: "HKD",
    JP: "JPY",
    DE: "EUR",
    FR: "EUR",
    IT: "EUR",
    ES: "EUR",
};

const COUNTRY_ALIAS_MAP = {
    us: { code: "US", currency: "USD" },
    usa: { code: "US", currency: "USD" },
    "united states": { code: "US", currency: "USD" },
    美国: { code: "US", currency: "USD" },
    uk: { code: "GB", currency: "GBP" },
    gb: { code: "GB", currency: "GBP" },
    england: { code: "GB", currency: "GBP" },
    "united kingdom": { code: "GB", currency: "GBP" },
    英国: { code: "GB", currency: "GBP" },
    ca: { code: "CA", currency: "CAD" },
    canada: { code: "CA", currency: "CAD" },
    加拿大: { code: "CA", currency: "CAD" },
    au: { code: "AU", currency: "AUD" },
    australia: { code: "AU", currency: "AUD" },
    澳大利亚: { code: "AU", currency: "AUD" },
    sg: { code: "SG", currency: "SGD" },
    singapore: { code: "SG", currency: "SGD" },
    新加坡: { code: "SG", currency: "SGD" },
    hk: { code: "HK", currency: "HKD" },
    "hong kong": { code: "HK", currency: "HKD" },
    香港: { code: "HK", currency: "HKD" },
    jp: { code: "JP", currency: "JPY" },
    japan: { code: "JP", currency: "JPY" },
    日本: { code: "JP", currency: "JPY" },
    de: { code: "DE", currency: "EUR" },
    germany: { code: "DE", currency: "EUR" },
    德国: { code: "DE", currency: "EUR" },
    fr: { code: "FR", currency: "EUR" },
    france: { code: "FR", currency: "EUR" },
    法国: { code: "FR", currency: "EUR" },
    it: { code: "IT", currency: "EUR" },
    italy: { code: "IT", currency: "EUR" },
    意大利: { code: "IT", currency: "EUR" },
    es: { code: "ES", currency: "EUR" },
    spain: { code: "ES", currency: "EUR" },
    西班牙: { code: "ES", currency: "EUR" },
};

let selectedPlan = "plus";
let generatedLink = "";
let isGeneratingCheckoutLink = false;
let paymentAccounts = [];
let isLoadingBindTaskList = false;
const vendorProgressState = {
    taskId: 0,
    timer: null,
    cursor: 0,
    elapsedSeconds: 0,
    forceStopAfterSeconds: 120,
    lastConfig: null,
};

const bindTaskState = {
    page: 1,
    pageSize: 50,
    status: "",
    search: "",
};
let bindTaskAutoRefreshTimer = null;
const bindTaskActionRunning = new Set();
let bindTaskLoadPromise = null;

let billingBatchProfiles = [];

function formatErrorMessage(error) {
    if (!error) return "未知错误";
    if (typeof error === "string") return error;

    // ApiClient 会把后端错误挂在 error.data 上
    const detail = error?.data?.detail ?? error?.message;
    if (typeof detail === "string" && detail && detail !== "[object Object]") {
        return detail;
    }
    try {
        return JSON.stringify(detail || error);
    } catch {
        return String(detail || error);
    }
}

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

async function watchPaymentOpTask(taskId, onUpdate, maxWaitMs = 20 * 60 * 1000) {
    const startedAt = Date.now();
    while (Date.now() - startedAt < maxWaitMs) {
        const task = await api.get(`/payment/ops/tasks/${taskId}`, {
            timeoutMs: 30000,
            retry: 0,
            requestKey: `payment:op-task:${taskId}`,
            cancelPrevious: true,
        });

        if (typeof onUpdate === "function") {
            onUpdate(task);
        }

        const status = String(task?.status || "").toLowerCase();
        if (["completed", "failed", "cancelled"].includes(status)) {
            return task;
        }

        await sleep(1200);
    }

    throw new Error("支付任务等待超时，请稍后重试");
}

function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = String(value ?? "");
    return div.innerHTML;
}

function yesNo(value) {
    return value ? "是" : "否";
}

function showSessionDiagnosticPanel(text) {
    const panel = document.getElementById("session-diagnostic-panel");
    const pre = document.getElementById("session-diagnostic-text");
    if (!panel || !pre) return;
    pre.textContent = String(text || "");
    panel.classList.add("show");
}

function clearSessionDiagnosticPanel() {
    const panel = document.getElementById("session-diagnostic-panel");
    const pre = document.getElementById("session-diagnostic-text");
    if (!panel || !pre) return;
    pre.textContent = "";
    panel.classList.remove("show");
}

function formatSessionDiagnosticPayload(payload) {
    if (!payload || typeof payload !== "object") {
        return "诊断结果为空";
    }
    const token = payload.token_state || {};
    const cookie = payload.cookie_state || {};
    const bootstrap = payload.bootstrap_capability || {};
    const probe = payload.probe || null;
    const notes = Array.isArray(payload.notes) ? payload.notes : [];

    const lines = [
        `账号: ${payload.email || "-"} (ID=${payload.account_id || "-"})`,
        `Access Token: ${yesNo(token.has_access_token)} | len=${token.access_token_len || 0} | ${token.access_token_preview || "-"}`,
        `Refresh Token: ${yesNo(token.has_refresh_token)} | len=${token.refresh_token_len || 0}`,
        `Session(DB): ${yesNo(token.has_session_token_db)} | len=${token.session_token_db_len || 0} | ${token.session_token_db_preview || "-"}`,
        `Session(Cookie): ${yesNo(token.has_session_token_cookie)} | len=${token.session_token_cookie_len || 0} | ${token.session_token_cookie_preview || "-"}`,
        `Session(Resolved): len=${token.resolved_session_token_len || 0} | ${token.resolved_session_token_preview || "-"}`,
        `Cookies: ${yesNo(cookie.has_cookies)} | len=${cookie.cookies_len || 0}`,
        `oai-did: ${yesNo(cookie.has_oai_did)} | ${cookie.resolved_oai_did || "-"}`,
        `Session 分片: count=${cookie.session_chunk_count || 0} | [${(cookie.session_chunk_indices || []).join(", ")}]`,
        `自动补会话能力: ${yesNo(bootstrap.can_login_bootstrap)} | has_password=${yesNo(bootstrap.has_password)} | email_service=${bootstrap.email_service_type || "-"}`,
    ];

    if (probe) {
        lines.push(
            `实时探测: ok=${yesNo(probe.ok)} | http=${probe.http_status ?? "-"} | session=${yesNo(probe.session_token_found)} | session_json_access=${yesNo(probe.access_token_in_session_json)}`
        );
        if (probe.session_token_preview) {
            lines.push(`探测 session 预览: ${probe.session_token_preview}`);
        }
        if (probe.access_token_preview) {
            lines.push(`探测 access 预览: ${probe.access_token_preview}`);
        }
        if (probe.error) {
            lines.push(`探测错误: ${probe.error}`);
        }
    }

    if (notes.length) {
        lines.push("诊断备注:");
        notes.forEach((n) => lines.push(`- ${n}`));
    }
    if (payload.recommendation) {
        lines.push(`建议: ${payload.recommendation}`);
    }
    if (payload.checked_at) {
        lines.push(`检查时间: ${payload.checked_at}`);
    }
    return lines.join("\n");
}

async function runSessionDiagnostic() {
    const accountId = Number(document.getElementById("account-select")?.value || 0);
    if (!accountId) {
        toast.warning("请先选择账号");
        return;
    }
    setButtonLoading("session-diagnostic-btn", "诊断中...", true);
    showSessionDiagnosticPanel("正在诊断会话上下文，请稍候...");
    try {
        const data = await api.get(`/payment/accounts/${accountId}/session-diagnostic?probe=1`);
        const diag = data?.diagnostic || {};
        showSessionDiagnosticPanel(formatSessionDiagnosticPayload(diag));
        toast.success("会话诊断完成");
    } catch (error) {
        const message = formatErrorMessage(error);
        showSessionDiagnosticPanel(`会话诊断失败: ${message}`);
        toast.error(`会话诊断失败: ${message}`);
    } finally {
        setButtonLoading("session-diagnostic-btn", "诊断中...", false);
    }
}

async function runSessionBootstrap() {
    const accountId = Number(document.getElementById("account-select")?.value || 0);
    if (!accountId) {
        toast.warning("请先选择账号");
        return;
    }
    setButtonLoading("session-bootstrap-btn", "补全中...", true);
    showSessionDiagnosticPanel("正在执行会话补全，请稍候（可能需要等待邮箱验证码）...");
    try {
        const data = await api.post(`/payment/accounts/${accountId}/session-bootstrap`, {});
        if (data?.success) {
            toast.success(`会话补全成功（len=${data?.session_token_len || 0}）`);
        } else {
            toast.warning(data?.message || "会话补全未命中");
        }
        await runSessionDiagnostic();
    } catch (error) {
        const message = formatErrorMessage(error);
        showSessionDiagnosticPanel(`会话补全失败: ${message}`);
        toast.error(`会话补全失败: ${message}`);
    } finally {
        setButtonLoading("session-bootstrap-btn", "补全中...", false);
    }
}

async function saveManualSessionToken() {
    const accountId = Number(document.getElementById("account-select")?.value || 0);
    if (!accountId) {
        toast.warning("请先选择账号");
        return;
    }
    const sessionToken = String(document.getElementById("manual-session-token-input")?.value || "").trim();
    if (!sessionToken) {
        toast.warning("请先粘贴 session_token");
        return;
    }

    setButtonLoading("save-session-token-btn", "保存中...", true);
    try {
        const data = await api.post(`/payment/accounts/${accountId}/session-token`, {
            session_token: sessionToken,
            merge_cookie: true,
        });
        if (data?.success) {
            toast.success(`Session Token 已保存（len=${data?.session_token_len || 0}）`);
            await runSessionDiagnostic();
            return;
        }
        toast.warning(data?.message || "保存完成，但未返回 success");
    } catch (error) {
        toast.error(`保存 Session Token 失败: ${formatErrorMessage(error)}`);
    } finally {
        setButtonLoading("save-session-token-btn", "保存中...", false);
    }
}

function getInputValue(id) {
    return (document.getElementById(id)?.value || "").trim();
}

function setInputValue(id, value) {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = value ?? "";
}

function maskCardNumber(cardNumber) {
    const digits = String(cardNumber || "").replace(/\D/g, "");
    if (!digits) return "-";
    if (digits.length <= 8) return `${digits.slice(0, 2)}****${digits.slice(-2)}`;
    return `${digits.slice(0, 4)}****${digits.slice(-4)}`;
}

function resolveCountryAlias(raw) {
    const key = String(raw || "").trim();
    if (!key) return null;
    const normalized = key.toLowerCase();
    if (COUNTRY_ALIAS_MAP[normalized]) {
        return COUNTRY_ALIAS_MAP[normalized];
    }
    const upper = key.toUpperCase();
    if (BILLING_COUNTRY_CURRENCY_MAP[upper]) {
        return {
            code: upper,
            currency: BILLING_COUNTRY_CURRENCY_MAP[upper],
        };
    }
    return null;
}

function normalizeMonth(value) {
    const digits = String(value || "").replace(/\D/g, "");
    if (!digits) return "";
    return digits.slice(0, 2).padStart(2, "0");
}

function normalizeYear(value) {
    const digits = String(value || "").replace(/\D/g, "");
    if (!digits) return "";
    if (digits.length === 2) return `20${digits}`;
    return digits.slice(0, 4);
}

function formatExpiryInput(month, year) {
    const mm = normalizeMonth(month);
    const rawYear = String(year || "").replace(/\D/g, "");
    const yy = rawYear ? rawYear.slice(-2) : "";
    if (!mm && !yy) return "";
    return yy ? `${mm}/${yy}` : mm;
}

function parseExpiryInput(value) {
    const raw = String(value || "").trim();
    if (!raw) {
        return { exp_month: "", exp_year: "" };
    }

    const slashMatch = raw.match(/^(\d{1,2})\s*\/\s*(\d{1,4})$/);
    if (slashMatch) {
        return {
            exp_month: normalizeMonth(slashMatch[1]),
            exp_year: normalizeYear(slashMatch[2]),
        };
    }

    const digits = raw.replace(/\D/g, "");
    if (!digits) {
        return { exp_month: "", exp_year: "" };
    }
    if (digits.length <= 2) {
        return { exp_month: normalizeMonth(digits), exp_year: "" };
    }
    return {
        exp_month: normalizeMonth(digits.slice(0, 2)),
        exp_year: normalizeYear(digits.slice(2)),
    };
}

function normalizeExpiryInputForTyping(value) {
    const digits = String(value || "").replace(/\D/g, "").slice(0, 6);
    if (!digits) return "";
    if (digits.length <= 2) return digits;
    return `${digits.slice(0, 2)}/${digits.slice(2)}`;
}

function parseCardText(rawText) {
    const text = String(rawText || "").trim();
    if (!text) return {};

    const lines = text
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean);
    const kv = {};
    for (const line of lines) {
        const match = line.match(/^(.+?)\s*[:：]\s*(.+)$/);
        if (match) {
            kv[match[1].trim().toLowerCase()] = match[2].trim();
        }
    }

    const result = {};

    // 卡号
    const cardKeyCandidates = [
        "卡号",
        "card number",
        "card",
        "card_number",
    ];
    for (const key of cardKeyCandidates) {
        if (!kv[key]) continue;
        const digits = kv[key].replace(/\D/g, "");
        if (digits.length >= 13 && digits.length <= 19) {
            result.card_number = digits;
            break;
        }
    }

    if (!result.card_number) {
        for (const line of lines) {
            const m = line.replace(/-/g, " ").match(/^(\d{13,19})\s+(0[1-9]|1[0-2])\s+(\d{2,4})\s+(\d{3,4})$/);
            if (m) {
                result.card_number = m[1];
                result.exp_month = normalizeMonth(m[2]);
                result.exp_year = normalizeYear(m[3]);
                result.cvv = m[4];
                break;
            }
        }
    }

    if (!result.card_number) {
        for (const line of lines) {
            const digits = line.replace(/\D/g, "");
            if (digits.length >= 13 && digits.length <= 19) {
                result.card_number = digits;
                break;
            }
        }
    }

    // 有效期
    const expiryKeyCandidates = ["有效期", "exp", "expiry", "expiration", "exp_date"];
    for (const key of expiryKeyCandidates) {
        if (!kv[key]) continue;
        const value = kv[key];
        let m = value.match(/(0[1-9]|1[0-2])\s*\/\s*(\d{2,4})/);
        if (m) {
            result.exp_month = normalizeMonth(m[1]);
            result.exp_year = normalizeYear(m[2]);
            break;
        }
        m = value.match(/^(0[1-9]|1[0-2])(\d{2,4})$/);
        if (m) {
            result.exp_month = normalizeMonth(m[1]);
            result.exp_year = normalizeYear(m[2]);
            break;
        }
    }
    if (!result.exp_month || !result.exp_year) {
        for (const line of lines) {
            const m = line.match(/\b(0[1-9]|1[0-2])\s*\/\s*(\d{2,4})\b/);
            if (m) {
                result.exp_month = normalizeMonth(m[1]);
                result.exp_year = normalizeYear(m[2]);
                break;
            }
        }
    }

    // CVV
    const cvvKeyCandidates = ["cvv", "cvc", "安全码"];
    for (const key of cvvKeyCandidates) {
        if (!kv[key]) continue;
        const m = kv[key].match(/\b(\d{3,4})\b/);
        if (m) {
            result.cvv = m[1];
            break;
        }
    }
    if (!result.cvv) {
        for (let i = 0; i < lines.length; i += 1) {
            const line = lines[i];
            if (!/(cvv|cvc|安全码)/i.test(line)) continue;
            const direct = line.match(/\b(\d{3,4})\b/);
            if (direct) {
                result.cvv = direct[1];
                break;
            }
            const next = lines[i + 1] || "";
            const m = next.match(/\b(\d{3,4})\b/);
            if (m) {
                result.cvv = m[1];
                break;
            }
        }
    }

    // 姓名
    const nameKeyCandidates = ["姓名", "name", "cardholder", "持卡人"];
    for (const key of nameKeyCandidates) {
        if (kv[key]) {
            result.billing_name = kv[key];
            break;
        }
    }
    if (!result.billing_name) {
        for (const line of lines) {
            if (/^[A-Z][a-z]+(\s+[A-Z][a-z]+){0,4}$/.test(line)) {
                result.billing_name = line;
                break;
            }
        }
    }

    // 地址字段
    const addressLine = kv["地址"] || kv["address"] || kv["address_line1"] || "";
    const city = kv["城市"] || kv["city"] || "";
    const state = kv["州"] || kv["state"] || kv["省"] || "";
    const postal = kv["邮编"] || kv["postal_code"] || kv["zip"] || kv["zipcode"] || kv["zip_code"] || "";
    const countryRaw = kv["国家"] || kv["country"] || kv["地区"] || "";

    if (addressLine) result.address_line1 = addressLine;
    if (city) result.address_city = city;
    if (state) result.address_state = state;
    if (postal) result.postal_code = postal;

    if (countryRaw) {
        const country = resolveCountryAlias(countryRaw);
        if (country) {
            result.country_code = country.code;
            result.currency = country.currency;
        }
    }

    // 账单地址单行模式
    if (!result.address_line1) {
        let addressCandidate = "";
        for (const line of lines) {
            if (/(账单地址|billing\s*address)/i.test(line)) {
                addressCandidate = line.replace(/^.*?(账单地址|billing\s*address)\s*[:：]?\s*/i, "").trim();
                if (addressCandidate) break;
            }
        }
        if (!addressCandidate) {
            addressCandidate = lines.find((line) => line.includes(",") && /\d/.test(line)) || "";
        }
        if (addressCandidate) {
            result.raw_address = addressCandidate;
            const parts = addressCandidate.split(",").map((item) => item.trim()).filter(Boolean);
            if (parts.length) {
                result.address_line1 = parts[0];
            }
            if (!result.postal_code) {
                const zip = addressCandidate.match(/\b(\d{5}(?:-\d{4})?)\b/);
                if (zip) result.postal_code = zip[1];
            }
            if (!result.address_state) {
                const stateCode = addressCandidate.match(/\b([A-Z]{2})\b/);
                if (stateCode) result.address_state = stateCode[1];
            }
            const suffix = parts[parts.length - 1];
            if (!result.country_code && suffix) {
                const country = resolveCountryAlias(suffix);
                if (country) {
                    result.country_code = country.code;
                    result.currency = country.currency;
                }
            }
        }
    }

    return result;
}

function buildParsedSummary(parsed) {
    const parts = [];
    if (parsed.card_number) parts.push(`卡号: ${maskCardNumber(parsed.card_number)}`);
    if (parsed.exp_month || parsed.exp_year) {
        parts.push(`有效期: ${formatExpiryInput(parsed.exp_month, parsed.exp_year) || "--/--"}`);
    }
    if (parsed.cvv) parts.push("CVV: ***");
    if (parsed.billing_name) parts.push(`姓名: ${parsed.billing_name}`);
    if (parsed.address_line1 || parsed.raw_address) {
        parts.push(`地址: ${parsed.raw_address || parsed.address_line1}`);
    }
    if (parsed.country_code) parts.push(`国家: ${parsed.country_code}`);
    return parts;
}

function fillBillingForm(parsed) {
    if (!parsed || typeof parsed !== "object") return;

    if (parsed.card_number) setInputValue("card-number-input", parsed.card_number);
    if (parsed.exp_month || parsed.exp_year) {
        setInputValue("card-expiry-input", formatExpiryInput(parsed.exp_month, parsed.exp_year));
    }
    if (parsed.cvc || parsed.cvv) setInputValue("card-cvc-input", String(parsed.cvc || parsed.cvv || ""));
    if (parsed.billing_name) setInputValue("billing-name-input", parsed.billing_name);
    if (parsed.address_line1) setInputValue("billing-line1-input", parsed.address_line1);
    if (parsed.address_city) setInputValue("billing-city-input", parsed.address_city);
    if (parsed.address_state) setInputValue("billing-state-input", parsed.address_state);
    if (parsed.postal_code) setInputValue("billing-postal-input", parsed.postal_code);

    if (parsed.country_code) {
        const countryEl = document.getElementById("billing-country-input");
        if (countryEl) countryEl.value = parsed.country_code;
    }

    if (parsed.currency) {
        setInputValue("billing-currency-input", parsed.currency);
    } else {
        onBillingCountryChanged();
    }

    persistBillingProfileNonSensitive();
}

function onBillingCountryChanged() {
    const country = (document.getElementById("billing-country-input")?.value || "US").toUpperCase();
    const currencyEl = document.getElementById("billing-currency-input");
    if (!currencyEl) return;
    if (!currencyEl.value || currencyEl.value === "USD" || currencyEl.value.length < 3) {
        currencyEl.value = BILLING_COUNTRY_CURRENCY_MAP[country] || "USD";
    }
}

function setRandomBillingHint(message, mode = "info") {
    const hintEl = document.getElementById("random-billing-hint");
    if (!hintEl) return;
    hintEl.textContent = String(message || "");
    if (mode === "success") {
        hintEl.style.color = "var(--success-color)";
        return;
    }
    if (mode === "error") {
        hintEl.style.color = "var(--danger-color)";
        return;
    }
    hintEl.style.color = "var(--text-secondary)";
}

async function randomBillingByCountry() {
    const country = String(getInputValue("billing-country-input") || "US").toUpperCase();
    setButtonLoading("random-billing-btn", "生成中...", true);
    setRandomBillingHint("正在获取随机账单资料...", "info");
    try {
        const data = await api.get(`/payment/random-billing?country=${encodeURIComponent(country)}`);
        const profile = data?.profile || {};
        if (!profile || typeof profile !== "object") {
            throw new Error("返回的账单资料格式无效");
        }

        fillBillingForm({
            billing_name: profile.billing_name || "",
            country_code: profile.country_code || country,
            currency: profile.currency || "",
            address_line1: profile.address_line1 || "",
            address_city: profile.address_city || "",
            address_state: profile.address_state || "",
            postal_code: profile.postal_code || "",
        });

        const source = String(profile.source || "unknown");
        let sourceLabel = "本地兜底";
        if (source === "meiguodizhi") sourceLabel = "meiguodizhi";
        if (source === "local_geo") sourceLabel = "本地生成";
        if (source === "local_geo_fallback") sourceLabel = "本地兜底";
        const fallbackReason = String(profile.fallback_reason || "").trim();
        const city = String(profile.address_city || "-");
        const state = String(profile.address_state || "-");
        const postal = String(profile.postal_code || "-");
        if ((source === "local_fallback" || source === "local_geo_fallback") && fallbackReason) {
            setRandomBillingHint(`来源: ${sourceLabel} | ${city}, ${state}, ${postal} | 外部失败: ${fallbackReason}`, "error");
            toast.warning(`外部地址源不可用，已切换本地兜底：${fallbackReason}`);
        } else {
            setRandomBillingHint(`来源: ${sourceLabel} | ${city}, ${state}, ${postal}`, "success");
            toast.success(`已按 ${country} 随机填充账单资料（${sourceLabel}）`);
        }
    } catch (error) {
        setRandomBillingHint(`随机失败: ${formatErrorMessage(error)}`, "error");
        toast.error(`随机账单资料失败: ${formatErrorMessage(error)}`);
    } finally {
        setButtonLoading("random-billing-btn", "生成中...", false);
    }
}

function collectBillingFormData() {
    const expiry = parseExpiryInput(getInputValue("card-expiry-input"));
    return {
        card_number: getInputValue("card-number-input").replace(/\D/g, ""),
        exp_month: expiry.exp_month,
        exp_year: expiry.exp_year,
        cvc: getInputValue("card-cvc-input").replace(/\D/g, ""),
        billing_name: getInputValue("billing-name-input"),
        country_code: getInputValue("billing-country-input").toUpperCase(),
        currency: getInputValue("billing-currency-input").toUpperCase(),
        address_line1: getInputValue("billing-line1-input"),
        address_city: getInputValue("billing-city-input"),
        address_state: getInputValue("billing-state-input"),
        postal_code: getInputValue("billing-postal-input"),
    };
}

function persistBillingProfileNonSensitive() {
    const data = collectBillingFormData();
    storage.set(BILLING_STORAGE_KEY, {
        billing_name: data.billing_name,
        country_code: data.country_code,
        currency: data.currency,
        address_line1: data.address_line1,
        address_city: data.address_city,
        address_state: data.address_state,
        postal_code: data.postal_code,
    });
}

function restoreBillingProfileNonSensitive() {
    const saved = storage.get(BILLING_STORAGE_KEY, null);
    if (!saved || typeof saved !== "object") {
        setInputValue("billing-country-input", "US");
        setInputValue("billing-currency-input", "USD");
        return;
    }
    if (saved.billing_name) setInputValue("billing-name-input", saved.billing_name);
    if (saved.country_code) setInputValue("billing-country-input", saved.country_code);
    if (saved.currency) setInputValue("billing-currency-input", saved.currency);
    if (saved.address_line1) setInputValue("billing-line1-input", saved.address_line1);
    if (saved.address_city) setInputValue("billing-city-input", saved.address_city);
    if (saved.address_state) setInputValue("billing-state-input", saved.address_state);
    if (saved.postal_code) setInputValue("billing-postal-input", saved.postal_code);
    onBillingCountryChanged();
}

function getBillingTemplates() {
    const raw = storage.get(BILLING_TEMPLATE_STORAGE_KEY, []);
    if (!Array.isArray(raw)) return [];
    return raw
        .filter((item) => item && typeof item === "object" && item.id && item.name && item.data)
        .slice(0, BILLING_TEMPLATE_MAX);
}

function saveBillingTemplates(list) {
    const safeList = Array.isArray(list) ? list.slice(0, BILLING_TEMPLATE_MAX) : [];
    storage.set(BILLING_TEMPLATE_STORAGE_KEY, safeList);
}

function normalizeTemplateData(data) {
    const source = data && typeof data === "object" ? data : {};
    return {
        card_number: String(source.card_number || "").replace(/\D/g, ""),
        exp_month: normalizeMonth(source.exp_month),
        exp_year: normalizeYear(source.exp_year),
        cvc: String(source.cvc || source.cvv || "").replace(/\D/g, "").slice(0, 4),
        billing_name: String(source.billing_name || "").trim(),
        country_code: String(source.country_code || "US").trim().toUpperCase() || "US",
        currency: String(source.currency || "").trim().toUpperCase(),
        address_line1: String(source.address_line1 || "").trim(),
        address_city: String(source.address_city || "").trim(),
        address_state: String(source.address_state || "").trim(),
        postal_code: String(source.postal_code || "").trim(),
    };
}

function refreshBillingTemplateSelect(selectedId = "") {
    const selectEl = document.getElementById("billing-template-select");
    if (!selectEl) return;
    const templates = getBillingTemplates();

    const options = ['<option value="">-- 选择已保存模板 --</option>'];
    templates.forEach((tpl) => {
        options.push(`<option value="${escapeHtml(tpl.id)}">${escapeHtml(tpl.name)}</option>`);
    });
    selectEl.innerHTML = options.join("");

    if (selectedId && templates.some((tpl) => tpl.id === selectedId)) {
        selectEl.value = selectedId;
    }
}

function saveCurrentAsTemplate() {
    const name = getInputValue("billing-template-name");
    if (!name) {
        toast.warning("请先填写模板名称");
        return;
    }

    const form = normalizeTemplateData(collectBillingFormData());
    const hasValue = Boolean(
        form.card_number ||
        (form.exp_month && form.exp_year) ||
        form.cvc ||
        form.billing_name ||
        form.address_line1
    );
    if (!hasValue) {
        toast.warning("当前表单为空，无法保存模板");
        return;
    }

    const templates = getBillingTemplates();
    const normalizedName = name.toLowerCase();
    const existing = templates.find((tpl) => String(tpl.name || "").toLowerCase() === normalizedName);
    const nowIso = new Date().toISOString();
    if (existing) {
        existing.data = form;
        existing.updated_at = nowIso;
        saveBillingTemplates(templates);
        refreshBillingTemplateSelect(existing.id);
        toast.success(`模板已更新: ${name}`);
        return;
    }

    const newTemplate = {
        id: `tpl_${Date.now()}_${Math.random().toString(16).slice(2, 8)}`,
        name,
        data: form,
        created_at: nowIso,
        updated_at: nowIso,
    };
    templates.unshift(newTemplate);
    saveBillingTemplates(templates);
    refreshBillingTemplateSelect(newTemplate.id);
    toast.success(`模板已保存: ${name}`);
}

function applySelectedTemplate() {
    const selectEl = document.getElementById("billing-template-select");
    if (!selectEl || !selectEl.value) {
        toast.warning("请先选择模板");
        return;
    }
    const templates = getBillingTemplates();
    const selected = templates.find((tpl) => tpl.id === selectEl.value);
    if (!selected) {
        toast.warning("模板不存在或已删除");
        refreshBillingTemplateSelect();
        return;
    }
    fillBillingForm(selected.data || {});
    setInputValue("billing-template-name", selected.name || "");
    toast.success(`已应用模板: ${selected.name}`);
}

async function deleteSelectedTemplate() {
    const selectEl = document.getElementById("billing-template-select");
    if (!selectEl || !selectEl.value) {
        toast.warning("请先选择模板");
        return;
    }

    const templates = getBillingTemplates();
    const selected = templates.find((tpl) => tpl.id === selectEl.value);
    if (!selected) {
        toast.warning("模板不存在或已删除");
        refreshBillingTemplateSelect();
        return;
    }

    const ok = await confirm(`确认删除模板「${selected.name}」吗？`, "删除模板");
    if (!ok) return;

    const next = templates.filter((tpl) => tpl.id !== selected.id);
    saveBillingTemplates(next);
    refreshBillingTemplateSelect();
    setInputValue("billing-template-name", "");
    toast.success(`模板已删除: ${selected.name}`);
}

function saveBatchProfilesAsTemplates() {
    if (!billingBatchProfiles.length) {
        toast.warning("请先批量识别文本");
        return;
    }

    const templates = getBillingTemplates();
    const now = new Date();
    let saved = 0;

    billingBatchProfiles.forEach((item, idx) => {
        const parsed = item?.parsed || {};
        const data = normalizeTemplateData({
            card_number: parsed.card_number,
            exp_month: parsed.exp_month,
            exp_year: parsed.exp_year,
            cvc: parsed.cvv,
            billing_name: parsed.billing_name,
            country_code: parsed.country_code,
            currency: parsed.currency,
            address_line1: parsed.address_line1,
            address_city: parsed.address_city,
            address_state: parsed.address_state,
            postal_code: parsed.postal_code,
        });
        const hasValue = Boolean(
            data.card_number ||
            (data.exp_month && data.exp_year) ||
            data.cvc ||
            data.billing_name ||
            data.address_line1
        );
        if (!hasValue) return;

        const suffix = data.card_number ? data.card_number.slice(-4) : String(idx + 1).padStart(2, "0");
        const name = `批量模板-${now.toISOString().slice(0, 10)}-${suffix}`;
        templates.unshift({
            id: `tpl_${Date.now()}_${Math.random().toString(16).slice(2, 8)}_${idx}`,
            name,
            data,
            created_at: now.toISOString(),
            updated_at: now.toISOString(),
        });
        saved += 1;
    });

    if (!saved) {
        toast.warning("批量记录里没有可保存的模板");
        return;
    }

    saveBillingTemplates(templates);
    refreshBillingTemplateSelect();
    toast.success(`已保存 ${saved} 个模板`);
}

function setParseResult(message, type = "info") {
    const resultEl = document.getElementById("billing-parse-result");
    if (!resultEl) return;
    resultEl.textContent = message || "";
    if (type === "error") {
        resultEl.style.color = "var(--danger-color)";
    } else if (type === "success") {
        resultEl.style.color = "var(--success-color)";
    } else {
        resultEl.style.color = "var(--text-secondary)";
    }
}

function parseSingleBillingText() {
    const text = getInputValue("billing-paste-text");
    if (!text) {
        setParseResult("请先粘贴文本", "error");
        return;
    }

    const parsed = parseCardText(text);
    const summary = buildParsedSummary(parsed);
    if (!summary.length) {
        setParseResult("未识别到可用信息，请检查文本格式", "error");
        return;
    }

    fillBillingForm(parsed);
    setParseResult(`识别成功: ${summary.join(" | ")}`, "success");
}

function splitBatchBlocks(rawText) {
    const blocks = String(rawText || "")
        .split(/\n\s*\n+/)
        .map((part) => part.trim())
        .filter(Boolean);
    if (blocks.length > 1) return blocks;
    return String(rawText || "")
        .split(/(?:^-{3,}|^={3,})/m)
        .map((part) => part.trim())
        .filter(Boolean);
}

function renderBatchProfiles() {
    const wrap = document.getElementById("billing-batch-wrap");
    const summary = document.getElementById("billing-batch-summary");
    const tbody = document.getElementById("billing-batch-table");
    if (!wrap || !summary || !tbody) return;

    if (!billingBatchProfiles.length) {
        wrap.style.display = "none";
        tbody.innerHTML = "";
        summary.textContent = "";
        return;
    }

    wrap.style.display = "";
    summary.textContent = `已识别 ${billingBatchProfiles.length} 条资料，点击“填充”可写入下方表单。`;
    tbody.innerHTML = billingBatchProfiles.map((item, index) => {
        const parsed = item.parsed || {};
        const address = parsed.raw_address || [
            parsed.address_line1,
            parsed.address_city,
            parsed.address_state,
            parsed.postal_code,
        ].filter(Boolean).join(", ");
        return `
            <tr>
                <td>${index + 1}</td>
                <td class="bind-mask">${escapeHtml(maskCardNumber(parsed.card_number))}</td>
                <td>${escapeHtml(formatExpiryInput(parsed.exp_month, parsed.exp_year) || "-")}</td>
                <td>${escapeHtml(parsed.cvv ? "***" : "-")}</td>
                <td>${escapeHtml(parsed.country_code || "-")}</td>
                <td>${escapeHtml(address || "-")}</td>
                <td><button type="button" class="btn btn-secondary btn-sm" onclick="fillFromBatchProfile(${index})">填充</button></td>
            </tr>
        `;
    }).join("");
}

function parseBatchBillingText() {
    const text = getInputValue("billing-paste-text");
    if (!text) {
        setParseResult("请先粘贴文本", "error");
        return;
    }

    const blocks = splitBatchBlocks(text);
    if (!blocks.length) {
        setParseResult("未检测到可解析的文本块", "error");
        return;
    }

    billingBatchProfiles = blocks
        .map((blockText) => ({ raw: blockText, parsed: parseCardText(blockText) }))
        .filter((item) => {
            const parsed = item.parsed || {};
            return Boolean(
                parsed.card_number ||
                parsed.exp_month ||
                parsed.exp_year ||
                parsed.cvv ||
                parsed.address_line1 ||
                parsed.raw_address
            );
        });

    renderBatchProfiles();
    if (!billingBatchProfiles.length) {
        setParseResult("批量识别完成，但没有可用记录", "error");
        return;
    }

    setParseResult(`批量识别成功：共 ${billingBatchProfiles.length} 条`, "success");
}

function fillFromBatchProfile(index) {
    const item = billingBatchProfiles[index];
    if (!item) return;
    fillBillingForm(item.parsed || {});
    const summary = buildParsedSummary(item.parsed || {});
    setParseResult(`已填充第 ${index + 1} 条：${summary.join(" | ")}`, "success");
}

function clearBillingText() {
    setInputValue("billing-paste-text", "");
    billingBatchProfiles = [];
    renderBatchProfiles();
    setParseResult("", "info");
}

function bindBillingEvents() {
    document.getElementById("parse-billing-btn")?.addEventListener("click", parseSingleBillingText);
    document.getElementById("parse-batch-btn")?.addEventListener("click", parseBatchBillingText);
    document.getElementById("clear-billing-btn")?.addEventListener("click", clearBillingText);
    document.getElementById("save-billing-template-btn")?.addEventListener("click", saveCurrentAsTemplate);
    document.getElementById("apply-billing-template-btn")?.addEventListener("click", applySelectedTemplate);
    document.getElementById("delete-billing-template-btn")?.addEventListener("click", deleteSelectedTemplate);
    document.getElementById("save-batch-template-btn")?.addEventListener("click", saveBatchProfilesAsTemplates);
    document.getElementById("billing-template-select")?.addEventListener("change", (event) => {
        const selectValue = event?.target?.value || "";
        if (!selectValue) {
            setInputValue("billing-template-name", "");
            return;
        }
        const selected = getBillingTemplates().find((tpl) => tpl.id === selectValue);
        if (selected) {
            setInputValue("billing-template-name", selected.name || "");
        }
    });

    document.getElementById("billing-country-input")?.addEventListener("change", () => {
        onBillingCountryChanged();
        persistBillingProfileNonSensitive();
        setRandomBillingHint("");
    });

    [
        "card-number-input",
        "card-expiry-input",
        "card-cvc-input",
        "billing-name-input",
        "billing-country-input",
        "billing-currency-input",
        "billing-line1-input",
        "billing-city-input",
        "billing-state-input",
        "billing-postal-input",
    ].forEach((id) => {
        const node = document.getElementById(id);
        node?.addEventListener("input", debounce(() => {
            persistBillingProfileNonSensitive();
            resetGenerateLinkButtonState();
        }, 200));
        node?.addEventListener("change", resetGenerateLinkButtonState);
    });

    document.getElementById("card-expiry-input")?.addEventListener("input", (event) => {
        const el = event.target;
        if (!el) return;
        const next = normalizeExpiryInputForTyping(el.value);
        if (el.value !== next) {
            el.value = next;
        }
    });

    document.getElementById("card-number-input")?.addEventListener("input", (event) => {
        const el = event.target;
        if (!el) return;
        const digits = String(el.value || "").replace(/\D/g, "").slice(0, 19);
        const grouped = digits.replace(/(\d{4})(?=\d)/g, "$1 ").trim();
        if (el.value !== grouped) {
            el.value = grouped;
        }
    });

    document.getElementById("card-cvc-input")?.addEventListener("input", (event) => {
        const el = event.target;
        if (!el) return;
        const digits = String(el.value || "").replace(/\D/g, "").slice(0, 4);
        if (el.value !== digits) {
            el.value = digits;
        }
    });
}

function getTaskStatusText(status) {
    const mapping = {
        link_ready: "待打开",
        opened: "已打开",
        waiting_user_action: "待用户完成",
        paid_pending_sync: "已支付待同步",
        verifying: "验证中",
        completed: "已完成",
        failed: "失败",
    };
    return mapping[status] || status || "-";
}

function startBindTaskAutoRefresh() {
    stopBindTaskAutoRefresh();
    bindTaskAutoRefreshTimer = setInterval(() => {
        if (document.hidden) return;
        const bindTaskTab = document.getElementById("tab-content-bind-task");
        if (!bindTaskTab?.classList.contains("active")) return;
        loadBindCardTasks(true);
    }, 20000);
}

function stopBindTaskAutoRefresh() {
    if (!bindTaskAutoRefreshTimer) return;
    clearInterval(bindTaskAutoRefreshTimer);
    bindTaskAutoRefreshTimer = null;
}

function setButtonLoading(buttonId, loadingText, isLoading) {
    const btn = document.getElementById(buttonId);
    if (!btn) return;
    if (isLoading) {
        if (!btn.dataset.originalText) {
            btn.dataset.originalText = btn.textContent || "";
        }
        btn.disabled = true;
        btn.textContent = loadingText;
        return;
    }
    btn.disabled = false;
    btn.textContent = btn.dataset.originalText || btn.textContent;
}

function resetGenerateLinkButtonState() {
    const btn = document.getElementById("generate-link-btn");
    if (!btn) return;
    btn.disabled = false;
    if (btn.dataset.originalText) {
        btn.textContent = btn.dataset.originalText;
    }
}

function getBindMode() {
    const modeSelect = document.getElementById("bind-mode-select");
    const mode = String(modeSelect?.value || "semi_auto").trim().toLowerCase() || "semi_auto";
    if (ALLOWED_BIND_MODES.has(mode)) {
        return mode;
    }
    if (modeSelect) {
        modeSelect.value = "semi_auto";
    }
    return "semi_auto";
}

function updateSemiAutoActionsVisibility(mode) {
    const isSemiAuto = (mode || getBindMode()) === "semi_auto";
    const loginBtn = document.getElementById("semi-login-gpt-btn");
    const emailBtn = document.getElementById("semi-account-email-btn");
    if (loginBtn) {
        loginBtn.style.display = isSemiAuto ? "" : "none";
    }
    if (emailBtn) {
        emailBtn.style.display = isSemiAuto ? "" : "none";
    }
}

function getSelectedAccountEmail() {
    const accountId = Number(document.getElementById("account-select")?.value || 0);
    if (!accountId) return "";
    const matched = (paymentAccounts || []).find((acc) => Number(acc?.id || 0) === accountId);
    return String(matched?.email || "").trim();
}

function updateSelectedAccountEmailLabel() {
    const emailBtn = document.getElementById("semi-account-email-btn");
    if (!emailBtn) return;
    const email = getSelectedAccountEmail();
    emailBtn.textContent = "邮箱";
    emailBtn.title = email ? `当前账号: ${email}（点击查询最新验证码）` : "请先选择账号";
}

async function openGptOfficialLogin() {
    const accountId = Number(document.getElementById("account-select")?.value || 0);
    if (!accountId) {
        toast.warning("请先选择账号");
        return;
    }
    const loginUrl = "https://chatgpt.com/auth/login";
    try {
        const data = await api.post("/payment/open-incognito", {
            url: loginUrl,
            account_id: accountId,
        });
        if (data?.success) {
            toast.success("已打开 GPT 官方登录页（无痕）");
            return;
        }
        window.open(loginUrl, "_blank", "noopener,noreferrer");
        toast.warning(data?.message || "未找到可用浏览器，已在当前浏览器打开");
    } catch (error) {
        window.open(loginUrl, "_blank", "noopener,noreferrer");
        toast.warning(`打开无痕失败，已在当前浏览器打开: ${formatErrorMessage(error)}`);
    }
}

async function fetchSelectedAccountInbox() {
    const accountId = Number(document.getElementById("account-select")?.value || 0);
    const email = getSelectedAccountEmail();
    if (!accountId || !email) {
        toast.warning("请先选择账号");
        return;
    }
    toast.info(`正在查询 ${email} 收件箱...`);
    try {
        const result = await api.post(`/accounts/${accountId}/inbox-code`, {});
        if (result?.success && result?.code) {
            const code = String(result.code).trim();
            copyToClipboard(code);
            toast.success(`${email} 最新验证码: ${code}（已复制）`, 8000);
            return;
        }
        toast.error(`查询失败: ${result?.error || "未收到验证码"}`);
    } catch (error) {
        toast.error(`查询失败: ${formatErrorMessage(error)}`);
    }
}

function stopVendorProgressPolling() {
    if (!vendorProgressState.timer) return;
    clearTimeout(vendorProgressState.timer);
    vendorProgressState.timer = null;
}

function setVendorStopButtonState(disabled = false, show = true) {
    const btn = document.getElementById("vendor-stop-btn");
    if (!btn) return;
    btn.style.display = show ? "" : "none";
    btn.disabled = Boolean(disabled);
}

function setVendorRetryButtonState(disabled = false, show = true) {
    const btn = document.getElementById("vendor-retry-btn");
    if (!btn) return;
    btn.style.display = show ? "" : "none";
    btn.disabled = Boolean(disabled);
}

function showVendorProgressPanel(show = true) {
    const panel = document.getElementById("vendor-progress-panel");
    if (!panel) return;
    panel.classList.toggle("show", Boolean(show));
}

function resetVendorProgressPanel() {
    vendorProgressState.cursor = 0;
    vendorProgressState.elapsedSeconds = 0;
    vendorProgressState.forceStopAfterSeconds = 120;
    const bar = document.getElementById("vendor-progress-bar");
    const percent = document.getElementById("vendor-progress-percent");
    const log = document.getElementById("vendor-progress-log");
    const title = document.getElementById("vendor-progress-title");
    const runtime = document.getElementById("vendor-progress-runtime");
    if (bar) bar.style.width = "0%";
    if (percent) percent.textContent = "0%";
    if (title) title.textContent = "卡商订阅进度";
    if (log) log.textContent = "等待执行...";
    if (runtime) runtime.textContent = "已运行 0 秒 / 可随时停止";
    setVendorRetryButtonState(false, false);
}

function appendVendorLogs(logs) {
    if (!Array.isArray(logs) || !logs.length) return;
    const logBox = document.getElementById("vendor-progress-log");
    if (!logBox) return;
    const lines = logs.map((item) => `[${item?.time || "--:--:--"}] ${item?.message || ""}`);
    const existing = String(logBox.textContent || "").trim();
    logBox.textContent = existing ? `${existing}\n${lines.join("\n")}` : lines.join("\n");
    logBox.scrollTop = logBox.scrollHeight;
}

function updateVendorRuntimeHint(progressPayload = {}, status = "running") {
    const runtime = document.getElementById("vendor-progress-runtime");
    if (!runtime) return;
    const elapsed = Math.max(
        0,
        Number(progressPayload?.elapsed_seconds ?? vendorProgressState.elapsedSeconds ?? 0),
    );
    const forceStopAfter = Math.max(
        1,
        Number(progressPayload?.force_stop_after_seconds ?? vendorProgressState.forceStopAfterSeconds ?? 120),
    );
    const canForceStop = Boolean(progressPayload?.can_force_stop) || elapsed >= forceStopAfter;
    vendorProgressState.elapsedSeconds = elapsed;
    vendorProgressState.forceStopAfterSeconds = forceStopAfter;

    const currentStatus = String(status || progressPayload?.status || "running").toLowerCase();
    if (currentStatus === "completed") {
        runtime.textContent = `任务已完成（总耗时 ${elapsed} 秒）`;
        return;
    }
    if (currentStatus === "failed") {
        runtime.textContent = `任务已失败（运行 ${elapsed} 秒）`;
        return;
    }
    if (currentStatus === "cancelled") {
        runtime.textContent = `任务已停止（运行 ${elapsed} 秒）`;
        return;
    }
    if (currentStatus === "pending") {
        runtime.textContent = `已运行 ${elapsed} 秒 / 接口执行已完成，等待订阅同步`;
        return;
    }

    if (canForceStop) {
        runtime.textContent = `已运行 ${elapsed} 秒 / 已可停止`;
        return;
    }

    const left = Math.max(0, forceStopAfter - elapsed);
    runtime.textContent = `已运行 ${elapsed} 秒 / 可随时停止（建议 ${forceStopAfter} 秒后强制，剩余 ${left} 秒）`;
}

function formatBeijingDateTime(dateStr) {
    if (!dateStr) return "-";
    const raw = String(dateStr).trim();
    if (!raw) return "-";
    // 后端多数时间字段是 UTC naive（无时区），这里按 UTC 解析后固定转北京时间显示
    const normalized = /[zZ]$|[+\-]\d{2}:\d{2}$/.test(raw) ? raw : `${raw}Z`;
    const date = new Date(normalized);
    if (Number.isNaN(date.getTime())) {
        return format.date(dateStr);
    }
    return date.toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
    });
}

function updateVendorProgressUi(progressPayload, taskId) {
    const progress = Math.max(0, Math.min(100, Number(progressPayload?.progress || 0)));
    const status = String(progressPayload?.status || "running").toLowerCase();
    const bar = document.getElementById("vendor-progress-bar");
    const percent = document.getElementById("vendor-progress-percent");
    const title = document.getElementById("vendor-progress-title");
    if (bar) bar.style.width = `${progress}%`;
    if (percent) percent.textContent = `${progress}%`;
    if (title) {
        if (status === "completed") {
            title.textContent = `任务 #${taskId} 订阅完成`;
        } else if (status === "failed") {
            title.textContent = `任务 #${taskId} 订阅失败`;
        } else if (status === "cancelled") {
            title.textContent = `任务 #${taskId} 已停止`;
        } else if (status === "pending") {
            title.textContent = `任务 #${taskId} 已提交，等待同步`;
        } else {
            title.textContent = `任务 #${taskId} 执行中`;
        }
    }
    updateVendorRuntimeHint(progressPayload, status);
    if (status === "running") {
        setVendorStopButtonState(false, true);
        setVendorRetryButtonState(false, false);
        return;
    }
    if (status === "failed") {
        setVendorStopButtonState(true, true);
        setVendorRetryButtonState(false, true);
        return;
    }
    if (status === "pending") {
        setVendorStopButtonState(false, true);
        setVendorRetryButtonState(false, true);
        return;
    }
    setVendorStopButtonState(true, true);
    setVendorRetryButtonState(false, false);
}

async function pollVendorProgress(taskId) {
    if (!taskId) return;
    try {
        const payload = await api.get(`/payment/bind-card/tasks/${taskId}/vendor-progress?cursor=${vendorProgressState.cursor}`);
        const progressPayload = payload?.progress || {};
        const logs = progressPayload?.logs || [];
        appendVendorLogs(logs);
        vendorProgressState.cursor = Number(progressPayload?.next_cursor || vendorProgressState.cursor || 0);
        updateVendorProgressUi(progressPayload, taskId);
        const state = String(progressPayload?.status || "").toLowerCase();
        if (state === "completed") {
            toast.success(`任务 #${taskId} 订阅完成`);
            stopVendorProgressPolling();
            setVendorStopButtonState(true, true);
            setVendorRetryButtonState(false, false);
            await loadBindCardTasks(true);
            return;
        }
        if (state === "failed") {
            toast.error(`任务 #${taskId} 订阅失败`);
            stopVendorProgressPolling();
            setVendorStopButtonState(true, true);
            setVendorRetryButtonState(false, true);
            await loadBindCardTasks(true);
            return;
        }
        if (state === "cancelled") {
            toast.warning(`任务 #${taskId} 已停止`, 4000);
            stopVendorProgressPolling();
            setVendorStopButtonState(true, true);
            setVendorRetryButtonState(false, true);
            await loadBindCardTasks(true);
            return;
        }
        if (state === "pending") {
            toast.warning(`任务 #${taskId} 接口执行已完成，等待订阅同步`, 6000);
            stopVendorProgressPolling();
            // pending 阶段仍允许手动停止（仅停止订阅任务，不影响其他任务）
            setVendorStopButtonState(false, true);
            setVendorRetryButtonState(false, true);
            await loadBindCardTasks(true);
            return;
        }
        vendorProgressState.timer = setTimeout(() => {
            pollVendorProgress(taskId);
        }, 2000);
    } catch (error) {
        const status = Number(error?.response?.status || 0);
        const detail = String(error?.data?.detail || "").trim();
        if (status === 404 && detail.includes("绑卡任务不存在")) {
            appendVendorLogs([{ time: "--:--:--", message: "进度服务未命中任务，已停止轮询（可刷新任务列表后重试）" }]);
            stopVendorProgressPolling();
            setVendorStopButtonState(false, true);
            setVendorRetryButtonState(false, true);
            return;
        }
        appendVendorLogs([{ time: "--:--:--", message: `进度轮询失败: ${formatErrorMessage(error)}` }]);
        vendorProgressState.timer = setTimeout(() => {
            pollVendorProgress(taskId);
        }, 3000);
    }
}

async function startVendorAutoBind(task, vendorConfig) {
    const taskId = Number(task?.id || 0);
    if (!taskId) {
        throw new Error("任务 ID 无效");
    }
    const redeemCode = String(vendorConfig?.redeem_code || "").trim();
    if (!redeemCode) {
        throw new Error("兑换码不能为空");
    }
    showVendorProgressPanel(true);
    resetVendorProgressPanel();
    vendorProgressState.taskId = taskId;
    vendorProgressState.lastConfig = {
        redeem_code: redeemCode,
        checkout_url: String(vendorConfig?.checkout_url || "").trim(),
        api_url: String(vendorConfig?.api_url || "").trim(),
        api_key: String(vendorConfig?.api_key || "").trim(),
    };
    stopVendorProgressPolling();
    setVendorStopButtonState(true, true);
    setVendorRetryButtonState(true, false);
    appendVendorLogs([{ time: "--:--:--", message: `任务 #${taskId} 已创建，开始执行卡商接口订阅...` }]);
    const data = await api.post(`/payment/bind-card/tasks/${taskId}/auto-bind-vendor`, {
        redeem_code: redeemCode,
        checkout_url: String(vendorConfig?.checkout_url || "").trim() || undefined,
        api_url: String(vendorConfig?.api_url || "").trim() || undefined,
        api_key: String(vendorConfig?.api_key || "").trim() || undefined,
        timeout_seconds: 240,
    });
    updateVendorProgressUi(data?.progress || { progress: 5, status: "running" }, taskId);
    appendVendorLogs(data?.progress?.logs || []);
    vendorProgressState.cursor = Number(data?.progress?.next_cursor || 0);
    vendorProgressState.timer = setTimeout(() => {
        pollVendorProgress(taskId);
    }, 1200);
}

async function retryVendorAutoTask() {
    const taskId = Number(vendorProgressState.taskId || 0);
    if (!taskId) {
        toast.warning("当前没有可重测任务");
        return;
    }
    const fallbackConfig = collectEfunConfig();
    const config = {
        ...(vendorProgressState.lastConfig || {}),
        redeem_code: String((vendorProgressState.lastConfig || {}).redeem_code || fallbackConfig.code || "").trim(),
        api_url: String((vendorProgressState.lastConfig || {}).api_url || fallbackConfig.api_url || "").trim(),
        api_key: String((vendorProgressState.lastConfig || {}).api_key || fallbackConfig.api_key || "").trim(),
        checkout_url: String((vendorProgressState.lastConfig || {}).checkout_url || "").trim(),
    };
    if (!config.redeem_code) {
        toast.warning("请先填写兑换码");
        return;
    }
    try {
        await startVendorAutoBind({ id: taskId }, config);
        toast.success(`任务 #${taskId} 已重新开始测试`);
    } catch (error) {
        toast.error(`重新测试失败: ${formatErrorMessage(error)}`);
    }
}

async function stopVendorAutoTask() {
    let taskId = Number(vendorProgressState.taskId || 0);
    setVendorStopButtonState(true, true);
    try {
        if (!taskId) {
            const activeStop = await api.post("/payment/bind-card/vendor-stop-active", {});
            const resolvedId = Number(activeStop?.task?.id || 0);
            if (!resolvedId) {
                throw new Error("当前没有可停止的卡商任务");
            }
            taskId = resolvedId;
            vendorProgressState.taskId = resolvedId;
            appendVendorLogs(activeStop?.progress?.logs || []);
            updateVendorProgressUi(activeStop?.progress || { status: "cancelled", progress: 100 }, taskId);
            vendorProgressState.cursor = Number(activeStop?.progress?.next_cursor || vendorProgressState.cursor || 0);
            stopVendorProgressPolling();
            toast.success(`任务 #${taskId} 已发送停止指令`);
            await loadBindCardTasks(true);
            return;
        }
        const stopPaths = [
            `/payment/bind-card/tasks/${taskId}/vendor-stop`,
            `/payment/bind-card/tasks/${taskId}/stop-vendor`,
            `/payment/bind-card/tasks/${taskId}/stop`,
            `/payment/bind-card/tasks/${taskId}/cancel`,
        ];
        let data = null;
        let lastNotFoundError = null;
        for (const path of stopPaths) {
            try {
                data = await api.post(path, {});
                break;
            } catch (error) {
                const status = Number(error?.response?.status || 0);
                const detail = String(error?.data?.detail || error?.message || "").trim();
                const isRouteNotFound = status === 404 && (!detail || detail.toLowerCase() === "not found");
                if (isRouteNotFound) {
                    lastNotFoundError = error;
                    continue;
                }
                throw error;
            }
        }
        if (!data) {
            // 兜底：按“当前活跃任务”停止，避免 taskId 漂移导致无法停止。
            const activeStop = await api.post("/payment/bind-card/vendor-stop-active", {});
            const resolvedId = Number(activeStop?.task?.id || taskId || 0);
            if (!resolvedId) {
                throw lastNotFoundError || new Error("停止接口不可用");
            }
            taskId = resolvedId;
            vendorProgressState.taskId = resolvedId;
            data = activeStop;
        }
        appendVendorLogs(data?.progress?.logs || []);
        updateVendorProgressUi(data?.progress || { status: "cancelled", progress: 100 }, taskId);
        vendorProgressState.cursor = Number(data?.progress?.next_cursor || vendorProgressState.cursor || 0);
        stopVendorProgressPolling();
        toast.success(`任务 #${taskId} 已发送停止指令`);
        await loadBindCardTasks(true);
    } catch (error) {
        setVendorStopButtonState(false, true);
        toast.error(`停止失败: ${formatErrorMessage(error)}`);
    }
}

function collectVendorConfig() {
    const checkoutUrl = getInputValue("vendor-checkout-input");
    const redeemCode = normalizeVendorRedeemCode(getInputValue("vendor-redeem-input"));
    setInputValue("vendor-redeem-input", redeemCode);
    return {
        checkout_url: checkoutUrl,
        redeem_code: redeemCode,
    };
}

function normalizeVendorRedeemCode(raw) {
    const compact = String(raw || "").toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 16);
    if (!compact) return "";
    const groups = compact.match(/.{1,4}/g) || [];
    return groups.join("-");
}

function normalizeEfunCode(raw) {
    const compact = String(raw || "")
        .toUpperCase()
        .replace(/[^A-Z0-9]/g, "");
    if (!compact.startsWith("UK")) {
        return String(raw || "").trim().toUpperCase().replace(/\s+/g, "");
    }
    const body = compact.slice(2);
    if (body.length !== 25) {
        return String(raw || "").trim().toUpperCase().replace(/\s+/g, "");
    }
    return `UK-${body.slice(0, 5)}-${body.slice(5, 10)}-${body.slice(10, 15)}-${body.slice(15, 20)}-${body.slice(20, 25)}`;
}

function safeJsonParse(raw, fallback) {
    try {
        const parsed = JSON.parse(raw);
        return parsed ?? fallback;
    } catch (_) {
        return fallback;
    }
}

function normalizeCardPoolSupplier(value) {
    return String(value || "")
        .trim()
        .toLowerCase()
        .replace(/[\s_-]+/g, "");
}

function isEfunSupplier(value) {
    const normalized = normalizeCardPoolSupplier(value);
    return normalized === CARD_POOL_EFUN_SUPPLIER_KEY || normalized === "efuncard";
}

function getCardPoolResolvedStatus(item) {
    if (!item) return "used";
    const rawStatus = String(item.status || "").trim().toLowerCase();
    if (rawStatus === "used") return "used";
    const expiresRaw = String(item.expires_at || "").trim();
    if (expiresRaw) {
        const expiresAt = Date.parse(expiresRaw);
        if (Number.isFinite(expiresAt) && expiresAt <= Date.now()) {
            return "expired";
        }
    }
    if (rawStatus === "expired") return "expired";
    return "unused";
}

function loadCardPoolRedeemCodes() {
    const parsed = safeJsonParse(localStorage.getItem(CARD_POOL_REDEEM_STORAGE_KEY) || "[]", []);
    if (!Array.isArray(parsed)) {
        return [];
    }
    return parsed
        .map((item) => ({
            code: normalizeEfunCode(item?.code || ""),
            supplier: normalizeCardPoolSupplier(item?.supplier || ""),
            status: getCardPoolResolvedStatus(item),
            raw: item || {},
        }))
        .filter((item) => item.code);
}

function getAvailableEfunPoolCodes() {
    return loadCardPoolRedeemCodes().filter(
        (item) => isEfunSupplier(item.supplier) && item.status === "unused"
    );
}

function renderEfunPoolHint() {
    const hintEl = document.getElementById("efun-pool-hint");
    if (!hintEl) return;
    const manualCode = normalizeEfunCode(getInputValue("efun-code-input"));
    const available = getAvailableEfunPoolCodes();
    if (manualCode) {
        hintEl.textContent = `已手动输入兑换码；卡池可用（供应商=${CARD_POOL_EFUN_SUPPLIER_LABEL}）：${available.length}`;
        return;
    }
    if (available.length > 0) {
        hintEl.textContent = `卡池可用（供应商=${CARD_POOL_EFUN_SUPPLIER_LABEL}）：${available.length}，留空将自动使用第一张`;
        return;
    }
    hintEl.textContent = `卡池可用（供应商=${CARD_POOL_EFUN_SUPPLIER_LABEL}）：0，请手动输入兑换码`;
}

function resolveEfunCodeFromInputOrPool() {
    const manualCode = normalizeEfunCode(getInputValue("efun-code-input"));
    if (manualCode) {
        return { code: manualCode, source: "manual" };
    }
    const available = getAvailableEfunPoolCodes();
    if (!available.length) {
        return { code: "", source: "none" };
    }
    const picked = available[0];
    const code = normalizeEfunCode(picked.code);
    if (code) {
        setInputValue("efun-code-input", code);
        storage.set(EFUN_CODE_STORAGE_KEY, code);
    }
    return { code, source: "pool" };
}

function markCardPoolCodeUsed(code, email = "") {
    const normalizedCode = normalizeEfunCode(code);
    if (!normalizedCode) return false;
    const parsed = safeJsonParse(localStorage.getItem(CARD_POOL_REDEEM_STORAGE_KEY) || "[]", []);
    if (!Array.isArray(parsed)) return false;
    let changed = false;
    const nowIso = new Date().toISOString();
    const next = parsed.map((item) => {
        const itemCode = normalizeEfunCode(item?.code || "");
        if (!itemCode || itemCode !== normalizedCode) {
            return item;
        }
        changed = true;
        return {
            ...item,
            status: "used",
            used_by_email: String(email || item?.used_by_email || "").trim(),
            used_at: nowIso,
        };
    });
    if (changed) {
        localStorage.setItem(CARD_POOL_REDEEM_STORAGE_KEY, JSON.stringify(next));
    }
    return changed;
}

function setEfunResult(content, isError = false) {
    const box = document.getElementById("efun-result-box");
    if (!box) return;
    const text = typeof content === "string" ? content : JSON.stringify(content || {}, null, 2);
    box.textContent = text;
    box.classList.add("show");
    box.style.color = isError ? "#fecaca" : "#c7d2fe";
}

function collectEfunConfig() {
    const apiUrl = String(getInputValue("efun-base-url-input") || "").trim();
    const apiKey = String(getInputValue("efun-api-key-input") || "").trim();
    const code = normalizeEfunCode(getInputValue("efun-code-input"));
    setInputValue("efun-code-input", code);
    return {
        api_url: apiUrl || undefined,
        // 兼容旧逻辑：仍保留 base_url 别名，映射到同一地址。
        base_url: apiUrl || undefined,
        api_key: apiKey || undefined,
        code,
    };
}

async function callEfunApi(path, payload) {
    return api.post(`/payment/efun/${path}`, payload);
}

async function redeemEfunAndFill(showToast = true, overrideConfig = null) {
    const config = {
        ...collectEfunConfig(),
        ...(overrideConfig || {}),
    };
    config.code = normalizeEfunCode(config.code || "");
    setInputValue("efun-code-input", config.code);
    if (!config.code) {
        throw new Error("请先填写 CDK 激活码");
    }
    if (!EFUN_CODE_REGEX.test(config.code)) {
        throw new Error("CDK 格式无效");
    }
    const payload = {
        code: config.code,
        base_url: config.base_url,
        api_key: config.api_key,
    };
    const data = await callEfunApi("redeem", payload);
    const card = data?.card || {};
    const number = String(card.card_number || "").replace(/\s+/g, "");
    const month = String(card.exp_month || "").trim();
    const year = String(card.exp_year || "").trim();
    const cvc = String(card.cvc || "").trim();

    if (number) setInputValue("card-number-input", number);
    if (month || year) setInputValue("card-expiry-input", formatExpiryInput(month, year));
    if (cvc) setInputValue("card-cvc-input", cvc);
    if (!getInputValue("billing-country-input")) setInputValue("billing-country-input", "US");
    if (!getInputValue("billing-currency-input")) setInputValue("billing-currency-input", "USD");

    storage.set(EFUN_CODE_STORAGE_KEY, config.code);
    storage.set(EFUN_BASE_URL_STORAGE_KEY, String(config.base_url || EFUN_BASE_URL_DEFAULT));
    setEfunResult(data?.raw || data);

    if (showToast) {
        toast.success(`EFun 开卡成功，已回填卡号 ${String(card.masked || "").trim() || ""}`);
    }
    return data;
}

async function queryEfunCard() {
    const config = collectEfunConfig();
    if (!config.code) {
        toast.warning("请先填写 CDK 激活码");
        return;
    }
    try {
        const data = await callEfunApi("query", {
            code: config.code,
            base_url: config.base_url,
            api_key: config.api_key,
        });
        setEfunResult(data?.data || data);
        toast.success("查卡完成");
    } catch (error) {
        setEfunResult(`查卡失败: ${formatErrorMessage(error)}`, true);
        toast.error(`查卡失败: ${formatErrorMessage(error)}`);
    }
}

async function queryEfunBilling() {
    const config = collectEfunConfig();
    if (!config.code) {
        toast.warning("请先填写 CDK 激活码");
        return;
    }
    try {
        const data = await callEfunApi("billing", {
            code: config.code,
            base_url: config.base_url,
            api_key: config.api_key,
        });
        setEfunResult(data?.data || data);
        toast.success("账单查询完成");
    } catch (error) {
        setEfunResult(`账单查询失败: ${formatErrorMessage(error)}`, true);
        toast.error(`账单查询失败: ${formatErrorMessage(error)}`);
    }
}

async function queryEfun3ds() {
    const config = collectEfunConfig();
    if (!config.code) {
        toast.warning("请先填写 CDK 激活码");
        return;
    }
    try {
        const data = await callEfunApi("3ds/verify", {
            code: config.code,
            base_url: config.base_url,
            api_key: config.api_key,
            minutes: config.minutes || 30,
        });
        setEfunResult(data?.data || data);
        toast.success("3DS 查询完成");
    } catch (error) {
        setEfunResult(`3DS 查询失败: ${formatErrorMessage(error)}`, true);
        toast.error(`3DS 查询失败: ${formatErrorMessage(error)}`);
    }
}

async function cancelEfunCard() {
    const config = collectEfunConfig();
    if (!config.code) {
        toast.warning("请先填写 CDK 激活码");
        return;
    }
    const ok = await confirm(`确认销卡 ${config.code} 吗？`, "EFun 销卡");
    if (!ok) return;
    try {
        const data = await callEfunApi("cancel", {
            code: config.code,
            base_url: config.base_url,
            api_key: config.api_key,
        });
        setEfunResult(data?.data || data);
        toast.success("销卡完成");
    } catch (error) {
        setEfunResult(`销卡失败: ${formatErrorMessage(error)}`, true);
        toast.error(`销卡失败: ${formatErrorMessage(error)}`);
    }
}

function updateBindModeSpecificUi(mode) {
    const bindMode = mode || getBindMode();
    const isVendor = bindMode === "vendor_auto";
    const isVendorEfun = bindMode === "vendor_efun";
    const isVendorFlow = isVendor || isVendorEfun;
    const thirdPartyPanel = document.getElementById("third-party-config");
    const vendorPanel = document.getElementById("vendor-auto-config");
    const vendorEfunPanel = document.getElementById("vendor-efun-config");
    const commonFields = document.getElementById("bind-common-fields");
    const pasteSection = document.getElementById("bind-paste-section");
    const cardAddressSection = document.getElementById("bind-card-address-section");
    const generateBtn = document.getElementById("generate-link-btn");
    const autoOpenWrap = document.getElementById("bind-auto-open-wrap");
    const linkBox = document.getElementById("link-box");
    const stopBtn = document.getElementById("vendor-stop-btn");

    if (thirdPartyPanel) thirdPartyPanel.style.display = bindMode === "third_party" ? "" : "none";
    if (vendorPanel) vendorPanel.style.display = isVendor ? "" : "none";
    if (vendorEfunPanel) vendorEfunPanel.style.display = isVendorEfun ? "" : "none";
    if (commonFields) commonFields.style.display = isVendor ? "none" : "";
    if (pasteSection) pasteSection.style.display = isVendorEfun ? "none" : "";
    if (cardAddressSection) cardAddressSection.style.display = isVendorEfun ? "none" : "";
    if (generateBtn) generateBtn.style.display = isVendorFlow ? "none" : "";
    if (autoOpenWrap) autoOpenWrap.style.display = isVendorFlow ? "none" : "flex";
    if (stopBtn) stopBtn.style.display = isVendorFlow ? "" : "none";
    setVendorRetryButtonState(false, false);
    if (linkBox) {
        if (isVendorFlow) {
            linkBox.classList.remove("show");
            linkBox.style.display = "none";
            generatedLink = "";
            const linkText = document.getElementById("link-text");
            const openStatus = document.getElementById("open-status");
            if (linkText) linkText.value = "";
            if (openStatus) openStatus.textContent = "";
        } else {
            linkBox.style.display = "";
        }
    }
    if (!isVendorFlow) {
        showVendorProgressPanel(false);
        stopVendorProgressPolling();
        vendorProgressState.taskId = 0;
        vendorProgressState.lastConfig = null;
    }
    if (isVendorEfun) {
        renderEfunPoolHint();
    }
}

function onBindModeChange() {
    const mode = getBindMode();
    updateBindModeSpecificUi(mode);

    const actionBtn = document.getElementById("create-bind-task-btn");
    if (actionBtn) {
        actionBtn.classList.remove("btn-primary", "btn-secondary", "btn-success", "btn-purple", "btn-danger");
        actionBtn.classList.remove("efun-subscribe-wide");
        if (mode === "local_auto") {
            actionBtn.textContent = "创建并执行全自动订阅";
            actionBtn.classList.add("btn-secondary");
        } else if (mode === "vendor_efun") {
            actionBtn.textContent = "订阅";
            actionBtn.classList.add("btn-success");
            actionBtn.classList.add("efun-subscribe-wide");
        } else {
            actionBtn.textContent = "生成并加入绑卡任务（半自动）";
            actionBtn.classList.add("btn-secondary");
        }
    }
    updateSemiAutoActionsVisibility(mode);
    updateSelectedAccountEmailLabel();
    storage.set(BIND_MODE_STORAGE_KEY, mode);
}

function collectThirdPartyConfig() {
    const apiUrl = getInputValue("third-party-api-url");
    const apiKey = getInputValue("third-party-api-key");
    return { api_url: apiUrl, api_key: apiKey };
}

function restoreBindModeConfig() {
    const modeSelect = document.getElementById("bind-mode-select");
    const savedMode = String(storage.get(BIND_MODE_STORAGE_KEY, "semi_auto") || "semi_auto");
    if (modeSelect) {
        modeSelect.value = ALLOWED_BIND_MODES.has(savedMode) ? savedMode : "semi_auto";
    }

    const savedApiUrl = String(storage.get(THIRD_PARTY_BIND_URL_STORAGE_KEY, "") || "").trim();
    const initialApiUrl = savedApiUrl || THIRD_PARTY_BIND_DEFAULT_URL;
    setInputValue("third-party-api-url", initialApiUrl);
    if (!savedApiUrl) {
        storage.set(THIRD_PARTY_BIND_URL_STORAGE_KEY, initialApiUrl);
    }

    const savedVendorCheckout = String(storage.get(VENDOR_CHECKOUT_STORAGE_KEY, "") || "").trim();
    const savedRedeemCode = normalizeVendorRedeemCode(String(storage.get(VENDOR_REDEEM_STORAGE_KEY, "") || "").trim());
    setInputValue("vendor-checkout-input", savedVendorCheckout);
    setInputValue("vendor-redeem-input", savedRedeemCode);
    setInputValue(
        "efun-base-url-input",
        String(storage.get(EFUN_BASE_URL_STORAGE_KEY, EFUN_BASE_URL_DEFAULT) || EFUN_BASE_URL_DEFAULT).trim() || EFUN_BASE_URL_DEFAULT
    );
    setInputValue("efun-api-key-input", String(storage.get(EFUN_API_KEY_STORAGE_KEY, "") || "").trim());
    setInputValue("efun-code-input", normalizeEfunCode(String(storage.get(EFUN_CODE_STORAGE_KEY, "") || "").trim()));

    onBindModeChange();
}

function switchPaymentTab(tab) {
    const isLink = tab === "link";
    document.getElementById("tab-btn-link")?.classList.toggle("active", isLink);
    document.getElementById("tab-btn-bind-task")?.classList.toggle("active", !isLink);
    document.getElementById("tab-content-link")?.classList.toggle("active", isLink);
    document.getElementById("tab-content-bind-task")?.classList.toggle("active", !isLink);
    if (!isLink) {
        loadBindCardTasks(true);
    }
}

function getCheckoutPayload() {
    const accountId = Number(document.getElementById("account-select")?.value || 0);
    if (!accountId) {
        throw new Error("请先选择账号");
    }
    const payload = {
        account_id: accountId,
        plan_type: selectedPlan,
        country: (document.getElementById("country-select")?.value || "US").toUpperCase(),
        currency: (document.getElementById("currency-display")?.value || "USD").toUpperCase(),
    };
    if (selectedPlan === "team") {
        payload.workspace_name = document.getElementById("workspace-name")?.value || "MyTeam";
        payload.seat_quantity = Number(document.getElementById("seat-quantity")?.value || 5) || 5;
        payload.price_interval = document.getElementById("price-interval")?.value || "month";
    }
    return payload;
}

function showGeneratedLink(data) {
    generatedLink = data.link || "";
    const linkText = document.getElementById("link-text");
    const linkBox = document.getElementById("link-box");
    const statusEl = document.getElementById("open-status");
    if (!linkText || !linkBox || !statusEl) return;

    linkText.value = generatedLink;
    linkBox.classList.add("show");

    const source = data.source ? `来源: ${data.source}` : "";
    const routeHint = data.is_official_checkout
        ? "已拿到官方 checkout 链接，可直接绑卡"
        : "当前是中转链接，点击“直接打开支付页”继续跳转";
    statusEl.textContent = [source, routeHint].filter(Boolean).join(" | ");
}

document.addEventListener("DOMContentLoaded", () => {
    const countrySelect = document.getElementById("country-select");
    const currencyDisplay = document.getElementById("currency-display");
    if (countrySelect) {
        countrySelect.value = countrySelect.value || "US";
    }
    if (currencyDisplay) {
        currencyDisplay.value = currencyDisplay.value || "USD";
    }

    const searchInput = document.getElementById("bind-task-search");
    if (searchInput) {
        searchInput.addEventListener(
            "input",
            debounce(() => {
                bindTaskState.search = (searchInput.value || "").trim();
                bindTaskState.page = 1;
                loadBindCardTasks();
            }, 250)
        );
    }

    const statusSelect = document.getElementById("bind-task-status");
    if (statusSelect) {
        statusSelect.addEventListener("change", () => {
            bindTaskState.status = statusSelect.value || "";
            bindTaskState.page = 1;
            loadBindCardTasks();
        });
    }
    document.getElementById("account-select")?.addEventListener("change", () => {
        resetGenerateLinkButtonState();
        updateSelectedAccountEmailLabel();
    });

    bindBillingEvents();
    restoreBillingProfileNonSensitive();
    refreshBillingTemplateSelect();
    restoreBindModeConfig();

    document.getElementById("third-party-api-url")?.addEventListener(
        "input",
        debounce(() => {
            const apiUrl = getInputValue("third-party-api-url");
            storage.set(THIRD_PARTY_BIND_URL_STORAGE_KEY, apiUrl);
        }, 200)
    );
    document.getElementById("vendor-checkout-input")?.addEventListener(
        "input",
        debounce(() => {
            storage.set(VENDOR_CHECKOUT_STORAGE_KEY, getInputValue("vendor-checkout-input"));
        }, 200)
    );
    document.getElementById("vendor-redeem-input")?.addEventListener(
        "input",
        debounce(() => {
            const normalized = normalizeVendorRedeemCode(getInputValue("vendor-redeem-input"));
            setInputValue("vendor-redeem-input", normalized);
            storage.set(VENDOR_REDEEM_STORAGE_KEY, normalized);
        }, 200)
    );
    document.getElementById("efun-base-url-input")?.addEventListener(
        "input",
        debounce(() => {
            const value = String(getInputValue("efun-base-url-input") || "").trim();
            storage.set(EFUN_BASE_URL_STORAGE_KEY, value || EFUN_BASE_URL_DEFAULT);
        }, 200)
    );
    document.getElementById("efun-api-key-input")?.addEventListener(
        "input",
        debounce(() => {
            storage.set(EFUN_API_KEY_STORAGE_KEY, String(getInputValue("efun-api-key-input") || "").trim());
        }, 200)
    );
    document.getElementById("efun-code-input")?.addEventListener(
        "input",
        debounce(() => {
            const normalized = normalizeEfunCode(getInputValue("efun-code-input"));
            setInputValue("efun-code-input", normalized);
            storage.set(EFUN_CODE_STORAGE_KEY, normalized);
            renderEfunPoolHint();
        }, 200)
    );
    document.getElementById("bind-mode-select")?.addEventListener("change", onBindModeChange);
    window.addEventListener("storage", (event) => {
        if (!event || event.key === CARD_POOL_REDEEM_STORAGE_KEY) {
            renderEfunPoolHint();
        }
    });

    loadAccounts();
    onCountryChange();
    loadBindCardTasks();
    startBindTaskAutoRefresh();
    switchPaymentTab("link");

    window.addEventListener("beforeunload", () => {
        stopBindTaskAutoRefresh();
        stopVendorProgressPolling();
    });
});

// 加载账号列表
async function fetchBindFailStats(accountIds = []) {
    const ids = (Array.isArray(accountIds) ? accountIds : [])
        .map((item) => Number(item))
        .filter((item) => Number.isFinite(item) && item > 0);
    if (!ids.length) {
        return new Map();
    }
    try {
        const data = await api.post("/payment/bind-card/tasks/fail-stats", {
            account_ids: ids,
        }, {
            timeoutMs: 12000,
            retry: 0,
            silentNetworkError: true,
            silentTimeoutError: true,
            requestKey: "payment:bind-fail-stats",
            cancelPrevious: true,
            priority: "high",
        });
        const rows = Array.isArray(data?.stats) ? data.stats : [];
        const map = new Map();
        rows.forEach((row) => {
            const accountId = Number(row?.account_id || 0);
            const failCount = Number(row?.fail_count || 0);
            if (!Number.isFinite(accountId) || accountId <= 0) return;
            map.set(accountId, Number.isFinite(failCount) && failCount > 0 ? failCount : 0);
        });
        return map;
    } catch (_e) {
        return new Map();
    }
}

async function loadAccounts() {
    try {
        // 后端 page_size 最大为 100，超限会返回 422。
        // 支付页账号下拉：仅显示「母号」且未订阅 plus/team 的账号。
        const data = await api.get("/accounts?page=1&page_size=100");
        const sel = document.getElementById("account-select");
        if (!sel) return;

        sel.innerHTML = '<option value="">-- 请选择账号 --</option>';
        const allAccounts = Array.isArray(data.accounts) ? data.accounts : [];
        const bindFailCountMap = await fetchBindFailStats(allAccounts.map((acc) => Number(acc?.id || 0)));
        const toRoleRank = (acc) => {
            const role = String(acc?.role_tag || "").trim().toLowerCase();
            if (role === "parent") return 0;
            if (role === "child") return 1;
            return 2;
        };
        const toPriority = (acc) => {
            const num = Number(acc?.priority || 50);
            return Number.isFinite(num) ? num : 50;
        };
        const toLastUsedRank = (acc) => {
            const ms = Date.parse(String(acc?.last_used_at || ""));
            if (Number.isNaN(ms)) return -1;
            return ms;
        };
        const toFailRank = (acc) => {
            const accountId = Number(acc?.id || 0);
            if (Number.isFinite(accountId) && accountId > 0) {
                const count = bindFailCountMap.get(accountId);
                if (Number.isFinite(count)) {
                    return count;
                }
            }
            return String(acc?.status || "").trim().toLowerCase() === "failed" ? 1 : 0;
        };

        paymentAccounts = allAccounts
            .filter((acc) => {
                const sub = String(acc?.subscription_type || "").trim().toLowerCase();
                if (sub === "plus" || sub === "team") {
                    return false;
                }
                const roleTag = String(acc?.role_tag || "").trim().toLowerCase();
                const accountLabel = String(acc?.account_label || "").trim().toLowerCase();
                const isParent = roleTag === "parent" || accountLabel === "mother" || accountLabel === "母号";
                return isParent;
            })
            .sort((a, b) => {
                const roleDiff = toRoleRank(a) - toRoleRank(b);
                if (roleDiff !== 0) return roleDiff;

                const priorityDiff = toPriority(a) - toPriority(b);
                if (priorityDiff !== 0) return priorityDiff;

                const lastUsedDiff = toLastUsedRank(a) - toLastUsedRank(b);
                if (lastUsedDiff !== 0) return lastUsedDiff;

                const failDiff = toFailRank(a) - toFailRank(b);
                if (failDiff !== 0) return failDiff;

                return Number(a?.id || 0) - Number(b?.id || 0);
            });

        paymentAccounts.forEach((acc) => {
            const opt = document.createElement("option");
            opt.value = acc.id;
            const subText = acc.subscription_type ? ` (${String(acc.subscription_type).toUpperCase()})` : "";
            opt.textContent = `${acc.email}${subText}`;
            sel.appendChild(opt);
        });
        updateSelectedAccountEmailLabel();
        updateSemiAutoActionsVisibility(getBindMode());
    } catch (e) {
        toast.error(`加载账号失败: ${formatErrorMessage(e)}`);
    }
}

// 国家切换
function onCountryChange() {
    const country = document.getElementById("country-select")?.value || "US";
    const currency = COUNTRY_CURRENCY_MAP[country] || "USD";
    const currencyEl = document.getElementById("currency-display");
    if (currencyEl) {
        currencyEl.value = currency;
    }
}

// 选择套餐
function selectPlan(plan) {
    selectedPlan = plan;
    document.getElementById("plan-plus")?.classList.toggle("selected", plan === "plus");
    document.getElementById("plan-team")?.classList.toggle("selected", plan === "team");
    document.getElementById("team-options")?.classList.toggle("show", plan === "team");

    // 切换套餐时隐藏已有链接，避免误用旧链接。
    document.getElementById("link-box")?.classList.remove("show");
    generatedLink = "";
    resetGenerateLinkButtonState();
}

// 生成支付链接
async function generateLink() {
    if (isGeneratingCheckoutLink) {
        return;
    }

    let payload;
    try {
        payload = getCheckoutPayload();
    } catch (err) {
        toast.warning(err.message || "参数不完整");
        return;
    }

    isGeneratingCheckoutLink = true;
    setButtonLoading("generate-link-btn", "生成中...", true);
    try {
        const data = await api.post("/payment/generate-link", payload);
        if (!data?.success || !data?.link) {
            throw new Error(data?.detail || "生成链接失败");
        }
        showGeneratedLink(data);
        toast.success("支付链接生成成功");
    } catch (e) {
        toast.error(`生成链接失败: ${formatErrorMessage(e)}`);
    } finally {
        isGeneratingCheckoutLink = false;
        setButtonLoading("generate-link-btn", "生成中...", false);
        resetGenerateLinkButtonState();
    }
}

async function submitThirdPartyAutoBind(task, bindData) {
    const thirdParty = collectThirdPartyConfig();
    const apiUrl = String(thirdParty.api_url || "").trim();
    const apiKey = String(thirdParty.api_key || "").trim();
    if (apiUrl) {
        storage.set(THIRD_PARTY_BIND_URL_STORAGE_KEY, apiUrl);
    }

    const expYear = String(bindData.exp_year || "").replace(/\D/g, "");
    const payload = {
        api_url: apiUrl || undefined,
        api_key: apiKey || undefined,
        timeout_seconds: 180,
        interval_seconds: 10,
        card: {
            number: String(bindData.card_number || "").replace(/\D/g, ""),
            exp_month: String(bindData.exp_month || "").replace(/\D/g, "").padStart(2, "0").slice(0, 2),
            exp_year: (expYear.slice(-2) || expYear || "").padStart(2, "0"),
            cvc: String(bindData.cvc || "").replace(/\D/g, "").slice(0, 4),
        },
        profile: {
            name: String(bindData.billing_name || "").trim(),
            email: String(task?.account_email || "").trim() || undefined,
            country: String(bindData.country_code || "US").toUpperCase(),
            line1: String(bindData.address_line1 || "").trim(),
            city: String(bindData.address_city || "").trim(),
            state: String(bindData.address_state || "").trim(),
            postal: String(bindData.postal_code || "").trim(),
        },
    };

    return api.post(`/payment/bind-card/tasks/${task.id}/auto-bind-third-party`, payload);
}

async function submitLocalAutoBind(task, bindData) {
    const expYear = String(bindData.exp_year || "").replace(/\D/g, "");
    const payload = {
        browser_timeout_seconds: 220,
        post_submit_wait_seconds: 90,
        verify_timeout_seconds: 180,
        verify_interval_seconds: 10,
        headless: false,
        card: {
            number: String(bindData.card_number || "").replace(/\D/g, ""),
            exp_month: String(bindData.exp_month || "").replace(/\D/g, "").padStart(2, "0").slice(0, 2),
            exp_year: (expYear.slice(-2) || expYear || "").padStart(2, "0"),
            cvc: String(bindData.cvc || "").replace(/\D/g, "").slice(0, 4),
        },
        profile: {
            name: String(bindData.billing_name || "").trim(),
            email: String(task?.account_email || "").trim() || undefined,
            country: String(bindData.country_code || "US").toUpperCase(),
            line1: String(bindData.address_line1 || "").trim(),
            city: String(bindData.address_city || "").trim(),
            state: String(bindData.address_state || "").trim(),
            postal: String(bindData.postal_code || "").trim(),
        },
    };
    return api.post(`/payment/bind-card/tasks/${task.id}/auto-bind-local`, payload);
}

async function runLocalAutoBindInBackground(task, bindData) {
    try {
        const autoResult = await submitLocalAutoBind(task, bindData);
        if (autoResult?.verified) {
            toast.success(`任务 #${task.id} 全自动绑卡完成: ${String(autoResult.subscription_type || "").toUpperCase()}`);
        } else if (autoResult?.paid_confirmed) {
            toast.success(`任务 #${task.id} 已确认支付，等待订阅同步（可点“同步订阅”）`, 7000);
        } else if (autoResult?.pending || autoResult?.need_user_action) {
            const stage = String(autoResult?.local_auto?.stage || autoResult?.local_auto?.error || "challenge").toUpperCase();
            toast.warning(
                `任务 #${task.id} 全自动绑卡已提交（${stage}），请在支付页完成验证后点击“我已完成支付”或“同步订阅”。`,
                9000
            );
        } else {
            const sub = String(autoResult?.subscription_type || "free").toUpperCase();
            toast.warning(`任务 #${task.id} 全自动绑卡执行完成，但当前订阅为 ${sub}，请稍后再同步`, 7000);
        }
    } catch (autoErr) {
        toast.error(`任务 #${task.id} 全自动绑卡失败: ${formatErrorMessage(autoErr)}`);
    } finally {
        try {
            await loadBindCardTasks();
        } catch (_) {
            // 忽略刷新异常，避免覆盖前面的业务提示
        }
    }
}

// 生成并创建绑卡任务
async function createBindCardTask() {
    let payload;
    try {
        payload = getCheckoutPayload();
    } catch (err) {
        toast.warning(err.message || "参数不完整");
        return;
    }

    const bindMode = getBindMode();
    const isVendorEfun = bindMode === "vendor_efun";
    const efunConfig = isVendorEfun ? collectEfunConfig() : {};
    const vendorConfigForRun = isVendorEfun
        ? {
            redeem_code: "",
            checkout_url: String(payload?.custom_checkout_url || "").trim(),
            api_url: String(efunConfig.api_url || "").trim(),
            api_key: String(efunConfig.api_key || "").trim(),
        }
        : null;
    if (isVendorEfun) {
        const resolved = resolveEfunCodeFromInputOrPool();
        const resolvedCode = String(resolved?.code || "").trim();
        if (!resolvedCode) {
            renderEfunPoolHint();
            toast.warning("卡商EFun模式需要兑换码：可先在卡池添加供应商=EFun的可用码，或手动填写");
            return;
        }
        efunConfig.code = resolvedCode;
        vendorConfigForRun.redeem_code = resolvedCode;
        setInputValue("efun-code-input", resolvedCode);
        storage.set(EFUN_CODE_STORAGE_KEY, resolvedCode);
        if (!EFUN_CODE_REGEX.test(String(efunConfig.code || ""))) {
            toast.warning("CDK 格式无效");
            return;
        }
        renderEfunPoolHint();
        setEfunResult("已准备就绪：将执行接口流程（强制生成 Checkout -> EFun 开卡 -> bindcard 提交 -> 同步订阅）");
    } else {
        let bindData = collectBillingFormData();
        const missing = [];
        if (!bindData.card_number) missing.push("卡号");
        if (!bindData.exp_month || !bindData.exp_year) missing.push("有效期");
        if (!bindData.cvc) missing.push("CVC");
        if (!bindData.billing_name) missing.push("姓名");
        if (!bindData.address_line1) missing.push("地址");
        if (!bindData.postal_code) missing.push("邮编");
        if (missing.length && bindMode === "semi_auto") {
            toast.warning(`绑卡资料未完整：${missing.join("、")}（本次仅创建半自动任务，不会阻断）`, 5000);
        }
        if (missing.length && bindMode === "local_auto") {
            toast.warning(`全自动绑卡需要完整资料：${missing.join("、")}`, 5000);
            return;
        }
    }

    payload.auto_open = isVendorEfun ? false : Boolean(document.getElementById("bind-auto-open")?.checked);
    payload.bind_mode = bindMode;

    setButtonLoading("create-bind-task-btn", "创建中...", true);
    try {
        const data = await api.post("/payment/bind-card/tasks", payload);
        if (!data?.success || !data?.task) {
            throw new Error(data?.detail || "创建绑卡任务失败");
        }

        if (data.link && !isVendorEfun) {
            showGeneratedLink({
                link: data.link,
                source: data.source,
                is_official_checkout: data.is_official_checkout,
            });
        }

        if (bindMode === "local_auto") {
            const bindData = collectBillingFormData();
            toast.info(`任务 #${data.task.id} 已创建，已在后台执行全自动绑卡，可继续修改参数并创建新任务`, 5000);
            runLocalAutoBindInBackground(data.task, { ...bindData });
        } else if (bindMode === "vendor_efun") {
            toast.info(`任务 #${data.task.id} 已创建，开始执行接口订阅流程`, 4000);
            showVendorProgressPanel(true);
            await startVendorAutoBind(data.task, vendorConfigForRun || {});
            await loadBindCardTasks(true);
        } else {
            toast.success(`绑卡任务已创建 #${data.task.id}${data.auto_opened ? "，浏览器已打开" : ""}`);
            switchPaymentTab("bind-task");
            await loadBindCardTasks();
            return;
        }
        if (bindMode !== "vendor_efun") {
            switchPaymentTab("bind-task");
        }
        await loadBindCardTasks();
    } catch (e) {
        toast.error(`创建绑卡任务失败: ${formatErrorMessage(e)}`);
    } finally {
        setButtonLoading("create-bind-task-btn", "创建中...", false);
    }
}

function copyLink() {
    if (!generatedLink) {
        toast.warning("请先生成链接");
        return;
    }
    copyToClipboard(generatedLink);
}

// 在当前浏览器直接打开支付页（适合 Docker/远程部署场景）
function openCheckout() {
    if (!generatedLink) {
        toast.warning("请先生成链接");
        return;
    }
    const w = window.open(generatedLink, "_blank", "noopener,noreferrer");
    if (!w) {
        window.location.href = generatedLink;
    }
}

// 无痕打开浏览器（携带账号 cookie）
async function openIncognito() {
    if (!generatedLink) {
        toast.warning("请先生成链接");
        return;
    }
    const accountId = Number(document.getElementById("account-select")?.value || 0);
    const statusEl = document.getElementById("open-status");
    if (statusEl) {
        statusEl.textContent = "正在打开...";
    }
    try {
        const body = { url: generatedLink };
        if (accountId) body.account_id = accountId;
        const data = await api.post("/payment/open-incognito", body);
        if (data?.success) {
            if (statusEl) statusEl.textContent = "已在无痕模式打开浏览器";
            toast.success("无痕浏览器已打开");
        } else {
            if (statusEl) statusEl.textContent = data?.message || "未找到可用浏览器，请手动复制链接";
            toast.warning(data?.message || "未找到可用浏览器");
        }
    } catch (e) {
        if (statusEl) statusEl.textContent = `请求失败: ${formatErrorMessage(e)}`;
        toast.error(`请求失败: ${formatErrorMessage(e)}`);
    }
}

async function loadBindCardTasks(silent = false) {
    const tbody = document.getElementById("bind-card-task-table");
    if (!tbody) return;
    if (bindTaskLoadPromise) {
        if (!silent) {
            toast.info("列表正在刷新，请稍候...", 1800);
        }
        return bindTaskLoadPromise;
    }

    const runner = async () => {
        isLoadingBindTaskList = true;
        if (!silent) {
            setButtonLoading("refresh-bind-task-btn", "刷新中...", true);
        }
        try {
            const params = new URLSearchParams({
                page: String(bindTaskState.page),
                page_size: String(bindTaskState.pageSize),
            });
            if (bindTaskState.status) params.set("status", bindTaskState.status);
            if (bindTaskState.search) params.set("search", bindTaskState.search);

            const data = await api.get(`/payment/bind-card/tasks?${params.toString()}`, {
                timeoutMs: 45000,
                retry: silent ? 0 : 1,
                requestKey: "payment:bind-task-list",
                cancelPrevious: true,
                silentNetworkError: silent,
                silentTimeoutError: silent,
                priority: silent ? "low" : "normal",
            });
            const tasks = data?.tasks || [];

            if (!tasks.length) {
                tbody.innerHTML = `
                    <tr>
                        <td colspan="8"><div class="empty-state">暂无绑卡任务</div></td>
                    </tr>
                `;
                return;
            }

            tbody.innerHTML = tasks.map((task) => `
                <tr>
                    <td>${task.id}</td>
                    <td>${escapeHtml(task.account_email || "-")}</td>
                    <td>${String(task.plan_type || "-").toUpperCase()}</td>
                    <td><span class="bind-task-badge ${escapeHtml(task.status || "")}">${escapeHtml(getTaskStatusText(task.status))}</span></td>
                    <td>
                        <a class="bind-task-url" href="${escapeHtml(task.checkout_url || "#")}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(task.checkout_url || "")}">
                            ${escapeHtml(task.checkout_url || "-")}
                        </a>
                    </td>
                    <td>${escapeHtml(task.checkout_source || "-")}</td>
                    <td>${formatBeijingDateTime(task.created_at)}</td>
                    <td>
                        <div class="bind-task-actions">
                            <button class="btn btn-primary bind-task-action-btn" onclick="openBindCardTask(${task.id})">打开</button>
                            <button class="btn btn-primary bind-task-action-btn" onclick="markBindCardTaskUserAction(${task.id})">我已完成支付</button>
                            <button class="btn btn-secondary bind-task-action-btn" onclick="syncBindCardTask(${task.id})">同步订阅</button>
                            <button class="btn btn-danger bind-task-action-btn" onclick="deleteBindCardTask(${task.id})">删除</button>
                        </div>
                        ${task.last_error ? `<div class="hint" style="margin-top:6px;color:var(--danger-color);" title="${escapeHtml(task.last_error)}">${escapeHtml(task.last_error)}</div>` : ""}
                    </td>
                </tr>
            `).join("");
        } catch (e) {
            if (!silent) {
                tbody.innerHTML = `
                    <tr>
                        <td colspan="8"><div class="empty-state">加载失败: ${escapeHtml(formatErrorMessage(e))}</div></td>
                    </tr>
                `;
            }
        } finally {
            isLoadingBindTaskList = false;
            if (!silent) {
                setButtonLoading("refresh-bind-task-btn", "刷新中...", false);
            }
        }
    };

    bindTaskLoadPromise = runner();
    try {
        return await bindTaskLoadPromise;
    } finally {
        bindTaskLoadPromise = null;
    }
}

async function openBindCardTask(taskId) {
    try {
        const data = await api.post(`/payment/bind-card/tasks/${taskId}/open`, {});
        if (data?.success) {
            toast.success(`任务 #${taskId} 已尝试打开`);
            await loadBindCardTasks();
            return;
        }
        throw new Error(data?.detail || "打开失败");
    } catch (e) {
        toast.error(`打开任务失败: ${formatErrorMessage(e)}`);
    }
}

async function markBindCardTaskUserAction(taskId) {
    const lockKey = `mark:${taskId}`;
    if (bindTaskActionRunning.has(lockKey)) {
        toast.info(`任务 #${taskId} 正在验证中，请稍候...`, 2500);
        return;
    }
    bindTaskActionRunning.add(lockKey);
    try {
        toast.info(`任务 #${taskId} 已进入后台验证，最多等待 180 秒...`, 3000);
        const opTask = await api.post(`/payment/bind-card/tasks/${taskId}/mark-user-action/async`, {
            timeout_seconds: 180,
            interval_seconds: 10,
        }, {
            timeoutMs: 20000,
            retry: 0,
        });

        const opTaskId = String(opTask?.id || "").trim();
        if (!opTaskId) {
            throw new Error("任务创建失败：未返回任务 ID");
        }

        const finalTask = await watchPaymentOpTask(opTaskId);
        const finalStatus = String(finalTask?.status || "").toLowerCase();
        const data = finalTask?.result || {};

        if (finalStatus === "failed") {
            throw new Error(finalTask?.error || finalTask?.message || "验证任务失败");
        }

        if (finalStatus === "cancelled" || data?.cancelled) {
            toast.warning(`任务 #${taskId} 验证已取消`, 5000);
        } else if (data?.verified) {
            toast.success(`任务 #${taskId} 验证成功: ${String(data.subscription_type || "").toUpperCase()}`);
        } else {
            const sub = String(data?.subscription_type || "free").toUpperCase();
            const source = String(data?.detail?.source || "unknown");
            const confidence = String(data?.detail?.confidence || "unknown");
            const note = String(data?.detail?.note || "");
            const suffix = note ? `, note=${note}` : "";
            toast.warning(
                `任务 #${taskId} 暂未检测到订阅（当前 ${sub}, source=${source}, confidence=${confidence}${suffix}），已切回待用户完成`,
                7000
            );
        }
        await loadBindCardTasks();
    } catch (e) {
        // 兼容旧后端：如果 mark-user-action/async 尚未部署，自动降级到旧同步接口。
        const detail = String(e?.data?.detail || "").toLowerCase();
        const isRouteNotFound = e?.response?.status === 404 && detail === "not found";
        if (isRouteNotFound) {
            try {
                const fallbackMark = await api.post(`/payment/bind-card/tasks/${taskId}/mark-user-action`, {
                    timeout_seconds: 180,
                    interval_seconds: 10,
                }, {
                    timeoutMs: 210000,
                    retry: 0,
                });
                if (fallbackMark?.verified) {
                    toast.success(`任务 #${taskId} 验证成功: ${String(fallbackMark.subscription_type || "").toUpperCase()}`);
                } else {
                    const sub = String(fallbackMark?.subscription_type || "free").toUpperCase();
                    toast.warning(`任务 #${taskId} 兼容验证完成，但当前仍是 ${sub}`, 5000);
                }
                await loadBindCardTasks();
                return;
            } catch (fallbackErr) {
                // 兼容极老版本：mark-user-action 不存在时再降级 sync-subscription。
                const detail2 = String(fallbackErr?.data?.detail || "").toLowerCase();
                const isMarkRouteMissing = fallbackErr?.response?.status === 404 && detail2 === "not found";
                if (isMarkRouteMissing) {
                    try {
                        const fallback = await api.post(`/payment/bind-card/tasks/${taskId}/sync-subscription`, {}, {
                            timeoutMs: 60000,
                            retry: 0,
                        });
                        const sub = String(fallback?.subscription_type || "free").toUpperCase();
                        if (sub === "PLUS" || sub === "TEAM") {
                            toast.success(`任务 #${taskId} 已通过兼容模式同步成功: ${sub}`);
                        } else {
                            toast.warning(`任务 #${taskId} 兼容同步完成，但当前仍是 ${sub}`, 5000);
                        }
                        await loadBindCardTasks();
                        return;
                    } catch (fallbackSyncErr) {
                        toast.error(`验证订阅失败（兼容模式也失败）: ${formatErrorMessage(fallbackSyncErr)}`);
                        return;
                    }
                }
                toast.error(`验证订阅失败（兼容模式失败）: ${formatErrorMessage(fallbackErr)}`);
                return;
            }
        }
        toast.error(`验证订阅失败: ${formatErrorMessage(e)}`);
    } finally {
        bindTaskActionRunning.delete(lockKey);
    }
}

async function syncBindCardTask(taskId) {
    try {
        const data = await api.post(`/payment/bind-card/tasks/${taskId}/sync-subscription`, {}, {
            timeoutMs: 60000,
            retry: 0,
        });
        const sub = String(data?.subscription_type || "free").toUpperCase();
        const source = String(data?.detail?.source || "unknown");
        const confidence = String(data?.detail?.confidence || "unknown");
        const note = String(data?.detail?.note || "");
        const suffix = note ? `, note=${note}` : "";
        const msg = `同步完成: ${sub} (source=${source}, confidence=${confidence}${suffix})`;
        if (sub === "PLUS" || sub === "TEAM") {
            toast.success(msg);
        } else {
            toast.warning(msg, 7000);
        }
        await loadBindCardTasks();
    } catch (e) {
        toast.error(`同步订阅失败: ${formatErrorMessage(e)}`);
    }
}

async function deleteBindCardTask(taskId) {
    const ok = await confirm(`确认删除绑卡任务 #${taskId} 吗？`, "删除任务");
    if (!ok) return;

    try {
        await api.delete(`/payment/bind-card/tasks/${taskId}`);
        toast.success(`任务 #${taskId} 已删除`);
        await loadBindCardTasks();
    } catch (e) {
        toast.error(`删除任务失败: ${formatErrorMessage(e)}`);
    }
}

window.selectPlan = selectPlan;
window.generateLink = generateLink;
window.createBindCardTask = createBindCardTask;
window.copyLink = copyLink;
window.openCheckout = openCheckout;
window.openIncognito = openIncognito;
window.onBindModeChange = onBindModeChange;
window.switchPaymentTab = switchPaymentTab;
window.loadBindCardTasks = loadBindCardTasks;
window.openBindCardTask = openBindCardTask;
window.markBindCardTaskUserAction = markBindCardTaskUserAction;
window.syncBindCardTask = syncBindCardTask;
window.deleteBindCardTask = deleteBindCardTask;
window.fillFromBatchProfile = fillFromBatchProfile;
window.randomBillingByCountry = randomBillingByCountry;
window.redeemEfunAndFill = redeemEfunAndFill;
window.queryEfunCard = queryEfunCard;
window.queryEfunBilling = queryEfunBilling;
window.queryEfun3ds = queryEfun3ds;
window.cancelEfunCard = cancelEfunCard;
window.retryVendorAutoTask = retryVendorAutoTask;
