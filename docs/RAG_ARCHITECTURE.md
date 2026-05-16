# RAG и архитектура агентов — NorthMedAI

Версия: 0.1 (дизайн-док, код ещё не написан)
Дата: 2026-05-16
Статус: на ревью

Документ описывает, как из текущего «detector → placeholder sources»
сделать настоящий fact-check pipeline с доказательной базой. Скоупом этого
документа являются **только бэкенд-агенты и retrieval-слой**. UI расширения,
БД-инфраструктура, транскрипт-слой — затрагиваются только в местах
интеграции.

---

## 1. Цели и нецели

### Цели

1. Каждый claim в оверлее должен иметь **минимум один настоящий источник**
   с конкретной ссылкой на статью/документ, а не на страницу поиска.
2. Verdict, который видит пользователь, должен быть согласован с источниками
   (если PubMed говорит «эффект доказан», мы не показываем `false`).
3. Конфликт источников отображается явно — система не выбирает сторону.
4. Отсутствие источников даёт `unverifiable`, а не `false`.
5. Пайплайн остаётся в рамках Yandex AI Studio (требование хакатона):
   YandexGPT для извлечения и судейства, Yandex Search API — для веба.
6. Латентность: добавление retrieval не должно превышать +30s к текущему
   времени анализа на 10-минутном видео.

### Нецели

- Полнотекстовый предварительный индекс всех PubMed / WHO / CDC.
  Используем только on-demand запросы по конкретному claim'у.
