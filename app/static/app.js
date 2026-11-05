import { locale, setLocale, t, L, hasMessage, number, date, errorMessage, translateStatic } from "./i18n.js";

const byId = (id) => document.getElementById(id);
const state = { user: null, authMode: "login", route: "dashboard", load: 0, users: {}, teachers: { view: "requests", status: "pending", page: 1 }, catalogs: "groups", workload: null, students: { page: 1 }, grades: {}, sessions: {}, audit: { page: 1 }, ai: null };
state.registrationOptions = { groups: [], student_email_required: false, student_email_domain: "" };
let registrationOptionsRequest;
const actionNames = {
  login: "Кіру", logout: "Шығу", password_change: "Құпиясөзді өзгерту", record_view: "Жазбаны қарау", record_update: "Жазбаны өзгерту", export: "Экспорт",
  register: "Тіркелу", user_register: "Тіркелу", login_failed: "Сәтсіз кіру", user_update: "Пайдаланушыны өзгерту", role_update: "Рөлді өзгерту", role_permissions_update: "Құқықтарды өзгерту",
  grade_update: "Бағаны өзгерту", grade_view: "Ведомосты қарау", document_upload: "Құжат жүктеу", document_view: "Құжатты қарау", document_delete: "Құжатты жою",
  session_block: "Сессияны бұғаттау", session_unblock: "Бұғаттан шығару", session_terminate: "Сессияны тоқтату", session_flagged: "Күдікті сессия белгісі", assignment_change: "Оқытушыны бекіту",
  grade_change: "Бағаны өзгерту", user_change: "Тіркелгіні өзгерту", role_change: "Рөлді өзгерту", permission_change: "Құқықтарды өзгерту", session_expired: "Сессия мерзімінің аяқталуы", access_denied: "Қолжетімділіктен бас тарту", admin_bootstrap: "Алғашқы әкімшіні құру",
};
const outcomeNames = { success: "Сәтті", failure: "Сәтсіз", denied: "Рұқсат берілмеді" };
const statusNames = { active: "Белсенді", blocked: "Бұғатталған", revoked: "Тоқтатылған", expired: "Мерзімі өткен", pending: "Растауды күтуде", inactive: "Белсенді емес" };
const roleNames = { admin: "Әкімші", teacher: "Оқытушы", head: "Кафедра меңгерушісі", auditor: "Аудитор", pending: "Растауды күтуде", reader: "Оқырман" };
const nav = [
  ["dashboard", "Басты бет", "Dashboard.View", "dashboard"], ["users", "Пайдаланушылар", "Users.Manage", "users"],
  ["requests", "requests.title", "Users.Manage", "users"],
  ["teachers", "teachers.title", "Users.Manage", "users"], ["catalogs", "catalogs.title", "Users.Manage", "grades"],
  ["students", "students.title", "Grades.View", "users"],
  ["roles", "Рөлдер мен құқықтар", "Roles.Manage", "shield"], ["grades", "Ведомостар", "Grades.View", "grades"],
  ["tasks", "learning.title", "Learning.View", "grades"],
  ["documents", "Құжаттар", "Documents.View", "file"], ["sessions", "Сессиялар", "Sessions.Manage", "sessions"],
  ["audit", "Аудит журналы", "Audit.View", "audit"], ["assistant", "AI көмекші", "Audit.View", "spark"],
];

function el(tag, className = "", text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined && text !== null) item.textContent = String(L(text));
  return item;
}
function button(text, onClick, className = "button") {
  const item = el("button", className, text); item.type = "button";
  if (onClick) item.addEventListener("click", onClick);
  return item;
}
function link(text, href, className = "button") { const item = el("a", className, text); item.href = href; return item; }
function dataEl(tag, className, value) { const item = el(tag, className); item.textContent = String(value ?? "—"); return item; }
function can(permission) { return state.user?.approval_status !== "pending" && state.user?.approval_status !== "rejected" && Boolean(state.user?.permissions?.includes(permission)); }
function routeAllowed(entry) { if (entry[0] === "students") return ["admin", "head", "teacher"].includes(state.user?.role_code) && (can("Grades.View") || can("Users.Manage")); return can(entry[2]) && (!["requests", "teachers", "catalogs"].includes(entry[0]) || state.user?.role_code === "admin"); }
function actionName(value) { const alias = { user_approve: "registration_approve", user_reject: "registration_reject", submission_resubmit: "submission_update" }; const key = `action.${alias[value] || value}`; return hasMessage(key) ? t(key) : L(actionNames[value] || String(value || "—")); }
function roleName(user) { return hasMessage(`role.${user.role_code}`) ? t(`role.${user.role_code}`) : L(roleNames[user.role_code] || user.role_label || user.role_code || "—"); }
function utc(value) {
  return date(value, true);
}
function shortUtc(value) { return utc(value).replace(" UTC", ""); }
function byteSize(bytes) { return new Intl.NumberFormat(locale, { style: "unit", unit: bytes < 1024 ? "byte" : bytes < 1024 * 1024 ? "kilobyte" : "megabyte", maximumFractionDigits: 1 }).format(bytes < 1024 ? bytes : bytes < 1024 * 1024 ? bytes / 1024 : bytes / 1024 / 1024); }
function badge(value, type) {
  const text = hasMessage(`status.${value}`) ? t(`status.${value}`) : L(outcomeNames[value] || statusNames[value] || value);
  const inferred = type || (["success", "active"].includes(value) ? "success" : ["failure", "blocked", "inactive"].includes(value) ? "danger" : ["denied", "pending"].includes(value) ? "warning" : "neutral");
  return el("span", `badge ${inferred}`, text);
}
function message(text, kind = "") { return el("div", `inline-message ${kind}`, text); }
function empty(title = "Жазба табылмады", description = "Сүзгілерді өзгертіңіз немесе кейінірек қайта тексеріңіз.") {
  const box = el("div", "empty-state"); box.append(el("div", "empty-mark", "≡"), el("h3", "", title), el("p", "", description)); return box;
}
function field(label, control, hint) {
  const wrap = el("div", "field"); const id = control.id || `field-${crypto.randomUUID()}`; control.id = id;
  const caption = el("label", "", label); caption.htmlFor = id; wrap.append(caption, control);
  if (hint) wrap.append(el("p", "field-hint", hint)); return wrap;
}
function input(type = "text", value = "") { const item = el("input"); item.type = type; item.value = value ?? ""; return item; }
function select(options, value = "", blank = "Барлығы") {
  const item = el("select");
  if (blank !== null) { const option = el("option", "", blank); option.value = ""; item.append(option); }
  for (const option of options) { const label = option.label ?? option.name ?? option.value ?? option.code; const node = option.raw ? dataEl("option", "", label) : el("option", "", label); node.value = String(option.value ?? option.code ?? option.id); item.append(node); }
  item.value = String(value ?? ""); return item;
}
function table(columns, rows, caption) {
  if (!Array.isArray(rows)) throw new ApiError("Сервердегі жазбалар пішімі дұрыс емес. Бетті жаңартыңыз.", "invalid_response", 502);
  if (!rows.length) return empty();
  const wrap = el("div", "table-wrap"); const grid = el("table"); grid.append(el("caption", "sr-only", caption));
  const head = el("thead"); const hr = el("tr");
  for (const column of columns) { const th = el("th", "", column.label); th.scope = "col"; hr.append(th); }
  head.append(hr); grid.append(head); const body = el("tbody");
  for (const row of rows) {
    const tr = el("tr");
    for (const column of columns) { const td = el("td", column.className || ""); const value = column.render(row); if (value instanceof Node) td.append(value); else td.textContent = String(value ?? "—"); tr.append(td); }
    body.append(tr);
  }
  grid.append(body); wrap.append(grid); return wrap;
}
function panel(title, content, description, extra) {
  const box = el("section", "panel");
  if (title) { const head = el("div", "panel-head"); const titleBox = el("div"); titleBox.append(el("h2", "", title)); if (description) titleBox.append(el("p", "panel-description", description)); head.append(titleBox); if (extra) head.append(extra); box.append(head); }
  if (content) box.append(content); return box;
}
function heading(title, subtitle, actions = []) {
  const wrap = el("div", "page-heading"); const copy = el("div"); copy.append(el("p", "eyebrow", "КАФЕДРА / ЖҰМЫС КЕҢІСТІГІ"), el("h1", "", title), el("p", "", subtitle)); wrap.append(copy);
  if (actions.length) { const controls = el("div", "heading-actions"); controls.append(...actions); wrap.append(controls); } return wrap;
}
function refresh() { return button("↻  Жаңарту", () => void renderRoute(), "button subtle"); }
function toast(text, error = false) {
  const item = el("div", `toast ${error ? "error" : ""}`); item.setAttribute("role", error ? "alert" : "status");
  item.append(el("span", "", text), button("×", () => item.remove(), "")); item.lastChild.setAttribute("aria-label", L("Хабарламаны жабу")); const region = byId("toast-region"); while (region.childElementCount >= 3) region.firstElementChild.remove(); region.append(item); window.setTimeout(() => item.remove(), error ? 11000 : 6500);
}
class ApiError extends Error { constructor(message, code, status) { super(L(message)); this.code = code; this.status = status; } }
function userError(error) { return error instanceof ApiError ? error.message : hasMessage(error?.message) ? L(error.message) : t("error.generic"); }
function csrfToken() { const entry = document.cookie.split(";").map((value) => value.trim()).find((value) => value.startsWith("arm_csrf=")); return entry ? decodeURIComponent(entry.slice("arm_csrf=".length)) : ""; }
async function api(path, options = {}) {
  const method = options.method || "GET"; const headers = new Headers(options.headers || {});
  headers.set("Accept-Language", locale);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  if (["POST", "PATCH", "PUT", "DELETE"].includes(method) && csrfToken()) headers.set("X-CSRF-Token", csrfToken());
  let response;
  try { response = await fetch(path, { ...options, method, headers, credentials: "same-origin", body: options.body instanceof FormData ? options.body : options.body ? JSON.stringify(options.body) : undefined }); }
  catch (error) { if (error instanceof TypeError) throw new ApiError("Серверге қосылу мүмкін болмады. Байланысты тексеріп, қайта көріңіз.", "network_error", 0); throw error; }
  if (options.blob && response.ok) return response.blob();
  let data;
  try { data = response.status === 204 ? {} : await response.json(); }
  catch (error) { if (error instanceof SyntaxError) throw new ApiError("Сервер жауабы оқылмады. Қайта көріңіз.", "invalid_response", response.status); throw error; }
  if (!response.ok) { if (response.status === 401 && state.user && !path.startsWith("/api/auth/login")) showAuth(L("Сессия аяқталды. Жүйеге қайта кіріңіз.")); throw new ApiError(errorMessage(data.error?.code, data.error?.message), data.error?.code, response.status); }
  return data;
}
function query(values) { const params = new URLSearchParams(); Object.entries(values).forEach(([key, value]) => { if (value !== "" && value !== undefined && value !== null) params.set(key, String(value)); }); return params.toString() ? `?${params}` : ""; }
async function registrationOptions() {
  if (!registrationOptionsRequest) { state.registrationOptionsLoading = true; syncRegistrationClaim(); registrationOptionsRequest = api("/api/auth/registration-options").then((data) => { state.registrationOptions = data; if (!state.user && state.authMode === "register") renderAuthClaimFields(); return data; }).finally(() => { registrationOptionsRequest = null; state.registrationOptionsLoading = false; syncRegistrationClaim(); }); }
  return registrationOptionsRequest;
}
function claimFields(prefix, values = {}, options = state.registrationOptions) {
  const root = el("fieldset", "student-claim-fields"); root.append(el("legend", "", t("claim.formTitle")), message(t("claim.unverifiedHint"), "warning"));
  const name = input("text", values.name || ""); name.id = `${prefix}-name`; name.minLength = 2; name.maxLength = 120; name.autocomplete = "name"; name.required = true; name.addEventListener("input", () => { const normalized = name.value.replace(/\s+/gu, " "); if (name.value !== normalized) name.value = normalized; }); name.addEventListener("blur", () => { name.value = name.value.trim().replace(/\s+/gu, " "); });
  const studentNumber = input("text", values.number || ""); studentNumber.id = `${prefix}-number`; studentNumber.minLength = 3; studentNumber.maxLength = 32; studentNumber.pattern = "[A-Za-z0-9][A-Za-z0-9_\\-]{2,31}"; studentNumber.autocomplete = "off"; studentNumber.spellcheck = false; studentNumber.required = true; studentNumber.addEventListener("blur", () => { studentNumber.value = studentNumber.value.trim().toUpperCase(); });
  const group = select((options.groups || []).map((row) => ({ value: row.group_id, label: row.name, raw: true })), values.group_id || "", t("claim.selectGroup")); group.id = `${prefix}-group`; group.required = true;
  const email = input("email", values.email || ""); email.id = `${prefix}-email`; email.maxLength = 254; email.autocomplete = "email"; email.required = Boolean(options.student_email_required);
  root.append(field(t("claim.name"), name), field(t("claim.number"), studentNumber, t("claim.numberHint")), field(t("claim.group"), group), field(t(options.student_email_required ? "claim.emailRequired" : "claim.emailOptional"), email, options.student_email_domain ? t("claim.emailDomain", { domain: options.student_email_domain }) : t("claim.emailHint")));
  const hasGroups = Boolean(options.groups?.length); if (!hasGroups) root.append(message(t("students.noRegistrationGroups"), "warning"));
  const controls = [name, studentNumber, group, email];
  const read = () => ({ name: name.value.trim().replace(/\s+/gu, " "), number: studentNumber.value.trim().toUpperCase(), group_id: Number(group.value) || null, email: email.value.trim() });
  const payload = () => { const data = read(); name.value = data.name; if ([...data.name].length < 2 || [...data.name].length > 120) throw new ApiError(t("error.invalid_request"), "invalid_request", 422); if (!hasGroups || !data.group_id) throw new ApiError(t("error.invalid_student_claim_group"), "invalid_student_claim_group", 422); return { student_claim_name: data.name, student_claim_number: data.number, student_claim_group_id: data.group_id, ...(data.email ? { student_claim_email: data.email } : {}) }; };
  return { root, controls, read, payload, hasGroups };
}
function syncRegistrationClaim() {
  const visible = state.authMode === "register" && byId("requested-role")?.value === "student";
  if (state.authClaim) { state.authClaim.root.hidden = !visible; state.authClaim.controls.forEach((control) => control.disabled = !visible); }
  byId("auth-submit").disabled = visible && (!state.authClaim?.hasGroups || Boolean(state.registrationOptionsLoading)); if (state.authClaim) state.authClaim.root.setAttribute("aria-busy", String(visible && Boolean(state.registrationOptionsLoading)));
}
function renderAuthClaimFields(clear = false) {
  const focused = state.authClaim?.root.contains(document.activeElement) ? document.activeElement.id : null;
  const previous = clear ? {} : state.authClaim?.read() || {};
  const fields = claimFields("student-claim", previous); if (state.authClaim) state.authClaim.root.replaceWith(fields.root); else byId("requested-role-field")?.after(fields.root);
  state.authClaim = fields; syncRegistrationClaim();
  if (focused && !clear) { const control = byId(focused); if (control && !control.disabled) control.focus({ preventScroll: true }); }
}
function claimDetails(claim, masked = false) {
  const details = el("dl", "detail-grid claim-details");
  const rows = [["claim.name", claim?.name], ["claim.number", masked ? claim?.number_masked : claim?.number], ["claim.group", claim?.group_name], ...(!masked && claim?.email ? [["claim.emailRequired", claim.email]] : [])];
  for (const [label, value] of rows) { const pair = el("div"); pair.append(el("dt", "", t(label)), dataEl("dd", "", value || "—")); details.append(pair); }
  return details;
}
function modal({ title, content, submitLabel = null, onSubmit, danger = false, wide = false, onClose, rawTitle = false }) {
  const dialog = el("dialog", wide ? "wide-dialog" : ""); const form = el("form", "dialog-form"); const head = el("div", "dialog-header"); const titleNode = rawTitle ? dataEl("h2", "", title) : el("h2", "", title);
  titleNode.id = `dialog-title-${crypto.randomUUID()}`; dialog.setAttribute("aria-labelledby", titleNode.id);
  const previousFocus = document.activeElement;
  const close = button("×", () => dialog.close(), "dialog-close"); close.setAttribute("aria-label", L("Терезені жабу")); head.append(titleNode, close);
  const body = el("div", "dialog-content"); body.append(content); const error = message("", "error dialog-error"); error.hidden = true; error.setAttribute("role", "alert");
  const footer = el("div", "dialog-footer"); footer.append(button(submitLabel ? "Бас тарту" : "Жабу", () => dialog.close()));
  let submit; if (submitLabel) { submit = el("button", `button ${danger ? "danger" : "primary"}`, submitLabel); submit.type = "submit"; footer.append(submit); }
  form.append(head, body, error, footer); dialog.append(form); document.body.append(dialog); let submitting = false;
  form.addEventListener("submit", async (event) => { event.preventDefault(); if (submitting || !onSubmit) return; error.hidden = true; submitting = true; if (submit) submit.disabled = true; try { await onSubmit(); dialog.close(); } catch (failure) { error.textContent = userError(failure); error.hidden = false; } finally { submitting = false; if (submit) submit.disabled = false; } });
  dialog.addEventListener("close", () => { dialog.remove(); if (previousFocus?.isConnected && !previousFocus.closest("[inert]")) previousFocus.focus({ preventScroll: true }); if (onClose) onClose(); }); dialog.showModal(); dialog.querySelector("input:not([type=checkbox]), select, textarea")?.focus(); return dialog;
}
function confirmation(title, text, onSubmit, submitLabel = "Растау") { const content = el("div"); content.append(el("p", "scope-note", text)); return modal({ title, content, submitLabel, danger: true, onSubmit }); }
function icon(name) {
  const paths = {
    dashboard: ["M3 3h7v7H3z", "M14 3h7v7h-7z", "M3 14h7v7H3z", "M14 14h7v7h-7z"], users: ["M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2", "M16 3a4 4 0 0 1 0 8", "M22 21v-2a4 4 0 0 0-3-3.87", "M12 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0"], shield: ["M12 22s9-4 9-11V4l-9-3-9 3v7c0 7 9 11 9 11", "m9 12 2 2 4-4"], grades: ["M4 3h16v18H4z", "M8 7h8", "M8 12h2", "M14 12h2", "M8 17h2", "M14 17h2"], file: ["M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z", "M14 2v6h6", "M8 13h8", "M8 17h5"], sessions: ["M3 4h18v12H3z", "M8 21h8", "M12 16v5", "M8 9h8"], audit: ["M9 3H5v18h14V3h-4", "M9 2h6v4H9z", "M8 11h8", "M8 15h8", "M8 18h4"], spark: ["m12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3z"],
  };
  const wrap = el("span", "nav-icon"); wrap.setAttribute("aria-hidden", "true"); const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); svg.setAttribute("viewBox", "0 0 24 24");
  for (const path of paths[name] || paths.file) { const item = document.createElementNS(svg.namespaceURI, "path"); item.setAttribute("d", path); svg.append(item); } wrap.append(svg); return wrap;
}

