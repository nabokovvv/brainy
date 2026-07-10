# Brainy: аудит сохранности quality/context механик

Статус: forensic-аудит выполнен 2026-07-10 до дальнейшей реализации Web ON.

## Итог

Необратимой потери нет: точный старый код, авторские prompts, JSON-примеры и
оркестрация доступны в Git-коммите `3275525`. Но значимая часть продуктового знания
после удаления provider-coupled runtime оставалась только в истории Git и не была
достаточно явно представлена в текущем плане. Этот документ становится обязательным
recovery index и characterization spec.

Старые `together_client.py`, `ollama_client.py`, Yandex adapter и mode handlers нельзя
возвращать целиком. Они смешивали prompts с transport, логировали приватный контент,
допускали платный endpoint, принимали придуманные моделью URL и содержали сломанный
prompt с буквальными `{query}`, `{lang}`, `{chunk}`.

## Lossless recovery index

Точный текст владельца всегда можно извлечь без догадок:

```bash
git show 3275525:together_client.py
git show 3275525:ollama_client.py
git show 3275525:bot.py
git show 3275525:search_client.py
git show 3275525:xml_parser.py
```

| Quality asset | Источник в `3275525` | Что сохранить |
|---|---|---|
| Multilingual query expansion | `together_client.py:get_sub_queries` | До 4 вариантов: короткие/длинные, три на выбранном языке и один EN |
| Entity-aware research plan | `get_research_steps` | До 6 шагов, полные имена сущностей вместо местоимений, контекст исходного вопроса в каждом шаге |
| SERP synthesis | `generate_answer_from_serp` | Уникальные факты, адаптивная длина, язык пользователя, источники рядом с evidence |
| Grouped multi-query synthesis | `synthesize_answer` | Группировка evidence по подзапросу и ограничение входного контекста |
| Per-step grounded summary | `generate_summary_from_chunks` | Не добавлять факты вне evidence, сохранять язык и provenance |
| Editorial final pass | `polish_research_answer` | Проверка противоречий, связная структура, короткие читаемые абзацы, inline citations |
| Deep map/reduce | `bot.py:deep_research_handler`, `together_client.py:summarize_research_chunk` | Шаги → подзапросы → retrieval/rerank → summaries → bounded map/reduce |
| Fast Web workflow | `bot.py:fast_web_handler` | SERP → multilingual rerank → optional entity evidence → synthesis |
| Deep Search workflow | `bot.py:deep_search_handler` | Query expansion → SERP + страницы → rerank per query → grouped synthesis |
| Free-provider pacing | `_parse_rate_headers`, `chat_with_fallback` | Уважать rate headers, общий deadline, fail closed; переносится только как Stage 3 policy |
| Local evidence-only synthesis | `ollama_client.py:synthesize_answer` | Только факты из snippets, язык пользователя, читаемые абзацы ориентировочно 40–80 слов |
| Local adaptive SERP answer | `ollama_client.py:generate_answer_from_serp` | Короткий факт или подробный ответ по типу вопроса; старый ориентир 10–200 слов |
| Local detailed summary | `ollama_client.py:generate_summary_from_chunks` | Grounded-only summary; старый ориентир 100–300 слов для подробного path |
| Research overview schema | `ollama_client.py:synthesize_research_answer` | Отдельные `intro` и `tldr` для будущего research report |
| Local expansion/planning variants | `ollama_client.py:get_sub_queries`, `get_research_steps` | Исторически до 10 элементов; сохранить breadth как eval reference, но ограничить fan-out в MVP |

## Сохранённые structured-output смыслы

Старые prompts показывали JSON с полем `thinking`. Новые контракты не запрашивают,
не сохраняют и не логируют chain-of-thought. Сохраняются только проверяемые поля:

```text
QueryExpansion  -> { queries: [string] }
ResearchPlan    -> { steps: [string] }
WebSynthesis    -> { answer: string, citation_ids: [evidence_id] }
ResearchSection -> { answer: string, citation_ids: [evidence_id] }
ResearchEdit    -> { answer: string, citation_ids: [evidence_id] }
ResearchOverview -> { intro: string, tldr: string, citation_ids: [evidence_id] }
```

