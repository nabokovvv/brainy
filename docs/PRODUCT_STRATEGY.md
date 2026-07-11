# Brainy: продуктовая стратегия

Статус: решение для перезапуска проекта, 2026-07-10.

## Позиционирование

> Brainy — быстрый мультиязычный чат прямо в Telegram: локально по умолчанию, с вебом по явному переключателю.

Brainy не конкурирует с ChatGPT, Claude или Gemini по общей мощности. Он выигрывает временем до полезного ответа, отсутствием отдельного приложения и понятным выбором между локальной приватностью и актуальными источниками.

«Бесплатно» и «локально» — свойства продукта, но не главный рекламный тезис. Бесплатные внешние модели нестабильны и не должны определять обещание пользователю.

## MVP

Пользователь видит один боевой чат, а не четыре режима:

1. Пишет вопрос.
2. Переключатель `Web OFF/ON` явно определяет, нужен ли поиск; состояние видно рядом с полем ответа/кнопками.
3. При `Web OFF` запрос сразу идёт в локальную модель без дополнительной классификации.
4. При `Web ON` поиск и синтез работают как единый web path с источниками.
5. Получает короткий ответ; кнопка «Изучить источники» просит текущий path раскрыть ответ глубже.

В ответе показывается измеримое происхождение:

- `⚡ Local · 2.8s`
- `🌐 Live · 7.4s · 3 источника`

Все существующие переводы сохраняются: они уже проверены и нужны для контентной стратегии сайта. Новый пользовательский текст обязан иметь значения во всех текущих локалях; missing-key тест блокирует merge.

## Решение по текущим режимам

| Текущий режим | Решение | Новая роль |
|---|---|---|
| Fast Reply | Объединить | Единый чат при `Web OFF` |
| Fast Web | Объединить | Единый чат при `Web ON` |
| Deep Search | Убрать как отдельный режим | Кнопка «Изучить источники» внутри текущего Web ON/OFF path |
| Deep Research | Убрать из публичного MVP | Возможная beta только после durable jobs и подтверждённого спроса |

Графики и экспорт приватных разговоров в Markdown удаляются из продукта: они не поддерживают MVP и не должны создавать неочевидное хранение личного контента. Стратегия для сайта будет обсуждаться отдельно, без автоматического использования пользовательских диалогов.

Voice/Whisper остаётся продуктовой возможностью: он уже проверен владельцем проекта и работает хорошо. Его сохраняем как voice input, добавляя регрессионные тесты и замеры потребления памяти/очереди, а не удаляя ради упрощения.

## Целевая цепочка

```text
Telegram message
  -> request snapshot + fair queue
  -> explicit Web OFF/ON state
     -> OFF: local Ollama model
     -> ON: SearchGateway -> EvidenceBundle -> grounded cited synthesis
  -> optional bounded "Изучить источники"
  -> short Telegram response + latency/source badge
```

Inference fallback для сложных или недоступных локально задач:

```text
local Ollama -> NVIDIA free-quota curated model -> OpenRouter curated :free models
```

`openrouter/free` разрешён только как последний аварийный fallback: он выбирает модель динамически, поэтому качество и latency непредсказуемы. Любая модель с ценой выше нуля отбрасывается до запроса.

## Mac mini M4, 16 GB

Уже установленная через Ollama App **Gemma 4 E2B** — исходный кандидат для local fast path: по наблюдению владельца она даёт около 30 токенов/с, хорошее качество и поддерживает контекст до 64K. Точный Ollama model tag фиксируется командой `ollama list` при подключении к Mac.

Ограничения MVP:

- одна локальная генерация одновременно;
- не вводить искусственный жёсткий предел 8K: поддержать динамический контекст до 64K и отдельно измерить 8K/32K/64K по latency и памяти;
- тяжёлые необязательные компоненты не загружаются при startup без необходимости; Whisper сохраняется и измеряется отдельно;
- RSS и swap измеряются на реальном Mac; длительный memory pressure/thrashing недопустим.

## Поиск

Production runtime не должен зависеть от MCP-процесса. MCP полезен агентам разработки, а боту нужен простой HTTP contract.

Решение с нулевым бюджетом:

1. Ротировать только явно настроенные free-quota adapters: Brave Search API → Tavily
   → SerpAPI. Лимиты ведутся по UTC календарному месяцу: 900, 900 и 200 запросов.
2. Один запрос fan-out выполняется параллельно по всем доступным providers; это
   ускоряет latency, но расходует по одному месячному credit на каждый вызванный
   provider.
3. При исчерпании локального счётчика, API quota/error или полном отсутствии
   доступных providers Web ON fail-closes до начала следующего UTC месяца.
4. Не включать auto-routing, платные endpoints, автопополнение или скрытый fallback;
   provider-specific API keys читаются только из environment.

