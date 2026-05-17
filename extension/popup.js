/**
 * popup.js — NorthMedAI UI
 * Исправлено: на не-YouTube страницах статус соответствует реальному состоянию тумблера.
 */

const STORAGE = {
  enabled: "nmai_enabled",
  ticks:   "nmai_ticks",
  voice:   "nmai_voice",
  lang:    "nmai_lang",
  theme:   "nmai_theme",
};

const DEFAULTS = {
  [STORAGE.enabled]: true,
  [STORAGE.ticks]:   true,
  [STORAGE.voice]:   false,
  [STORAGE.lang]:    detectInitialLang(),
  [STORAGE.theme]:   "dark",
};

function detectInitialLang() {
  const ui = (chrome.i18n?.getUILanguage?.() || navigator.language || "ru").toLowerCase();
  return ui.startsWith("ru") ? "ru" : "en";
}

// ─── DOM элементы ──────────────────────────────────────────────────────────
const els = {
  mainView:          document.getElementById("main-view"),
  settingsView:      document.getElementById("settings-view"),
  backBtn:           document.getElementById("back-btn"),
  resetBtn:          document.getElementById("reset-btn"),
  statusCard:        document.getElementById("status-card"),
  statusTitle:       document.getElementById("status-title"),
  statusSub:         document.getElementById("status-sub"),
  enabledToggle:     document.getElementById("enabled-toggle"),
  ticksToggle:       document.getElementById("ticks-toggle"),
  voiceToggle:       document.getElementById("voice-toggle"),
  countFalse:        document.getElementById("count-false"),
  countDisputed:     document.getElementById("count-disputed"),
  countSophism:      document.getElementById("count-sophism"),
  tiles: {
    false:    document.querySelector(".tile.false"),
    disputed: document.querySelector(".tile.disputed"),
    sophism:  document.querySelector(".tile.sophism"),
  },
  recheckBtn:        document.getElementById("recheck-btn"),
  langChips:         document.querySelectorAll(".lang-chip"),
  themeHeaderBtn:    document.getElementById("theme-header-btn"),
  settingsHeaderBtn: document.getElementById("settings-header-btn"),
};

let currentLang = "ru";
let currentTab = null;

// ─── Тема: загрузка, сохранение, переключение ─────────────────────────────
function injectThemeStyles() {
  if (document.getElementById("nmai-theme-styles")) return;
  const style = document.createElement("style");
  style.id = "nmai-theme-styles";
  style.textContent = `
    body.light-theme {
      --bg:        #ffffff;
      --surface:   #f6f8fa;
      --surface-2: #eaeef2;
      --border:    #d0d7de;
      --text:      #1f2328;
      --muted:     #656d76;
      --muted-2:   #8c959f;
      --accent:    #0969da;
      --accent-2:  #0550ae;
      --good:      #2da44e;
      --warn:      #bf8700;
      --bad:       #cf222e;
      --purple:    #8250df;
    }
    body.light-theme .status-card.on {
      border-color: rgba(45, 164, 78, 0.3);
      background: linear-gradient(180deg, rgba(45,164,78,0.05), transparent 70%), var(--surface);
    }
    body.light-theme .slider {
      background: #8c959f;
    }
    body.light-theme .slider::before {
      background: white;
    }
    body.light-theme .lang-chip.active {
      background: rgba(9, 105, 218, 0.1);
      border-color: var(--accent);
      color: var(--accent);
    }
    body.light-theme .icon-btn:hover {
      background: var(--surface-2);
    }
  `;
  document.head.appendChild(style);
}

async function loadTheme() {
  const stored = await chrome.storage.local.get([STORAGE.theme]);
  const theme = stored[STORAGE.theme] || DEFAULTS[STORAGE.theme];
  if (theme === "light") {
    document.body.classList.add("light-theme");
    if (els.themeHeaderBtn) els.themeHeaderBtn.textContent = "☀️";
  } else {
    document.body.classList.remove("light-theme");
    if (els.themeHeaderBtn) els.themeHeaderBtn.textContent = "🌙";
  }
}

async function setTheme(theme) {
  await chrome.storage.local.set({ [STORAGE.theme]: theme });
  if (theme === "light") {
    document.body.classList.add("light-theme");
    if (els.themeHeaderBtn) els.themeHeaderBtn.textContent = "☀️";
  } else {
    document.body.classList.remove("light-theme");
    if (els.themeHeaderBtn) els.themeHeaderBtn.textContent = "🌙";
  }
}

