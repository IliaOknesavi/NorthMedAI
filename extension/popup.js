/**
 * popup.js
 * Управляет UI попапа расширения.
 * Отправляет сообщение в background.js, показывает статус и итоги анализа.
 */

const urlInput   = document.getElementById("url");
const analyzeBtn = document.getElementById("analyze");
const statusEl   = document.getElementById("status");
const resultsEl  = document.getElementById("results");

// При открытии попапа — подставить URL текущей вкладки если это YouTube
chrome.tabs.query({ active: true, currentWindow: true }, ([tab]) => {
  if (tab?.url?.includes("youtube.com/watch")) {
    urlInput.value = tab.url;
  }
});

analyzeBtn.addEventListener("click", async () => {
  const url = urlInput.value.trim();
  if (!url) return;

  setStatus("loading", "Анализирую видео...");
  analyzeBtn.disabled = true;
  resultsEl.classList.add("hidden");

  // Получить активную вкладку (нужна для overlay)
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

  chrome.runtime.sendMessage({ action: "analyze", url }, (response) => {
    analyzeBtn.disabled = false;

    if (chrome.runtime.lastError) {
      setStatus("error", "Ошибка расширения: " + chrome.runtime.lastError.message);
      return;
    }

    if (!response?.ok) {
      const msg = response?.error ?? "Неизвестная ошибка";

      if (msg.includes("субтитры") || msg.includes("No subtitles") || msg.includes("subtitles")) {
        setStatus("warn", "У этого видео нет субтитров — анализ недоступен");
      } else {
        setStatus("error", msg);
      }
      return;
    }

    const claims = response.data ?? [];

    if (claims.length === 0) {
      setStatus("success", "Проверяемых утверждений не найдено");
      return;
    }

    // Показать итоги
    setStatus("success", `Найдено меток: ${claims.length}. Смотрите видео — пометки появятся автоматически.`);
    showResults(claims);

    // Передать результаты в content_script открытой вкладки
    chrome.tabs.sendMessage(tab.id, { action: "showOverlay", claims });
  });
});

// ─── Helpers ─────────────────────────────────────────────────────────────────

function setStatus(type, text) {
  statusEl.className = type;
  statusEl.innerHTML =
    type === "loading"
      ? `<div class="spinner"></div><span>${text}</span>`
      : `<span>${text}</span>`;
}

function showResults(claims) {
  const falseCount     = claims.filter(c => c.verdict === "false").length;
  const misleadCount   = claims.filter(c => c.verdict === "misleading" || c.verdict === "conflicting").length;
  const sophismCount   = claims.filter(c => c.type === "sophism").length;

  document.getElementById("count-false").textContent     = falseCount;
  document.getElementById("count-misleading").textContent = misleadCount;
  document.getElementById("count-sophism").textContent   = sophismCount;

  resultsEl.classList.remove("hidden");
}
