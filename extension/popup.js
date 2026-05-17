/**
 * popup.js — UI попапа NorthMedAI.
 *
 * Связки:
 *   chrome.storage.sync.nmai_enabled — глобальный тумблер авто-проверки
 *   chrome.storage.sync.nmai_ticks   — показывать ли тики на таймлайне
 *   chrome.storage.sync.nmai_voice   — озвучка ошибок (пока заглушка)
 *   chrome.storage.sync.nmai_lang    — ru | en
 *   chrome.storage.sync.nmai_backend — URL бэкенда (default localhost:8000)
 *
 * Связь с активной вкладкой:
 *   getState/analyze — через background
 *   showOverlay      — отправка claims в content_script вкладки
 *   setEnabled / setTicks — мгновенное применение изменений в content_script
 */

const STORAGE = {
  enabled: "nmai_enabled",
  ticks:   "nmai_ticks",
  voice:   "nmai_voice",
  lang:    "nmai_lang",
  backend: "nmai_backend",
};

const DEFAULTS = {
  [STORAGE.enabled]: true,
  [STORAGE.ticks]:   true,
  [STORAGE.voice]:   false,
  [STORAGE.lang]:    detectInitialLang(),
  [STORAGE.backend]: "http://localhost:8000",
};

function detectInitialLang() {
  const ui = (chrome.i18n?.getUILanguage?.() || navigator.language || "ru").toLowerCase();
  return ui.startsWith("ru") ? "ru" : "en";
}

// ─── DOM refs ──────────────────────────────────────────────────────────────

const els = {
  mainView:      document.getElementById("main-view"),
  settingsView:  document.getElementById("settings-view"),
  settingsBtn:   document.getElementById("settings-btn"),
  backBtn:       document.getElementById("back-btn"),
  resetBtn:      document.getElementById("reset-btn"),

  statusCard:    document.getElementById("status-card"),
  statusTitle:   document.getElementById("status-title"),
  statusSub:     document.getElementById("status-sub"),

  enabledToggle: document.getElementById("enabled-toggle"),
  ticksToggle:   document.getElementById("ticks-toggle"),
  voiceToggle:   document.getElementById("voice-toggle"),

  countFalse:    document.getElementById("count-false"),
  countDisputed: document.getElementById("count-disputed"),
  countSophism:  document.getElementById("count-sophism"),
  tiles: {
    false:    document.querySelector(".tile.false"),
    disputed: document.querySelector(".tile.disputed"),
    sophism:  document.querySelector(".tile.sophism"),
  },

  recheckBtn:    document.getElementById("recheck-btn"),
  langChips:     document.querySelectorAll(".lang-chip"),
  backendInput:  document.getElementById("backend-input"),
};

// ─── State ────────────────────────────────────────────────────────────────

let currentLang = "ru";
let currentTab  = null;

// ─── Init ─────────────────────────────────────────────────────────────────

(async function init() {
  const stored = await chrome.storage.sync.get(Object.values(STORAGE));
  const cfg = { ...DEFAULTS, ...stored };

  currentLang = cfg[STORAGE.lang];
  applyI18n(currentLang);

  els.enabledToggle.checked = cfg[STORAGE.enabled];
  els.ticksToggle.checked   = cfg[STORAGE.ticks];
  els.voiceToggle.checked   = cfg[STORAGE.voice];
  els.backendInput.value    = cfg[STORAGE.backend];
  markLangChip(currentLang);

  [currentTab] = await chrome.tabs.query({ active: true, currentWindow: true });

  if (isYouTubeVideo(currentTab?.url)) {
    await loadCurrentState();
  } else {
    setNotYouTubeState();
  }

  // Если статус видео потребовал перерисовки тумблера — он мог поменять enabled.
  // Перечитаем после первой попытки.
  refreshStatusFromToggle();
})();

function isYouTubeVideo(url) {
  return !!url && /^https?:\/\/(www\.)?youtube\.com\/watch/.test(url);
}

// ─── i18n ─────────────────────────────────────────────────────────────────

function applyI18n(lang) {
  currentLang = lang;
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    const key = node.getAttribute("data-i18n");
    node.textContent = window.NMAI_I18N.t(lang, key);
  });
  document.querySelectorAll("[data-i18n-title]").forEach((node) => {
    const key = node.getAttribute("data-i18n-title");
    node.title = window.NMAI_I18N.t(lang, key);
  });
}