function setAuthMode(mode) {
  state.authMode = mode; const registering = mode === "register";
  byId("auth-title").textContent = L(registering ? "Тіркелгі жасау" : "Жүйеге кіру"); byId("auth-description").textContent = registering ? t("auth.registrationIntro") : L("Жұмыс кеңістігіне кіру үшін тіркелгіңізді пайдаланыңыз.");
  byId("login-tab").setAttribute("aria-selected", String(!registering)); byId("register-tab").setAttribute("aria-selected", String(registering)); byId("display-name-field").hidden = !registering; byId("display-name").required = registering;
  byId("password-hint").hidden = !registering; byId("registration-note").hidden = !registering; byId("registration-note").textContent = t("auth.registrationNote"); byId("password").autocomplete = registering ? "new-password" : "current-password"; byId("auth-submit").textContent = L(registering ? "Тіркелуге өтінім беру" : "Кіру"); byId("auth-message").hidden = true;
  byId("requested-role-field").hidden = !registering;
  syncRegistrationClaim();
  if (registering) void registrationOptions().catch((error) => { if (!state.user && state.authMode === "register") { byId("auth-message").textContent = userError(error); byId("auth-message").hidden = false; } });
}
function showAuth(text) {
  state.user = null; state.students = { page: 1 }; state.grades = {}; state.teachers = { view: "requests", status: "pending", page: 1 }; state.workload = null; state.ai = null; state.load += 1; byId("page-content").replaceChildren(); byId("toast-region").replaceChildren(); byId("startup").hidden = true; byId("app-screen").hidden = true; byId("auth-screen").hidden = false; document.querySelectorAll("dialog").forEach((dialog) => dialog.close()); setAuthMode("login"); byId("password").value = "";
  if (text) { byId("auth-message").textContent = text; byId("auth-message").className = "inline-message error"; byId("auth-message").hidden = false; }
}
function enterApp(user) {
  if (state.user?.user_id !== user.user_id) { state.students = { page: 1 }; state.grades = {}; state.teachers = { view: "requests", status: "pending", page: 1 }; state.workload = null; } state.user = user; byId("page-content").replaceChildren(); byId("startup").hidden = true; byId("auth-screen").hidden = true; byId("app-screen").hidden = false; byId("password").value = "";
  byId("app-screen").classList.toggle("restricted", ["pending", "rejected"].includes(user.approval_status));
  byId("current-user-name").textContent = user.display_name || user.login; byId("current-user-role").textContent = roleName(user); byId("current-user-login").textContent = user.login; byId("user-avatar").textContent = (user.display_name || user.login).split(/\s+/).slice(0, 2).map((part) => part[0] || "").join("").toUpperCase();
  const menu = byId("main-navigation"); menu.replaceChildren();
  for (const entry of nav) { const [route, title, , glyph] = entry; if (!routeAllowed(entry)) continue; const item = link("", `#${route}`, `nav-link ${route === "assistant" ? "ai-nav" : ""}`); item.append(icon(glyph), el("span", "", title)); item.dataset.route = route; menu.append(item); }
  updateLanguageControls();
  void renderRoute();
}
function toggleNav(open) { const mobile = window.matchMedia("(max-width: 760px)").matches; byId("sidebar").classList.toggle("open", open); byId("sidebar").inert = mobile && !open; byId("nav-overlay").hidden = !open || !mobile; byId("nav-toggle").setAttribute("aria-expanded", String(open)); }
byId("login-tab").addEventListener("click", () => setAuthMode("login")); byId("register-tab").addEventListener("click", () => setAuthMode("register"));
byId("password-toggle").addEventListener("click", () => { const visible = byId("password").type === "password"; byId("password").type = visible ? "text" : "password"; byId("password-toggle").textContent = L(visible ? "Жасыру" : "Көрсету"); byId("password-toggle").setAttribute("aria-pressed", String(visible)); byId("password-toggle").setAttribute("aria-label", L(visible ? "Құпиясөзді жасыру" : "Құпиясөзді көрсету")); });
byId("auth-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const submit = byId("auth-submit"); if (submit.disabled) return; submit.disabled = true; const mode = state.authMode; byId("auth-message").hidden = true;
  try { const body = { login: byId("login").value.trim(), password: byId("password").value }; if (mode === "register") { body.display_name = byId("display-name").value.trim(); body.requested_role = byId("requested-role").value; if (body.requested_role === "student") Object.assign(body, state.authClaim.payload()); } const data = await api(`/api/auth/${mode}`, { method: "POST", body }); if (mode === "register") { setAuthMode("login"); renderAuthClaimFields(true); byId("password").value = ""; byId("auth-message").textContent = t("auth.registered"); byId("auth-message").className = "inline-message"; byId("auth-message").hidden = false; } else enterApp(data.user); }
  catch (error) { byId("auth-message").textContent = userError(error); byId("auth-message").className = "inline-message error"; byId("auth-message").hidden = false; } finally { submit.disabled = false; if (state.authMode === "register") syncRegistrationClaim(); }
});
byId("logout-button").addEventListener("click", async () => { const control = byId("logout-button"); control.disabled = true; try { await api("/api/auth/logout", { method: "POST", body: {} }); byId("user-menu").open = false; showAuth(); } catch (error) { toast(userError(error), true); } finally { control.disabled = false; } });
byId("nav-toggle").addEventListener("click", () => { const open = !byId("sidebar").classList.contains("open"); toggleNav(open); if (open) byId("main-navigation").querySelector("a")?.focus(); });
byId("nav-overlay").addEventListener("click", () => { toggleNav(false); byId("nav-toggle").focus(); });
byId("main-navigation").addEventListener("click", (event) => { if (event.target.closest("a")) toggleNav(false); });
document.querySelector(".skip-link").addEventListener("click", (event) => { event.preventDefault(); if (state.user) byId("page-content").focus(); else byId("login").focus(); });
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && byId("sidebar").classList.contains("open")) { toggleNav(false); byId("nav-toggle").focus(); } });
window.addEventListener("resize", () => toggleNav(false)); window.addEventListener("hashchange", () => { if (state.user) void renderRoute(); });

function auditColumns(compact = false) {
  const columns = [
    { label: "ID / УАҚЫТ (UTC)", render: (row) => { const cell = el("div"); cell.append(button(`#${row.audit_id}`, () => void auditDetail(row.audit_id), "button text mono"), el("small", "mono", shortUtc(row.occurred_at))); return cell; } },
    { label: "ПАЙДАЛАНУШЫ", render: (row) => { const cell = el("div"); cell.append(el("strong", "", row.actor_login || row.user_id), el("small", "", roleName({ role_code: row.role }))); return cell; } },
    { label: "ӘРЕКЕТ", render: (row) => actionName(row.action), className: "cell-wrap" }, { label: "НӘТИЖЕ", render: (row) => badge(row.result) },
  ];
  if (!compact) { columns.splice(2, 0, { label: "IP-МЕКЕНЖАЙ", render: (row) => el("span", "mono", row.ip_address || "—") }); columns.push({ label: "НЫСАН", render: (row) => `${row.object_type} · ${row.object_id}` }); } return columns;
}
async function dashboardPage() {
  const [data, workload] = await Promise.all([api("/api/dashboard"), teacherWorkload()]); const view = el("div"); view.append(heading("Басқару панелі", "Кафедрадағы қолжетімділік, оқу деректері және соңғы әрекеттердің шолуы.", [refresh()])); const cards = el("div", "kpi-grid");
  const metrics = [["users", "Пайдаланушылар", "users", "◉"], ["active_users", "Белсенді тіркелгілер", "users", "✓"], ["students", "Студенттер", "students", "⌘"], ["documents", "Құжаттар", "documents", "▤"], ["active_sessions", "Белсенді сессиялар", "sessions", "▣"], ["suspicious_sessions", "Күдікті сессиялар", "sessions", "!"], ["audit_events", "Аудит оқиғалары", "audit", "≡"]];
  for (const [key, label, route, glyph] of metrics) { const value = data.kpis?.[key]; if (value === null || value === undefined) continue; const allowed = nav.some((entry) => entry[0] === route && routeAllowed(entry)); const card = allowed ? link("", `#${route}`, "kpi-card") : el("div", "kpi-card"); if (key === "suspicious_sessions" && value > 0) card.classList.add("warning-kpi"); card.append(el("p", "", label), el("span", "kpi-icon", glyph), el("strong", "kpi-value", number(value))); cards.append(card); }
  if (cards.childElementCount) view.append(cards); const columns = el("div", "dashboard-columns"); const main = el("div"); const side = el("div");
  if (can("Audit.View")) main.append(panel("Соңғы әрекеттер", table(auditColumns(true), data.recent_audit || [], "Соңғы аудит оқиғалары"), "Жүйеде тіркелген соңғы оқиғалар", link("Барлық журнал  →", "#audit", "button text"))); else main.append(panel("Жұмыс кеңістігі", empty("Қолжетімді бөлімді таңдаңыз", "Рөліңізге берілген бөлімдер сол жақтағы мәзірде көрсетілген.")));
  const quick = el("div", "quick-links"); const shortcuts = [["tasks", t("learning.title"), t("learning.subtitle"), "▤"], ["grades", "Ведомостар", "Бағаларды қарап, өзгеріс тарихын ашу", "▤"], ["documents", "Құжаттар", "Оқу материалдарымен жұмыс істеу", "▱"], ["users", "Пайдаланушылар", "Тіркелгілер мен рөлдерді басқару", "◉"], ["sessions", "Сессияларды бақылау", "Күдікті белгілерді тексеру", "◈"]];
  for (const [route, label, description, glyph] of shortcuts) { if (!nav.some(([path, , permission]) => path === route && can(permission))) continue; const item = link("", `#${route}`, "quick-link"); const copy = el("div"); copy.append(el("strong", "", label), el("small", "", description)); item.append(el("span", "", glyph), copy, el("span", "arrow", "→")); quick.append(item); }
  if (quick.childElementCount) side.append(panel("Жылдам өту", quick)); if (workload) side.append(workloadPanel(workload)); else if (data.my_assignments?.length) { const list = el("ul", "assignment-list"); for (const item of data.my_assignments) list.append(el("li", "", typeof item === "string" ? item : [item.group_name, item.subject_name, item.term_name].filter(Boolean).join(" · "))); side.append(panel("Маған бекітілген", list)); } columns.append(main, side); view.append(columns); return view;
}

