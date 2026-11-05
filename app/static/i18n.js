const response = await fetch("/static/locales.json", { credentials: "same-origin" });
if (!response.ok) throw new Error("Locale catalog unavailable");
const messages = await response.json();
const sourceIndex = new Map(Object.entries(messages).map(([key, value]) => [value.kk, key]));
const locales = new Set(["kk", "ru", "en"]);
let saved;
try { saved = localStorage.getItem("arm_locale"); } catch (error) { if (!(error instanceof DOMException)) throw error; }
export let locale = locales.has(saved) ? saved : "kk";
export function setLocale(value) {
  if (!locales.has(value)) return;
  locale = value;
  try { localStorage.setItem("arm_locale", locale); } catch (error) { if (!(error instanceof DOMException)) throw error; }
  document.documentElement.lang = locale;
}
export function t(key, params = {}) {
  const row = messages[key];
  let value = row?.[locale] || row?.kk || key;
  for (const [name, replacement] of Object.entries(params)) value = value.replaceAll(`{${name}}`, String(replacement));
  return value;
}
export function hasMessage(key) { return Object.hasOwn(messages, key); }
export function L(value) { return typeof value === "string" ? hasMessage(value) ? t(value) : sourceIndex.has(value) ? t(sourceIndex.get(value)) : value : value; }
export function number(value) { return new Intl.NumberFormat(locale === "kk" ? "kk-KZ" : locale === "ru" ? "ru-RU" : "en-GB").format(value); }
export function date(value, utc = false) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return new Intl.DateTimeFormat(locale === "kk" ? "kk-KZ" : locale === "ru" ? "ru-RU" : "en-GB", { dateStyle: "short", timeStyle: "medium", ...(utc ? { timeZone: "UTC" } : {}) }).format(parsed) + (utc ? " UTC" : "");
}
export function errorMessage(code, fallback) { return hasMessage(`error.${code}`) ? t(`error.${code}`) : fallback ? L(fallback) : t("error.generic"); }
const originalText = new WeakMap();
const originalAttributes = new WeakMap();
let staticTexts;
let staticElements;
export function translateStatic(root) {
  if (!staticTexts) {
    staticTexts = []; const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT); let node;
    while ((node = walker.nextNode())) if (!["SCRIPT", "STYLE"].includes(node.parentElement?.tagName)) { staticTexts.push(node); originalText.set(node, node.textContent); }
    staticElements = [...root.querySelectorAll("[aria-label], [title], [placeholder], [alt]")];
    for (const element of staticElements) { const originals = new Map(); for (const attr of ["aria-label", "title", "placeholder", "alt"]) if (element.hasAttribute(attr)) originals.set(attr, element.getAttribute(attr)); originalAttributes.set(element, originals); }
  }
  for (const node of staticTexts) {
    if (!node.isConnected) continue;
    const source = originalText.get(node); const stripped = source.trim();
    if (hasMessage(stripped)) node.textContent = source.replace(stripped, t(stripped));
  }
  for (const element of staticElements) {
    const originals = originalAttributes.get(element);
    for (const [attr, source] of originals) element.setAttribute(attr, L(source));
  }
}
setLocale(locale);
