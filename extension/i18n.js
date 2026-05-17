// i18n.js — простой словарь переводов попапа.
// Подключается до popup.js. Использует глобальную переменную window.NMAI_I18N.

(() => {
  const dict = {
    ru: {
      subtitle: "Проверка медицинского контента",

      // Главная карточка
      auto_on_title: "Авто-проверка включена",
      auto_off_title: "Авто-проверка выключена",
      auto_off_sub: "Включите тумблер, чтобы метки появлялись автоматически",
      claims_found_one:  "{n} метка найдена",
      claims_found_few:  "{n} метки найдены",
      claims_found_many: "{n} меток найдено",
      claims_zero:       "Нет спорных утверждений",
      checking:          "Анализирую видео…",
      not_youtube:       "Откройте видео на YouTube",
      cache_loaded:      "Загружено из БД {when}",

      // Плитки
      tile_false:    "Ложных",
      tile_disputed: "Спорных",
      tile_sophism:  "Софизмов",

      // Тумблеры
      toggle_auto:   "Авто-проверка",
      toggle_ticks:  "Метки на таймлайне",
      toggle_voice:  "Озвучивание ошибок",
      voice_soon:    "скоро",

      // Кнопки
      btn_recheck:   "Перепроверить",
      btn_recheck_hint: "Создаст новую версию анализа. История сохранится.",
      btn_settings:  "Настройки",
      btn_back:      "Назад",

      // Settings
      settings_title: "Настройки",
      lang_label:     "Язык интерфейса",
      lang_ru:        "Русский",
      lang_en:        "English",
      backend_label:  "URL бэкенда",
      backend_hint:   "По умолчанию http://localhost:8000",
      reset_label:    "Сбросить настройки",
      reset_btn:      "Сбросить",
      // about удалён по просьбе пользователя

      // Ошибки
      err_connection_reset: "YouTube временно сбросил соединение. Подождите 30–60 секунд и нажмите «Перепроверить».",
      err_no_subtitles:     "У этого видео нет субтитров — анализ недоступен.",
      err_unknown:          "Что-то пошло не так",
    },

    en: {
      subtitle: "Medical content fact-checker",

      auto_on_title: "Auto-check is on",
      auto_off_title: "Auto-check is off",
      auto_off_sub: "Turn the switch on to see claims automatically",
      claims_found_one:  "{n} claim found",
      claims_found_few:  "{n} claims found",
      claims_found_many: "{n} claims found",
      claims_zero:       "No disputed claims",
      checking:          "Analyzing the video…",
      not_youtube:       "Open a YouTube video",
      cache_loaded:      "Loaded from cache {when}",

      tile_false:    "False",
      tile_disputed: "Disputed",
      tile_sophism:  "Fallacies",

      toggle_auto:   "Auto-check",
      toggle_ticks:  "Timeline ticks",
      toggle_voice:  "Voice alerts",
      voice_soon:    "soon",

      btn_recheck:   "Re-check",
      btn_recheck_hint: "Creates a new analysis version. History is kept.",
      btn_settings:  "Settings",
      btn_back:      "Back",

      settings_title: "Settings",
      lang_label:     "Interface language",
      lang_ru:        "Русский",
      lang_en:        "English",
      backend_label:  "Backend URL",
      backend_hint:   "Default: http://localhost:8000",
      reset_label:    "Reset settings",
      reset_btn:      "Reset",
      // about removed

      err_connection_reset: "YouTube reset the connection. Wait 30–60 seconds and click Re-check.",
      err_no_subtitles:     "This video has no subtitles — analysis is unavailable.",
      err_unknown:          "Something went wrong",
    },
  };

  // Простая плюрализация для русского. Не идеал, но достаточно.
  function plural(n, lang) {
    if (lang === "ru") {
      const mod10 = n % 10;
      const mod100 = n % 100;
      if (mod10 === 1 && mod100 !== 11) return "one";
      if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return "few";
      return "many";
    }
    // en — одна и много
    return n === 1 ? "one" : "many";
  }

  function format(template, params) {
    return template.replace(/\{(\w+)\}/g, (_, k) => (params[k] ?? `{${k}}`));
  }

  window.NMAI_I18N = {
    t(lang, key, params = {}) {
      const lex = dict[lang] || dict.ru;
      const tpl = lex[key] ?? dict.ru[key] ?? key;
      return format(tpl, params);
    },

    claimsCount(lang, n) {
      if (n === 0) return this.t(lang, "claims_zero");
      const form = plural(n, lang);
      const key = `claims_found_${form}`;
      return this.t(lang, key, { n });
    },
  };
})();