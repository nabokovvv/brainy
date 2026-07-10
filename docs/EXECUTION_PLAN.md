# Brainy: пошаговый план и агентный workflow

Статус аудита: 2026-07-10. Рабочее дерево на момент первичного аудита было чистым; тестов, CI и `AGENTS.md` не было.

## Текущий checkpoint Stage 0

Локально завершены slices 0.1 и 0.2 в части, не требующей реальных credentials:

- добавлены `pyproject.toml`, безопасный `.env.example` и local-first README;
- конфигурация импортируется без Telegram/Together/Yandex/Wikidata keys и fail-closed
  отклоняет Web/remote inference;
- добавлены provider-neutral contracts и bounded Ollama adapter с shared client,
  общим deadline, response cap, loopback-only default и concurrency `1`;
- Whisper сохранён, загружается лениво и сериализует как загрузку, так и
  транскрибацию, включая cancellation path;
- очередь bounded и stable для одинаковых priority; route/language фиксируются в
  request snapshot;
- charts и автоматический Markdown-экспорт приватных разговоров удалены;
- все 8 локалей и фиксированный baseline из 26 ключей защищены тестом;
- `uv.lock` зафиксирован; frozen sync с default и `research` extra воспроизводится;
- 57 offline regression/contract tests проходят, Python 3.12 `compileall`, Ruff
  check и format check green;
- optional spaCy/Wikidata/reranker/page utilities устанавливаются и проходят import
  smoke без загрузки языковых моделей при import.

Локальные коммиты checkpoint: `a842d48`, `4fff399`, `2911107`, `57d1083`.

Stage 0 ещё не закрыт. До exit gate остаются:

- real smoke Ollama/Whisper/Telegram: на текущем MacBook не установлены Ollama и
  FFmpeg, а target model tag должен быть подтверждён на Mac mini;
- завершить characterization и доработку optional research utilities перед их
  подключением к Web ON path;
- выбрать доказанно бесплатный CI: self-hosted runner на Mac mini либо hosted Actions
  только после подтверждения public/free minutes и spending limit `0`;
- добавить бесплатный dependency audit: GitHub Dependency Review нельзя считать
  доступным для private repo без подтверждённого GitHub Advanced Security.

## Исходное состояние

Проект нельзя безопасно развивать поверх текущего runtime без нулевого этапа.

Подтверждённые P0/P1 проблемы:

- `ollama_client.py` не компилируется из-за некорректного f-string около строки 406.
- README обещает Python 3.8+, но код использует синтаксис Python 3.10+.
- local client несовместим с вызовами deep handlers и частично зависит от Together.
- Один из Together Deep Research prompts не форматируется и отправляет модели буквальные `{query}`, `{lang}` и `{chunk}`; результат этого этапа нельзя считать grounded.
- `config.py` требует Together, Yandex и Wikidata keys при import независимо от выбранного пути.
- `requirements.txt` — dump окружения: отсутствуют прямые `py3langid`, `spacy`, `nltk`, но присутствует множество неиспользуемых транзитивных/GUI-пакетов.
- `nltk` скачивает данные при import.
- Reranker безусловно загружается при startup; Whisper тоже загружается при startup, но это проверенная владельцем ценная voice-возможность и не является кандидатом на удаление.
- Запрос в очереди не фиксирует выбранный режим; worker читает изменяемый `chat_data` позже.
- `PriorityQueue` не имеет монотонного sequence id для одинаковых приоритетов.
- Поиск возвращает Yandex XML string, содержит hardcoded `folderId` и всегда использует EN localization.
- Новый HTTP client создаётся на каждый search; connection pooling теряется.
- Загрузка страниц использует `ssl=False`, не ограничивает redirect targets/content size и маскируется ротацией браузерных User-Agent; это MITM, SSRF и memory-exhaustion risk.
- Yandex XML parser теряет mixed content внутри `<hlword>` из-за использования `.text` вместо `itertext()`.
- В логи попадают полные запросы, ответы, SERP и содержимое страниц; приватные запросы и ответы также бессрочно экспортируются в `md/`.
- `.env` не игнорируется, хотя README предлагает его использовать; tracked `config.py` одновременно указан в ignore.
- Нет streaming: «Fast» показывает typing до полного ответа.
- Состояние, очередь и настройки пользователей теряются при рестарте.