function markLangChip(lang) {
  els.langChips.forEach((b) => {
    b.classList.toggle("active", b.dataset.lang === lang);
  });
}

// ─── Status card ──────────────────────────────────────────────────────────

function setStatus({ mode, title, sub }) {
  els.statusCard.classList.remove("on", "off", "checking");
  if (mode) els.statusCard.classList.add(mode);
  els.statusTitle.textContent = title ?? "";
  els.statusSub.textContent   = sub ?? "";
}

function refreshStatusFromToggle() {
  // Вызывается когда нет реальных данных по видео, чтобы карточка показывала
  // корректный «выкл/вкл» текст из i18n.
  if (els.recheckBtn.disabled) return; // идёт анализ — не трогаем
  if (!isYouTubeVideo(currentTab?.url)) return;

  if (!els.enabledToggle.checked) {
    setStatus({
      mode: "off",
      title: window.NMAI_I18N.t(currentLang, "auto_off_title"),
      sub:   window.NMAI_I18N.t(currentLang, "auto_off_sub"),
    });
  }
}

function setNotYouTubeState() {
  setStatus({
    mode: "off",
    title: window.NMAI_I18N.t(currentLang, "auto_off_title"),
    sub:   window.NMAI_I18N.t(currentLang, "not_youtube"),
  });
  els.recheckBtn.disabled = true;
}

// ─── Loading state from backend ───────────────────────────────────────────

async function loadCurrentState() {
  if (!els.enabledToggle.checked) {
    setStatus({
      mode: "off",
      title: window.NMAI_I18N.t(currentLang, "auto_off_title"),
      sub:   window.NMAI_I18N.t(currentLang, "auto_off_sub"),
    });
    return;
  }

  setStatus({
    mode: "checking",
    title: window.NMAI_I18N.t(currentLang, "checking"),
    sub:   "",
  });

  const resp = await sendMessageAsync({ action: "getState", url: currentTab.url });
  if (resp?.notFound) {
    // Запись ещё не создана — content_script сам сделает analyze.
    // Здесь просто покажем "анализирую".
    return;
  }
  handleResponse(resp);
}

// ─── Reactions to backend response ────────────────────────────────────────

function handleResponse(resp) {
  if (!resp?.ok) {
    const msg = resp?.error ?? window.NMAI_I18N.t(currentLang, "err_unknown");
    let key = null;
    if (msg.includes("Connection reset") || msg.includes("502") || msg.includes("временно не отдаёт")) key = "err_connection_reset";
    else if (msg.includes("отключены") || msg.includes("не найдено") || msg.includes("subtitles")) key = "err_no_subtitles";

    setStatus({
      mode: "off",
      title: window.NMAI_I18N.t(currentLang, "err_unknown"),
      sub:   key ? window.NMAI_I18N.t(currentLang, key) : msg,
    });
    return;
  }

  const data = resp.data ?? {};
  const claims = data.claims ?? [];
  showCounts(claims);

  const ttl = window.NMAI_I18N.t(currentLang, "auto_on_title");
  const cnt = window.NMAI_I18N.claimsCount(currentLang, claims.length);
  const sub = data.cached
    ? window.NMAI_I18N.t(currentLang, "cache_loaded", { when: formatDate(data.created_at) })
    : cnt;

  setStatus({
    mode: "on",
    title: data.cached ? ttl : cnt,
    sub:   data.cached ? cnt + " · " + sub : sub,
  });

  pushOverlayToTab(claims);
}

function showCounts(claims) {
  const f = claims.filter(c => c.verdict === "false").length;
  // «Спорные» включают misleading, conflicting и unverifiable.
  // unverifiable — это claim'ы без авторитетных подтверждений,
  // для зрителя они проходят в той же категории «надо обратить внимание».
  const d = claims.filter(c =>
    c.verdict === "misleading" ||
    c.verdict === "conflicting" ||
    c.verdict === "unverifiable"
  ).length;
  const s = claims.filter(c => c.type === "sophism").length;

  els.countFalse.textContent    = f;
  els.countDisputed.textContent = d;
  els.countSophism.textContent  = s;

  els.tiles.false.classList.toggle("zero", f === 0);
  els.tiles.disputed.classList.toggle("zero", d === 0);
  els.tiles.sophism.classList.toggle("zero", s === 0);
}