async function usersPage() {
  const [data, rolesData] = await Promise.all([api(`/api/admin/users${query(state.users)}`), api("/api/admin/roles")]); const roles = rolesData.roles; const view = el("div"); view.append(heading("Пайдаланушылар", "Тіркелгілерді табыңыз, рөл беріңіз және жаңа өтінімдерді растаңыз.", [refresh()]));
  const box = panel("Пайдаланушылар тізімі", null, t("count.accounts", { count: number(data.total) })); const filters = el("form", "filter-bar"); const search = input("search", state.users.q); search.placeholder = L("Аты-жөні немесе логин");
  const role = select(roles.map((row) => ({ value: row.code, label: row.label })), state.users.role_code, "Барлық рөл"); const active = select([{ value: "true", label: "Белсенді" }, { value: "false", label: "Белсенді емес" }], state.users.active, "Барлық күй");
  const searchField = field("Іздеу", search); searchField.classList.add("wide"); filters.append(searchField, field("Рөл", role), field("Тіркелгі күйі", active)); const apply = el("button", "button primary", "Іздеу"); apply.type = "submit"; filters.append(apply);
  filters.addEventListener("submit", (event) => { event.preventDefault(); state.users = { q: search.value.trim(), role_code: role.value, active: active.value }; void renderRoute(); });
  box.append(filters, table([
    { label: "ПАЙДАЛАНУШЫ", render: (row) => { const cell = el("div"); cell.append(dataEl("strong", "", row.display_name), el("small", "", row.login)); return cell; }, className: "cell-wrap" }, { label: "РӨЛ", render: (row) => roleName(row) }, { label: "КҮЙІ", render: (row) => badge(["pending", "rejected"].includes(row.approval_status) ? row.approval_status : row.active ? "active" : "inactive") },
    { label: "ӘРЕКЕТТЕР", render: (row) => { const actions = el("div", "table-actions"); if (state.user.role_code !== "admin") { actions.append(el("span", "muted", "—")); return actions; } if (row.approval_status && row.approval_status !== "approved") actions.append(button(t("requests.approve"), () => void approveRequest(row), "button small"), button(t("requests.reject"), () => rejectRequest(row), "button small text danger-text")); else actions.append(button("Өзгерту", () => void editUser(row, roles), "button small")); if (row.role_code === "teacher" && row.approval_status !== "pending") actions.append(button("Пән бекіту", () => void assignmentsDialog(row), "button small text")); return actions; } },
  ], data.items, "Пайдаланушылар")); view.append(box); return view;
}
async function editUser(user, roles) {
  if (user.approval_status && user.approval_status !== "approved") { await approveRequest(user); return; }
  try { const catalog = await api("/api/academic/catalog"); const content = el("div"); content.append(dataEl("p", "scope-note", `${user.display_name} · ${user.login}`)); const role = select(roles.filter((item) => item.code !== "pending").map((item) => ({ value: item.code, label: t(`role.${item.code}`) })), user.role_code, null); const active = input("checkbox"); active.checked = Boolean(user.active); const activeLabel = el("label", "checkbox-label"); activeLabel.append(active, el("span", "", "Тіркелгі белсенді"));
    const student = studentChoice(catalog, user.student_id); const linked = field(t("requests.student"), student, t("requests.studentHint")); const sync = () => { linked.hidden = role.value !== "student"; student.required = role.value === "student"; }; role.addEventListener("change", sync); sync(); content.append(field(L("Рөл"), role), linked, activeLabel);
    modal({ title: L("Тіркелгіні басқару"), content, submitLabel: L("Өзгерістерді сақтау"), onSubmit: async () => { await api(`/api/admin/users/${user.user_id}`, { method: "PATCH", body: { role_code: role.value, active: active.checked, student_id: role.value === "student" ? Number(student.value) : null } }); toast(L("Тіркелгі өзгертілді.")); if (user.user_id === state.user.user_id) { const me = await api("/api/auth/me"); enterApp(me.user); } else await renderRoute(); } });
  } catch (error) { toast(userError(error), true); }
}
function catalogOptions(items, type) { return (items || []).map((item) => { const label = item.name ?? item[`${type}_name`] ?? item.label ?? item.code; return { value: item[`${type}_id`] ?? item.id, raw: type === "group", label: type === "term" && item.academic_year ? `${L(label)} · ${item.academic_year}` : label }; }); }
function catalogActive(row) { return row?.active !== false && row?.active !== 0; }
function activeCatalogOptions(items, type, current) { return catalogOptions((items || []).filter((row) => catalogActive(row) || String(row[`${type}_id`]) === String(current)), type).map((option) => ({ ...option, label: `${option.raw ? option.label : L(option.label)}${!catalogActive((items || []).find((row) => String(row[`${type}_id`]) === String(option.value))) ? ` · ${t("status.archived")}` : ""}` })); }
function assignmentText(row) { return `${row.group_name} · ${L(row.subject_name)} · ${L(row.term_name)}${row.academic_year ? ` · ${row.academic_year}` : ""}`; }
async function teacherWorkload() { if (state.user?.role_code !== "teacher" || !(can("Grades.View") || can("Learning.View"))) return null; const data = await api("/api/academic/workload"); state.workload = data; return data; }
function workloadPanel(data) { if (!data) return null; const body = el("div", "panel-body workload-body"); if (!data.items.length) body.append(message(t("teachers.noAssignments"), "warning")); else { const list = el("ul", "assignment-list"); for (const row of data.items) list.append(dataEl("li", "", assignmentText(row))); body.append(list); } return panel(t("teachers.myWorkload"), body, t("teachers.workloadHint")); }
function pageTabs(items, current, onChange) { const tabs = el("div", "page-tabs"); tabs.setAttribute("role", "group"); for (const [value, label] of items) { const control = button(t(label), () => onChange(value), `button ${value === current ? "primary" : "subtle"}`); control.setAttribute("aria-pressed", String(value === current)); control.dataset.view = value; tabs.append(control); } return tabs; }
function pagePagination(data, onPage, totalLabel) { const pages = Math.max(1, Math.ceil(data.total / data.page_size)); const box = el("div", "pagination"); const controls = el("div", "pagination-controls"); const previous = button("←  Алдыңғы", () => onPage(data.page - 1), "button small"); previous.disabled = data.page <= 1; const next = button("Келесі  →", () => onPage(data.page + 1), "button small"); next.disabled = data.page >= pages; controls.append(previous, el("span", "", `${number(data.page)} / ${number(pages)}`), next); box.append(el("span", "", totalLabel), controls); return box; }
async function teachersPage() {
  const data = await api(`/api/admin/teachers${query({ ...state.teachers, page_size: 25 })}`); const requests = state.teachers.view === "requests"; const view = el("div"); view.append(heading(t("teachers.title"), t("teachers.subtitle"), [refresh()]), pageTabs([["requests", "teachers.requests"], ["teachers", "teachers.list"]], state.teachers.view, (value) => { state.teachers = { view: value, status: value === "requests" ? "pending" : "active", page: 1 }; void renderRoute(); }));
  const filters = el("form", "filter-bar teacher-filters"); const search = input("search", state.teachers.q); search.id = "teachers-search"; search.maxLength = 120; const status = select((requests ? ["pending", "rejected", "all"] : ["active", "inactive", "all"]).map((value) => ({ value, label: value === "all" ? L("Барлық күй") : t(`status.${value}`) })), state.teachers.status, null); status.id = "teachers-status"; const group = select(catalogOptions(data.groups, "group"), state.teachers.group_id, t("students.allGroups")); group.id = "teachers-group"; const subject = select(catalogOptions(data.subjects, "subject"), state.teachers.subject_id, L("Барлық пән")); subject.id = "teachers-subject"; group.disabled = requests; subject.disabled = requests; const apply = el("button", "button primary", L("Қолдану")); apply.type = "submit"; filters.append(field(L("Аты-жөні немесе логин"), search), field(L("Күйі"), status), field(L("Оқу тобы"), group), field(L("Пән"), subject), apply); filters.addEventListener("submit", (event) => { event.preventDefault(); state.teachers = { view: state.teachers.view, status: status.value, q: search.value.trim(), group_id: group.value, subject_id: subject.value, page: 1 }; void renderRoute(); });
  const box = panel(t(requests ? "teachers.requests" : "teachers.list"), null, t("count.accounts", { count: number(data.total) })); box.append(filters, table([
    { label: L("ПАЙДАЛАНУШЫ"), render: (row) => { const cell = el("div"); cell.append(dataEl("strong", "", row.display_name), dataEl("small", "mono", row.login)); return cell; }, className: "cell-wrap" }, { label: L("Күйі"), render: (row) => badge(row.approval_status === "approved" ? row.active ? "active" : "inactive" : row.approval_status) },
    { label: t("teachers.dates"), render: (row) => { const cell = el("div"); cell.append(dataEl("small", "", `${t("teachers.registered")}: ${utc(row.created_at)}`)); if (row.approved_at) cell.append(dataEl("small", "", `${t("teachers.approved")}: ${utc(row.approved_at)}`)); return cell; } }, { label: t("teachers.assignments"), render: (row) => { const list = el("div", "teacher-assignment-list"); for (const item of row.assignments || []) { const line = dataEl("p", "", assignmentText(item)); if (!item.active) line.append(document.createTextNode(` · ${t("status.inactive")}`)); list.append(line); } if (!list.childElementCount) list.append(el("span", "muted", t("teachers.none"))); return list; }, className: "cell-wrap" },
    { label: L("ӘРЕКЕТТЕР"), render: (row) => { const actions = el("div", "table-actions"); actions.append(button(t("teachers.profile"), () => void teacherProfile(row.user_id), "button small")); if (requests) actions.append(button(t("requests.approve"), () => teacherApprove(row), "button small text"), button(t("requests.reject"), () => teacherReject(row), "button small text danger-text")); return actions; } },
  ], data.items, t("teachers.title")), pagePagination(data, (page) => { state.teachers.page = page; void renderRoute(); }, t("count.accounts", { count: number(data.total) }))); view.append(box); return view;
}
function teacherApprove(user, onSaved) { const content = el("div"); content.append(dataEl("p", "scope-note", `${user.display_name} · ${user.login}`), message(t("teachers.approveHint"))); modal({ title: t("teachers.approveTitle"), content, submitLabel: t("requests.approve"), onSubmit: async () => { await api(`/api/admin/teachers/${user.user_id}/approve`, { method: "POST", body: {} }); toast(t("requests.approved")); await renderRoute(); if (onSaved) await onSaved(); else await teacherProfile(user.user_id); } }); }
function teacherReject(user, onSaved) { const content = el("div"); content.append(dataEl("p", "scope-note", `${user.display_name} · ${user.login}`)); const reason = el("textarea"); reason.required = true; reason.minLength = 5; reason.maxLength = 300; content.append(field(t("requests.reason"), reason)); modal({ title: t("requests.rejectTitle"), content, submitLabel: t("requests.reject"), danger: true, onSubmit: async () => { await api(`/api/admin/teachers/${user.user_id}/reject`, { method: "POST", body: { reason: reason.value.trim() } }); toast(t("requests.rejected")); await renderRoute(); if (onSaved) await onSaved(); } }); }
async function teacherProfile(id) {
  try { const data = await api(`/api/admin/teachers/${id}`); const user = data.teacher; const content = el("div"); let dialog; const reload = async () => { dialog.close(); await renderRoute(); await teacherProfile(id); }; const info = el("dl", "detail-grid"); for (const [label, value] of [[t("claim.accountName"), user.display_name], [L("Логин"), user.login], [L("Рөл"), roleName(user)], [L("Күйі"), t(`status.${user.approval_status === "approved" ? user.active ? "active" : "inactive" : user.approval_status}`)], [t("teachers.registered"), utc(user.created_at)], [t("teachers.approved"), utc(user.approved_at)]]) { const pair = el("div"); pair.append(el("dt", "", label), dataEl("dd", "", value)); info.append(pair); } content.append(info);
    const actions = el("div", "task-actions"); if (user.approval_status !== "approved") actions.append(button(t("requests.approve"), () => teacherApprove(user, reload), "button primary"), button(t("requests.reject"), () => teacherReject(user, reload), "button danger")); else { if (user.active) actions.append(button(t("teachers.addAssignment"), () => teacherAssignmentEditor(data, null, reload), "button primary")); else content.append(message(t("teachers.activateToAssign"), "warning")); actions.append(button(t(user.active ? "teachers.deactivate" : "teachers.activate"), () => confirmation(t(user.active ? "teachers.deactivate" : "teachers.activate"), t("teachers.accountStateHint"), async () => { await api(`/api/admin/teachers/${id}`, { method: "PATCH", body: { active: !user.active } }); toast(t("teachers.saved")); await renderRoute(); await reload(); }), "button")); } content.append(actions, el("h3", "detail-subtitle", t("teachers.assignments")), message(t("teachers.exactHint")));
    content.append(table([{ label: t("teachers.exactAssignment"), render: (row) => dataEl("span", "", assignmentText(row)), className: "cell-wrap" }, { label: L("Күйі"), render: (row) => badge(row.active ? "active" : "inactive") }, { label: L("ӘРЕКЕТТЕР"), render: (row) => { const controls = el("div", "table-actions"); controls.append(button(L("Өзгерту"), () => teacherAssignmentEditor(data, row, reload), "button small"), button(L("Қайтарып алу"), () => confirmation(L("Бекітуді қайтарып алу"), t("teachers.revokeHint"), async () => { await api("/api/academic/assignments", { method: "PUT", body: { user_id: id, subject_id: row.subject_id, group_id: row.group_id, term_id: row.term_id, assigned: false } }); toast(L("Бекіту қайтарылды.")); await reload(); }, L("Қайтарып алу")), "button small danger-text")); return controls; } }], data.assignments, t("teachers.assignments"))); dialog = modal({ title: t("teachers.profile"), content, wide: true });
  } catch (error) { toast(userError(error), true); }
}
function teacherAssignmentEditor(data, assignment, onSaved) {
  const content = el("div"); content.append(message(t("teachers.exactHint"))); const group = select(activeCatalogOptions(data.groups, "group", assignment?.group_id), assignment?.group_id, L("Топты таңдаңыз")); const subject = select(activeCatalogOptions(data.subjects, "subject", assignment?.subject_id), assignment?.subject_id, L("Пәнді таңдаңыз")); const term = select(activeCatalogOptions(data.terms, "term", assignment?.term_id), assignment?.term_id, L("Кезеңді таңдаңыз")); [group, subject, term].forEach((control) => control.required = true); content.append(field(L("Оқу тобы"), group), field(L("Пән"), subject), field(L("Оқу кезеңі"), term)); modal({ title: t(assignment ? "teachers.editAssignment" : "teachers.addAssignment"), content, submitLabel: L("Сақтау"), onSubmit: async () => { const body = { subject_id: Number(subject.value), group_id: Number(group.value), term_id: Number(term.value) }; await api(assignment ? `/api/academic/assignments/${assignment.assignment_id}` : "/api/academic/assignments", { method: assignment ? "PATCH" : "PUT", body: assignment ? body : { ...body, user_id: data.teacher.user_id, assigned: true } }); toast(t("teachers.assignmentSaved")); if (onSaved) await onSaved(); } });
}
async function catalogsPage() {
  const data = await api("/api/admin/catalogs"); const kind = state.catalogs; const type = { groups: "group", subjects: "subject", terms: "term" }[kind]; const view = el("div"); view.append(heading(t("catalogs.title"), t("catalogs.subtitle"), [button(t("catalogs.add"), () => catalogEditor(kind), "button primary"), refresh()]), pageTabs([["groups", "catalogs.groups"], ["subjects", "catalogs.subjects"], ["terms", "catalogs.terms"]], kind, (value) => { state.catalogs = value; void renderRoute(); }), message(t("catalogs.archiveHint")));
  const columns = [{ label: t("catalogs.name"), render: (row) => dataEl("strong", "", kind === "groups" ? row.name : L(row.name)) }, ...(kind === "terms" ? [{ label: t("catalogs.academicYear"), render: (row) => dataEl("span", "", row.academic_year) }] : []), { label: L("Күйі"), render: (row) => badge(row.active ? "active" : "archived") }, { label: L("ӘРЕКЕТТЕР"), render: (row) => { const controls = el("div", "table-actions"); controls.append(button(L("Өзгерту"), () => catalogEditor(kind, row), "button small"), button(t(row.active ? "catalogs.archive" : "catalogs.restore"), () => confirmation(t(row.active ? "catalogs.archive" : "catalogs.restore"), t("catalogs.archiveHint"), async () => { await api(`/api/admin/catalogs/${kind}/${row[`${type}_id`]}`, { method: "PATCH", body: { active: !row.active } }); toast(t("catalogs.saved")); await renderRoute(); }), "button small")); return controls; } }]; view.append(panel(t(`catalogs.${kind}`), table(columns, data[kind], t(`catalogs.${kind}`)))); return view;
}
function catalogEditor(kind, item) { const type = { groups: "group", subjects: "subject", terms: "term" }[kind]; const name = input("text", item?.name); name.required = true; name.minLength = 2; name.maxLength = 120; const year = input("text", item?.academic_year); year.required = kind === "terms"; year.minLength = 2; year.maxLength = 32; const content = el("div"); content.append(field(t("catalogs.name"), name)); if (kind === "terms") content.append(field(t("catalogs.academicYear"), year)); modal({ title: t(item ? "catalogs.edit" : "catalogs.add"), content, submitLabel: L("Сақтау"), onSubmit: async () => { await api(item ? `/api/admin/catalogs/${kind}/${item[`${type}_id`]}` : `/api/admin/catalogs/${kind}`, { method: item ? "PATCH" : "POST", body: { name: name.value.trim().replace(/\s+/gu, " "), ...(kind === "terms" ? { academic_year: year.value.trim().replace(/\s+/gu, " ") } : {}) } }); toast(t("catalogs.saved")); await renderRoute(); } }); }
async function assignmentsDialog(user) {
  try {
    const [catalog, data] = await Promise.all([api("/api/academic/catalog"), api(`/api/academic/assignments${query({ user_id: user.user_id })}`)]); const content = el("div"); content.append(el("p", "scope-note", t("template.assignmentContext", { name: user.display_name }))); const list = el("div"); content.append(list); const assigned = (data.items || []).filter((row) => String(row.user_id) === String(user.user_id));
    const paintAssignments = () => { list.replaceChildren(table([
      { label: "ТОП / ПӘН / КЕЗЕҢ", render: (row) => `${row.group_name || row.group_id} · ${L(row.subject_name) || row.subject_id} · ${L(row.term_name) || row.term_id}`, className: "cell-wrap" },
      { label: "", render: (row) => button("Қайтарып алу", () => confirmation("Бекітуді қайтарып алу", "Оқытушы осы топ пен пән бағаларын өзгерту құқығынан айырылады.", async () => { await api("/api/academic/assignments", { method: "PUT", body: { user_id: user.user_id, group_id: row.group_id, subject_id: row.subject_id, term_id: row.term_id, assigned: false } }); assigned.splice(assigned.indexOf(row), 1); paintAssignments(); toast("Бекіту қайтарылды."); }, "Қайтарып алу"), "button small text danger-text") },
    ], assigned, "Оқытушыға бекітілген пәндер")); }; paintAssignments();
    const group = select(activeCatalogOptions(catalog.groups, "group"), "", "Топты таңдаңыз"); group.required = true; const subject = select(activeCatalogOptions(catalog.subjects, "subject"), "", "Пәнді таңдаңыз"); subject.required = true; const term = select(activeCatalogOptions(catalog.terms, "term"), "", "Кезеңді таңдаңыз"); term.required = true;
    content.append(field("Оқу тобы", group), field("Пән", subject), field("Оқу кезеңі", term)); modal({ title: "Оқытушыға пән бекіту", content, wide: true, submitLabel: "Бекіту", onSubmit: async () => { await api("/api/academic/assignments", { method: "PUT", body: { user_id: user.user_id, group_id: Number(group.value), subject_id: Number(subject.value), term_id: Number(term.value), assigned: true } }); toast("Пән оқытушыға бекітілді."); } });
  } catch (error) { toast(userError(error), true); }
}
async function rolesPage() {
  const data = await api("/api/admin/roles"); const view = el("div"); view.append(heading("Рөлдер мен құқықтар", "Әр рөлдің қолжетімділігін басқарыңыз. Өзгеріс барлық тиісті тіркелгілерге қолданылады.", [refresh()])); const grid = el("table", "permission-matrix"); grid.append(el("caption", "sr-only", "Рөлдер мен құқықтар матрицасы")); const head = el("thead"); const tr = el("tr"); tr.append(el("th", "", "ҚҰҚЫҚ / РӨЛ")); const controls = new Map();
  for (const role of data.roles) { const th = el("th", "", role.label); th.scope = "col"; th.append(button("Сақтау", () => { const chosen = [...controls.get(role.code)].filter(([, checkbox]) => checkbox.checked).map(([permission]) => permission); confirmation("Рөл құқықтарын өзгерту", t("template.roleChange", { role: t(`role.${role.code}`), count: number(chosen.length) }), async () => { await api(`/api/admin/roles/${encodeURIComponent(role.code)}/permissions`, { method: "PUT", body: { permissions: chosen } }); const me = await api("/api/auth/me"); toast("Рөлдің құқықтары сақталды."); enterApp(me.user); }, "Өзгерісті сақтау"); }, "button small")); controls.set(role.code, new Map()); tr.append(th); }
  head.append(tr); grid.append(head); const body = el("tbody");
  for (const permission of data.permissions) { const row = el("tr"); const label = el("td", "permission-label", t(`permission.${permission.code}`)); label.append(el("small", "mono", permission.code)); row.append(label); for (const role of data.roles) { const td = el("td"); const check = input("checkbox"); check.checked = role.permissions.includes(permission.code); check.setAttribute("aria-label", `${t(`role.${role.code}`)}: ${t(`permission.${permission.code}`)}`); controls.get(role.code).set(permission.code, check); td.append(check); row.append(td); } body.append(row); }
  grid.append(body); const wrap = el("div", "table-wrap"); wrap.append(grid); view.append(panel("Құқықтар матрицасы", wrap, "Әр бағанды жеке сақтаңыз. Соңғы әкімшінің басқару құқығы қорғалған.")); return view;
}