На момент первичного аудита baseline-команда падала на syntax error:

```bash
PYTHONPYCACHEPREFIX=/tmp/brainy-pycache python3 -m py_compile *.py
```

В текущем checkpoint этот compile gate исправлен; список выше сохранён как
историческая причина Stage 0, а не как описание текущего HEAD.

## Как запускается работа

Один пользовательский direction превращается orchestrator-агентом в вертикальные slices. На каждый slice создаются:

1. scope и non-goals;
2. acceptance criteria;
3. reproducer/test или benchmark;
4. отдельная ветка `codex/<issue>-<slug>`;
5. implementation;
6. независимый verification pass;
7. атомарный commit;
8. обновление этого плана.

Максимум параллельно:

- один orchestrator;
- до двух implementers на непересекающихся файлах/интерфейсах;
- один verifier/reviewer.

Research и review могут идти параллельно в одном рабочем дереве read-only. Параллельные edits допускаются только в отдельных worktrees. Интеграция выполняется последовательно после green gates.

## Stage 0 — Reproducible baseline

Цель: чистая установка запускает local fast reply одной командой, обычные тесты не используют сеть.

### Slice 0.1 — Toolchain и boot

- Выбрать и зафиксировать Python >= 3.11.
- Перейти от environment dump к `pyproject.toml` с прямыми runtime/dev dependencies и lockfile.
- Исправить syntax/import failures.
- Убрать import-time downloads и тяжёлые необязательные initializers, не удаляя Whisper.
- Добавить `.env.example`, typed settings и условную startup validation.
- Игнорировать `.env` и другие локальные secret-файлы; не полагаться на ignore для уже tracked config.
- Полностью удалить chart generation и Markdown-экспорт приватных разговоров, их конфигурацию, output directories и неиспользуемые зависимости.
- Исправить README до одного проверенного local quickstart.

Gate:

- fresh env устанавливается по документации;
- `python -m compileall` green;
- import smoke test green без ключей для выключенных providers;
- `ruff check`, `ruff format --check`, `pytest -q` доступны одной командой каждый.

Commit slices:

1. `build: define reproducible python toolchain`
2. `fix: make local configuration importable`
3. `test: add boot and settings smoke tests`

### Slice 0.2 — Provider contracts

- Ввести `ChatRequest`, `ChatResult`, `ProviderModel`, `ProviderHealth` и `InferenceProvider` protocol.
- Реализовать минимальный Ollama adapter на одном OpenAI-compatible endpoint.
- Перенести prompts/use cases из provider transport.
- Удалить прямую зависимость handlers от `llm_client.client` и provider-specific exceptions.

Gate:

- contract tests на fake Ollama HTTP;
- fast reply работает без Together/NVIDIA/OpenRouter keys;
- timeout, invalid JSON и unavailable Ollama дают bounded user error.

### Slice 0.3 — Очередь и privacy

- Request snapshot хранит query, route intent, language, request id и sequence id.
- Добавить per-user fairness/cancellation policy и bounded queue.
- Убрать plaintext user/content logs.
- Включить TLS verification; удалить browser User-Agent rotation.
- Разрешать только `http/https`; блокировать loopback/private/link-local targets после DNS и каждого redirect.
- Ограничить content type, число redirect, response bytes, fetch concurrency и время обработки.
- Удалить бессрочное сохранение prompts/responses/PNG и закрывать временные файлы.
- Сохранить voice path: тестировать транскрибацию через fake Whisper и не регрессировать UX status/result messages.

Gate:

- два запроса одинакового priority не сравнивают Telegram objects;
- смена режима после enqueue не меняет route уже принятого запроса;
- concurrency/cancellation tests green;
- log-capture test не находит prompt/response text;
- fetch tests блокируют redirect на localhost, oversized/binary response и invalid scheme.

### Slice 0.4 — CI

- [ ] Добавить zero-cost CI для Python syntax, Ruff и pytest после выбора бесплатного
  runner; workflow не должен автоматически расходовать hosted minutes.
