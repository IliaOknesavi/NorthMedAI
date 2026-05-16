/**
 * background.js — Service Worker
 *
 * Flow:
 *   popup sends { action: "analyze", url }
 *   → background fetches transcript directly from YouTube (user's IP, no bans)
 *   → sends transcript text to backend /analyze
 *   → returns results to popup
 */

import { extractVideoId, fetchTranscript } from "./transcript.js";

const BACKEND_URL = "http://localhost:8000"; // change for production

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.action === "analyze") {
    handleAnalyze(message.url)
      .then((result) => sendResponse({ ok: true, data: result }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true; // keep message channel open for async response
  }
});

async function handleAnalyze(url) {
  const videoId = extractVideoId(url);
  if (!videoId) throw new Error("Не удалось извлечь ID видео из URL");

  // Fetch transcript directly from YouTube — no server, no IP ban
  const snippets = await fetchTranscript(videoId, ["ru", "en"]);

  // Send transcript to backend for AI analysis
  const response = await fetch(`${BACKEND_URL}/analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ video_id: videoId, transcript: snippets }),
  });

  if (!response.ok) {
    throw new Error(`Backend error: ${response.status}`);
  }

  return response.json(); // array of { timestamp, type, claim, verdict, ... }
}