async function gradesPage() {
  let resetFilters = false; const loadGrades = async () => { try { return await api(`/api/grades${query(state.grades)}`); } catch (error) { if (!Object.values(state.grades).some(Boolean) || ![403, 422].includes(error.status)) throw error; state.grades = {}; resetFilters = true; return api("/api/grades"); } }; const [catalog, data, workload] = await Promise.all([api("/api/academic/catalog"), loadGrades(), teacherWorkload()]); const view = el("div"); view.append(heading("Ведомостар мен бағалар", "Студенттердің нәтижелерін қарап, өзгеріс авторы мен тарихын қадағалаңыз.", [refresh()])); if (resetFilters) view.append(message(t("grades.filtersReset"), "warning")); if (workload && !workload.items.length) view.append(message(t("teachers.noAssignments"), "warning"));
  if (can("Grades.Edit")) view.append(el("p", "scope-note", "Бағаны өзгерту тек сізге берілген топ, пән және оқу кезеңінде қолжетімді. Әр өзгеріс журналға жазылады."));
  const box = panel("Бағалар тізімі", null, t("count.records", { count: number(data.total) })); const filters = el("form", "filter-bar"); const group = select(catalogOptions(catalog.groups, "group"), state.grades.group_id, "Барлық топ"); const subject = select(catalogOptions(catalog.subjects, "subject"), state.grades.subject_id, "Барлық пән"); const term = select(catalogOptions(catalog.terms, "term"), state.grades.term_id, "Барлық кезең");
  filters.append(field("Оқу тобы", group), field("Пән", subject), field("Оқу кезеңі", term)); const apply = el("button", "button primary", "Қолдану"); apply.type = "submit"; filters.append(apply);
  filters.addEventListener("submit", (event) => { event.preventDefault(); state.grades = { group_id: group.value, subject_id: subject.value, term_id: term.value }; void renderRoute(); });
  box.append(filters, table([
    { label: "СТУДЕНТ", render: (row) => { const cell = el("div"); cell.append(dataEl("strong", "", row.student_name), el("small", "mono", row.student_code)); return cell; }, className: "cell-wrap" }, { label: "ТОП", render: (row) => row.group_name },
    { label: "ПӘН / КЕЗЕҢ", render: (row) => { const cell = el("div"); cell.append(el("span", "", L(row.subject_name)), el("small", "", L(row.term_name))); return cell; }, className: "cell-wrap" }, { label: "БАҒА", render: (row) => el("strong", "grade-value", row.value ?? "—") },
    { label: "ӘРЕКЕТТЕР", render: (row) => { const actions = el("div", "table-actions"); if (can("Grades.Edit") && row.can_edit) actions.append(button("Өзгерту", () => editGrade(row), "button small")); actions.append(button("Тарих", () => void gradeHistory(row), "button text small")); return actions; } },
  ], data.items, "Студент бағалары")); view.append(box); return view;
}
function editGrade(grade) {
  const content = el("div"); content.append(el("p", "scope-note", t("template.gradeContext", { student: grade.student_name, group: grade.group_name, subject: L(grade.subject_name), value: grade.value === null ? "—" : number(grade.value) }))); const value = input("number", grade.value); value.min = "0"; value.max = "100"; value.step = "1"; value.required = true;
  content.append(field("Жаңа баға", value, "0–100 аралығы. Автор, UTC уақыт және IP өзгеріс тарихында сақталады."));
  modal({ title: "Бағаны өзгерту", content, submitLabel: "Бағаны сақтау", onSubmit: async () => { await api(`/api/grades/${grade.grade_id}`, { method: "PATCH", body: { value: Number(value.value), version: grade.version } }); toast("Баға өзгертіліп, тарихқа жазылды."); await renderRoute(); } });
}
async function gradeHistory(grade) {
  try { const data = await api(`/api/grades/${grade.grade_id}/history`); const content = el("div"); content.append(dataEl("p", "scope-note", `${grade.student_name} · ${L(grade.subject_name)}`));
    content.append(table([{ label: "БҰРЫН", render: (row) => row.old_value ?? "—" }, { label: "КЕЙІН", render: (row) => row.new_value }, { label: "КІМ ӨЗГЕРТТІ", render: (row) => row.actor_login }, { label: "УАҚЫТ (UTC)", render: (row) => el("span", "mono", shortUtc(row.occurred_at)) }, { label: "IP", render: (row) => el("span", "mono", row.ip_address) }, { label: "АУДИТ", render: (row) => can("Audit.View") ? button(`#${row.audit_id}`, () => void auditDetail(row.audit_id), "button text mono") : `#${row.audit_id}` }], data.items, "Баға өзгерістерінің тарихы")); modal({ title: "Бағаның өзгеріс тарихы", content, wide: true });
  } catch (error) { toast(userError(error), true); }
}

async function documentsPage() {
  const [data] = await Promise.all([api("/api/documents"), teacherWorkload()]); const view = el("div"); const actions = [refresh()]; if (can("Documents.Upload")) actions.unshift(button("+  Құжат жүктеу", () => void uploadDocument(), "button primary")); view.append(heading("Құжаттар", "Оқу материалдарын жүктеңіз және рұқсат берілген құжаттарды қараңыз.", actions));
  const distinction = message(t("documents.separateFlow")); if (canCreateTasks() && can("Learning.View")) distinction.append(document.createTextNode(" "), button(t("documents.createTask"), () => { location.hash = "tasks"; void taskEditor(); }, "button small")); view.append(distinction);
  view.append(panel("Құжаттар тізімі", table([
    { label: "ҚҰЖАТ", render: (row) => { const cell = el("div", "file-name"); const copy = el("div"); copy.append(dataEl("strong", "", row.original_name), el("small", "", byteSize(row.size_bytes))); cell.append(el("span", "file-icon", row.original_name.split(".").pop().toUpperCase().slice(0, 4)), copy); return cell; } },
    { label: "ТОП / ПӘН", render: (row) => { const cell = el("div"); cell.append(el("span", "", L(row.subject_name)), dataEl("small", "", `${row.group_name} · ${L(row.term_name)}`)); return cell; }, className: "cell-wrap" },
    { label: "ЖҮКТЕГЕН / УАҚЫТ", render: (row) => { const cell = el("div"); cell.append(el("span", "", row.uploader_login), el("small", "mono", shortUtc(row.created_at))); return cell; } },
    { label: "ӘРЕКЕТТЕР", render: (row) => { const actions = el("div", "table-actions"); actions.append(button("Ашу", () => void viewDocument(row), "button small")); if (can("Documents.Delete")) actions.append(button("Жою", () => confirmation("Құжатты жою", t("template.documentDelete", { name: row.original_name }), async () => { await api(`/api/documents/${row.document_id}`, { method: "DELETE" }); toast("Құжат тізімнен алынды."); await renderRoute(); }, "Құжатты жою"), "button small text danger-text")); return actions; } },
  ], data.items, "Құжаттар"), t("count.documents", { count: number(data.total) }))); return view;
}
async function uploadDocument() {
  try { const catalog = await api("/api/academic/catalog"); const content = el("div"); const file = input("file"); file.required = true; file.accept = ".pdf,.txt,.png,.jpg,.jpeg";
    content.append(message(t("documents.uploadPurpose")));
    const group = select(catalogOptions(catalog.groups, "group"), "", "Топты таңдаңыз"); group.required = true; const subject = select(catalogOptions(catalog.subjects, "subject"), "", "Пәнді таңдаңыз"); subject.required = true; const term = select(catalogOptions(catalog.terms, "term"), "", "Кезеңді таңдаңыз"); term.required = true;
    content.append(field("Файл", file, "PDF, UTF-8 TXT, PNG, JPG/JPEG. Ең үлкен көлемі — 5 МиБ."), field("Оқу тобы", group), field("Пән", subject), field("Оқу кезеңі", term));
    modal({ title: "Құжат жүктеу", content, submitLabel: "Жүктеу", onSubmit: async () => { if (file.files[0].size > 5 * 1024 * 1024) throw new Error("Файл көлемі 5 МиБ-тан аспауы керек."); const data = new FormData(); data.append("file", file.files[0]); data.append("group_id", group.value); data.append("subject_id", subject.value); data.append("term_id", term.value); await api("/api/documents", { method: "POST", body: data }); toast("Құжат жүктелді."); await renderRoute(); } });
  } catch (error) { toast(userError(error), true); }
}
async function viewDocument(documentRow) {
  try { const blob = await api(`/api/documents/${documentRow.document_id}/content`, { blob: true }); const url = URL.createObjectURL(blob); const content = el("div"); content.append(el("p", "scope-note", t("template.documentView", { name: documentRow.original_name, size: byteSize(documentRow.size_bytes) })));
    if (documentRow.mime_type === "text/plain") content.append(dataEl("pre", "text-preview", await blob.text())); else content.append(el("p", "scope-note", "Құжатты құрылғыңызда қарау үшін файлды жүктеп алыңыз."));
    const download = link("↓  Файлды жүктеп алу", url, "button primary download-link"); download.download = documentRow.original_name; content.append(download); modal({ title: "Құжатты қарау", content, wide: true, onClose: () => URL.revokeObjectURL(url) });
  } catch (error) { toast(userError(error), true); }
}