- [ ] В CI не передавать real provider keys и не запускать quota-consuming smoke tests.
- [ ] Добавить доступный для private repo dependency/security audit без destructive
  upgrades и платного GitHub Advanced Security.
- [ ] Подтвердить green run после явного разрешения на push и настройки spending
  limit `0` либо self-hosted runner.

Exit Stage 0:

- local fast reply smoke green на чистом checkout;
- все gates green;
- ни один внешний ключ не обязателен для local path.
- все существующие локали имеют одинаковый обязательный набор translation keys.

## Stage 1 — One fast mode

Цель: один понятный UX и доказанное преимущество скорости на Mac mini M4 16 GB.

Checkpoint: старые mode handlers и Together/Yandex provider adapters удалены.
spaCy/Wikidata/reranker/page utilities сохранены отдельным optional research extra и
не импортируются fast path. Runtime оставляет один local chat path и Whisper.
Forensic-аудит старых prompts/workflows закреплён в `LEGACY_QUALITY_AUDIT.md`:
ценные механики доступны в Git и перенесены в Stage 2 как provider-neutral contracts.
Явный `Web OFF/ON` intent реализован без запуска поиска: состояние сохраняется в
`chat_data`, а route/language фиксируются при первом сообщении buffered request. Пока
Stage 2 adapter отсутствует, Web snapshot отвечает локализованной unavailable-ошибкой
и не подменяется local inference. Durable persistence между рестартами остаётся Stage
4 задачей.

На target Mac mini подтверждены exact `gemma4:e2b`, key-based SSH и initial
single-user 8K/32K/64K baseline без нового swap; результаты в
`MAC_MINI_BENCHMARK_BASELINE.md`. Persistent Whisper large-v3 combined-memory прогон
выполнен: Gemma осталась быстрой, но загрузка Whisper добавила около 746 MB swap,
что владелец считает допустимой ценой качества для MVP. Large-v3 перенесена из
volatile `/tmp` в постоянный каталог Brainy. Optional whisper.cpp runtime adapter
добавлен и прошёл real large-v3 smoke на встроенном публичном аудио; Python Whisper
остаётся fallback для development. Telegram Ogg/Opus всегда конвертируется FFmpeg в
mono 16K PCM WAV; exact `.oga` target smoke остаётся pending из-за исчерпанного
approval budget среды, а не из-за ошибки runtime.
Full-context retention smoke прошёл на фактических
32K/64K input tokens без нового swap, но с TTFT 70/192 s, поэтому это capability,
а не fast default. Multilingual baseline дал 14/15 по ручной оценке;
три concurrent arrivals через один generation slot завершились без нового swap и
максимум за 9.95 s end-to-end. Это закрывает bounded batch из трёх запросов, но не
доказывает отсутствие starvation под длительным потоком.

- [x] Заменить четыре режима одним чатом и явным session-persistent переключателем
  `Web OFF/ON`; никакого LLM/freshness preflight.
- [x] Подключить установленную Gemma 4 E2B как benchmark baseline и зафиксировать
  точный Ollama tag на Mac.
- [x] Поддержать динамический context ceiling до 64K; выполнить initial 8K/32K/64K
  allocation benchmark. Full-context quality/memory validation остаётся open.
- Реализовать Telegram progressive delivery через Bot API 10.1 Rich Messages/streaming с fallback на обычные entities/HTML.
- Использовать expandable quotes, structured citations, code/math formatting и уместные бесплатные message effects; не использовать paid broadcasts/Stars.
- Убрать spaCy, Wikidata, chart generator и тяжёлый reranker из fast path; Whisper сохраняется как поддерживаемый voice path.
- Зафиксировать точный установленный tag Gemma на Mac и использовать его как первый local benchmark candidate.
- Добавить latency/source badge и feedback button.

Gate:

- минимум 15-question multilingual fixture и loopback-only runner сохранены в
  репозитории без персональных данных; runner может сохранять только ответы на эти
  fixture в versioned JSON для ручной оценки, никогда не реальные диалоги;