function toggleTheme() {
  const isLight = document.body.classList.contains("light-theme");
  setTheme(isLight ? "dark" : "light");
}

// ─── Вспомогательные функции ──────────────────────────────────────────────
function isYouTubeVideo(url) {
  return !!url && /^https?:\/\/(www\.)?youtube\.com\/watch/.test(url);
}

function applyI18n(lang) {
  currentLang = lang;
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    const key = node.getAttribute("data-i18n");
    if (window.NMAI_I18N && window.NMAI_I18N.t) {
      node.textContent = window.NMAI_I18N.t(lang, key);
    }
  });
  document.querySelectorAll("[data-i18n-title]").forEach((node) => {
    const key = node.getAttribute("data-i18n-title");
    if (window.NMAI_I18N && window.NMAI_I18N.t) {
      node.title = window.NMAI_I18N.t(lang, key);
    }
  });
}

function markLangChip(lang) {
  els.langChips.forEach((chip) => {
    chip.classList.toggle("active", chip.dataset.lang === lang);
  });
}

function setStatus({ mode, title, sub }) {
  els.statusCard.classList.remove("on", "off", "checking");
  if (mode) els.statusCard.classList.add(mode);
  els.statusTitle.textContent = title ?? "";
  els.statusSub.textContent = sub ?? "";
}

// ✅ Исправленная функция для состояния "не YouTube"
function setNotYouTubeState() {
  const isEnabled = els.enabledToggle.checked;
  const title = isEnabled
    ? (window.NMAI_I18N?.t(currentLang, "auto_on_title") || "Авто-проверка включена")
    : (window.NMAI_I18N?.t(currentLang, "auto_off_title") || "Авто-проверка выключена");
  const sub = window.NMAI_I18N?.t(currentLang, "not_youtube") || "Откройте YouTube видео";
  setStatus({
    mode: "off",
    title: title,
    sub: sub,
  });
  els.recheckBtn.disabled = true;
}

function refreshStatusFromToggle() {
  if (els.recheckBtn.disabled) return;
  if (!isYouTubeVideo(currentTab?.url)) return;
  if (!els.enabledToggle.checked) {
    setStatus({
      mode: "off",
      title: window.NMAI_I18N?.t(currentLang, "auto_off_title") || "Автоматическая проверка выключена",
      sub: window.NMAI_I18N?.t(currentLang, "auto_off_sub") || "Включите, чтобы анализировать видео",
    });
  }
}

function showCounts(claims) {
  const f = claims.filter(c => c.verdict === "false").length;
  const d = claims.filter(c =>
    c.verdict === "misleading" ||
    c.verdict === "conflicting" ||
    c.verdict === "unverifiable"
  ).length;
  const s = claims.filter(c => c.type === "sophism").length;

  els.countFalse.textContent = f;
  els.countDisputed.textContent = d;
  els.countSophism.textContent = s;

  els.tiles.false.classList.toggle("zero", f === 0);
  els.tiles.disputed.classList.toggle("zero", d === 0);
  els.tiles.sophism.classList.toggle("zero", s === 0);
}

async function pushOverlayToTab(claims) {
  if (!currentTab?.id) return;
  chrome.tabs.sendMessage(currentTab.id, { action: "showOverlay", claims }).catch(() => {});
}