Все backends возвращают общий `SearchResult(title, url, snippet, rank, provider, published_at)`.

Web ON не передаёт модели сырую выдачу бесконтрольно. SERP snippets, безопасно
полученные фрагменты страниц и optional spaCy/Wikidata facts проходят multilingual
chunking, deduplication, semantic rerank и source-diversity selection. Затем они
укладываются в token-budgeted `EvidenceBundle` со стабильными evidence IDs и
provenance. Модель отвечает только по этому контексту и возвращает citation IDs;
реальные canonical URLs подставляет и проверяет код. Детальная recovery-карта старых
авторских prompts и workflow хранится в `LEGACY_QUALITY_AUDIT.md`.

## OpenRouter scanner

Scanner обновляет каталог, но не принимает продуктовые решения сам:

```text
discovered -> eligible -> canary -> active
                         -> quarantine
```

Eligibility требует `:free`, нулевые используемые price dimensions, text capability, достаточные context/output limits, отсутствие истечения и нужные параметры. Каталог кэшируется; при ошибке используется last-known-good snapshot. Active route фиксируется на весь запрос, чтобы модель не менялась посреди многошаговой операции.

**Apriel Thinker 15B** остаётся `discovered` candidate: по наблюдению владельца она
красиво пишет по-английски. Мультиязычность, latency и фактическая нулевая цена
конкретного endpoint должны быть подтверждены canary до production routing.

## Мягкие метрики MVP

Это ориентиры для наблюдения, а не причины бесконечно откладывать закрытую beta.

Скорость:

- local: первый видимый текст обычно <= 3 s, полный короткий ответ обычно <= 15 s;
- web: первый статус сразу, полный ответ обычно <= 30 s;
- «Изучить источники»: целевой предел 60 s.

Надёжность и ресурсы:

- successful responses >= 95%;
- очередь остаётся отзывчивой при 3 одновременных пользователях;
- RSS оставляет системе безопасный запас; длительный swap/thrashing недопустим.

Качество:

- smoke/eval минимум 15 мультиязычных вопросов перед beta;
- полезность и корректность отслеживаются feedback-кнопкой и ручной выборкой;
- большая часть проверяемых web-утверждений должна иметь реальный источник из использованной выдачи.

Экономика:

- денежные расходы всегда равны 0;
- платные модели, paid fallbacks, платные поисковые запросы и Telegram Stars запрещены;
- при исчерпании free quota bot переходит на local path или честно сообщает о недоступности web;
- latency, errors, tokens и requests считаются отдельно по provider/model;
- исчерпание внешней квоты не ломает local path.

## Telegram UX

Bot API 10.1 добавил Rich Messages и streaming AI replies. Используем progressive rich response, структурированные ссылки/цитаты, code/math blocks, expandable quotes и аккуратные message effects только там, где они улучшают состояние completion/error. Custom emoji и стили кнопок применяются capability-aware, без требования покупки username или Stars.

Текущий Python wrapper может отставать от Bot API. Поэтому Telegram adapter должен иметь rich path через поддерживаемый SDK/raw Bot API и обязательный fallback на обычные entities/HTML; пользователь никогда не теряет ответ из-за неподдержанного украшения.

## Не входит в ближайший scope

- общий конкурент полноценным AI-ассистентам;
- обещание unlimited deep research;
- автоматическая публикация новых free-моделей;
- автоматический freshness router;
- charts, long-term memory и сложные агенты внутри пользовательского запроса;
- собственный Docker search cluster;
- масштабирование за пределы закрытой beta до прохождения SLO.

## Удалённый Mac

Доступ к Mac нужен только для Stage 1: зафиксировать список установленных Ollama-моделей, запустить benchmark Gemma, измерить TTFT/tokens per second/RSS/swap и затем настроить сервис. Перед первым таким действием агент запрашивает у владельца способ безопасного подключения; не предполагает SSH, пароль или сетевую схему.

## Внешние источники решений

- [Apple Mac mini M4 specifications](https://support.apple.com/en-us/121555)
- [OpenRouter Models API](https://openrouter.ai/docs/api/api-reference/models/get-models)
- [OpenRouter free variant](https://openrouter.ai/docs/guides/routing/model-variants/free)
- [OpenRouter rate limits](https://openrouter.ai/docs/api/reference/limits)
- [OpenRouter model fallbacks](https://openrouter.ai/docs/guides/routing/model-fallbacks)
- [NVIDIA hosted OpenAI-compatible endpoint example](https://build.nvidia.com/openai/gpt-oss-120b?nim=self-hosted)
- [NVIDIA NIM API reference](https://docs.nvidia.com/nim/large-language-models/latest/reference/api-reference.html)
- [Telegram Bot API 10.1 Rich Messages](https://core.telegram.org/bots/api)
- [Telegram animated message effects](https://core.telegram.org/api/effects)