- local TTFT/complete/RSS/swap измерены по мягким ориентирам из `PRODUCT_STRATEGY.md`, включая warm-process сценарий с загруженным Whisper;
- 3 concurrent-user benchmark не приводит к swap или starvation;
- UI tests проверяют Markdown, long-message split и Telegram edit limits.

Recommended commits:

1. `refactor: replace mode menu with route intent`
2. `feat: stream bounded local answers and preserve voice input`
3. `test: add m4 latency and quality eval harness`

## Stage 2 — Smart web

Цель: предсказуемый Web ON path со ссылками и нулевыми денежными расходами.

### Search contract

- Ввести `SearchProvider` и нормализованный `SearchResult`.
- Shared async client, timeouts, retry только для transient errors, circuit breaker и cache.
- URL canonicalization/dedup до загрузки страниц.
- Язык и запрошенные пользователем search filters передаются каждому backend.
- Provider-specific parsing остаётся внутри adapter; XML mixed content сохраняется полностью через `itertext()`.

### Backends

- Бесплатный DuckDuckGo-compatible HTTP/Python provider как первый best-effort backend.
- Yandex fallback только при гарантированном отсутствии списаний, с configurable folder id и без XML leakage за пределы adapter.
- Любой provider с риском платного запроса выключен на уровне policy/config.

### Answer path

- Web path запускается только при явном `Web ON`; состояние фиксируется в request snapshot.
- Base Web ON начинает с исходного запроса и не тратит отдельный LLM-вызов на
  freshness classifier или query expansion.
- SERP snippets и безопасно извлечённые page chunks проходят multilingual chunking,
  canonical/near dedupe, semantic rerank per query и source-diversity selection.
- spaCy -> Wikidata/Wikipedia enrichment остаётся optional evidence с коротким
  timeout и fail-soft поведением; оно не является финальным источником истины.
- Выбранный контекст упаковывается в детерминированный token-budgeted
  `EvidenceBundle`: stable evidence ID, text, canonical URL, provenance, rank и trust
  type. Shared clients создаются на lifespan, а sync spaCy/reranker work не блокирует
  event loop.
- Web synthesis получает только исходный вопрос и `EvidenceBundle`, отвечает на
  выбранном языке, не добавляет фактов вне evidence, отмечает конфликты/неуверенность
  и возвращает structured `{answer, citation_ids}` без chain-of-thought.
- Источники назначаются кодом только из реально использованных evidence IDs; URL,
  придуманный моделью, и неизвестный citation ID отбрасываются.
- Web content явно отделяется как недоверенные данные; команды внутри страниц не
  исполняются как prompt instructions.
- «Подробнее»: до 2 параллельных подзапросов, 4 страниц, общий deadline 30 секунд.
- При падении поиска bot явно сообщает, что свежесть не проверена; не выдаёт local answer как актуальный.

### Preserved quality library

- Characterization fixtures сохраняют смыслы legacy `get_sub_queries`,
  `get_research_steps`, `generate_summary_from_chunks`, grouped synthesis и
  `polish_research_answer`, но prompts отделены от transport/provider code.
- Query expansion для «Подробнее» сохраняет короткие/длинные формулировки на языке
  пользователя и один English-вариант, но ограничивается двумя подзапросами в MVP.
- Multilingual sentence chunking проверяется на всех 8 локалях; safe redirects
  следуются вручную не более 2–3 hops с повторной DNS/IP validation каждого target.
- Rerank policy получает инъецируемые `top_n`/threshold; exact и near duplicates
  схлопываются без потери provenance.
- Legacy deep workflow (entity-aware plan до 6 шагов -> retrieval/rerank -> grounded
  section summaries -> bounded map/reduce -> editorial pass) архивируется как будущая
  beta, а не возвращается отдельным публичным режимом MVP.

Gate:

- общий contract suite проходит для DuckDuckGo-compatible/Yandex fixtures;
- основной free search failure -> разрешённый zero-cost fallback;
- все providers fail -> bounded transparent response;
- citation support >= 90%, stale-answer rate < 2%;
- context pack детерминированно соблюдает token budget и не режет evidence посередине;
- multilingual chunking/dedupe/rerank fixtures сохраняют релевантный контекст и
  diversity источников;