async function sessionsPage() {
  const data = await api(`/api/sessions${query(state.sessions)}`); const view = el("div"); view.append(heading("Сессияларды бақылау", "Күдікті белгілерді тексеріп, қажет сессияны себебін көрсетіп тоқтатыңыз.", [refresh()])); view.append(el("p", "scope-note", "Күдікті белгі — тексеру ережесінің нәтижесі. Жалған дабыл болуы мүмкін; бұғаттау шешімін әкімші қабылдайды."));
  const box = panel("Сессиялар", null, t("count.sessions", { count: number(data.total) })); const filters = el("form", "filter-bar"); const status = select(["active", "blocked", "revoked", "expired"].map((value) => ({ value, label: statusNames[value] })), state.sessions.status, "Барлық күй"); const suspicious = select([{ value: "true", label: "Күдікті белгісі бар" }, { value: "false", label: "Күдікті белгісі жоқ" }], state.sessions.suspicious, "Барлық сессия");
  filters.append(field("Күйі", status), field("Ереже белгісі", suspicious)); const apply = el("button", "button primary", "Қолдану"); apply.type = "submit"; filters.append(apply); filters.addEventListener("submit", (event) => { event.preventDefault(); state.sessions = { status: status.value, suspicious: suspicious.value }; void renderRoute(); });
  box.append(filters, table([
    { label: "ПАЙДАЛАНУШЫ", render: (row) => { const cell = el("div"); cell.append(dataEl("strong", "", row.display_name), el("small", "", row.login)); return cell; } },
    { label: "IP-МЕКЕНЖАЙ", render: (row) => { const cell = el("div", "ip-pair"); cell.append(el("span", "mono", row.last_ip), el("small", "", t("template.initialIp", { ip: row.initial_ip }))); return cell; } },
    { label: "СОҢҒЫ БЕЛСЕНДІЛІК / БАСТАЛУЫ", render: (row) => { const cell = el("div"); cell.append(el("span", "mono", shortUtc(row.last_seen_at)), el("small", "mono", shortUtc(row.created_at))); return cell; } },
    { label: "КҮЙІ / СЕБЕБІ", render: (row) => { const cell = el("div", "session-note"); cell.append(badge(row.status)); if (row.suspicious_reason) cell.append(el("small", "", `⚑ ${row.suspicious_reason}`)); if (row.block_reason) cell.append(el("small", "", row.block_reason)); return cell; } },
    { label: t("sessions.review"), render: (row) => { const cell = el("div", "session-review"); if (row.review_status && row.review_status !== "none") { cell.append(badge(row.review_status), el("span", "review-source-label", t(row.review_source === "ai" ? "sessions.aiSource" : "sessions.ruleSource"))); if (row.review_audit_ids?.length) { cell.append(el("span", "field-hint", t("sessions.evidence"))); if (can("Audit.View")) cell.append(evidenceReferences(row.review_audit_ids)); else cell.append(dataEl("span", "mono", row.review_audit_ids.map((id) => `#${id}`).join(", "))); } } else cell.append(el("span", "muted", "—")); return cell; } },
    { label: "ӘРЕКЕТТЕР", render: (row) => { const controls = el("div", "table-actions"); if (row.status === "active") controls.append(button("Бұғаттау", () => sessionAction(row, "block"), "button small")); if (row.status === "blocked") controls.append(button("Бұғаттан шығару", () => sessionAction(row, "unblock"), "button small")); if (row.status === "active" && row.review_status === "awaiting_admin") controls.append(button(t("sessions.dismiss"), () => dismissSession(row), "button small text"), button(t("sessions.aiReview"), (event) => void sessionAiReview(row, event.currentTarget), "button small text")); if (["active", "blocked"].includes(row.status)) controls.append(button("Тоқтату", () => sessionAction(row, "terminate"), "button small text danger-text")); if (!controls.childElementCount) controls.append(el("span", "muted", "—")); return controls; } },
  ], data.items, "Сессиялар тізімі")); view.append(box); return view;
}
function sessionAction(session, action) {
  const titles = { block: "Сессияны бұғаттау", terminate: "Сессияны тоқтату", unblock: "Сессияны бұғаттан шығару" }; const content = el("div"); content.append(el("p", "scope-note", `${session.login} · ${session.last_ip}. ${L(action === "unblock" ? "Бұрынғы токен қайта белсенбейді. Пайдаланушы жүйеге қайта кіруі керек." : "Осы сессиядағы қолжетімділік дереу тоқтатылады.")}`)); const reason = el("textarea"); reason.required = true; reason.minLength = 5; reason.maxLength = 300; reason.rows = 3; content.append(field("Әрекет себебі", reason, "5–300 таңба. Себеп аудит журналында сақталады."));
  modal({ title: titles[action], content, submitLabel: "Растау", danger: action !== "unblock", onSubmit: async () => { await api(`/api/sessions/${encodeURIComponent(session.session_id)}/${action}`, { method: "POST", body: { reason: reason.value.trim() } }); toast(action === "unblock" ? "Бұғат алынды. Пайдаланушы қайта кіруі керек." : "Сессияның қолжетімділігі тоқтатылды."); await renderRoute(); } });
}
function dismissSession(session) { const content = el("div"); content.append(dataEl("p", "scope-note", `${session.login} · ${session.last_ip}`)); const reason = el("textarea"); reason.required = true; reason.minLength = 5; reason.maxLength = 300; content.append(field(L("Әрекет себебі"), reason, L("5–300 таңба. Себеп аудит журналында сақталады."))); modal({ title: t("sessions.dismissTitle"), content, submitLabel: t("sessions.dismiss"), onSubmit: async () => { await api(`/api/sessions/${session.session_id}/dismiss`, { method: "POST", body: { reason: reason.value.trim() } }); toast(t("sessions.dismissed")); await renderRoute(); } }); }
async function sessionAiReview(session, control) {
  control.disabled = true;
  try { const data = await api(`/api/sessions/${session.session_id}/ai-review`, { method: "POST", body: {} }); await renderRoute(); const content = el("div"); content.append(message(t("sessions.aiNotice"), "warning")); for (const claim of data.summary || []) content.append(dataEl("p", "recorded-text", claim.text), evidenceReferences(claim.audit_ids)); if (data.audit_ids?.length) content.append(evidenceReferences(data.audit_ids)); modal({ title: t("sessions.aiSuggestion"), content, wide: true }); }
  catch (error) { toast(userError(error), true); } finally { control.disabled = false; }
}

function utcInput(value) { return value ? `${value}${value.length === 16 ? ":00" : ""}Z` : ""; }
async function auditPage() {
  const { start_input, end_input, ...saved } = state.audit; const [data, options] = await Promise.all([api(`/api/audit${query({ ...saved, page_size: 20 })}`), api("/api/audit/options")]); const view = el("div"); view.append(heading("Аудит журналы", "Кім, қашан және қай IP-мекенжайдан әрекет жасағанын тексеріңіз.", [link("✧  AI арқылы іздеу", "#assistant", "button"), refresh()]));
  const box = panel("Әрекеттер журналы", null, t("template.auditCount", { count: number(data.total) })); const filters = el("form", "filter-bar"); const from = input("datetime-local", start_input); const to = input("datetime-local", end_input); const actor = input("search", state.audit.actor); actor.placeholder = L("Пайдаланушы логині");
  const action = select((options.actions || []).map((item) => typeof item === "string" ? { value: item, label: actionName(item) } : { value: item.code || item.value, label: item.label }), state.audit.action, "Барлық әрекет"); const result = select((options.results || ["success", "failure", "denied"]).map((item) => typeof item === "string" ? { value: item, label: outcomeNames[item] || item } : { value: item.code || item.value, label: item.label }), state.audit.result, "Барлық нәтиже"); const ip = input("search", state.audit.ip); ip.placeholder = L("Мысалы, 127.0.0.1");
  filters.append(field("Басталуы (UTC, кіреді)", from), field("Аяқталуы (UTC, кірмейді)", to), field("Пайдаланушы", actor), field("Әрекет", action), field("Нәтиже", result), field("IP-мекенжай", ip)); const apply = el("button", "button primary", "Сүзгілеу"); apply.type = "submit"; filters.append(apply, button("Тазарту", () => { state.audit = { page: 1 }; void renderRoute(); }, "button subtle"));
  filters.addEventListener("submit", (event) => { event.preventDefault(); if (from.value && to.value && from.value >= to.value) { toast("Басталу уақыты аяқталу уақытынан бұрын болуы керек.", true); return; } state.audit = { page: 1, start_input: from.value, end_input: to.value, start_at: utcInput(from.value), end_at: utcInput(to.value), actor: actor.value.trim(), action: action.value, result: result.value, ip: ip.value.trim() }; void renderRoute(); }); box.append(filters, table(auditColumns(), data.items, "Аудит оқиғалары"));
  const pages = Math.max(1, Math.ceil(data.total / data.page_size)); const pagination = el("div", "pagination"); pagination.append(el("span", "", t("template.eventTotal", { count: number(data.total) }))); const pager = el("div", "pagination-controls"); const previous = button("←  Алдыңғы", () => { state.audit.page = data.page - 1; void renderRoute(); }, "button small"); previous.disabled = data.page <= 1; const next = button("Келесі  →", () => { state.audit.page = data.page + 1; void renderRoute(); }, "button small"); next.disabled = data.page >= pages; pager.append(previous, el("span", "", `${data.page} / ${pages}`), next); pagination.append(pager); box.append(pagination); view.append(box); return view;
}
function pretty(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string") { try { return JSON.stringify(JSON.parse(value), null, 2); } catch (error) { if (error instanceof SyntaxError) return value; throw error; } } return JSON.stringify(value, null, 2);
}
async function auditDetail(id) {
  try { const data = await api(`/api/audit/${encodeURIComponent(id)}`); const row = data.item || data; const content = el("dl", "detail-grid"); const entries = [["Аудит ID", `#${row.audit_id}`], ["Уақыт (UTC)", utc(row.occurred_at)], ["Пайдаланушы", row.actor_login || row.user_id], ["Оқиға кезіндегі рөл", roleName({ role_code: row.role })], ["IP-мекенжай", row.ip_address || "—"], ["Нәтиже", L(outcomeNames[row.result] || row.result)], ["Әрекет", actionName(row.action)], ["Нысан", `${row.object_type} · ${row.object_id}`], ["Сессия", row.session_id || "—"], ["Жазба себебі", row.reason || "—"], ["Алдыңғы мән", row.old_value], ["Жаңа мән", row.new_value]];
    for (const [label, value] of entries) { const item = el("div", ["Сессия", "Жазба себебі"].includes(label) ? "span-all" : ""); item.append(el("dt", "", label)); const description = el("dd"); if (["Алдыңғы мән", "Жаңа мән"].includes(label)) description.append(dataEl("pre", "", pretty(value))); else description.textContent = String(value ?? "—"); item.append(description); content.append(item); } modal({ title: t("template.auditTitle", { id: row.audit_id }), content, wide: true });
  } catch (error) { toast(userError(error), true); }
}

