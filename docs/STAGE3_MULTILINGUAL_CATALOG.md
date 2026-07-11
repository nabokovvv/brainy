# Stage 3: multilingual NVIDIA/OpenRouter catalog

Статус: реализован scanner/adapters и выполнен первый canary на Mac mini,
2026-07-11. Production active set остаётся пустым.

## Context

Внешний fallback допустим только при фактической цене 0 и не должен менять
local-first позиционирование. Название, family или marketing description модели не
доказывают качество на восьми языках Brainy (`de/en/es/fr/id/pt/ru/tr`).

OpenRouter публикует `id`, pricing dimensions, text modalities, context/output limits,
expiration и supported parameters в
[Models API](https://openrouter.ai/docs/api/api-reference/models/get-models).
Вариант [`:free`](https://openrouter.ai/docs/guides/routing/model-variants/free)
имеет нулевую цену, но низкие и меняющиеся лимиты. `openrouter/free` выбирает модель
динамически, поэтому остаётся только последним аварийным fallback, а не canary route.
NVIDIA публикует текущие model IDs через `https://integrate.api.nvidia.com/v1/models`;
machine-readable remaining-credit endpoint не используется и не предполагается.

## Decision

- OpenRouter scanner принимает только точный suffix `:free`, все числовые pricing
  dimensions равны нулю, text input/output, context >=16K, output >=512,
  `max_tokens` support и неистёкший endpoint.
- Catalog имеет TTL 15 минут, single-flight refresh и persistent LKG с
  stale-if-error 24 часа. Пустой catalog или полный schema drift не стирает LKG.
- NVIDIA использует только короткий curated candidate allowlist из general instruct
  families; наличие ID в каталоге даёт лишь `eligible`, не `active`.
- Canary задаёт восемь коротких локализованных factual prompts. В отчёт попадают
  только language codes, safe error codes и latency; тексты ответов не сохраняются.
- Promotion требует одновременно technical eligibility, прохождение 8/8 canary и
  явное включение ID в curated active set (максимум три модели).
- Удалённые adapters используют shared client, streaming, concurrency 1, rolling
  20 RPM limiter, persistent UTC daily counter и максимум три попытки только для
  connect timeout/429/502/503/504 с full jitter/`Retry-After`.
- Повреждённый budget state fail-closes как exhausted, а не сбрасывает счётчик.

## Evidence

Публичный smoke нашёл 14 технически eligible OpenRouter `:free` entries и пять
curated NVIDIA candidates. Bounded canary выполнен на Mac mini; итоговый безопасный
отчёт: [`stage3-multilingual-canary-2026-07-11.json`](../tests/results/stage3-multilingual-canary-2026-07-11.json).

Ни одна проверенная модель не прошла 8/8:

- OpenRouter Gemma 4 26B: 0/8; Gemma 4 31B: 1/8 (`en`, median 2.60 s для
  успешного ответа); GPT-OSS 20B: 0/8 и 8 provider errors.
- NVIDIA Gemma 4 31B: 8 provider errors; Qwen3 Next: 1/8 (`tr`) и median
  29.66 s; Gemma 3 12B: 8 provider errors.

После canary локальные counters: OpenRouter 40/50, NVIDIA 24/40. Повторные live
smoke сегодня не нужны. Все кандидаты остаются `quarantine`; automatic activation
и remote fallback в Telegram runtime не включены.

## Reversal plan

Catalog/adapters изолированы от local provider и выключены без ключей. Для нового
кандидата достаточно дождаться следующего UTC day/quota window, запустить scanner
с явным `--model`, проверить 8/8 и только затем отдельно добавить ID в curated
active set. Удаление ключей из secret environment полностью отключает live canary.

## Commands

Discovery не расходует inference quota:

```bash
uv run python -m tools.scan_remote_models --provider openrouter
uv run python -m tools.scan_remote_models --provider nvidia
```

Canary требует явный model ID и соответствующий ключ в environment:

```bash
uv run python -m tools.scan_remote_models \
  --provider openrouter --canary --model vendor/model:free
```