- entity enrichment fail-soft при отсутствии spaCy model или Wikidata;
- prompt-injection fixture не меняет system rules и не добавляет неподтверждённые ссылки;
- latency записывается и сравнивается с мягкими ориентирами, не блокируя раннюю закрытую beta без явной регрессии.

## Stage 3 — NVIDIA и OpenRouter catalog

Цель: внешние бесплатные/квотные модели повышают доступность, не управляют продуктом.

Общая policy: только модели/endpoint с фактической ценой 0; никакого автоматического перехода на платный вариант. Quota exhausted означает local fallback/fail closed.

### NVIDIA adapter

- Base URL `https://integrate.api.nvidia.com/v1`, общий OpenAI-compatible contract.
- Curated allowlist, shared client, streaming, concurrency 1 по умолчанию (максимум 2 после измерений).
- Retry `429/502/503/504` и connect timeout с full jitter/`Retry-After`; auth/validation errors не retry.
- Локальные RPM/daily budgets и метрики, потому что стабильный machine-readable remaining-credit endpoint не подтверждён.

### OpenRouter catalog/scanner

- `GET /api/v1/models`, shortlist details и persistent last-known-good snapshot.
- TTL 15 минут, stale-if-error 24 часа, single-flight refresh с jitter.
- Eligibility по `:free`, price/capability/context/output/expiration policy.
- Canary проверяет короткую мультиязычную выборку, latency и заявленные JSON/tools capabilities.
- Active set — 2–3 curated models; `openrouter/free` — последний fallback.
- Учитывать free limits и вести собственный UTC daily counter.
- Apriel Thinker 15B сохраняется в статусе `discovered`: владелец отметил качество
  английского текста; multilingual eval и доказанная цена endpoint `0` обязательны
  до `eligible/canary/active`.

Gate:

- schema drift или пустой catalog не уничтожает LKG;
- новая модель не становится active без canary/eval;
- 429/quota exhaustion не создаёт retry storm;
- route закреплён на запрос; fallback chain имеет общий time budget;
- provider/model latency, errors, usage и quarantine видимы в admin diagnostics.

## Stage 4 — Closed beta hardening

Цель: безопасная beta на 20–50 пользователей.

- SQLite для user settings, короткого context и durable jobs; миграции и backup policy.
- Graceful shutdown/restart, healthcheck и launchd service на Mac mini.
- Rate limiting per user, abuse bounds, queue backpressure.
- Structured metrics и alerts без пользовательского текста.
- Nightly quota-limited real smoke: один короткий запрос на включённый внешний provider, не на каждый commit.
- Runbook для key rotation, exhausted quota, provider outage и rollback.

Exit beta:

- successful responses ориентировочно >= 95%;
- ошибки и fallback rate наблюдаемы и не показывают устойчивой деградации;
- минимум две недели telemetry без plaintext data;
- подтверждённый спрос на «Подробнее» до обсуждения возврата Deep Research.

## Правила продуктовых решений агентами

Агенты могут самостоятельно:

- выбирать implementation details в пределах утверждённой стратегии;
- удалять dead code и объединять старые mode paths после characterization tests;
- менять модель-кандидат по результату reproducible eval;
- предлагать новый slice с ожидаемым влиянием на SLO.

Новый продуктовый scope оформляется коротким ADR:

```text
Context -> Options -> Decision -> Evidence -> Reversal plan
```

Если решение обратимо и проходит текущие SLO, orchestrator может принять его. Расходы, публичный deploy, privacy changes и необратимые изменения требуют пользователя.

## Шаблон отчёта каждого slice

```text
Outcome:
Changed:
Tests/benchmarks:
Metrics before/after:
Risks remaining:
Commit:
Next unblocked slice:
```

## Первый рекомендуемый запуск

Начать только со Stage 0, slices 0.1 и 0.2. Не добавлять внешние search/inference providers или OpenRouter scanner, пока local fast path не компилируется, не тестируется и не запускается без лишних ключей. После этого Stage 1 даст измеримое подтверждение или опровержение главной гипотезы продукта — скорости.
