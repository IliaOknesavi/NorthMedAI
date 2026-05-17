/**
 * content_script.js
 * Запускается на youtube.com/watch.
 * Получает claims от popup, рисует оверлей поверх плеера,
 * синхронизирует виджет с timeupdate видео.
 */

(() => {
  let claims = [];        // все метки для текущего видео
  let activeClaim = null; // метка, показанная сейчас
  let currentVideoId = null;
  let bootstrapInFlight = false;
  // Для каких video_id уже хотя бы раз делали bootstrap в этой вкладке —
  // даже если он ещё не закончился. Защищает от двойных /analyze при
  // повторных autoBootstrap/yt-navigate-finish/setEnabled пока модель считает.
  const bootstrapStarted = new Set();
  const STORAGE_KEY = "nmai_enabled";
  const TICKS_KEY = "nmai_ticks";
  const VOICE_KEY = "nmai_voice";
  const BACKEND_KEY = "nmai_backend";
  const DEFAULT_BACKEND = "http://localhost:8000";

  // Кэшируем настройку тиков, чтобы не дёргать storage на каждый кадр.
  let ticksEnabled = true;
  let voiceEnabled = false;
  let backendUrl = DEFAULT_BACKEND;

  chrome.storage.sync.get([TICKS_KEY, VOICE_KEY, BACKEND_KEY]).then((s) => {
    if (typeof s[TICKS_KEY] === "boolean") ticksEnabled = s[TICKS_KEY];
    if (typeof s[VOICE_KEY] === "boolean") voiceEnabled = s[VOICE_KEY];
    if (typeof s[BACKEND_KEY] === "string" && s[BACKEND_KEY]) backendUrl = s[BACKEND_KEY];
  });
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "sync") return;
    if (changes[TICKS_KEY]) ticksEnabled = !!changes[TICKS_KEY].newValue;
    if (changes[VOICE_KEY]) voiceEnabled = !!changes[VOICE_KEY].newValue;
    if (changes[BACKEND_KEY] && typeof changes[BACKEND_KEY].newValue === "string") {
      backendUrl = changes[BACKEND_KEY].newValue || DEFAULT_BACKEND;
    }
  });

  // ─── Утилиты для доступа к полям claim ───────────────────────────────────
  const claimTime = (c) => (typeof c.start === "number" ? c.start : c.timestamp) ?? 0;
  const claimText = (c) => c.text ?? c.claim ?? "";

  function extractVideoId(url) {
    const m = url?.match(/[?&]v=([A-Za-z0-9_-]{11})/);
    return m ? m[1] : null;
  }

  function normalizeClaims(rawClaims) {
    return (rawClaims ?? []).map((c) => ({
      ...c,
      start: claimTime(c),
      text: claimText(c),
    }));
  }

  // ─── Обработчик сообщений ────────────────────────────────────────────────
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.action === "showOverlay") {
      claims = normalizeClaims(msg.claims);
      console.log("[NMAI] showOverlay: получено claims =", claims.length);
      init();
      return;
    }

    if (msg.action === "autoBootstrap") {
      // background попросил попробовать самому подтянуть состояние
      bootstrap("autoBootstrap");
      return;
    }

    if (msg.action === "setEnabled") {
      if (msg.enabled) {
        bootstrap("setEnabled:on");
      } else {
        console.log("[NMAI] тумблер выкл — снимаю оверлей");
        claims = [];
        removeOverlay();
      }
      return;
    }

    if (msg.action === "setTicks") {
      // Тики на таймлайне можно убрать без сноса оверлея и без потери claims
      if (msg.enabled) {
        const video = document.querySelector("video.html5-main-video");
        if (video) renderTicks(video);
      } else {
        document.querySelectorAll(".nmai-tick").forEach((el) => el.remove());
      }
      return;
    }
  });

  // ─── Авто-bootstrap: спрашиваем у бэкенда сами ────────────────────────────
  async function bootstrap(reason = "init") {
    if (bootstrapInFlight) {
      console.log(`[NMAI] bootstrap (${reason}) пропущен — уже идёт`);
      return;
    }

    const vid = extractVideoId(location.href);
    if (!vid) return;

    // Проверим тумблер
    const stored = await chrome.storage.sync.get(STORAGE_KEY);
    const enabled = stored[STORAGE_KEY] ?? true;
    if (!enabled) {
      console.log("[NMAI] bootstrap пропущен — авто-проверка выключена");
      return;
    }

    // Если уже работает оверлей для этого же видео — ничего не делаем
    if (vid === currentVideoId && claims.length > 0) return;

    // Если для этого video_id уже запускали bootstrap (может быть ещё идёт
    // на бэкенде) — не делаем повторно. Защищает от случая, когда между
    // первым /analyze и его ответом приходят autoBootstrap-сообщения.
    if (bootstrapStarted.has(vid) && reason !== "manual") {
      console.log(`[NMAI] bootstrap (${reason}) для ${vid} уже стартовал ранее, пропускаю`);
      return;
    }

    bootstrapInFlight = true;
    bootstrapStarted.add(vid);
    currentVideoId = vid;
    console.log(`[NMAI] bootstrap (${reason}) для ${vid}`);

    try {
      // 1) пробуем достать из БД
      let resp = await sendMessageAsync({ action: "getState", url: location.href });
      console.log("[NMAI] getState ответ:", resp);

      // 2) если в БД нет — запускаем анализ (он же создаст запись)
      if (resp?.notFound) {
        console.log("[NMAI] в БД нет, запускаю /analyze");
        resp = await sendMessageAsync({ action: "analyze", url: location.href });
        console.log("[NMAI] analyze ответ:", resp);
      }

      if (!resp?.ok) {
        console.warn("[NMAI] bootstrap не получил данные:", resp?.error, resp);
        // Если /analyze упал — снимаем флажок, чтобы можно было повторить
        bootstrapStarted.delete(vid);
        return;
      }

      claims = normalizeClaims(resp.data?.claims);
      console.log(`[NMAI] bootstrap готово: claims=${claims.length}, cached=${resp.data?.cached}`);
      init();
    } catch (e) {
      console.warn("[NMAI] bootstrap ошибка:", e);
      bootstrapStarted.delete(vid);
    } finally {
      bootstrapInFlight = false;
    }
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

  // ─── Реакция на SPA-навигацию YouTube ─────────────────────────────────────
  // youtube-navigate — кастомное событие YT. Подстрахуемся ещё одним способом:
  // следим за изменением location.href.
  let lastHref = location.href;
  setInterval(() => {
    if (location.href === lastHref) return;
    lastHref = location.href;

    const newVid = extractVideoId(location.href);
    if (newVid && newVid !== currentVideoId) {
      console.log("[NMAI] SPA-навигация на новое видео:", newVid);
      claims = [];
      removeOverlay();
      currentVideoId = null;
      // новое видео может ещё не быть в bootstrapStarted — bootstrap() сам разберётся
      bootstrap("spa-nav");
    }
  }, 1000);

  document.addEventListener("yt-navigate-finish", () => bootstrap("yt-navigate-finish"));

  // Первая загрузка
  bootstrap("initial");

  // ─── Инициализация ────────────────────────────────────────────────────────
  let timeUpdateHandler = null;

  function init() {
    removeOverlay();
    waitForVideo().then((video) => {
      injectStyles();
      buildProgressTicks(video);

      // снимаем старый листенер, чтобы при повторном нажатии «Проверить»
      // не вешать N копий
      if (timeUpdateHandler) {
        video.removeEventListener("timeupdate", timeUpdateHandler);
      }
      timeUpdateHandler = () => onTimeUpdate(video);
      video.addEventListener("timeupdate", timeUpdateHandler);

      // Когда метаданные подтянутся — перерисуем тики. Это критично для
      // случая, когда content_script инициализируется ДО загрузки видео
      // (autoplay заблокирован, видео в фоновой вкладке, медленный коннект).
      const onMeta = () => {
        console.log("[NMAI] loadedmetadata / durationchange — перерисую тики");
        renderTicks(video);
      };
      video.addEventListener("loadedmetadata", onMeta);
      video.addEventListener("durationchange", onMeta);

      // если уже стоим на claim'е — показать бейдж сразу, не ждать timeupdate
      onTimeUpdate(video);
    });
  }

  function waitForVideo() {
    return new Promise((resolve) => {
      const check = () => {
        const v = document.querySelector("video.html5-main-video");
        if (v) return resolve(v);
        setTimeout(check, 300);
      };
      check();
    });
  }

  // ─── CSS ──────────────────────────────────────────────────────────────────
  function injectStyles() {
    if (document.getElementById("nmai-styles")) return;
    const s = document.createElement("style");
    s.id = "nmai-styles";
    s.textContent = `
      /* Контейнер для оверлея — позиционируется поверх плеера,
         не перехватывает клики, дочерние элементы позиционируются от него.

         --nmai-scale — пропорция плеера к «базовой» ширине 1280px.
         Кладётся динамически через ResizeObserver (см. attachPlayerResizeObserver).
         Бейдж и тултип используют её через transform: scale(...) — все
         элементы внутри (шрифты, паддинги, иконки) масштабируются вместе. */
      #nmai-overlay {
        position: absolute;
        inset: 0;
        z-index: 60;
        pointer-events: none;
        --nmai-scale: 1;
      }
      #nmai-overlay > * { pointer-events: auto; }

      /* Кружок-индикатор. Размеры фиксированные в «базовых» px, всё
         масштабируется через transform: scale(var(--nmai-scale)) с
         origin'ом в нижнем правом углу. */
      #nmai-badge {
        position: absolute;
        bottom: 64px;
        right: 16px;
        width: 32px;
        height: 32px;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 16px;
        cursor: pointer;
        transition: opacity 0.2s, transform 0.2s;
        opacity: 0;
        transform: scale(calc(0.7 * var(--nmai-scale)));
        transform-origin: bottom right;
        pointer-events: none;
        color: #1a1a1a;
        font-weight: 700;
      }
      #nmai-badge.visible {
        opacity: 1;
        transform: scale(var(--nmai-scale));
        pointer-events: auto;
      }
      #nmai-badge.misleading {
        background: rgba(253, 214, 99, 0.85);
        animation: nmai-pulse-slow 2s ease-in-out infinite;
      }
      #nmai-badge.false {
        background: rgba(242, 139, 130, 0.85);
        animation: nmai-pulse-fast 1s ease-in-out infinite;
      }
      #nmai-badge.sophism {
        background: rgba(138, 180, 248, 0.85);
        animation: nmai-pulse-slow 2s ease-in-out infinite;
      }
      /* unverifiable — визуально идентично misleading (жёлтый с медленной
         пульсацией). Семантика разная (нет подтверждений vs. подача
         вводит в заблуждение), но для зрителя оба сигнала одного класса
         «надо обратить внимание». */
      #nmai-badge.unverifiable {
        background: rgba(253, 214, 99, 0.85);
        animation: nmai-pulse-slow 2s ease-in-out infinite;
      }

      @keyframes nmai-pulse-slow {
        0%, 100% { box-shadow: 0 0 0 0 rgba(255,255,255,0.3); }
        50%       { box-shadow: 0 0 0 6px rgba(255,255,255,0); }
      }
      @keyframes nmai-pulse-fast {
        0%, 100% { box-shadow: 0 0 0 0 rgba(255,255,255,0.4); }
        50%       { box-shadow: 0 0 0 8px rgba(255,255,255,0); }
      }

      /* Тултип — позиционирован абсолютно в правом нижнем, масштабируется
         через transform: scale() с origin'ом bottom right (чтобы при
         уменьшении не вылетал за пределы плеера). */
      #nmai-tooltip {
        position: absolute;
        bottom: 104px;
        right: 16px;
        width: 340px;
        background: rgba(13, 16, 24, 0.97);
        backdrop-filter: blur(10px);
        border-radius: 14px;
        padding: 16px;
        color: #e8eaed;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        font-size: 13px;
        line-height: 1.5;
        z-index: 10000;
        display: none;
        border: 1px solid rgba(255,255,255,0.08);
        box-shadow: 0 12px 32px rgba(0,0,0,0.45);
        transform: scale(var(--nmai-scale));
        transform-origin: bottom right;
      }
      #nmai-tooltip.visible { display: block; }

      .nmai-pill {
        display: inline-block;
        border-radius: 6px;
        padding: 3px 9px;
        font-size: 11px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        margin-bottom: 10px;
      }
      .nmai-pill.false        { background: rgba(242,139,130,0.18); color: #f28b82; }
      .nmai-pill.misleading   { background: rgba(253,214,99,0.18);  color: #fdd663; }
      .nmai-pill.sophism      { background: rgba(183,148,246,0.18); color: #b794f6; }
      /* unverifiable идёт в той же категории «спорные» что и misleading —
         визуально (жёлтый) и по счётчику в попапе. */
      .nmai-pill.unverifiable { background: rgba(253,214,99,0.18);  color: #fdd663; }

      .nmai-claim {
        font-weight: 700;
        color: #fff;
        margin-bottom: 8px;
        font-size: 16px;
        line-height: 1.35;
      }
      .nmai-explanation {
        color: #9aa0b4;
        margin-bottom: 14px;
        font-size: 13px;
        line-height: 1.5;
      }

      /* Sources */
      .nmai-section-label {
        font-size: 10px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.6px;
        color: #6b7185;
        margin-bottom: 6px;
      }
      .nmai-sources {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        margin-bottom: 14px;
      }
      .nmai-source-pill {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        background: rgba(255,255,255,0.06);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 8px;
        padding: 5px 9px;
        font-size: 11px;
        font-weight: 600;
        color: #e8eaed;
        text-decoration: none;
        transition: background 0.15s;
      }
      .nmai-source-pill:hover { background: rgba(255,255,255,0.1); }
      .nmai-source-pill .nmai-source-dot {
        width: 6px; height: 6px; border-radius: 50%;
        background: #66d9b1;
        flex-shrink: 0;
      }
      .nmai-source-pill .nmai-source-arrow {
        font-size: 9px;
        opacity: 0.6;
      }
      .nmai-source-pill.tier-pubmed     .nmai-source-dot { background: #66d9b1; }
      .nmai-source-pill.tier-who        .nmai-source-dot { background: #66d9b1; }
      .nmai-source-pill.tier-cdc        .nmai-source-dot { background: #66d9b1; }
      .nmai-source-pill.tier-news       .nmai-source-dot { background: #fdd663; }
      .nmai-source-pill.tier-minzdrav   .nmai-source-dot { background: #66d9b1; }
      .nmai-source-pill.tier-unknown    .nmai-source-dot { background: #6b7185; }

      /* Confidence bar */
      .nmai-confidence-row {
        display: flex;
        align-items: center;
        gap: 10px;
        margin-bottom: 14px;
      }
      .nmai-confidence-label {
        font-size: 10px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.6px;
        color: #6b7185;
        flex-shrink: 0;
      }
      .nmai-confidence-bar {
        flex: 1;
        height: 4px;
        border-radius: 2px;
        background: rgba(255,255,255,0.08);
        overflow: hidden;
      }
      .nmai-confidence-fill {
        height: 100%;
        background: linear-gradient(90deg, #4f8ef7, #66d9b1);
        border-radius: 2px;
      }
      .nmai-confidence-value {
        font-size: 12px;
        font-weight: 700;
        color: #e8eaed;
        min-width: 36px;
        text-align: right;
      }

      .nmai-actions { display: flex; gap: 8px; margin-top: 4px; }
      .nmai-btn {
        flex: 1;
        background: rgba(255,255,255,0.05);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 10px;
        color: #e8eaed;
        font-size: 12px;
        font-weight: 600;
        padding: 9px 10px;
        text-align: center;
        cursor: pointer;
        text-decoration: none;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: 6px;
        transition: background 0.15s;
        font-family: inherit;
      }
      .nmai-btn:hover { background: rgba(255,255,255,0.09); }
      .nmai-btn .icon { font-size: 12px; opacity: 0.85; }
      .nmai-btn .arrow { font-size: 10px; opacity: 0.6; }
      .nmai-btn.primary {
        background: rgba(102,217,177,0.18);
        border-color: rgba(102,217,177,0.3);
        color: #66d9b1;
      }
      .nmai-btn.primary:hover { background: rgba(102,217,177,0.28); }

      /* Тики на прогресс-баре */
      .nmai-tick {
        position: absolute;
        top: -2px;
        width: 4px;
        height: calc(100% + 4px);
        border-radius: 2px;
        z-index: 40;
        pointer-events: none;
        box-shadow: 0 0 4px rgba(0,0,0,0.5);
      }
      .nmai-tick.false        { background: #f28b82; }
      .nmai-tick.misleading   { background: #fdd663; }
      .nmai-tick.sophism      { background: #8ab4f8; }
      .nmai-tick.unverifiable { background: #fdd663; }
    `;
    document.head.appendChild(s);
  }

  // ─── Тики на прогресс-баре ────────────────────────────────────────────────
  // YouTube периодически пересобирает прогресс-бар (смена качества/SPA-навигация),
  // из-за чего наши .nmai-tick тихо исчезают. Поэтому:
  //   1) держим renderTicks() идемпотентным (можно звать сколько угодно раз);
  //   2) подписываемся MutationObserver'ом на изменения внутри плеера и
  //      перевешиваем тики если их вычистили.
  let ticksObserver = null;

  function findProgressBar() {
    // .ytp-progress-bar — более стабильный родитель и тоже position: relative
    return document.querySelector(".ytp-progress-bar")
      ?? document.querySelector(".ytp-progress-bar-container");
  }

  function renderTicks(video) {
    const bar = findProgressBar();
    if (!bar) return false;
    if (!Number.isFinite(video.duration) || video.duration <= 0) return false;

    // Снести старые наши тики внутри этого bar — на случай повторного вызова.
    bar.querySelectorAll(".nmai-tick").forEach((el) => el.remove());

    // Тики выключены в настройках — рисовать не будем, но возвращаем true,
    // чтобы waitForBar-цикл не зацикливался.
    if (!ticksEnabled) {
      console.log("[NMAI] тики выключены настройкой, пропускаю");
      return true;
    }

    let drawn = 0;
    claims.forEach((c) => {
      const start = Number(c.start);
      if (!Number.isFinite(start)) return;
      const pct = (start / video.duration) * 100;
      if (!Number.isFinite(pct)) return;

      const tick = document.createElement("div");
      tick.className = `nmai-tick ${tickClass(c)}`;
      tick.style.left = `${Math.min(Math.max(pct, 0), 99.5)}%`;
      tick.title = `${formatTime(start)} — ${(c.text || "").slice(0, 80)}`;
      bar.appendChild(tick);
      drawn++;
    });
    console.log(
      "[NMAI] тиков нарисовано:", drawn,
      "из claims:", claims.length,
      "duration =", video.duration,
      "bar =", bar.className,
    );
    return drawn > 0;
  }

  function buildProgressTicks(video) {
    const tryRender = (attempt = 0) => {
      if (!renderTicks(video)) {
        // Не сдаёмся: помимо опроса, у нас есть loadedmetadata/durationchange
        // листенеры в init(). 120 попыток × 500мс = до 60 секунд — этого
        // более чем достаточно для любой реалистичной загрузки.
        if (attempt > 120) {
          console.warn("[NMAI] прогресс-бар/duration не подъехали за 60с; жду loadedmetadata");
          return;
        }
        return setTimeout(() => tryRender(attempt + 1), 500);
      }

      // Следим за пересборкой плеера и переустанавливаем тики если пропали.
      if (ticksObserver) ticksObserver.disconnect();
      const player = getPlayerContainer();
      if (player) {
        ticksObserver = new MutationObserver(() => {
          const bar = findProgressBar();
          if (bar && bar.querySelectorAll(".nmai-tick").length === 0 && claims.length > 0) {
            renderTicks(video);
          }
        });
        ticksObserver.observe(player, { childList: true, subtree: true });
      }
    };
    tryRender();
  }

  function formatTime(seconds) {
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${String(s).padStart(2, "0")}`;
  }

  function tickClass(c) {
    if (c.type === "sophism") return "sophism";
    if (c.verdict === "false") return "false";
    if (c.verdict === "unverifiable") return "unverifiable";
    return "misleading";
  }

  // ─── timeupdate — показываем нужную метку ─────────────────────────────────
  // Окно показа бейджа: от c.start - LEAD до c.start + HOLD.
  // LEAD = небольшой лид-ин (1.5с) чтобы пузырь появлялся ровно к моменту фразы.
  // HOLD = сколько секунд держим после фразы — иначе при быстрых сниппетах
  // бейдж мигает за пару кадров и его невозможно прочитать.
  const LEAD_S = 1.5;
  const HOLD_S = 6.0;

  function findActiveClaim(now) {
    // Берём все claims, у которых now в окне [start - LEAD, start + HOLD]
    // и выбираем тот, чей start ближе всего к now.
    let best = null;
    let bestDelta = Infinity;
    for (const c of claims) {
      if (now < c.start - LEAD_S) continue;
      if (now > c.start + HOLD_S) continue;
      const delta = Math.abs(c.start - now);
      if (delta < bestDelta) {
        best = c;
        bestDelta = delta;
      }
    }
    return best;
  }

  function onTimeUpdate(video) {
    const match = findActiveClaim(video.currentTime);

    if (match && match !== activeClaim) {
      activeClaim = match;
      showBadge(match);
    } else if (!match && activeClaim) {
      const justClosed = activeClaim;
      activeClaim = null;
      hideBadge();
      // В конце окна показа метки: если включён тумблер озвучки —
      // ставим паузу видео и зачитываем explanation. После окончания
      // play() обратно. См. speakExplanation().
      if (voiceEnabled && justClosed?.explanation) {
        speakExplanation(video, justClosed);
      }
    }
  }

  // ─── TTS через бэкенд /tts (Yandex SpeechKit) ──────────────────────────
  // Дёргаем бэкенд POST /tts с текстом explanation, получаем mp3,
  // играем через временный <audio>. Поверх ставим pause/play видео.
  // Используется только когда tumblr nmai_voice включён в попапе.
  let speakingNow = false;

  async function speakExplanation(video, claim) {
    if (speakingNow) return;     // не накладываем озвучки друг на друга
    const text = (claim?.explanation || "").trim();
    if (!text) return;

    speakingNow = true;
    const wasPlaying = !video.paused;
    try {
      video.pause();
    } catch { /* пофиг */ }

    let url = null;
    try {
      const r = await fetch(`${backendUrl}/tts`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!r.ok) {
        console.warn("[NMAI] tts: HTTP", r.status);
        speakingNow = false;
        if (wasPlaying) video.play().catch(() => {});
        return;
      }
      const blob = await r.blob();
      url = URL.createObjectURL(blob);
    } catch (e) {
      console.warn("[NMAI] tts: fetch упал", e);
      speakingNow = false;
      if (wasPlaying) video.play().catch(() => {});
      return;
    }

    const audio = new Audio(url);
    const cleanup = () => {
      try { URL.revokeObjectURL(url); } catch {}
      speakingNow = false;
      if (wasPlaying) video.play().catch(() => {});
    };
    audio.addEventListener("ended", cleanup);
    audio.addEventListener("error", cleanup);
    try {
      await audio.play();
    } catch (e) {
      console.warn("[NMAI] tts: audio.play() упал", e);
      cleanup();
    }
  }

  // ─── Бейдж ────────────────────────────────────────────────────────────────
  function getOverlayRoot() {
    let root = document.getElementById("nmai-overlay");
    if (root) return root;

    const player = getPlayerContainer();
    if (!player) return null;

    root = document.createElement("div");
    root.id = "nmai-overlay";
    player.appendChild(root);
    attachPlayerResizeObserver(player, root);
    return root;
  }

  // ─── Адаптивность: масштаб бейджа и тултипа по ширине плеера ──────────
  // Базовая ширина плеера = 1280px (стандарт YouTube). На неё откалиброваны
  // абсолютные размеры в CSS (32px badge, 340px tooltip, 13px шрифт).
  // На fullscreen 2560px → scale ≈ 1.5; на маленьком embedded 480px → 0.7.
  // Clamp удерживает в диапазоне [0.7, 1.6] чтобы не уходить в крайности.
  let _resizeObserver = null;

  function attachPlayerResizeObserver(player, root) {
    if (_resizeObserver) _resizeObserver.disconnect();

    const apply = () => {
      const w = player.getBoundingClientRect().width || 1280;
      const raw = w / 1280;
      const scale = Math.max(0.7, Math.min(1.6, raw));
      root.style.setProperty("--nmai-scale", scale.toFixed(3));
    };

    apply();   // первый замер сразу
    try {
      _resizeObserver = new ResizeObserver(apply);
      _resizeObserver.observe(player);
    } catch {
      // ResizeObserver не поддерживается — fallback на window.resize
      window.addEventListener("resize", apply);
    }
  }

  function getBadge() {
    let el = document.getElementById("nmai-badge");
    if (!el) {
      const root = getOverlayRoot();
      if (!root) return null;
      el = document.createElement("div");
      el.id = "nmai-badge";
      el.addEventListener("mouseenter", () => showTooltip(activeClaim));
      el.addEventListener("mouseleave", hideTooltip);
      el.addEventListener("click", () => showTooltip(activeClaim));
      root.appendChild(el);
    }
    return el;
  }

  function showBadge(claim) {
    const badge = getBadge();
    if (!badge) {
      console.warn("[NMAI] showBadge: getBadge() вернул null, плеер ещё не готов?");
      return;
    }
    badge.className = `visible ${tickClass(claim)}`;
    badge.textContent =
        claim.type === "sophism"   ? "💬"
      : claim.verdict === "false"  ? "✗"
      :                              "⚠";   // misleading + unverifiable = ⚠
    console.log("[NMAI] показан бейдж для claim @", claim.start, "s:", claim.text.slice(0, 60));
  }

  function hideBadge() {
    const badge = document.getElementById("nmai-badge");
    if (badge) badge.className = "";
    hideTooltip();
  }

  // ─── Тултип ───────────────────────────────────────────────────────────────
  function getTooltip() {
    let el = document.getElementById("nmai-tooltip");
    if (!el) {
      const root = getOverlayRoot();
      if (!root) return null;
      el = document.createElement("div");
      el.id = "nmai-tooltip";
      el.addEventListener("mouseenter", () => el.classList.add("visible"));
      el.addEventListener("mouseleave", hideTooltip);
      root.appendChild(el);
    }
    return el;
  }

  // Угадываем «уровень доверия» источника по домену — используется только
  // для CSS-цвета. На бэкенде это решает retriever (когда появится).
  function sourceTier(url) {
    const u = (url || "").toLowerCase();
    if (u.includes("pubmed") || u.includes("ncbi.nlm.nih.gov") || u.includes("cochrane")) return "pubmed";
    if (u.includes("who.int")) return "who";
    if (u.includes("cdc.gov") || u.includes("nejm.org")) return "cdc";
    if (u.includes("minzdrav")) return "minzdrav";
    if (u.includes("yandex") || u.includes("news") || u.includes("ria") || u.includes("tass")) return "news";
    return "unknown";
  }

  function sourceLabel(s) {
    // Если есть явный title — используем его, иначе показываем хост
    if (s.title && s.title.trim()) return s.title.trim();
    try {
      return new URL(s.url).hostname.replace(/^www\./, "");
    } catch {
      return s.url || "source";
    }
  }

  function showTooltip(claim) {
    if (!claim) return;
    const tt = getTooltip();

    const pillClass = tickClass(claim);
    const pillLabel =
        claim.type === "sophism"           ? "Логическая ошибка"
      : claim.verdict === "false"          ? "Ложное утверждение"
      :                                      "Спорное утверждение";   // misleading + unverifiable

    // экранируем чтобы кавычки/угловые скобки из текста не сломали разметку
    const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[ch]);

    const sourcesArr = Array.isArray(claim.sources) ? claim.sources : [];
    const sourcesHtml = sourcesArr.length
      ? `
        <div class="nmai-section-label">Источники</div>
        <div class="nmai-sources">
          ${sourcesArr
            .map((s) => {
              const tier = sourceTier(s.url);
              const label = esc(sourceLabel(s));
              const url = esc(s.url || "#");
              return `
                <a class="nmai-source-pill tier-${tier}" href="${url}" target="_blank" rel="noopener noreferrer">
                  <span class="nmai-source-dot"></span>
                  <span>${label}</span>
                  <span class="nmai-source-arrow">↗</span>
                </a>`;
            })
            .join("")}
        </div>`
      : "";

    const confPct = claim.confidence != null ? Math.round(claim.confidence * 100) : null;
    const confHtml = confPct != null
      ? `
        <div class="nmai-confidence-row">
          <span class="nmai-confidence-label">Уверенность</span>
          <div class="nmai-confidence-bar">
            <div class="nmai-confidence-fill" style="width: ${confPct}%"></div>
          </div>
          <span class="nmai-confidence-value">${confPct}%</span>
        </div>`
      : "";

    const explanation = claim.explanation ?? claim.verdict ?? "";

    // Уникальный id для текущего показа — чтобы кнопки находили claim
    const claimKey = `${claim.start}_${(claim.text || "").length}`;
    tt._activeClaimKey = claimKey;

    tt.innerHTML = `
      <div class="nmai-pill ${pillClass}">${pillLabel}</div>
      <div class="nmai-claim">${esc(claim.text)}</div>
      <div class="nmai-explanation">${esc(explanation)}</div>
      ${sourcesHtml}
      ${confHtml}
      <div class="nmai-actions">
        <button class="nmai-btn" data-nmai-action="close" type="button">
          <span>Закрыть</span>
        </button>
        <button class="nmai-btn primary" data-nmai-action="discuss" type="button">
          <span class="icon">💬</span>
          <span>Обсудить</span>
        </button>
      </div>
    `;

    // обработчики кнопок (а не inline onclick, чтобы не было CSP-проблем)
    tt.querySelector('[data-nmai-action="close"]')?.addEventListener("click", () => {
      hideTooltip();
    });
    tt.querySelector('[data-nmai-action="discuss"]')?.addEventListener("click", () => {
      // Пока заглушка: показываем небольшую плашку прямо в тултипе.
      // Когда сделаем retrieval-чат — будем открывать его.
      const actions = tt.querySelector(".nmai-actions");
      if (actions && !tt.querySelector(".nmai-hint")) {
        const hint = document.createElement("div");
        hint.className = "nmai-hint";
        hint.style.cssText = "margin-top:10px;padding:8px 10px;background:rgba(255,255,255,0.05);border-radius:8px;font-size:11px;color:#9aa0b4;";
        hint.textContent = "Скоро: чат с системой, которая прочла все источники.";
        actions.after(hint);
      }
    });

    tt.classList.add("visible");
  }

  function hideTooltip() {
    document.getElementById("nmai-tooltip")?.classList.remove("visible");
  }

  // ─── Вспомогательное ──────────────────────────────────────────────────────
  function getPlayerContainer() {
    // #movie_player у YouTube всегда position: relative — идеально для absolute-детей
    return document.querySelector("#movie_player")
      ?? document.querySelector(".html5-video-player")
      ?? document.querySelector(".html5-video-container");
  }

  function removeOverlay() {
    if (ticksObserver) {
      ticksObserver.disconnect();
      ticksObserver = null;
    }
    if (_resizeObserver) {
      _resizeObserver.disconnect();
      _resizeObserver = null;
    }
    document.getElementById("nmai-overlay")?.remove();
    document.getElementById("nmai-badge")?.remove();
    document.getElementById("nmai-tooltip")?.remove();
    document.querySelectorAll(".nmai-tick").forEach((el) => el.remove());
    activeClaim = null;
  }
})();