function aiFilters(filters) {
  const grid = el("dl", "filter-grid");
  const entries = [["Басталуы (UTC)", filters.start_at ? utc(filters.start_at) : L("Шектеусіз")], ["Аяқталуы (UTC)", filters.end_at ? utc(filters.end_at) : L("Шектеусіз")], ["Әрекет", filters.actions?.length ? filters.actions.map(actionName).join(", ") : filters.action ? actionName(filters.action) : L("Барлық әрекет")], ["Нәтиже", L(outcomeNames[filters.result] || "Барлық нәтиже")], ["Пайдаланушы", filters.user_login || L("Барлығы")], ["IP-мекенжай", filters.ip_address || "—"], ["Нысан", [filters.object_type, filters.object_id].filter(Boolean).join("/") || "—"], ["Себеп мәтініндегі мағына", filters.semantic_query || L("Қолданылмады")], ["Нәтиже шегі", number(filters.limit)]];
  for (const [label, value] of entries) { const item = el("div"); item.append(el("dt", "", L(label)), dataEl("dd", "", value)); grid.append(item); }
  const body = el("div", "panel-body"); body.append(grid); return panel(L("Түсінілген сүзгілер"), body);
}
function jumpAiRecord(id) {
  const record = byId(`audit-${id}`); if (!record) { void auditDetail(id); return; }
  document.querySelectorAll(".record-highlight").forEach((item) => item.classList.remove("record-highlight")); record.classList.add("record-highlight"); record.scrollIntoView({ block: "center", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" }); record.focus({ preventScroll: true }); window.setTimeout(() => record.classList.remove("record-highlight"), 4000);
}
function evidenceReferences(ids) { const refs = el("div", "audit-references"); for (const id of ids || []) { const ref = button(`#${id}`, () => void auditDetail(id), "audit-reference"); ref.setAttribute("aria-label", t("template.auditReference", { id })); refs.append(ref); } return refs; }
function aiOutput(data) {
  const output = el("div", "assistant-output"); output.append(aiFilters(data.filters));
  if (data.summary?.length) { const body = el("div", "panel-body"); const list = el("ul", "summary-list");
    for (const claim of data.summary) { const item = el("li"); item.append(dataEl("p", "", claim.text)); const refs = el("div", "audit-references"); for (const id of claim.audit_ids) { const ref = link(`#${id}`, `#audit-${id}`, "audit-reference"); ref.setAttribute("aria-label", t("template.auditReference", { id })); ref.addEventListener("click", (event) => { event.preventDefault(); jumpAiRecord(id); }); refs.append(ref); } item.append(refs); list.append(item); }
    body.append(list); if (data.insufficient_data) body.append(el("p", "score-note", L("Бұл жазбалар себепті, қауіп деңгейін немесе журналдан тыс оқиғаларды анықтауға жеткіліксіз."))); output.append(panel(L("Дереккөздері бар қорытынды"), body));
  }
  output.append(el("p", "score-note", t(data.filters.semantic_query ? "template.semantic" : "template.nonsemantic", { count: number(data.candidate_count), threshold: number(data.semantic_threshold) })));
  if (data.truncated) output.append(message(L("Кандидаттар немесе нәтиже саны шектелді. Толығырақ қамту үшін сүзгіні нақтылаңыз."), "warning"));
  if (!data.records.length) { output.append(panel(null, empty(L("Сәйкес жазба табылмады"), L("Түсінілген сүзгілерді тексеріп, уақыт аралығын немесе сұрақты өзгертіңіз.")))); return output; }
  const heading = el("div", "section-heading"); heading.append(el("h2", "", t("template.recordsFound", { count: number(data.records.length) }))); output.append(heading);
  const evidence = new Map((data.evidence || []).map((row) => [row.audit_id, row.ip_address])); const records = el("ol", "ai-records");
  for (const row of data.records) { const card = el("li", "ai-record"); card.id = `audit-${row.audit_id}`; card.tabIndex = -1; const head = el("div", "ai-record-head"); const key = el("div"); key.append(button(`#${row.audit_id}`, () => void auditDetail(row.audit_id), "button text mono"), el("span", "mono muted", utc(row.occurred_at))); const tags = el("div"); tags.append(el("span", "badge neutral", actionName(row.action)), badge(row.result)); head.append(key, tags); card.append(head, dataEl("p", "ai-record-reason", row.reason)); const footer = el("div", "ai-record-footer"); footer.append(dataEl("span", "", `${row.user_id} · ${roleName({ role_code: row.role })} · ${L("IP-мекенжай")}: ${evidence.get(row.audit_id) || "—"}`)); if (typeof row.score === "number") footer.append(el("span", "mono", t("template.similarity", { score: number(row.score) }))); card.append(footer); records.append(card); } output.append(records); return output;
}
async function assistantPage() {
  const view = el("div"); view.append(heading(L("AI аудит көмекшісі"), L("Журналға табиғи тілде сұрақ қойып, қорытындыны бастапқы жазбалармен салыстырыңыз."))); const layout = el("div", "assistant-layout"); const main = el("div"); const body = el("div", "panel-body"); const form = el("form"); const question = el("textarea"); question.id = "ai-question"; question.name = "question"; question.maxLength = 1000; question.required = true; question.placeholder = L("Мысалы, соңғы аптада сәтсіз кіру әрекеттері болған ба?"); question.value = state.ai?.question || "";
  form.append(field(L("Іздеу сұрағы"), question)); const toolbar = el("div", "assistant-toolbar"); toolbar.append(el("p", "field-hint", L("1000 таңбаға дейін · Ctrl + Enter арқылы іздеу"))); const submit = el("button", "button primary", L("Журналдан іздеу")); submit.type = "submit"; toolbar.append(submit); form.append(toolbar); body.append(form); main.append(panel(L("Аудиттен іздеу"), body)); main.firstChild.classList.add("assistant-search"); const result = el("div"); if (state.ai?.data && state.ai.locale === locale) result.append(aiOutput(state.ai.data)); main.append(result); let searching = false;
  form.addEventListener("submit", async (event) => { event.preventDefault(); if (searching) return; if (!question.value.trim()) { result.replaceChildren(message(t("error.required"), "error ai-message")); question.focus(); return; } searching = true; submit.disabled = true; examples.querySelectorAll("button").forEach((item) => item.disabled = true); question.readOnly = true; const requestedLocale = locale; state.ai = { question: question.value.trim(), locale }; result.replaceChildren(); const progress = message(L("Сұрақ өңделуде. Қорытындының әр пікірі журнал жазбаларына сүйенеді."), "ai-message loading"); progress.prepend(el("span", "spinner")); result.append(progress);
    try { const data = await api("/api/audit/search", { method: "POST", body: { question: question.value.trim() } }); if (locale !== requestedLocale) return; state.ai = { question: question.value.trim(), data, locale }; result.replaceChildren(aiOutput(data)); }
    catch (error) { if (locale !== requestedLocale) return; const box = message(error.code === "model_unavailable" ? t("error.model_unavailable") : userError(error), "error ai-message"); if (error.code === "model_unavailable") box.append(document.createTextNode(" "), link(L("Аудит журналын ашу"), "#audit", "button small")); result.replaceChildren(box); } finally { searching = false; submit.disabled = false; examples.querySelectorAll("button").forEach((item) => item.disabled = false); question.readOnly = false; }
  }); question.addEventListener("keydown", (event) => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); if (!searching) form.requestSubmit(); } });
  const aside = el("aside", "assistant-aside"); aside.append(el("h2", "", t("ai.examplesTitle")), el("p", "", t("ai.exampleHint"))); const examples = el("div", "example-list expanded-examples"); for (let index = 1; index <= 14; index++) examples.append(button(t(`ai.example.${index}`), () => { if (searching) return; question.value = t(`ai.example.${index}`); question.focus(); form.requestSubmit(); }, "")); aside.append(examples); layout.append(main, aside); view.append(layout); return view;
}

function updateLanguageControls() {
  if (!state.user) document.title = t("app.title");
  document.querySelectorAll(".locale-select").forEach((control) => { control.value = locale; control.setAttribute("aria-label", t("app.language")); });
  byId("help-button").setAttribute("aria-label", t("app.help")); byId("help-button").title = t("app.help");
  const requested = byId("requested-role"); const value = requested.value || "teacher"; requested.replaceChildren();
  for (const role of ["teacher", "student"]) { const option = el("option", "", t(`role.${role}`)); option.value = role; requested.append(option); }
  requested.value = value; byId("requested-role-field").querySelector("label").textContent = t("auth.requestRole"); byId("requested-role-field").querySelector(".field-hint").textContent = t("auth.requestRoleHint");
  byId("display-name-field").querySelector("label").textContent = t("claim.accountName");
  const visible = byId("password").type === "text"; byId("password-toggle").textContent = L(visible ? "Жасыру" : "Көрсету"); byId("password-toggle").setAttribute("aria-label", L(visible ? "Құпиясөзді жасыру" : "Құпиясөзді көрсету"));
  renderAuthClaimFields();
}
function installLanguageControls() {
  for (const [id, parent] of [["locale-auth", document.querySelector(".auth-card-wrap")], ["locale-app", document.querySelector(".topbar-right")]]) {
    const control = select([{ value: "kk", label: "Қазақша" }, { value: "ru", label: "Русский" }, { value: "en", label: "English" }], locale, null); control.id = id; control.className = "locale-select"; parent.prepend(control);
    control.addEventListener("change", () => { const mode = state.authMode; setLocale(control.value); translateStatic(document.body); byId("toast-region").replaceChildren(); if (state.ai) state.ai = { question: state.ai.question }; updateLanguageControls(); if (state.user) enterApp(state.user); else setAuthMode(mode); document.querySelectorAll("input,select,textarea").forEach((item) => item.setCustomValidity("")); });
  }
  const help = button("?", () => openHelp(), "icon-button help-button"); help.id = "help-button"; document.querySelector(".topbar-right").prepend(help);
  const requested = select([], "", null); requested.id = "requested-role"; requested.name = "requested_role";
  const wrap = field(t("auth.requestRole"), requested, t("auth.requestRoleHint")); wrap.id = "requested-role-field"; wrap.hidden = true; byId("display-name-field").after(wrap);
  requested.addEventListener("change", () => { syncRegistrationClaim(); if (requested.value === "student") void registrationOptions().catch((error) => { if (!state.user && state.authMode === "register") { byId("auth-message").textContent = userError(error); byId("auth-message").hidden = false; } }); });
  updateLanguageControls();
  void registrationOptions().catch(() => {});
}
function contextSteps(route) {
  if (["pending", "rejected"].includes(state.user?.approval_status)) return ["help.pending.1", "help.pending.2"];
  if (route === "users" && state.user?.role_code !== "admin") return ["help.page.users.1", "help.users.read"];
  if (["users", "roles", "grades", "audit", "assistant"].includes(route)) return [`help.page.${route}.1`, `help.page.${route}.2`];
  if (route === "documents") return can("Documents.Upload") ? ["help.page.documents.1", "help.page.documents.2"] : ["help.page.documents.1", "help.page.documents.read"];
  if (route === "sessions") return ["help.page.sessions.1", "help.page.sessions.2", "help.page.sessions.3"];
  if (route === "requests") return ["help.page.requests.1", "help.page.requests.2"];
  if (route === "students") return ["help.page.students.1", "help.page.students.2"];
  if (route === "teachers") return ["help.page.teachers.1", "help.page.teachers.2"];
  if (route === "catalogs") return ["help.page.catalogs.1", "help.page.catalogs.2"];
  if (route === "dashboard" && state.user?.role_code === "admin" && can("Users.Manage") && can("Sessions.Manage")) return ["help.admin.1", "help.admin.2", "help.admin.3"];
  if (route === "dashboard" && state.user?.role_code === "head" && can("Grades.View") && can("Learning.View") && can("Audit.View")) return ["help.head.1", "help.head.2"];
  if (route === "tasks" || can("Learning.View")) {
    if (canCreateTasks()) return ["help.teacher.1", "help.teacher.2", "help.teacher.3"];
    if (can("Learning.Submit")) return ["help.student.1", "help.student.2", "help.student.3"];
    return ["help.tasks.read.1", "help.tasks.read.2"];
  }
  for (const [permission, page] of [["Users.Manage", "users"], ["Grades.View", "grades"], ["Audit.View", "audit"], ["Documents.View", "documents"]]) {
    if (can(permission)) return contextSteps(page);
  }
  return ["help.workspace.1", "help.workspace.2"];
}
function openHelp() { const content = el("ol", "context-steps"); for (const key of contextSteps(state.route)) content.append(el("li", "", t(key))); const dialog = modal({ title: t("app.helpTitle"), content }); dialog.dataset.contextHelp = "true"; }
function maybeHelp() {
  const key = `arm_help_${state.user.user_id}_${state.user.role_code}`; let seen;
  try { seen = localStorage.getItem(key); if (!seen) localStorage.setItem(key, "seen"); } catch (error) { if (!(error instanceof DOMException)) throw error; seen = state.helpSeen; state.helpSeen = true; }
  if (!seen) queueMicrotask(() => { if (state.user && !document.querySelector("dialog[open]")) openHelp(); });
}
function approvalPage() {
  state.route = "approval"; const rejected = state.user.approval_status === "rejected"; const needsReview = state.user.request_review_status === "needs_review"; const title = t(needsReview ? "claim.needsReviewTitle" : rejected ? "approval.rejectedTitle" : "approval.pendingTitle"); byId("breadcrumb").textContent = title; document.title = `${title} · ${t("app.title")}`;
  const view = el("div", "approval-page"); view.append(heading(title, t(rejected ? "approval.rejectedText" : "approval.pendingText")));
  const content = el("div", "panel-body"); content.append(badge(state.user.request_review_status || state.user.approval_status, needsReview ? "warning" : undefined), field(t("approval.requested"), dataEl("p", "", t(`role.${state.user.requested_role || "teacher"}`))));
  if (state.user.approval_reason) content.append(field(t("approval.reason"), dataEl("p", "recorded-text", state.user.approval_reason)));
  if (state.user.requested_role === "student") { content.append(el("h2", "", t("claim.privateTitle")), message(t("claim.unverifiedHint"), "warning"), state.user.student_claim ? claimDetails(state.user.student_claim) : message(t("claim.missing")), button(t("claim.correct"), () => void editStudentClaim(), "button")); }
  content.append(button(t("approval.refresh"), async () => { try { const data = await api("/api/auth/me"); enterApp(data.user); } catch (error) { toast(userError(error), true); } }, "button primary")); view.append(panel(null, content)); return view;
}
async function editStudentClaim() {
  const ownerId = state.user?.user_id; try { const [options, me] = await Promise.all([registrationOptions(), api("/api/auth/me")]); if (state.user?.user_id !== ownerId) return; if (me.user.approval_status === "approved") { enterApp(me.user); return; } state.user = me.user; const fields = claimFields("correct-claim", state.user.student_claim || {}, options); const content = el("div"); content.append(message(t("claim.correctionHint")), fields.root); const dialog = modal({ title: t("claim.correct"), content, wide: true, submitLabel: t("claim.resubmit"), onSubmit: async () => { await api("/api/auth/student-claim", { method: "PUT", body: fields.payload() }); const me = await api("/api/auth/me"); toast(t("claim.resubmitted")); enterApp(me.user); } }); if (!fields.hasGroups) dialog.querySelector("button[type=submit]").disabled = true; } catch (error) { toast(userError(error), true); }
}
async function studentsPage() {
  const load = () => api(`/api/students${query({ ...state.students, page_size: 25 })}`); let data; let resetGroup = false; try { data = await load(); } catch (error) { if (!state.students.group_id || ![403, 422].includes(error.status)) throw error; state.students = { ...state.students, group_id: "", page: 1 }; resetGroup = true; data = await load(); } const view = el("div"); view.append(heading(t("students.title"), t("students.subtitle"), [refresh()])); if (resetGroup) view.append(message(t("students.groupReset"), "warning"));
  const filters = el("form", "filter-bar students-filters"); const search = input("search", state.students.q); search.id = "students-search"; search.maxLength = 120; const group = select(data.groups.map((row) => ({ value: row.group_id, label: row.name, raw: true })), state.students.group_id, t("students.allGroups")); group.id = "students-group"; const apply = el("button", "button primary", L("Қолдану")); apply.type = "submit"; filters.append(field(t("students.search"), search), field(L("Оқу тобы"), group), apply); filters.addEventListener("submit", (event) => { event.preventDefault(); state.students = { q: search.value.trim(), group_id: group.value, page: 1 }; void renderRoute(); });
  const box = panel(t("students.roster"), null, t("students.total", { count: number(data.total) })); box.append(filters); if (!data.groups.length) box.append(message(t("students.noGroups"), "warning"));
  box.append(data.items.length ? table([{ label: t("claim.name"), render: (row) => dataEl("strong", "", row.display_name) }, { label: t("claim.number"), render: (row) => dataEl("span", "mono", row.student_code) }, { label: L("Оқу тобы"), render: (row) => dataEl("span", "", row.group_name) }], data.items, t("students.roster")) : empty(t("students.noResults"), t("students.noResultsHint")));
  const pages = Math.max(1, Math.ceil(data.total / data.page_size)); const pagination = el("div", "pagination"); const pager = el("div", "pagination-controls"); const previous = button("←  Алдыңғы", () => { state.students.page = data.page - 1; void renderRoute(); }, "button small"); previous.disabled = data.page <= 1; const next = button("Келесі  →", () => { state.students.page = data.page + 1; void renderRoute(); }, "button small"); next.disabled = data.page >= pages; pagination.append(el("span", "", t("students.total", { count: number(data.total) }))); pager.append(previous, el("span", "", `${number(data.page)} / ${number(pages)}`), next); pagination.append(pager); box.append(pagination); view.append(box); return view;
}
async function requestsPage() {
  const status = state.requestStatus || "pending"; const data = await api(`/api/admin/requests${query({ status })}`); const view = el("div"); view.append(heading(t("requests.title"), t("requests.subtitle"), [refresh()]));
  const filters = el("form", "filter-bar"); const choice = select(["pending", "needs_review", "approved", "rejected"].map((value) => ({ value, label: t(`status.${value}`) })), status, null); filters.append(field(L("Күйі"), choice)); const apply = el("button", "button primary", L("Қолдану")); apply.type = "submit"; filters.append(apply); filters.addEventListener("submit", (event) => { event.preventDefault(); state.requestStatus = choice.value; void renderRoute(); });
  const box = panel(t("requests.title"), null, t("count.accounts", { count: number(data.total) })); box.append(filters, table([
    { label: L("ПАЙДАЛАНУШЫ"), render: (row) => { const item = el("div"); item.append(dataEl("strong", "", row.display_name), dataEl("small", "mono", row.login)); return item; } },
    { label: t("requests.requested"), render: (row) => t(`role.${row.requested_role || "teacher"}`) }, { label: t("claim.listTitle"), render: (row) => { const item = el("div", "claim-list-summary"); if (row.student_claim) item.append(dataEl("strong", "", row.student_claim.name), dataEl("small", "mono", row.student_claim.number_masked), dataEl("small", "", row.student_claim.group_name), badge(t("claim.unverified"), "warning")); else item.append(el("span", "muted", row.requested_role === "student" ? t("claim.missing") : "—")); return item; } }, { label: t("requests.date"), render: (row) => utc(row.created_at) }, { label: L("КҮЙІ"), render: (row) => badge(row.request_review_status || row.approval_status) },
    { label: L("ӘРЕКЕТТЕР"), render: (row) => { const actions = el("div", "table-actions"); if (row.approval_status !== "approved") { actions.append(button(t("requests.approve"), () => void approveRequest(row), "button small")); if (row.requested_role === "student") actions.append(button(t("claim.needsReview"), () => needsReviewRequest(row), "button small text")); actions.append(button(t("requests.reject"), () => rejectRequest(row), "button small text danger-text")); } return actions; } },
  ], data.items, t("requests.title"))); view.append(box); return view;
}
function studentChoice(catalog, selected) { return select((catalog.students || []).map((row) => ({ value: row.student_id, raw: true, label: `${row.display_name} · ${row.student_code} · ${catalog.groups?.find((group) => group.group_id === row.group_id)?.name || row.group_id}` })), selected || "", t("requests.selectStudent")); }
async function approveRequest(user) {
  if (user.requested_role === "teacher") { teacherApprove(user, () => {}); return; }
  try { const [roles, detail] = await Promise.all([api("/api/admin/roles"), api(`/api/admin/requests/${user.user_id}`)]); user = detail.user; const claim = detail.student_claim; const candidates = detail.candidates || []; let dialog; const content = el("div"); content.append(dataEl("p", "scope-note", `${user.display_name} · ${user.login}`));
    const role = select(roles.roles.filter((row) => row.code !== "pending").map((row) => ({ value: row.code, label: t(`role.${row.code}`) })), user.requested_role || "teacher", null);
    const privateClaim = el("section", "claim-private"); privateClaim.append(el("h3", "", t("claim.privateTitle")), message(t("claim.unverifiedHint"), "warning"), claim ? claimDetails(claim) : message(t("claim.missing"), "warning"));
    const mode = select([{ value: "existing", label: t("approval.existingMode") }, { value: "create", label: t("approval.createMode") }], "existing", null); mode.id = "approval-student-mode"; const modeField = field(t("approval.mode"), mode, t("approval.modeHint")); const groups = detail.groups || []; const reviewedGroup = select(groups.map((row) => ({ value: row.group_id, label: row.name, raw: true })), "", t("claim.selectGroup")); reviewedGroup.id = "approval-confirmed-group"; const groupField = field(t("approval.reviewedGroup"), reviewedGroup, t("approval.groupHint")); const createHint = message(t("approval.createHint")); if (!claim || !groups.length) mode.querySelector('option[value="create"]').disabled = true;
    const student = select(candidates.map((row) => ({ value: row.student_id, raw: true, label: `${row.display_name} · ${row.student_code} · ${row.group_name}${row.already_linked ? ` · ${t("claim.alreadyLinked")}` : ""}` })), "", t("requests.selectStudent"));
    const linked = field(t("requests.student"), student, t("claim.chooseHint")); const comparison = el("div", "claim-comparison"); const confirm = input("checkbox"); const confirmationText = el("span"); const confirmation = el("label", "checkbox-label claim-confirmation"); confirmation.append(confirm, confirmationText); const acknowledge = input("checkbox"); const ackLabel = el("label", "checkbox-label claim-mismatch-ack"); ackLabel.append(acknowledge, el("span", "", t("claim.acknowledgeMismatch"))); let mismatch = true;
    const distinct = input("checkbox"); const distinctLabel = el("label", "checkbox-label claim-distinct-confirmation"); distinctLabel.append(distinct, el("span", "", t("approval.confirmDistinct"))); const collisionBox = el("section", "claim-private");
    const studentSection = el("div"); studentSection.append(privateClaim, modeField, linked, groupField, createHint, collisionBox, distinctLabel, comparison, confirmation, ackLabel); if (!groups.length) studentSection.append(message(t("students.noRegistrationGroups"), "warning")); content.append(field(t("requests.actualRole"), role), studentSection);
    const canonicalName = (value) => String(value || "").normalize("NFKC").trim().replace(/\s+/g, " ").toLocaleLowerCase();
    const sameNames = claim ? candidates.filter((row) => row.matches?.name ?? canonicalName(row.display_name) === canonicalName(claim.name)) : []; const sameCode = claim && candidates.some((row) => row.student_code?.trim().toUpperCase() === claim.number?.trim().toUpperCase()); const needsDistinct = sameNames.length > 0 && !sameCode; collisionBox.append(el("h3", "", t("approval.sameName")), message(t("approval.sameNameHint"), "warning")); for (const row of sameNames) collisionBox.append(dataEl("p", "scope-note", `${row.display_name} · ${row.student_code} · ${row.group_name}`));
    const sync = () => { const isStudent = role.value === "student"; const creating = isStudent && mode.value === "create"; const selected = candidates.find((row) => String(row.student_id) === student.value); studentSection.hidden = !isStudent; mode.disabled = !isStudent; linked.hidden = creating; student.disabled = !isStudent || creating; student.required = isStudent && !creating; groupField.hidden = !creating; createHint.hidden = !creating; reviewedGroup.disabled = !creating; reviewedGroup.required = creating; confirm.checked = false; acknowledge.checked = false; distinct.checked = false; collisionBox.hidden = !creating || !needsDistinct; distinctLabel.hidden = collisionBox.hidden; distinct.disabled = distinctLabel.hidden || !reviewedGroup.value; distinct.required = !distinctLabel.hidden; confirm.disabled = !isStudent || (creating ? !reviewedGroup.value : !selected); confirm.required = isStudent; confirmationText.textContent = t(creating ? "approval.confirmCreate" : "claim.confirmMatch"); comparison.replaceChildren();
      const matches = { name: selected?.matches?.name ?? Boolean(claim && selected && canonicalName(claim.name) === canonicalName(selected.display_name)), number: selected?.matches?.number ?? Boolean(claim && selected && claim.number?.trim().toUpperCase() === selected.student_code?.trim().toUpperCase()), group: selected?.matches?.group ?? Boolean(claim && selected && Number(claim.group_id) === Number(selected.group_id)) }; mismatch = !claim || !Object.values(matches).every(Boolean); ackLabel.hidden = !isStudent || creating || !selected || !mismatch; acknowledge.disabled = ackLabel.hidden; acknowledge.required = !ackLabel.hidden;
      if (creating && reviewedGroup.value && Number(reviewedGroup.value) !== Number(claim?.group_id)) comparison.append(message(t("approval.changedGroup"), "warning"));
      if (isStudent && !creating && selected) { comparison.append(message(t(mismatch ? "claim.mismatchWarning" : "claim.matchFound"), mismatch ? "warning" : "")); const grid = el("dl", "detail-grid"); for (const [key, value] of [["claim.name", selected.display_name], ["claim.number", selected.student_code], ["Оқу тобы", selected.group_name]]) { const pair = el("div"); pair.append(el("dt", "", t(key)), dataEl("dd", "", value)); grid.append(pair); } comparison.append(el("h3", "", t("claim.selectedRecord")), grid); if (selected.already_linked) comparison.append(message(t("claim.linkedWarning"), "warning")); }
    }; role.addEventListener("change", sync); student.addEventListener("change", sync); mode.addEventListener("change", sync); reviewedGroup.addEventListener("change", sync); sync();
    if (user.requested_role === "student") content.append(button(t("claim.needsReview"), () => { dialog.close(); needsReviewRequest(user); }, "button text"));
    dialog = modal({ title: t("requests.approveTitle"), content, wide: true, submitLabel: t("requests.approve"), onSubmit: async () => { const body = { role_code: role.value }; if (role.value === "student") Object.assign(body, { confirm_student_match: confirm.checked, claim_revision: detail.claim_revision }, mode.value === "create" ? { create_student: true, confirmed_group_id: Number(reviewedGroup.value), ...(needsDistinct && distinct.checked ? { confirm_distinct_student: true } : {}) } : { student_id: Number(student.value), ...(mismatch ? { acknowledge_mismatch: acknowledge.checked } : {}) }); await api(`/api/admin/requests/${user.user_id}/approve`, { method: "POST", body }); toast(t("requests.approved")); await renderRoute(); } });
  } catch (error) { toast(userError(error), true); }
}
function needsReviewRequest(user) { const content = el("div"); content.append(dataEl("p", "scope-note", `${user.display_name} · ${user.login}`), message(t("claim.needsReviewHint"))); const reason = el("textarea"); reason.required = true; reason.minLength = 5; reason.maxLength = 300; content.append(field(t("requests.reason"), reason)); modal({ title: t("claim.needsReview"), content, submitLabel: t("claim.sendForReview"), onSubmit: async () => { await api(`/api/admin/requests/${user.user_id}/needs-review`, { method: "POST", body: { reason: reason.value.trim() } }); toast(t("claim.reviewRequested")); await renderRoute(); } }); }
function rejectRequest(user) { if (user.requested_role === "teacher") { teacherReject(user); return; } const content = el("div"); content.append(dataEl("p", "scope-note", `${user.display_name} · ${user.login}`)); const reason = el("textarea"); reason.required = true; reason.minLength = 5; reason.maxLength = 300; content.append(field(t("requests.reason"), reason)); modal({ title: t("requests.rejectTitle"), content, submitLabel: t("requests.reject"), danger: true, onSubmit: async () => { await api(`/api/admin/requests/${user.user_id}/reject`, { method: "POST", body: { reason: reason.value.trim() } }); toast(t("requests.rejected")); await renderRoute(); } }); }

