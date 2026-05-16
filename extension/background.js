/**
 * background.js — Service Worker
 *
 * Новая упрощённая архитектура:
 *   popup  →  { action: "analyze", url }
 *   background  →  POST /analyze?video_id=xxx  на бэкенд (localhost)
 *   бэкенд сам забирает субтитры через youtube-transcript-api
 *   background  →  popup: { ok, data }
 */

const BACKEND_URL = "http://localhost:8000";

// Утилита: извлечь video_id из YouTube URL
function extractVideoId(url) {
  const patterns = [
    /[?&]v=([A-Za-z0-9_-]{11})/,
    /youtu\.be\/([A-Za-z0-9_-]{11})/,
    /embed\/([A-Za-z0-9_-]{11})/,
    /shorts\/([A-Za-z0-9_-]{11})/,
  ];
  for (const re of patterns) {
    const m = url.match(re);
    if (m) return m[1];
  }
  // Голый ID
  if (/^[A-Za-z0-9_-]{11}$/.test(url)) return url;
  return null;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === "analyze") {
    handleAnalyze(message.url)
      .then((result) => sendResponse({ ok: true, data: result }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true; // async response
  }

  if (message.action === "overlayReady") {
    // content_script сообщает что готов принимать метки
    sendResponse({ ok: true });
  }
});

async function handleAnalyze(url) {
  const videoId = extractVideoId(url);
  if (!videoId) throw new Error("Не удалось извлечь ID видео из URL");

  // Бэкенд сам получает субтитры и анализирует
  const response = await fetch(`${BACKEND_URL}/analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ video_id: videoId }),
  });

  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`Ошибка бэкенда (${response.status}): ${text.slice(0, 200)}`);
  }

  return response.json();
}
