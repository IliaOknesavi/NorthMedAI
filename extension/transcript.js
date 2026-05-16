/**
 * transcript.js
 * Fetches YouTube subtitles directly from the browser (InnerTube API).
 * No server needed — runs with the user's IP, no bans.
 */

const INNERTUBE_API_URL =
  "https://www.youtube.com/youtubei/v1/player?prettyPrint=false";

const INNERTUBE_CONTEXT = {
  client: {
    clientName: "WEB",
    clientVersion: "2.20240101.00.00",
  },
};

/**
 * Extract video ID from YouTube URL.
 * @param {string} url
 * @returns {string|null}
 */
export function extractVideoId(url) {
  const match = url.match(
    /(?:youtube\.com\/watch\?v=|youtu\.be\/)([a-zA-Z0-9_-]{11})/
  );
  return match ? match[1] : null;
}

/**
 * Fetch transcript snippets for a YouTube video.
 * Returns array of { text, start, duration } — same format as youtube-transcript-api.
 *
 * @param {string} videoId
 * @param {string[]} preferredLanguages - e.g. ["ru", "en"]
 * @returns {Promise<Array<{text: string, start: number, duration: number}>>}
 */
export async function fetchTranscript(videoId, preferredLanguages = ["ru", "en"]) {
  // Step 1: get player data via InnerTube
  const playerData = await fetchPlayerData(videoId);

  // Step 2: extract captions track list
  const captionTracks = extractCaptionTracks(playerData, videoId);

  // Step 3: pick best language
  const track = pickBestTrack(captionTracks, preferredLanguages);

  // Step 4: fetch and parse the XML subtitle file
  const snippets = await fetchAndParseTrack(track.baseUrl);

  return snippets;
}

// ─── Internal helpers ────────────────────────────────────────────────────────

async function fetchPlayerData(videoId) {
  const response = await fetch(INNERTUBE_API_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      context: INNERTUBE_CONTEXT,
      videoId,
    }),
  });

  if (!response.ok) {
    throw new Error(`InnerTube request failed: ${response.status}`);
  }

  return response.json();
}

function extractCaptionTracks(playerData, videoId) {
  const tracks =
    playerData?.captions?.playerCaptionsTracklistRenderer?.captionTracks;

  if (!tracks || tracks.length === 0) {
    throw new Error(`No subtitles available for video ${videoId}`);
  }

  return tracks;
  // Each track: { baseUrl, name, languageCode, kind, isTranslatable }
  // kind === "asr" means auto-generated
}

function pickBestTrack(tracks, preferredLanguages) {
  // Try preferred languages in order, manual subtitles first
  for (const lang of preferredLanguages) {
    const manual = tracks.find(
      (t) => t.languageCode === lang && t.kind !== "asr"
    );
    if (manual) return manual;
  }
  for (const lang of preferredLanguages) {
    const auto = tracks.find((t) => t.languageCode === lang);
    if (auto) return auto;
  }

  // Fallback: first available track
  return tracks[0];
}

async function fetchAndParseTrack(baseUrl) {
  // Request plain text XML (fmt=srv3 gives timestamps in ms, fmt=json3 gives JSON)
  const url = `${baseUrl}&fmt=json3`;
  const response = await fetch(url);

  if (!response.ok) {
    throw new Error(`Failed to fetch subtitle track: ${response.status}`);
  }

  const data = await response.json();

  // json3 format: { events: [{ tStartMs, dDurationMs, segs: [{utf8}] }] }
  return data.events
    .filter((e) => e.segs) // skip non-text events
    .map((e) => ({
      text: e.segs
        .map((s) => s.utf8 ?? "")
        .join("")
        .replace(/\n/g, " ")
        .trim(),
      start: e.tStartMs / 1000,       // ms → seconds
      duration: (e.dDurationMs ?? 0) / 1000,
    }))
    .filter((s) => s.text.length > 0);
}