async function pushOverlayToTab(claims) {
  if (!currentTab?.id) return;
  chrome.tabs.sendMessage(currentTab.id, { action: "showOverlay", claims }).catch(() => {});
}

// ─── Listeners: главный тумблер ──────────────────────────────────────────

els.enabledToggle.addEventListener("change", async () => {
  const v = els.enabledToggle.checked;
  await chrome.storage.sync.set({ [STORAGE.enabled]: v });
  // background сам разошлёт всем YouTube-вкладкам setEnabled (см. background.js)
  if (v) {
    if (isYouTubeVideo(currentTab?.url)) loadCurrentState();
    else setNotYouTubeState();
  } else {
    setStatus({
      mode: "off",
      title: window.NMAI_I18N.t(currentLang, "auto_off_title"),
      sub:   window.NMAI_I18N.t(currentLang, "auto_off_sub"),
    });
    // Тут же скрываем overlay в активной вкладке
    if (currentTab?.id) chrome.tabs.sendMessage(currentTab.id, { action: "setEnabled", enabled: false }).catch(() => {});
  }
});

// ─── Listeners: тики ─────────────────────────────────────────────────────

els.ticksToggle.addEventListener("change", async () => {
  const v = els.ticksToggle.checked;
  await chrome.storage.sync.set({ [STORAGE.ticks]: v });
  if (currentTab?.id) {
    chrome.tabs.sendMessage(currentTab.id, { action: "setTicks", enabled: v }).catch(() => {});
  }
});

// ─── Listeners: voice (заглушка) ─────────────────────────────────────────

els.voiceToggle.addEventListener("change", async () => {
  await chrome.storage.sync.set({ [STORAGE.voice]: els.voiceToggle.checked });
  console.log("[NMAI] voice toggle changed — фича пока не реализована");
});

// ─── Listeners: re-check ─────────────────────────────────────────────────

els.recheckBtn.addEventListener("click", async () => {
  if (!isYouTubeVideo(currentTab?.url)) return;
  els.recheckBtn.disabled = true;

  setStatus({
    mode: "checking",
    title: window.NMAI_I18N.t(currentLang, "checking"),
    sub:   "",
  });

  const resp = await sendMessageAsync({ action: "reanalyze", url: currentTab.url });
  els.recheckBtn.disabled = false;
  handleResponse(resp);
});

// ─── Listeners: Settings ─────────────────────────────────────────────────

els.settingsBtn.addEventListener("click", () => {
  els.settingsView.classList.add("open");
});
els.backBtn.addEventListener("click", () => {
  els.settingsView.classList.remove("open");
});

els.langChips.forEach((btn) => {
  btn.addEventListener("click", async () => {
    const lang = btn.dataset.lang;
    await chrome.storage.sync.set({ [STORAGE.lang]: lang });
    applyI18n(lang);
    markLangChip(lang);
    // обновим статус-карточку (там тоже текст из i18n)
    refreshStatusFromToggle();
  });
});

els.backendInput.addEventListener("change", async () => {
  const v = els.backendInput.value.trim() || DEFAULTS[STORAGE.backend];
  await chrome.storage.sync.set({ [STORAGE.backend]: v });
});

els.resetBtn.addEventListener("click", async () => {
  await chrome.storage.sync.clear();
  await chrome.storage.sync.set(DEFAULTS);
  // Перечитываем
  Object.assign(els.enabledToggle, { checked: DEFAULTS[STORAGE.enabled] });
  Object.assign(els.ticksToggle,   { checked: DEFAULTS[STORAGE.ticks] });
  Object.assign(els.voiceToggle,   { checked: DEFAULTS[STORAGE.voice] });
  els.backendInput.value = DEFAULTS[STORAGE.backend];
  applyI18n(DEFAULTS[STORAGE.lang]);
  markLangChip(DEFAULTS[STORAGE.lang]);
});

// ─── Helpers ──────────────────────────────────────────────────────────────

function sendMessageAsync(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, (response) => {
      if (chrome.runtime.lastError) {
        resolve({ ok: false, error: chrome.runtime.lastError.message });
        return;
      }
      resolve(response);
    });
  });
}

function formatDate(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return d.toLocaleString(currentLang === "ru" ? "ru-RU" : "en-US",
      { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}