- Замена YandexGPT на другие LLM.
- Кросс-видео reasoning (понимать, что claim из видео A противоречит claim'у из видео B).
- Чат «Обсудить» с пользователем — отдельная задача, использует тот же retriever,
  но не входит в этот документ.

---

## 2. Иерархия доверия источников

| Tier            | Источник                              | Доступ                       | Вес  |
|-----------------|---------------------------------------|------------------------------|------|
| `pubmed`        | PubMed, PMC, Cochrane Library         | NCBI E-utilities (без ключа) | 0.90 |
| `who`           | World Health Organization             | Yandex Search whitelist + scrape | 0.85 |
| `cdc_nejm`      | CDC, NEJM, BMJ, JAMA, Lancet          | Yandex Search whitelist + scrape | 0.80 |
| `minzdrav`      | Минздрав РФ, КР Минздрава, ФАРМЛЕКС  | Yandex Search whitelist (site:minzdrav.gov.ru), PDF fetch | 0.70 |
| `news_major`    | Reuters, AP, BBC, ТАСС, РИА, Коммерсант | Yandex Search whitelist | 0.60 |
| `unknown`       | прочие сайты                          | Yandex Search без whitelist  | 0.30 |
| `blog_forum`    | блоги, форумы, соцсети                | **не используются**          | —    |

### Принципы работы с весами

- **Вес ≠ истина.** PubMed может быть устаревшим (статья 1995 года), WHO может
  публиковать политический документ. Вес — это априорная вероятность качества,
  а не финальный приговор.
- **Конфликт показываем явно.** Если PubMed говорит А, а CDC говорит не-А,
  и оба настоящие — verdict `conflicting`, в UI показываем оба источника.
- **`unverifiable` ≠ `false`.** Если retriever ничего не нашёл, judge
  возвращает verdict `unverifiable`, в оверлей такие claim'ы либо не идут
  (default), либо идут отдельной нейтральной плашкой (настройка тумблера).
- **Свежесть.** Источникам моложе 5 лет добавляем +0.05 к весу, старше 15 лет
  — −0.10. Не применяется к Cochrane systematic reviews (там старше = валиднее).

---

## 3. Общая схема пайплайна

```
                    POST /analyze (video_id)
                              │
                              ▼
                    ┌─────────────────────┐
                    │  transcript.py      │
                    │  субтитры → ru      │
                    └──────────┬──────────┘
                               │ snippets[]
                               ▼
                    ┌─────────────────────┐
        Шаг 1       │  Extractor          │   уже есть (detector.py)
                    │  YandexGPT          │   model="yandexgpt-5.1"
                    │  чанки → claims[]   │   verdict ∈ {false, misleading,
                    └──────────┬──────────┘                conflicting}
                               │ raw_claims[] (без sources, без stance)
                               ▼
                    ┌─────────────────────┐
        Шаг 1.5     │  Stance Detector    │   новый, YandexGPT (lite)
                    │  весь транскрипт +  │   ОДИН вызов на видео:
                    │  все claim'ы → stance│  asserted/debunked_fully/
                    └──────────┬──────────┘   debunked_partially/quoted_neutral
                               │
                               ├─► debunked_fully → DROP (не идёт дальше)
                               │
                               ▼ raw_claims[] со stance ≠ debunked_fully
                    ┌─────────────────────┐
        Шаг 2       │  Query Former       │   новый, YandexGPT (lite)
                    │  claim → queries[]  │   batch по N claim'ов
                    └──────────┬──────────┘
                               │ claim + queries[per source]
                               ▼
                    ┌─────────────────────┐
        Шаг 3       │  Retriever          │   новый, async + параллельно
                    │  queries → evidence │   адаптеры PubMed/WHO/Yandex/Минздрав
                    └──────────┬──────────┘
                               │ claim + evidence[] (с весами)
                               ▼
                    ┌─────────────────────┐
        Шаг 4       │  Judge              │   новый, YandexGPT
                    │  учитывает stance   │   при debunked_partially —
                    │  при пересчёте      │   explanation: «Автор разобрал
                    │  verdict, confidence│    частично: …»
                    └──────────┬──────────┘
                               │ final_claims[] с sources и stance
                               ▼
                       db.save_analysis
                       → ответ клиенту
```

Каждый шаг — отдельный модуль, заменяемый. Шаги 2 и 3 могут идти параллельно
по разным claim'ам (см. §10). Шаг 1.5 — единственный, не параллелизуемый
по claim'ам, потому что работает на полном транскрипте сразу.

---

## 4. Агенты

### 4.1 Extractor — `detector.py` (существует)

**Изменения:** минимальные. Перестаёт вызывать `_placeholder_sources` —
теперь `sources=[]` у возвращаемых claim'ов, заполнятся ниже по конвейеру.
Контракт остаётся:

```python
async def extract_claims(snippets: list[Snippet]) -> list[RawClaim]
```

где `RawClaim`:

```python
{
  "text": str,              # ≤500 симв.
  "start": float,           # секунды
  "verdict": "false" | "misleading" | "conflicting",
  "type": "claim" | "sophism",
  "explanation": str,
  "confidence": float,      # 0..1, мнение экстрактора без источников
}
```

Это **черновой** verdict. Финальный решит judge на шаге 4.

### 4.1.5 Stance Detector — `agents/stance.py` (новый)

**Зачем:** ролики бывают разоблачающие. Автор называет миф «прививки
вызывают аутизм», через минуту его разбирает — Extractor (работающий
по чанкам) этого не понимает и честно выписывает миф как `false`. Нам
не нужно ставить красную метку поверх ролика, который как раз эту дезу
опровергает.

**Что делает:** для каждого RawClaim определяет его риторическую роль
в нарративе видео. Один из четырёх лейблов:

- `asserted`           — автор утверждает это всерьёз. Дефолт.
- `debunked_fully`     — автор сам **полностью** разоблачает миф, ставит
                         все нужные оговорки, доводит до правильного вывода.
- `debunked_partially` — автор разбирает миф, но что-то существенное упущено
                         (нет данных, пропущена альтернатива, нет источника).
- `quoted_neutral`     — автор цитирует чужое мнение, не разделяет и не
                         опровергает (например, в обзоре дискуссии).

**Контракт:**

```python
async def detect_stance(
    transcript: list[Snippet],
    claims: list[RawClaim],
) -> list[StanceLabel]
```

```python
StanceLabel = {
  "claim_index": int,
  "stance": "asserted" | "debunked_fully" | "debunked_partially" | "quoted_neutral",
  "missing": str,        # для debunked_partially — что именно автор не упомянул;
                          # для остальных — пустая строка
  "confidence": float,   # 0..1
}
```

**Реализация — один вызов на видео:**
- Stance анализируется относительно всей речи, а не локального окна
  (опровержение может быть через 2 минуты). Передаём в YandexGPT-lite
  **полный транскрипт с таймкодами** + список claim'ов с их таймкодами,
  получаем массив `StanceLabel`.
- Это ровно ОДИН вызов на видео, даже если claim'ов 20. RTT не растёт,
  токены умеренные (для 30-минутного ролика ~10K tokens — влезает).
- На очень длинных видео (>1ч) транскрипт чанкуем по 10K-token блокам,
  каждому claim'у назначаем блок «вокруг его таймкода» + соседи. Тогда
  вызовов 2-3, не 20. (P3-оптимизация, в MVP — один вызов.)

**Что делаем со stance дальше:**

| stance               | Что делает pipeline                                | Что видит пользователь        |
|----------------------|----------------------------------------------------|-------------------------------|
| `asserted`           | как обычно: Retriever → Judge → метка              | красная/жёлтая метка          |
| `debunked_fully`     | **drop** на выходе Stance Detector'а               | ничего                        |
| `debunked_partially` | идёт дальше; Judge обязан начать explanation с «Автор разобрал частично: …», подставив `missing` | метка с пометкой «недосказано» |
| `quoted_neutral`     | как `asserted`                                     | метка обычная                 |

**Стартовое поведение `debunked_fully` = drop**. Альтернативный режим
«показывать зелёные метки разоблачений» — отдельная фича P3+, пока за
кадром.

**Failure mode:** если Stance Detector упал — fallback на `asserted`
для всех claim'ов (текущее поведение). Логируем как warning, но не
блокируем pipeline.

**Промпт:** см. [PROMPTS.md → stance_detector](./PROMPTS.md#stance-detector).

### 4.2 Query Former — `agents/query_former.py` (новый)

Зачем отдельный шаг: claim'ы написаны естественным языком, часто с
эмоциональной окраской. «Все врачи скрывают, что простуду лечит чеснок за день» —
плохой запрос для PubMed. Хороший: `garlic common cold treatment efficacy`.

**Контракт:**

```python
async def make_queries(claim: RawClaim) -> ClaimQueries
```

```python
ClaimQueries = {
  "claim_text": str,
  "topic": str,              # 2-5 слов на ru, для логов
  "queries": {
    "pubmed":   list[str],   # английские, MeSH-style, 1-2 шт.
    "who":      list[str],   # английские, 1-2 шт.
    "cdc_nejm": list[str],   # английские, 1-2 шт.
    "minzdrav": list[str],   # русские, 1-2 шт.
    "news":     list[str],   # русский + английский, 1 шт. каждый
  },
  "must_not_contain": list[str],   # стоп-слова чтобы не находить наоборот
}
```

**LLM:** YandexGPT lite модель (быстрее, дешевле). Промпт см. в
[PROMPTS.md → query_former](./PROMPTS.md#query-former).

**Батчинг:** один вызов на 5-10 claim'ов сразу — экономит RTT.

**Failure mode:** если LLM не выдала ничего валидного — fallback на
наивные queries (текст claim'а + перевод на en через DeepL/YandexTranslate
или просто транслитерация).

### 4.3 Retriever — `agents/retriever.py` (новый)

Оркестратор. Принимает `ClaimQueries`, дёргает все source adapters
параллельно, возвращает `Evidence[]`.

**Контракт оркестратора:**

```python
async def retrieve(claim_queries: ClaimQueries) -> Evidence
```

```python
Evidence = {
  "claim_text": str,
  "sources": list[Source],   # отсортированы по итоговому скору (вес × relevance × свежесть)
  "errors": list[str],       # какие адаптеры упали — для дебага
  "retrieved_at": iso8601,
}

Source = {
  "title": str,
  "url": str,
  "tier": "pubmed" | "who" | "cdc_nejm" | "minzdrav" | "news_major" | "unknown",
  "weight": float,                  # tier weight, возможно +/- по свежести
  "relevance": float,               # 0..1, насколько ответ relevant к claim'у
  "score": float,                   # = weight × relevance × freshness_factor
  "snippet": str,                   # 1-3 предложения из источника (для judge)
  "published_at": iso8601 | null,
  "language": "ru" | "en" | str,
  "mock": False,                    # настоящие источники, не placeholder
}
```

`relevance` считается локально:
- если адаптер вернул свой score (PubMed E-utilities — relevance из BM25 NCBI) — используем его, нормализуя в 0..1;
- иначе берём косинус между claim_text и snippet через Yandex Embeddings API.

**Бюджет на claim:** ≤3 успешных источников по итогам ранжирования
(остальные дропаются). Это и быстрее, и judge не закопается в простыне.

### 4.4 Source adapters — общий контракт

Все адаптеры реализуют один интерфейс:

```python
class SourceAdapter(Protocol):
    tier: str
    base_weight: float

    async def search(self, queries: list[str], lang: str = "en") -> list[Source]:
        """Вернёт до 5 кандидатов. Не бросает — на ошибке возвращает []."""
```

Адаптеры лежат в `agents/sources/{pubmed,who,cdc_nejm,minzdrav,news_major}.py`,
регистрируются в `agents/sources/__init__.py` через словарь `ADAPTERS`.

Подробности по каждому — §5.

### 4.5 Judge — `agents/judge.py` (новый)

Берёт `RawClaim + Evidence` и выдаёт `FinalClaim`. Это **второй и
последний раз**, когда мы зовём «большую» YandexGPT — здесь важна
рассудительность, а не скорость.

**Контракт:**

```python
async def judge(raw: RawClaim, evidence: Evidence) -> FinalClaim
```

```python
FinalClaim = {
  # Поля из RawClaim, но verdict теперь финальный
  "text": str,
  "start": float,
  "verdict": "false" | "misleading" | "conflicting" | "unverifiable",
  "type": "claim" | "sophism",
  "explanation": str,            # перенаписан судьёй с учётом источников и stance
  "confidence": float,            # обновлён судьёй
  "sources": list[Source],        # top-3 из Evidence
  # Stance от шага 1.5 (debunked_fully сюда не доходит — отфильтрован раньше)
  "stance": "asserted" | "debunked_partially" | "quoted_neutral",
  "stance_missing": str,          # для debunked_partially — что упущено; иначе ""
  # Аудит
  "extractor_verdict": str,       # что говорил Extractor (для логов и UI-debug)
  "judge_notes": str,             # 1-2 предложения, почему judge поменял verdict
}
```

**Учёт stance при пересчёте explanation:**
- `stance == "debunked_partially"` → Judge ОБЯЗАН начать `explanation` с
  фразы «Автор разобрал частично: <stance_missing>. Дополнительно: <анализ источников>».
- `stance == "quoted_neutral"` → Judge должен в `explanation` упомянуть, что
  автор цитировал, а не утверждал (одно предложение в начале).
- `stance == "asserted"` → стандартное поведение.

Если Judge меняет verdict — обязательно объясняет в `judge_notes`. В UI этого
сразу не покажем, но в логах и в `/video/{id}/history` будет видно.

**Поведение при `unverifiable`:**
- `confidence` ≤ 0.4 по дефолту,
- claim в оверлей **не идёт** (фильтруется в `main.py` перед save),
- но в БД сохраняется — пригодится для метрики «сколько claim'ов мы не смогли
  проверить» и для будущей фичи «дай мне всё что детектор увидел».

**Промпт:** см. [PROMPTS.md → judge](./PROMPTS.md#judge). Включает few-shot
из 3 примеров: подтверждение, опровержение, конфликт.

---

## 5. Адаптеры источников

### 5.1 PubMed (`agents/sources/pubmed.py`)

API: NCBI E-utilities, без ключа. Лимит 3 req/sec, с ключом 10/sec —
ключ опциональный, на хакатоне без него обойдёмся.

Эндпоинты:
- `esearch.fcgi?db=pubmed&term=...&retmode=json&retmax=5` → PMID'ы
- `esummary.fcgi?db=pubmed&id=PMID1,PMID2&retmode=json` → метаданные
- (опционально) `efetch.fcgi?db=pubmed&id=PMID&rettype=abstract` → абстракт

Возвращаемый Source:
- `title` = название статьи
- `url` = `https://pubmed.ncbi.nlm.nih.gov/{PMID}/`
- `snippet` = абстракт (≤500 симв., обрезка по предложениям)
- `published_at` = PubDate из esummary
- `relevance` = нормализованный score из esearch (берём порядковую позицию,
  1.0 для первого, 0.5 для пятого)
- `weight` = 0.9, +0.05 если статья ≤5 лет, −0.1 если ≥15 лет

Cochrane Library не имеет своего публичного API. Делаем через PubMed
`source[Filter]=Cochrane Database of Systematic Reviews`. Этого хватит на демо.

**Rate-limit:** локальный async semaphore = 3 одновременных запроса.

### 5.2 WHO (`agents/sources/who.py`)

API нет. Делаем через Yandex Search с whitelist:
- query → Yandex Search API с `site:who.int`
- из топ-5 берём 2 URL → `httpx.get` → парсим заголовок и первый параграф (BeautifulSoup, `<meta name="description">` + первый `<p>` внутри `main`)
- для PDF-ссылок: качаем PDF, через `pdfplumber` достаём первые 2 страницы,
  берём первые ~600 символов как snippet

`weight` = 0.85, без модификатора свежести (WHO guidelines часто старые но
актуальные).

**Кэширование:** URL → HTML/PDF кэшируем в `videos.transcript`-стиле в
отдельной таблице (см. §9).

### 5.3 CDC / NEJM / BMJ / JAMA / Lancet (`agents/sources/cdc_nejm.py`)

Тот же подход что WHO, whitelist:
```
site:cdc.gov OR site:nejm.org OR site:bmj.com OR site:jamanetwork.com OR site:thelancet.com
```

NEJM и JAMA часто за пейволлом — берём то что доступно: абстракт, метаданные.

`weight` = 0.80.

### 5.4 Минздрав РФ (`agents/sources/minzdrav.py`)

Whitelist через Yandex Search:
```
site:minzdrav.gov.ru OR site:cr.minzdrav.gov.ru OR site:roszdravnadzor.gov.ru
```

Большинство — PDF. Парсинг как у WHO.

`weight` = 0.70 (ниже чем WHO/CDC, потому что Минздрав иногда отстаёт от
международных guidelines).

### 5.5 News major (`agents/sources/news_major.py`)

Whitelist через Yandex Search, домены:
```
reuters.com, apnews.com, bbc.com, bbc.co.uk,
tass.ru, ria.ru, kommersant.ru, vedomosti.ru
```

Только snippet из выдачи Yandex Search, без отдельного скрапа (новости часто
за региональной блокировкой). `weight` = 0.60.

Новости используем **только** для claim'ов с topic типа «вспышка такого-то
вируса», «новые рекомендации», «отзыв препарата». Query Former должен
проставлять флаг `use_news=True` явно — иначе адаптер не зовётся.

---

## 6. Разрешение конфликтов между источниками

Алгоритм после ретриверства (живёт в `retriever.py`):

1. Группируем `sources` по знаку: каждый snippet прогоняем через
   мини-классификатор (1-токеновый запрос к YandexGPT lite):
   `"supports" | "contradicts" | "neutral"` относительно claim'а.
   Это дешевле, чем гонять полноценный judge на каждый source.
2. Считаем взвешенные суммы:
   - `S_support = Σ score_i` для supports
   - `S_contra  = Σ score_i` для contradicts
3. Передаём judge:
   - если `S_contra > 2 * S_support` → judge склонится к подтверждению `false`
   - если `S_support > 2 * S_contra` → judge должен опровергнуть `false`
     (вернуть, например, `unverifiable` или даже отбросить claim)
   - если близко → judge ставит `conflicting`

**Важно:** judge видит знаки в evidence, но финальное решение — за ним.
Этот шаг просто помогает не «утопить» одно сильное опровержение в куче
слабых подтверждений.

---

## 7. Перерасчёт verdict и confidence

Возможные переходы от RawClaim к FinalClaim:

| Extractor verdict | Evidence       | Judge verdict      | Что показываем               |
|-------------------|----------------|--------------------|------------------------------|
| false             | contradicts    | **false**          | Красный, источники против    |
| false             | supports       | **unverifiable** или drop | (Extractor ошибся, не показываем как false) |
| false             | none           | **unverifiable**   | Не показываем по дефолту     |
| false             | mixed          | **conflicting**    | Жёлтый, оба мнения           |
| misleading        | supports → но с оговорками | **misleading** | Жёлтый                       |
| misleading        | strongly supports | **conflicting** или drop | Подача спорная, но факт верен |
| conflicting       | any            | **conflicting**    | Жёлтый, показываем оба      |
| any (sophism)     | any            | **false** (сохраняем type=sophism) | Логическая уловка остаётся уловкой независимо от истины вывода |

`confidence` финального claim'а **выставляет Judge** по эвристикам из
[PROMPTS.md → judge](./PROMPTS.md#judge) (0.9..1.0 — два сильных согласных
source'а, 0.7..0.9 — один сильный, и т.д.).

Код-сторона делает только sanity-check: `clip(confidence, 0.0, 1.0)` и
форсирует `confidence ≤ 0.4` для `verdict == "unverifiable"` (если Judge
вдруг выдал больше — нелогично, понижаем). Формулы пересчёта в коде нет —
это удерживает единый источник правды в промпте.

---

## 8. Контракты данных — сводно

```
Snippet                          → Extractor      → RawClaim
RawClaim[] + full transcript     → StanceDetector → RawClaim[] (со stance)
                                                    ─ debunked_fully → DROP
RawClaim (stance ≠ debunked_fully) → QueryFormer  → ClaimQueries
ClaimQueries                     → Retriever      → Evidence (sources[])
RawClaim + Evidence + stance     → Judge          → FinalClaim
FinalClaim                       → main.py / db.save_analysis
```

Все промежуточные структуры — Python TypedDict, описаны в
`backend/agents/types.py`. На API наружу торчит только `FinalClaim`
в текущем формате (полностью совместимом с тем что сегодня ждёт
`content_script.js`).

**UI совместимость:** `verdict ∈ {false, misleading, conflicting}` остаётся
неизменным. Новое значение `unverifiable` фильтруется до отдачи в content
script (см. §4.5).

---

## 9. Изменения в БД

### 9.1 Минимально-необходимые

В `models.Analysis` добавить поля:

```python
retriever_version:  Mapped[str] = mapped_column(String(32), default="")
judge_version:      Mapped[str] = mapped_column(String(32), default="")
stance_version:     Mapped[str] = mapped_column(String(32), default="")
unverifiable_count: Mapped[int] = mapped_column(Integer, default=0)
debunked_drop_count: Mapped[int] = mapped_column(Integer, default=0)  # сколько отфильтровал StanceDetector
```

В `models.Claim` поле `sources` уже JSONB — расширяем содержимое без
миграции схемы (добавляем `snippet`, `relevance`, `score`, `published_at`,
`language`).

Опциональные поля, полезные для дебага и истории:

```python
# в Claim
extractor_verdict: Mapped[str] = mapped_column(String(32), default="")
stance:            Mapped[str] = mapped_column(String(32), default="asserted")
stance_missing:    Mapped[str] = mapped_column(Text, default="")
judge_notes:       Mapped[str] = mapped_column(Text, default="")
search_queries:    Mapped[dict | None] = mapped_column(JSONB, nullable=True)
```

### 9.2 Опциональный кэш источников

Чтобы не дёргать одни и те же URL 100 раз, заводим:

```python
class SourceFetchCache(Base):
    __tablename__ = "source_fetch_cache"

    url:        Mapped[str]  = mapped_column(String(1000), primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status:     Mapped[int]  = mapped_column(Integer)
    title:      Mapped[str | None] = mapped_column(Text, nullable=True)
    snippet:    Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_html:   Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_pdf_text: Mapped[str | None] = mapped_column(Text, nullable=True)
```

TTL — 7 дней для веб-источников, 30 дней для PubMed. Стартовый MVP можно
запустить без этой таблицы (хранить в process memory через LRU 256
записей), завести её — когда инфраструктура устоится.

### 9.3 Опциональный pgvector

Если на каком-то этапе появится «ядерный» корпус PDF (10-30 WHO/Минздрав
guidelines, которые нужно искать каждый раз) — добавляем расширение
pgvector и таблицу:

```python
class CorpusChunk(Base):
    __tablename__ = "corpus_chunks"
    id:        Mapped[int]
    doc_url:   Mapped[str]
    chunk_idx: Mapped[int]
    text:      Mapped[str]
    embedding: Mapped[Vector(1024)]    # Yandex Embeddings dimension
    metadata:  Mapped[dict] = mapped_column(JSONB)
```

Это **не входит в MVP**. Решение принимаем после P1 (см. §13).

---

## 10. Кэширование и rate-limits

| Слой              | Что кэшируем                  | Где               | TTL    |
|-------------------|-------------------------------|-------------------|--------|
| Extractor (есть)  | весь анализ                   | `analyses` table  | бессрочно (override через /reanalyze) |
| Stance Detector   | не кэшируем (1 вызов на видео, дёшево) | — | — |
| Query Former      | claim_text → ClaimQueries     | LRU in-memory     | сессия |
| Source adapters   | url → snippet                 | `source_fetch_cache` или LRU | 7 дней (web), 30 дней (pubmed) |
| Conflict classifier | (claim_text, snippet) → знак | LRU in-memory     | сессия |
| Judge             | не кэшируем (зависит от всего ретривала) | — | — |

**Rate limits**:
- YandexGPT: пайплайн уже под `Semaphore(3)` (Extractor). Добавляем
  отдельные семафоры: Judge — 3, Query Former — 5 (lite), Stance Detector — 2
  (тяжелее по контексту, но один вызов на видео).
- NCBI E-utilities: `Semaphore(3)`.
- Yandex Search API: `Semaphore(5)`, но смотрим на квоту по API.

**Параллелизация на уровне claim'ов**: для каждого RawClaim запускаем
`(query_former → retriever → judge)` как отдельную task, gather по всем
claim'ам. Это самый честный способ распараллелить, потому что Judge не
зависит от других claim'ов.

---

## 11. Промпты

Все системные промпты вынесены в [`docs/PROMPTS.md`](./PROMPTS.md). В коде
они подгружаются как константы из одного места (`agents/prompts.py`),
чтобы их можно было итерировать без правок в логике агентов.

Промпты, которые нужно написать:
- **extractor** — есть, но переписать с учётом «не подставляй sources» (там сейчас `sources` упоминается в формате, надо убрать)
- **stance_detector** — новый (батч по видео)
- **query_former** — новый
- **judge** — новый
- **conflict_classifier** — новый (1 строка, мини-промпт)

---

## 12. Observability и логирование

`log_setup.py` уже даёт rotating handlers. Добавляем отдельные логгеры:

- `agents.stance`       → `backend/logs/stance.log` (DEBUG, raw IO)
- `agents.query_former` → `backend/logs/query_former.log` (DEBUG)
- `agents.retriever`    → `backend/logs/retriever.log` (DEBUG)
- `agents.judge`        → `backend/logs/judge.log` (DEBUG, raw IO)
- `agents.sources.<name>` → пишут в `retriever.log` через child-logger

Что логируем на INFO в `backend.log` для каждого `/analyze`:
```
[video_id] pipeline: claims_in=12 stance(asserted=8,debunked_full=2,debunked_part=1,quoted=1) → after_drop=10 → final=10
  queries_total=58 sources_total=147
  verdict_changes: false->unverifiable=1, false->conflicting=2
  per_source_hits: pubmed=24 who=11 cdc_nejm=8 minzdrav=4 news=0
  errors: pubmed=0 who=1(timeout) cdc_nejm=0 minzdrav=2(http5xx)
  timing: extract=8.3s stance=3.2s query_form=2.1s retrieve=12.4s judge=6.7s total=32.7s
```

Это даст возможность в любой момент сказать «retriever подыхает по who»
без копания в `tail -f`.

---

## 13. Фазовый план

### P0 — Скелет (1-2 сессии, без LLM-зависимостей)
- `agents/types.py` — TypedDict для RawClaim/StanceLabel/ClaimQueries/Evidence/Source/FinalClaim
- `agents/sources/__init__.py` — пустой реестр + интерфейс
- `agents/prompts.py` — пустые константы (импортятся из PROMPTS.md вручную)
- `agents/stance.py` — stub (возвращает все `asserted`)
- `agents/retriever.py` — оркестратор с моками (возвращает 1 dummy source)
- `agents/judge.py` — pass-through (verdict не меняет, sources подкладывает)
- `agents/query_former.py` — наивный (text claim'а → запрос)
- `agents/pipeline.py` — `enrich(snippets, claims)` склеивает все шаги
- Интеграция в `detector.py`: убрать `_placeholder_sources`, в конце
  `analyze_claims` вызывать `agents.pipeline.enrich(snippets, claims)`
- Миграция БД: добавить поля в Analysis и опциональные в Claim

**Definition of done P0:** `pytest backend/tests/test_pipeline_skeleton.py`
проходит, `/analyze` отвечает тем же форматом что сегодня, в `sources`
лежит dummy запись из retriever (а не placeholder), у каждого claim'а
проставлен `stance="asserted"`.

### P1 — Первый настоящий источник: PubMed + Stance Detector (1 сессия)
- `agents/sources/pubmed.py` — настоящий E-utilities клиент
- `agents/stance.py` — настоящий YandexGPT lite вызов (батч на видео)
- `agents/query_former.py` — настоящий YandexGPT lite вызов
- `agents/judge.py` — настоящий YandexGPT вызов с промптом из PROMPTS.md
- Conflict classifier
- E2E тест на ролик «прививки вызывают аутизм» (известный кейс, PubMed
  даст много контраргументов)
- E2E тест на ролик-разоблачение, где stance должен сработать (например,
  врач-блогер разбирает популярные мифы)

**DoD P1:** На демо-ролике хотя бы один claim получает реальную PubMed-ссылку,
judge корректно проставляет `false` + `confidence ≥ 0.7`. На ролике-разоблачении
большинство «фейковых» меток уходят в `debunked_fully` и не показываются.

### P2 — Yandex Search whitelist (WHO/CDC/Минздрав/news) (1-2 сессии)
- Общий хелпер `agents/sources/_yandex_search.py` с whitelist'ами
- Адаптеры who/cdc_nejm/minzdrav/news_major поверх него
- PDF parsing (pdfplumber) + `source_fetch_cache`
- Метрики per-source hits в логах

**DoD P2:** На демо-ролике у claim'ов в среднем 2-3 источника из разных
tier, конфликт явно отображается (нужен ролик с темой «алкоголь — польза/вред»).

### P3 — Polish и оптимизация (последняя сессия перед демо)
- LRU кэши на query_former и conflict_classifier
- Свежесть источников (модификатор веса)
- UI: тултип отображает tier-цвета (уже частично есть)
- Skill в Cowork: «обновить retriever на новый источник» (опционально)
- Demo-видео и презентация

**Out of scope для всех фаз:** pgvector, локальный embedding-индекс PDF,
чат «Обсудить». Это P4+, после хакатона.

---

## 14. Открытые вопросы

1. **YandexGPT vs Yandex Embeddings для relevance.** Нужно ли вообще
   считать relevance отдельно, или хватит порядковой позиции в выдаче?
   Гипотеза: на PubMed хватит, на Yandex Search — нет (он часто возвращает
   нерелевантные хиты). Решаем эмпирически на P1.

2. **Лимит источников на claim.** Сейчас в дизайне `≤3`. Возможно `≤2` хватит
   для UI (тултип маленький), но Judge может хотеть видеть 5. Развязка:
   Judge получает все, в UI идут top-3 по `score`.

3. **Что делать когда Yandex Search API недоступен.** Fallback: использовать
   только PubMed, помечать `errors: ["yandex_search: unavailable"]`,
   judge всё равно работает.

4. **Источники на английском, claim на русском.** Judge должен уметь читать
   английский snippet и оценивать русский claim. YandexGPT справляется,
   но в промпте надо это явно разрешить.

5. **Сохраняем ли мы snippets в БД.** Если да — БД растёт быстро, но история
   запросов становится дебажимой. Если нет — придётся повторно ходить за
   ними при /reanalyze. **Предложение:** сохраняем (Postgres TEXT дешёвый,
   на хакатоне это не проблема).

6. **Auth для NCBI E-utilities.** Без ключа — 3 req/sec, без identity. Если
   попадём в рейт-лимит на демо, надо иметь ключ наготове. Завести на
   `hatiko.is.me@gmail.com` за 5 минут, добавить в `.env` как
   `NCBI_API_KEY`.

7. **Тестовый набор claim'ов.** Нужен фикстурный набор из ~20 известных
   утверждений с известными правильными ответами (заговор про прививки,
   сода от рака, гомеопатия, ВИЧ-диссидентство, противотревожный
   эффект CBD и т.п.). Без него мы не сможем измерить, ухудшилась
   точность от изменений промптов или улучшилась.

8. **Stance Detector на длинных видео.** Полный транскрипт + claim'ы для
   часового ролика может вылезти за контекст YandexGPT-lite. Чанкование
   по 10K токенов — простое решение, но что если разоблачение лежит на
   границе чанков? Возможное решение: overlap 1K-2K токенов между
   чанками. P3.

9. **«Зелёные метки разоблачений».** Решено: НЕ делаем. `debunked_fully`
   остаётся молчаливым drop'ом. Принцип — оверлей появляется только когда
   зрителю надо обратить внимание, не загромождаем таймлайн «всё в порядке»-метками.

10. **Stance Detector false positives.** Что если модель решит, что автор
    «разобрал» миф, хотя на самом деле просто упомянул его и пошёл дальше?
    Тогда мы пропустим вредную дезинформацию. Митигация: в промпте
    жёстко формулируем критерий — `debunked_fully` ставится ТОЛЬКО если
    автор явно сказал «это неправда / это миф / на самом деле …». Любая
    двусмысленность → `quoted_neutral` или `asserted`.

---

## Приложение A: Структура файлов

```
backend/
├── agents/
│   ├── __init__.py
│   ├── types.py                 # TypedDict для всех контрактов
│   ├── prompts.py               # SYSTEM_PROMPT_STANCE, SYSTEM_PROMPT_QUERY_FORMER, SYSTEM_PROMPT_JUDGE, ...
│   ├── pipeline.py              # enrich(snippets, claims) — главная точка входа из detector
│   ├── stance.py                # шаг 1.5
│   ├── query_former.py          # шаг 2
│   ├── retriever.py             # шаг 3 (оркестратор + conflict classifier)
│   ├── judge.py                 # шаг 4
│   └── sources/
│       ├── __init__.py          # реестр ADAPTERS
│       ├── _base.py             # SourceAdapter Protocol + utils
│       ├── _yandex_search.py    # общий клиент Yandex Search с whitelist
│       ├── pubmed.py
│       ├── who.py
│       ├── cdc_nejm.py
│       ├── minzdrav.py
│       └── news_major.py
├── detector.py                  # шаг 1 — minor edits (убрать placeholder_sources)
├── main.py                      # вызов pipeline.enrich после detector
├── models.py                    # +3 поля в Analysis, +3 в Claim, опц. SourceFetchCache
├── db.py                        # минимальные правки save_analysis
└── log_setup.py                 # +3 логгера

docs/
├── RAG_ARCHITECTURE.md          # этот файл
└── PROMPTS.md                   # системные промпты в одном месте
```

## Приложение B: Что НЕ меняется

- `extension/*` — UI работает с тем же форматом claim'ов.
- `transcript.py` — без правок.
- `models.Video` — без правок.
- Эндпоинты `/analyze`, `/reanalyze`, `/video/{id}`, `/video/{id}/history`,
  `/health` — те же сигнатуры, то же поведение для клиента.
- `docker-compose.yml` — без правок (если решим взять pgvector в P4+,
  тогда поменяется).
