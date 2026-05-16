/**
 * background.js — Service Worker.
 *
 * Что делает:
 *   1) Принимает сообщения от popup/content_script и общается с бэкендом.
 *   2) Слушает chrome.tabs.onUpdated: на каждый watch?v=... шлёт
 *      content_script команду «попробуй авто-загрузиться».
 *   3) Сам решений по анализу не принимает — это content_script,
 *      исходя из тумблера и наличия кэша.
 *
 * Эндпоинты:
 *   GET  /video/{id}     — кэшированный анализ (404 = ещё не считалось)
 *   POST /analyze        — отдаст кэш или посчитает заново и сохранит
 *   POST /reanalyze      — всегда новая версия в БД
 */

const BACKEND_URL = "http://localhost:8000";
const STORAGE_KEY = "nmai_enabled";

// ─── helpers ───────────────────────────────────────────────────────────────

function extractVideoId(url) {
  const patterns = [
    /[?&]v=([A-Za-z0-9_-]{11})/,
    /youtu\.be\/([A-Za-z0-9_-]{11})/,
    /embed\/([A-Za-z0-9_-]{11})/,
    /shorts\/([A-Za-z0-9_-]{11})/,
  ];
  for (const re of patterns) {
    const m = url?.match(re);
    if (m) return m[1];
  }
  if (typeof url === "string" && /^[A-Za-z0-9_-]{11}$/.test(url)) return url;
  return null;
}

async function backendGetState(videoId) {
  const r = await fetch(`${BACKEND_URL}/video/${videoId}`);
  if (r.status === 404) return { notFound: true };
  if (!r.ok) throw new Error(`Бэкенд (${r.status}): ${(await r.text()).slice(0, 200)}`);
  return { ok: true, data: await r.json() };
}

async function backendAnalyze(videoId, { force = false } = {}) {
  const path = force ? "/reanalyze" : "/analyze";
  const r = await fetch(`${BACKEND_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ video_id: videoId }),
  });
  if (!r.ok) throw new Error(`Бэкенд (${r.status}): ${(await r.text()).slice(0, 200)}`);
  return { ok: true, data: await r.json() };
}

async function isEnabled() {
  const { [STORAGE_KEY]: enabled = true } = await chrome.storage.sync.get(STORAGE_KEY);
  return !!enabled;
}

// ─── Сообщения от popup и content_script ──────────────────────────────────

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    try {
      if (message.action === "getState") {
        const id = extractVideoId(message.url);
        if (!id) return sendResponse({ ok: false, error: "Не удалось извлечь ID видео" });
        const res = await backendGetState(id);
        return sendResponse(res);
      }

      if (message.action === "analyze") {
        // тот же эндпоинт что и кэш-чек, но создаст запись если её нет
        const id = extractVideoId(message.url);
        if (!id) return sendResponse({ ok: false, error: "Не удалось извлечь ID видео" });
        const res = await backendAnalyze(id, { force: false });
        return sendResponse(res);
      }

      if (message.action === "reanalyze") {
        const id = extractVideoId(message.url);
        if (!id) return sendResponse({ ok: false, error: "Не удалось извлечь ID видео" });
        const res = await backendAnalyze(id, { force: true });
        return sendResponse(res);
      }

      if (message.action === "overlayReady") {
        return sendResponse({ ok: true });
      }
    } catch (err) {
      sendResponse({ ok: false, error: err.message });
    }
  })();

  return true; // async
});

// ─── Авто-инициализация при навигации на видео ─────────────────────────────

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  // YouTube — SPA: URL меняется без полной перезагрузки, ловим оба события
  if (!tab?.url || !tab.url.includes("youtube.com/watch")) return;
  if (changeInfo.status !== "complete" && !changeInfo.url) return;

  if (!(await isEnabled())) return;

  // Просто пнём content_script — он сам решит что делать.
  // (sendMessage может упасть если в этой вкладке ещё нет content_script.)
  chrome.tabs.sendMessage(tabId, { action: "autoBootstrap" }).catch(() => {});
});

// Когда пользователь дёрнул тумблер — расскажем всем активным вкладкам.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "sync" || !changes[STORAGE_KEY]) return;
  const enabled = !!changes[STORAGE_KEY].newValue;
  chrome.tabs.query({ url: "*://www.youtube.com/watch*" }, (tabs) => {
    for (const t of tabs) {
      chrome.tabs.sendMessage(t.id, { action: "setEnabled", enabled }).catch(() => {});
    }
  });
});