function showPipeline(data) {
  const card = document.getElementById("pipeline-card");
  if (!card) return;
  const stats = data.pipeline_stats;
  const versions = data.versions || {};
  if (!stats) {
    card.hidden = true;
    return;
  }
  card.hidden = false;

  const timeEl = document.getElementById("pipeline-time");
  if (timeEl) {
    const duration = stats.duration_s;
    timeEl.textContent = (typeof duration === "number") ? `${duration.toFixed(1)} с` : "";
  }

  const funnel = document.getElementById("pipeline-funnel");
  if (funnel) {
    const inN = stats.claims_in ?? 0;
    const afterStance = stats.claims_after_drop ?? inN;
    const beforeQA = stats.claims_before_qa ?? afterStance;
    const finalN = stats.final_claims ?? 0;
    const stanceDropped = (stats.stance_debunked_fully ?? 0) + (stats.stance_quoted_neutral ?? 0);
    const qaDropped = stats.qa_dropped ?? 0;
    const qaRepaired = stats.qa_repaired ?? 0;
    const qaDedup = stats.qa_dedup_merges ?? 0;

    const denom = Math.max(inN, 1);
    const rows = [
      { name: "Extractor", val: inN, drop: false },
      { name: "Stance drop", val: stanceDropped, drop: true },
      { name: "После Stance", val: afterStance, drop: false },
      { name: "После Judge", val: beforeQA, drop: false },
      { name: "QA drop", val: qaDropped, drop: true },
      { name: "QA repair", val: qaRepaired, drop: true },
      { name: "QA dedup", val: qaDedup, drop: true },
      { name: "В оверлей", val: finalN, drop: false },
    ];

    funnel.innerHTML = rows.map((row) => {
      const pct = Math.min(100, Math.round((row.val / denom) * 100));
      return `
        <div class="pipeline-stage ${row.drop ? "drop" : ""}">
          <span class="stage-name">${row.name}</span>
          <span class="stage-bar"><span style="width:${pct}%"></span></span>
          <span class="stage-value">${row.val}</span>
        </div>`;
    }).join("");
  }

  const vEl = document.getElementById("pipeline-versions");
  if (vEl) {
    const items = [
      ["Detector", versions.detector],
      ["Stance", versions.stance],
      ["Retriever", versions.retriever],
      ["Judge", versions.judge],
      ["QA", versions.qa],
    ].filter(([, version]) => version);

    vEl.innerHTML = items.map(([name, version]) =>
      `<span class="ver-badge"><b>${name}</b>${version}</span>`
    ).join("");
  }
}

function handleResponse(resp) {
  if (!resp?.ok) {
    const msg = resp?.error ?? "Ошибка анализа";
    let key = null;
    if (msg.includes("Connection reset") || msg.includes("502") || msg.includes("временно")) key = "err_connection_reset";
    else if (msg.includes("отключены") || msg.includes("не найдено") || msg.includes("subtitles")) key = "err_no_subtitles";
    setStatus({
      mode: "off",
      title: window.NMAI_I18N?.t(currentLang, "err_unknown") || "Ошибка",
      sub: key ? (window.NMAI_I18N?.t(currentLang, key) || msg) : msg,
    });
    return;
  }

  const data = resp.data ?? {};
  const claims = data.claims ?? [];
  showCounts(claims);
  showPipeline(data);

  const ttl = window.NMAI_I18N?.t(currentLang, "auto_on_title") || "Анализ выполнен";
  const cnt = window.NMAI_I18N?.claimsCount?.(currentLang, claims.length) || `${claims.length} утверждений`;
  const sub = data.cached
    ? window.NMAI_I18N?.t(currentLang, "cache_loaded", { when: formatDate(data.created_at) }) || "из кеша"
    : cnt;

  setStatus({
    mode: "on",
    title: data.cached ? ttl : cnt,
    sub: data.cached ? cnt + " · " + sub : sub,
  });
  pushOverlayToTab(claims);
}