function datetimeLocal(value) { const time = value ? new Date(value) : new Date(); return new Date(time.getTime() - time.getTimezoneOffset() * 60000).toISOString().slice(0, 19); }
function zoneHint() { return t("learning.localTimeHint", { zone: Intl.DateTimeFormat().resolvedOptions().timeZone }); }
function canReadSubmissions(task) { return can("Learning.View") && ["admin", "teacher", "head"].includes(state.user?.role_code) && task.submission_count !== null; }
function canCreateTasks() { return can("Learning.Manage") && (state.user?.role_code === "admin" || (state.user?.role_code === "teacher" && Boolean(state.workload?.items?.length))); }
async function tasksPage() {
  const [data, workload] = await Promise.all([api("/api/tasks"), teacherWorkload()]); const actions = [refresh()]; if (canCreateTasks()) actions.unshift(button(t("learning.create"), () => void taskEditor(), "button primary"));
  const view = el("div"); view.append(heading(t("learning.title"), t("learning.subtitle"), actions)); if (workload && !workload.items.length) view.append(message(t("teachers.noAssignments"), "warning")); if (canCreateTasks()) view.append(message(t("learning.separateFlow"))); const cards = el("div", "task-grid");
  for (const task of data.items) { const body = el("div", "panel-body"); const labels = el("div", "task-labels"); labels.append(badge(task.status)); if (task.deadline_passed) labels.append(badge(t("learning.overdue"), "warning")); if (task.my_submission) labels.append(badge(task.my_submission.status)); body.append(labels, dataEl("h2", "task-title", task.title), dataEl("p", "task-scope", `${task.group_name} · ${L(task.subject_name)} · ${L(task.term_name)}`), el("p", "task-deadline", `${t("learning.deadline")}: ${date(task.deadline)}`), el("p", "field-hint", t(task.allow_late ? "learning.allowLateHint" : "learning.noLate")));
    if (task.my_submission?.published && task.my_submission.score !== null) body.append(el("p", "task-score", `${t("learning.myResult")}: ${number(task.my_submission.score)} / ${number(100)}`)); const controls = el("div", "task-actions"); controls.append(button(t("learning.details"), () => void taskDetail(task.task_id), "button small")); if (canReadSubmissions(task)) controls.append(button(t("learning.submissions"), () => void submissionsDialog(task), "button small text")); if (can("Learning.Submit") && task.status === "open" && (!task.my_submission || task.my_submission.can_resubmit)) controls.append(button(t(task.my_submission ? "learning.resubmit" : "learning.submit"), () => void submitDialog(task), "button small primary")); body.append(controls); const card = panel(null, body); card.classList.add("task-card"); cards.append(card); }
  view.append(cards.childElementCount ? cards : panel(null, empty(t("learning.noTasks"), t("learning.noTasksText")))); return view;
}
async function taskEditor(task) {
  try { if (task) task = await api(`/api/tasks/${task.task_id}`); const [catalog, workload] = await Promise.all([api("/api/academic/catalog"), teacherWorkload()]); const teacher = state.user?.role_code === "teacher"; if (teacher && !workload?.items.length) throw new ApiError(t("teachers.noAssignments"), "forbidden", 403); const content = el("div"); const title = input("text", task?.title); title.required = true; title.minLength = 3; title.maxLength = 180; const description = el("textarea"); description.value = task?.description || ""; description.required = true; description.maxLength = 10000; const rules = el("textarea"); rules.value = task?.rules || ""; rules.maxLength = 2000;
    const group = select(activeCatalogOptions(catalog.groups, "group", task?.group_id), task?.group_id, L("Топты таңдаңыз")); const subject = select(activeCatalogOptions(catalog.subjects, "subject", task?.subject_id), task?.subject_id, L("Пәнді таңдаңыз")); const term = select(activeCatalogOptions(catalog.terms, "term", task?.term_id), task?.term_id, L("Кезеңді таңдаңыз")); [group, subject, term].forEach((item) => item.required = true); const exact = select((workload?.items || []).map((row) => ({ value: row.assignment_id, raw: true, label: assignmentText(row) })), workload?.items.find((row) => task && row.group_id === task.group_id && row.subject_id === task.subject_id && row.term_id === task.term_id)?.assignment_id, t("teachers.selectAssignment")); exact.id = "task-assignment"; exact.required = teacher;
    const publication = input("datetime-local", datetimeLocal(task?.published_at)); publication.required = true; publication.step = "1"; const deadline = input("datetime-local", datetimeLocal(task?.deadline || Date.now() + 86400000)); deadline.required = true; deadline.step = "1"; deadline.dataset.futureDeadline = "true"; if (!task) deadline.min = datetimeLocal(Date.now() + 1000); const initialPublication = publication.value; const initialDeadline = deadline.value; const late = input("checkbox"); late.checked = Boolean(task?.allow_late); const lateLabel = el("label", "checkbox-label"); lateLabel.append(late, el("span", "", t("learning.allowLate")));
    content.append(field(t("learning.taskTitle"), title), field(t("learning.description"), description), field(t("learning.rules"), rules)); if (teacher) content.append(field(t("teachers.exactAssignment"), exact, t("teachers.exactHint"))); else content.append(field(L("Оқу тобы"), group), field(L("Пән"), subject), field(L("Оқу кезеңі"), term)); content.append(field(t("learning.publication"), publication, zoneHint()), field(t("learning.deadline"), deadline, zoneHint()), lateLabel);
    const material = input("file"); material.accept = ".pdf,.txt,.png,.jpg,.jpeg"; if (!task) content.append(field(t("learning.initialMaterial"), material, t("learning.initialMaterialHint")));
    modal({ title: t(task ? "learning.edit" : "learning.create"), content, wide: true, submitLabel: L("Сақтау"), onSubmit: async () => {
      const deadlineChanged = !task || deadline.value !== initialDeadline;
      if (deadlineChanged && new Date(deadline.value).getTime() <= Date.now()) throw new ApiError(t("error.deadline_in_past"), "deadline_in_past", 422);
      const scope = teacher ? workload.items.find((row) => String(row.assignment_id) === exact.value) : { group_id: Number(group.value), subject_id: Number(subject.value), term_id: Number(term.value) }; if (!scope) throw new ApiError(t("teachers.noAssignments"), "forbidden", 403);
      const payload = { title: title.value.trim(), description: description.value.trim(), rules: rules.value.trim(), group_id: scope.group_id, subject_id: scope.subject_id, term_id: scope.term_id, published_at: task && publication.value === initialPublication ? task.published_at : new Date(publication.value).toISOString(), deadline: task && !deadlineChanged ? task.deadline : new Date(deadline.value).toISOString(), allow_late: late.checked, ...(task ? { version: task.version } : {}) };
      if (!task && material.files.length) { const form = new FormData(); form.append("payload", JSON.stringify(payload)); form.append("file", material.files[0]); await api("/api/tasks/with-material", { method: "POST", body: form }); }
      else await api(task ? `/api/tasks/${task.task_id}` : "/api/tasks", { method: task ? "PATCH" : "POST", body: payload });
      toast(t(task ? "learning.updated" : "learning.created")); await renderRoute();
    } });
  } catch (error) { toast(userError(error), true); }
}
async function taskDetail(id) {
  try { const task = await api(`/api/tasks/${id}`); let dialog; const replaceDialog = (action) => { dialog.close(); void action(); }; const content = el("div"); content.append(badge(task.status), el("p", "task-deadline", `${t("learning.deadline")}: ${date(task.deadline)}`), dataEl("p", "recorded-text", task.description)); if (task.rules) content.append(el("h3", "detail-subtitle", t("learning.rules")), dataEl("p", "recorded-text", task.rules)); content.append(el("p", "field-hint", t(task.allow_late ? "learning.allowLateHint" : "learning.resubmitHint")));
    const controls = el("div", "task-actions"); if (task.material_name) controls.append(button(t("learning.openMaterial"), () => void openAttachment(`/api/tasks/${id}/material`, task.material_name, task.material_mime_type), "button small")); if (task.can_manage) controls.append(button(t("learning.edit"), () => replaceDialog(() => taskEditor(task)), "button small"), button(t("learning.uploadMaterial"), () => materialDialog(task, async () => { dialog.close(); await taskDetail(id); }), "button small")); if (canReadSubmissions(task)) controls.append(button(t("learning.submissions"), () => replaceDialog(() => submissionsDialog(task)), "button small")); if (can("Learning.Submit") && task.status === "open" && (!task.my_submission || task.my_submission.can_resubmit)) controls.append(button(t(task.my_submission ? "learning.resubmit" : "learning.submit"), () => replaceDialog(() => submitDialog(task)), "button small primary")); content.append(controls);
    if (task.my_submission) { const work = task.my_submission; content.append(el("h3", "detail-subtitle", t("learning.submission")), dataEl("p", "recorded-text", work.text), el("p", "field-hint", `${t("learning.submittedAt")}: ${date(work.submitted_at)}`)); if (work.file_name) content.append(button(t("learning.openAttachment"), () => void openAttachment(`/api/submissions/${work.submission_id}/file`, work.file_name, work.mime_type), "button small")); content.append(el("p", "", t(work.published ? "learning.publishedBadge" : "learning.unpublished"))); if (work.published) { content.append(el("p", "task-score", `${t("learning.score")}: ${number(work.score)}`), dataEl("p", "recorded-text", work.feedback || t("learning.noFeedback"))); } }
    dialog = modal({ title: task.title, content, wide: true, rawTitle: true });
  } catch (error) { toast(userError(error), true); }
}
function materialDialog(task, onSaved) { const file = input("file"); file.required = true; file.accept = ".pdf,.txt,.png,.jpg,.jpeg"; const content = field(t("learning.material"), file, L("PDF, UTF-8 TXT, PNG, JPG/JPEG. Ең үлкен көлемі — 5 МиБ.")); modal({ title: t("learning.uploadMaterial"), content, submitLabel: L("Жүктеу"), onSubmit: async () => { const form = new FormData(); form.append("file", file.files[0]); form.append("version", task.version); await api(`/api/tasks/${task.task_id}/material`, { method: "POST", body: form }); toast(t("learning.materialSaved")); await renderRoute(); if (onSaved) await onSaved(); } }); }
async function submitDialog(task) {
  try { task = await api(`/api/tasks/${task.task_id}`); } catch (error) { toast(userError(error), true); return; }
  const content = el("div"); content.append(dataEl("p", "scope-note", task.title), el("p", "task-deadline", `${t("learning.deadline")}: ${date(task.deadline)}`), el("p", "field-hint", t(task.allow_late ? "learning.allowLateHint" : "learning.resubmitHint")));
  if (task.my_submission?.score !== null && task.my_submission?.score !== undefined) content.append(message(t("learning.resubmitReviewReset"), "warning"));
  const text = el("textarea"); text.value = task.my_submission?.text || ""; text.maxLength = 30000; text.rows = 7; const file = input("file"); file.accept = ".pdf,.txt,.png,.jpg,.jpeg"; content.append(field(t("learning.workText"), text), field(t("learning.optionalFile"), file, L("PDF, UTF-8 TXT, PNG, JPG/JPEG. Ең үлкен көлемі — 5 МиБ.")));
  modal({ title: t(task.my_submission ? "learning.resubmit" : "learning.submit"), content, wide: true, submitLabel: t("learning.submit"), onSubmit: async () => { if (!text.value.trim() && !file.files.length && !task.my_submission?.file_name) throw new ApiError(t("error.invalid_submission"), "invalid_submission", 422); const form = new FormData(); form.append("text", text.value.trim()); form.append("version", task.my_submission?.version || 0); if (file.files.length) form.append("file", file.files[0]); await api(`/api/tasks/${task.task_id}/submission`, { method: "PUT", body: form }); toast(t("learning.submitted")); await renderRoute(); } });
}
async function submissionsDialog(task) {
  try { const data = await api(`/api/tasks/${task.task_id}/submissions`); const content = el("div"); const list = el("div"); const columns = [
    { label: t("learning.student"), render: (row) => { const item = el("div"); item.append(dataEl("strong", "", row.student_name), dataEl("small", "mono", row.student_code)); return item; } },
    { label: t("learning.submittedAt"), render: (row) => date(row.submitted_at) }, { label: L("Күйі"), render: (row) => badge(row.status) }, { label: t("learning.score"), render: (row) => { if (row.score === null) return "—"; const cell = el("div", "submission-score"); cell.append(dataEl("strong", "", number(row.score)), badge(t(row.published ? "learning.publishedBadge" : "learning.unpublished"), row.published ? "success" : "neutral")); return cell; } },
    { label: L("ӘРЕКЕТТЕР"), render: (row) => button(t(row.can_review ? "learning.review" : "learning.details"), () => void reviewDialog(row.submission_id, async () => { const fresh = await api(`/api/tasks/${task.task_id}/submissions`); list.replaceChildren(table(columns, fresh.items, t("learning.submissions"))); }), "button small") },
  ]; list.append(table(columns, data.items, t("learning.submissions"))); content.append(dataEl("p", "scope-note", task.title), list); modal({ title: t("learning.submissions"), content, wide: true });
  } catch (error) { toast(userError(error), true); }
}
async function reviewDialog(id, onSaved) {
  try { const work = await api(`/api/submissions/${id}`); const content = el("div"); content.append(dataEl("p", "scope-note", `${work.student_name} · ${work.student_code}`), dataEl("p", "recorded-text submission-text", work.text), el("p", "field-hint", `${t("learning.submittedAt")}: ${date(work.submitted_at)}`)); if (work.file_name) content.append(button(t("learning.openAttachment"), () => void openAttachment(`/api/submissions/${id}/file`, work.file_name, work.mime_type), "button small"));
    const feedback = el("textarea"); feedback.value = work.feedback || ""; feedback.maxLength = 5000; const score = input("number", work.score); score.required = true; score.min = 0; score.max = 100; score.step = 1; const published = input("checkbox"); published.checked = work.published; const label = el("label", "checkbox-label"); label.append(published, el("span", "", t("learning.published"))); content.append(field(t("learning.feedback"), feedback), field(t("learning.score"), score), label);
    if (!work.can_review) { feedback.readOnly = true; score.readOnly = true; published.disabled = true; }
    if (["admin", "teacher", "head"].includes(state.user?.role_code)) content.append(button(t("learning.reviewHistory"), () => void reviewHistory(id), "button small"));
    modal({ title: t(work.can_review ? "learning.review" : "learning.details"), content, wide: true, submitLabel: work.can_review ? L("Сақтау") : null, onSubmit: async () => { await api(`/api/submissions/${id}/review`, { method: "PATCH", body: { feedback: feedback.value.trim(), score: Number(score.value), published: published.checked, version: work.version } }); if (onSaved) await onSaved(); toast(t("learning.reviewSaved")); await renderRoute(); } });
  } catch (error) { toast(userError(error), true); }
}
async function reviewHistory(id) {
  try { const data = await api(`/api/submissions/${id}/history`); const content = el("div", "review-history");
    for (const row of data.items) { const item = el("section", "history-entry"); item.append(dataEl("h3", "", `${row.actor_name} · ${row.actor_login}`), dataEl("p", "mono field-hint", `${utc(row.occurred_at)} · ${row.ip_address}`)); const values = el("dl", "detail-grid");
      for (const [key, value] of [["learning.oldScore", row.old_score === null ? "—" : number(row.old_score)], ["learning.newScore", row.new_score === null ? "—" : number(row.new_score)], ["learning.oldPublication", t(row.old_published ? "learning.publishedBadge" : "learning.unpublished")], ["learning.newPublication", t(row.new_published ? "learning.publishedBadge" : "learning.unpublished")], ["learning.oldFeedback", row.old_feedback || "—"], ["learning.newFeedback", row.new_feedback || "—"]]) { const pair = el("div", key.endsWith("Feedback") ? "span-all" : ""); pair.append(el("dt", "", t(key)), dataEl("dd", "recorded-text", value)); values.append(pair); }
      item.append(values); if (can("Audit.View")) item.append(evidenceReferences([row.audit_id])); else item.append(dataEl("p", "mono field-hint", `${L("Аудит ID")}: #${row.audit_id}`)); content.append(item);
    }
    if (!data.items.length) content.append(empty(t("learning.historyEmpty"), "")); modal({ title: t("learning.reviewHistory"), content, wide: true });
  } catch (error) { toast(userError(error), true); }
}
async function openAttachment(path, name, mime) {
  try { const blob = await api(path, { blob: true }); const url = URL.createObjectURL(blob); const content = el("div"); content.append(dataEl("p", "scope-note", name)); if (mime === "text/plain") content.append(dataEl("pre", "text-preview", await blob.text())); const download = link(L("↓  Файлды жүктеп алу"), url, "button primary"); download.download = name; content.append(download); modal({ title: t("learning.openAttachment"), content, wide: true, onClose: () => URL.revokeObjectURL(url) }); } catch (error) { toast(userError(error), true); }
}