LLM никогда не возвращает произвольные URL как источник истины. Она возвращает
только `evidence_id`; код сопоставляет ID с реально полученным canonical URL и
отбрасывает неизвестные citations.

## Решения по вариантам Ollama prompts

- Требования «только по evidence», язык пользователя, уникальные факты и адаптивная
  подробность сохраняются в provider-neutral synthesis prompt и eval fixtures.
- Ориентиры 10–200 слов для базового Web ON, 100–300 для «Подробнее» и 40–80 слов
  на абзац сохраняются как мягкие readability heuristics, а не жёсткое обрезание.
- `{intro, tldr}` сохраняется для будущего research report, но не усложняет короткий
  ответ MVP отдельным LLM-вызовом.
- Ollama-варианты генерировали до 10 queries/steps. Это зафиксировано как историческая
  breadth reference, но сознательно адаптировано до <=2 subqueries в «Подробнее» и
  <=6 entity-aware steps в будущей beta: fan-out 10 ухудшает latency и нулевую
  экономику.
- Утверждение, что entity details — безусловный «final source of truth», отклонено:
  Wikidata/Wikipedia остаются optional evidence с provenance и могут ошибаться.

## Provider-neutral Web ON enrichment

```text
explicit Web ON snapshot
  -> original query
  -> SearchProvider -> normalized SERP results
  -> optional bounded expansion for «Подробнее» (<= 2 subqueries)
  -> safe page fetch (2–4 sources)
  -> multilingual chunking + canonical/near dedupe
  -> semantic rerank per query + source diversity
  -> optional spaCy -> Wikidata/Wikipedia evidence (short timeout, fail-soft)
  -> token-budgeted EvidenceBundle with stable evidence IDs
  -> grounded synthesis in selected language
  -> citation membership validation + Telegram rendering
```

`EvidenceBundle` содержит SERP snippets, безопасно извлечённые page chunks и
необязательные entity facts. Каждый элемент хранит provenance, canonical URL,
provider/result rank и trust type. Внешний текст всегда помечен как недоверенные
данные и не может менять system instructions.

## Что сознательно не переносится

- marketing claims про unlimited research, фиксированную скорость и сравнение с
  proprietary-моделями;
- `THINKING_GUIDANCE`, поле `thinking` и regex-парсинг псевдо-JSON;
- plaintext logs prompts, ответов, SERP, страниц и entity text;
- безусловное доверие Wikidata как «final source of truth»;
- citations, придуманные моделью или проверенные только по `http(s)` prefix;
- ranking по длине snippet;
- silent local fallback после search failure;
- огромный последовательный fan-out и retries без общего deadline;
- платные/неподтверждённо бесплатные Together/Yandex routes.

## Characterization gates перед Web ON

- fixtures для всех 8 языков проверяют sentence chunking, сокращения и пунктуацию;
- canonical и near-duplicate evidence схлопываются с сохранением provenance;
- rerank policy инъецирует `top_n`/threshold и сохраняет source diversity;
- entity enrichment работает fail-soft и не блокирует ответ при отсутствии spaCy
  model или Wikidata;
- context pack детерминированно укладывается в token budget и не режет evidence
  посередине;
- prompt-injection fixture из страницы не меняет system rules;
- неизвестные citation IDs отбрасываются;
- Web failure явно сообщает, что свежесть не проверена;
- base Web ON не делает LLM preflight/query expansion; «Подробнее» ограничено двумя
  подзапросами, четырьмя страницами и единым deadline.

## Отдельно сохранённые кандидаты

Apriel Thinker 15B сохраняется как Stage 3 `discovered` candidate: красивое английское
письмо отмечено владельцем, но multilingual quality и endpoint с доказанной ценой 0
должны пройти canary до статуса `active`.