async function loadCurrentState() {
  if (!els.enabledToggle.checked) {
    setStatus({
      mode: "off",
      title: window.NMAI_I18N?.t(currentLang, "auto_off_title") || "Выключено",
      sub: window.NMAI_I18N?.t(currentLang, "auto_off_sub") || "",
    });
    return;
  }
  setStatus({
    mode: "checking",
    title: window.NMAI_I18N?.t(currentLang, "checking") || "Анализируем...",
    sub: "",
  });
  const resp = await sendMessageAsync({ action: "getState", url: currentTab.url });
  if (resp?.notFound) return;
  handleResponse(resp);
}

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
    return d.toLocaleString(currentLang === "ru" ? "ru-RU" : "en-US", {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

// ─── Инициализация ─────────────────────────────────────────────────────────
(async function init() {
  injectThemeStyles();
  await loadTheme();

  const stored = await chrome.storage.sync.get(Object.values(STORAGE));
  const cfg = { ...DEFAULTS, ...stored };

  currentLang = cfg[STORAGE.lang];
  applyI18n(currentLang);
  markLangChip(currentLang);

  els.enabledToggle.checked = cfg[STORAGE.enabled];
  els.ticksToggle.checked = cfg[STORAGE.ticks];
  els.voiceToggle.checked = cfg[STORAGE.voice];

  [currentTab] = await chrome.tabs.query({ active: true, currentWindow: true });

  if (isYouTubeVideo(currentTab?.url)) {
    await loadCurrentState();
  } else {
    setNotYouTubeState();  // теперь учитывает реальное состояние тумблера
  }
  refreshStatusFromToggle();
})();

// ─── Обработчики событий ───────────────────────────────────────────────────
els.enabledToggle.addEventListener("change", async () => {
  const v = els.enabledToggle.checked;
  await chrome.storage.sync.set({ [STORAGE.enabled]: v });
  if (v) {
    if (isYouTubeVideo(currentTab?.url)) {
      await loadCurrentState();
    } else {
      setNotYouTubeState();  // обновляем статус для не-YouTube
    }
  } else {
    if (isYouTubeVideo(currentTab?.url)) {
      setStatus({
        mode: "off",
        title: window.NMAI_I18N?.t(currentLang, "auto_off_title") || "Выключено",
        sub: window.NMAI_I18N?.t(currentLang, "auto_off_sub") || "",
      });
      if (currentTab?.id) chrome.tabs.sendMessage(currentTab.id, { action: "setEnabled", enabled: false }).catch(() => {});
    } else {
      setNotYouTubeState();  // при выключении на не-YouTube
    }
  }
});

els.ticksToggle.addEventListener("change", async () => {
  const v = els.ticksToggle.checked;
  await chrome.storage.sync.set({ [STORAGE.ticks]: v });
  if (currentTab?.id) {
    chrome.tabs.sendMessage(currentTab.id, { action: "setTicks", enabled: v }).catch(() => {});
  }
});

els.voiceToggle.addEventListener("change", async () => {
  await chrome.storage.sync.set({ [STORAGE.voice]: els.voiceToggle.checked });
  // голос пока не реализован
});

els.recheckBtn.addEventListener("click", async () => {
  if (!isYouTubeVideo(currentTab?.url)) return;
  els.recheckBtn.disabled = true;
  setStatus({
    mode: "checking",
    title: window.NMAI_I18N?.t(currentLang, "checking") || "Анализируем...",
    sub: "",
  });
  const resp = await sendMessageAsync({ action: "reanalyze", url: currentTab.url });
  els.recheckBtn.disabled = false;
  handleResponse(resp);
});

// Настройки: открытие/закрытие
if (els.settingsHeaderBtn) {
  els.settingsHeaderBtn.addEventListener("click", () => {
    els.settingsView.classList.add("open");
  });
}
els.backBtn.addEventListener("click", () => {
  els.settingsView.classList.remove("open");
});

// Переключение языка
els.langChips.forEach((btn) => {
  btn.addEventListener("click", async () => {
    const lang = btn.dataset.lang;
    await chrome.storage.sync.set({ [STORAGE.lang]: lang });
    applyI18n(lang);
    markLangChip(lang);
    refreshStatusFromToggle();
    if (isYouTubeVideo(currentTab?.url) && els.enabledToggle.checked) {
      await loadCurrentState();
    } else {
      if (!isYouTubeVideo(currentTab?.url)) {
        setNotYouTubeState();
      } else if (!els.enabledToggle.checked) {
        setStatus({
          mode: "off",
          title: window.NMAI_I18N?.t(currentLang, "auto_off_title") || "Выключено",
          sub: window.NMAI_I18N?.t(currentLang, "auto_off_sub") || "",
        });
      }
    }
  });
});

// Кнопка сброса настроек
els.resetBtn.addEventListener("click", async () => {
  await chrome.storage.sync.clear();
  await chrome.storage.sync.set(DEFAULTS);
  els.enabledToggle.checked = DEFAULTS[STORAGE.enabled];
  els.ticksToggle.checked = DEFAULTS[STORAGE.ticks];
  els.voiceToggle.checked = DEFAULTS[STORAGE.voice];
  applyI18n(DEFAULTS[STORAGE.lang]);
  markLangChip(DEFAULTS[STORAGE.lang]);
  if (isYouTubeVideo(currentTab?.url)) {
    await loadCurrentState();
  } else {
    setNotYouTubeState();
  }
});

// Кнопка темы в шапке
if (els.themeHeaderBtn) {
  els.themeHeaderBtn.addEventListener("click", toggleTheme);
}