const pages = { dashboard: dashboardPage, teachers: teachersPage, catalogs: catalogsPage, requests: requestsPage, students: studentsPage, tasks: tasksPage, users: usersPage, roles: rolesPage, grades: gradesPage, documents: documentsPage, sessions: sessionsPage, audit: auditPage, assistant: assistantPage };
async function renderRoute() {
  if (!state.user) return; toggleNav(false); byId("user-menu").open = false;
  if (["pending", "rejected"].includes(state.user.approval_status)) { byId("page-content").replaceChildren(approvalPage()); byId("page-content").setAttribute("aria-busy", "false"); maybeHelp(); return; }
  const requested = location.hash.slice(1) || "dashboard"; const deepAudit = /^audit-([1-9]\d*)$/.exec(requested); let route = deepAudit ? "audit" : requested; const entry = nav.find(([path]) => path === route); if (!entry || !routeAllowed(entry)) route = nav.find(routeAllowed)?.[0]; const container = byId("page-content"); const seq = ++state.load; state.route = route;
  if (!route) { container.replaceChildren(heading("Жұмыс кеңістігі", "Қолжетімді бөлімдеріңіз әзірге жоқ."), panel(null, empty("Құқық берілмеген", "Қажетті бөлімге қолжетімділік алу үшін әкімшіге жүгініңіз."))); return; }
  document.querySelectorAll(".nav-link").forEach((item) => { const active = item.dataset.route === route; item.classList.toggle("active", active); if (active) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current"); }); const title = L(nav.find(([path]) => path === route)[1]); byId("breadcrumb").textContent = `${t("app.workspace")} / ${title}`; document.title = `${title} · ${t("app.title")}`; const loader = el("div", "page-loader"); loader.append(el("span", "spinner"), el("span", "", "Деректер жүктелуде…")); container.replaceChildren(loader); container.setAttribute("aria-busy", "true");
  try { const content = await pages[route](); if (seq !== state.load || !state.user) return; container.replaceChildren(content); if (deepAudit) void auditDetail(deepAudit[1]); else maybeHelp(); }
  catch (error) { if (seq !== state.load || !state.user) return; const box = el("div", "error-panel"); box.append(el("h2", "", error.status === 403 ? "Бұл бөлімге рұқсат жоқ" : "Деректер жүктелмеді"), el("p", "", userError(error)), button("Қайта көру", () => void renderRoute(), "button primary")); container.replaceChildren(heading(title, ""), box); }
  finally { if (seq === state.load) container.setAttribute("aria-busy", "false"); }
}
translateStatic(document.body);
installLanguageControls();
setAuthMode("login");
document.addEventListener("input", (event) => { if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement || event.target instanceof HTMLSelectElement) event.target.setCustomValidity(""); });
document.addEventListener("invalid", (event) => { const control = event.target; if (!control.validity) return; const validity = control.validity; control.setCustomValidity(validity.valueMissing ? t("error.required") : validity.tooShort ? t("error.minLength", { min: number(control.minLength) }) : validity.tooLong ? t("error.maxLength", { max: number(control.maxLength) }) : validity.rangeUnderflow && control.dataset.futureDeadline ? t("error.deadline_in_past") : validity.rangeUnderflow || validity.rangeOverflow ? t("error.range", { min: control.min, max: control.max }) : t("error.validation")); }, true);
try { const data = await api("/api/auth/me"); enterApp(data.user); }
catch (error) { showAuth(error.status === 401 ? "" : userError(error)); }
