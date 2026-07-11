# Дизайн: 👍/👎 feedback без хранения текста диалогов

Статус: дизайн для будущего slice, код не написан. Это расширение публичного
MVP — не начинать implementation, пока Stage 0 exit gate не закрыт (см.
`docs/BACKLOG_IDEAS.md`, тот же принцип). Когда Stage 0 закроется, этот файл
превращается в конкретный slice в `EXECUTION_PLAN.md` по обычному циклу
scope → acceptance criteria → test → implementation → review → commit.

## Цель / не-цель

Цель: дать пользователю способ оценить ответ бота одним тапом, чтобы позже
можно было сравнивать providers/models по доле 👎, без:

- хранения текста запроса/ответа где-либо (диск, лог, память дольше TTL);
- платной аналитики (Mixpanel/Amplitude/PostHog cloud/Segment и т.п.);
- durable БД или dashboard — это не в этом slice.

Не-цель: rate limiting, история голосов пользователя, экспорт/визуализация,
поддержка Web ON ответов (появится сама, когда Web ON существует — схема не
привязана к local-only, но UI для web ответов не реализуется в этом slice).

## Где это встраивается в текущий код

`handle_fast_chat` в `bot.py:520-583` уже вычисляет `result.model.provider`,
`result.model.name`, `result.latency_ms` и логирует их одной строкой
(`bot.py:559-563`). Ответ отправляется через `send_long_message` без
клавиатуры. Кнопки инлайн-клавиатуры уже есть паттерном в `get_route_keyboard`
и обрабатываются в `button()` (`bot.py:296-341`) через `callback_data` с
префиксом действия (`ACTION_TOGGLE_WEB` и т.п.) — feedback follows тот же
паттерн, никакого нового UI framework.

## Событийная схема

Одна структурная log-строка на голос, через существующий `logger` (тот же
`logging`, что уже используется для `"Fast reply completed provider=..."`).
Никакой новой БД в этом slice — агрегация делается позже offline-скриптом по
логам (grep/jq), без сети и без SaaS.

```python
logger.info(
    "feedback_recorded request_id=%s vote=%s provider=%s model=%s "
    "latency_ms=%.1f lang=%s route=%s",
    request_id, vote, provider, model, latency_ms, lang, route,
)
```

Поля:

| Поле | Тип | Источник | Комментарий |
|---|---|---|---|
| `request_id` | `str`, `uuid4().hex[:10]` | сгенерирован при отправке ответа | только для идемпотентности, не переиспользуется как ключ диалога |
| `vote` | `"up" \| "down"` | тап на кнопку | |
| `provider` | `str` | `result.model.provider` | уже логируется сегодня |
| `model` | `str` | `result.model.name` | уже логируется сегодня |
| `latency_ms` | `float` | `result.latency_ms` | уже логируется сегодня |
| `lang` | `str` | `chat_data["language"]` | UI-язык, не текст |
| `route` | `"local" \| "web"` | `route_intent.value` | |

Явно запрещённые поля в этой log-строке: `chat_id`, `user_id`, `message_id`,
текст запроса, текст ответа, IP. Тест должен фиксировать это как contract
(см. Acceptance tests, пункт 3/8).

## Correlation / жизненный цикл `request_id`

- Генерируется в `handle_fast_chat` в момент отправки ответа, аналогично уже
  существующему `draft_id = next(_draft_ids)`.
- Кладётся в bounded in-memory TTL-словарь (новый маленький модуль, например
  `brainy_core/feedback.py`), значение — **только** `{provider, model,
  latency_ms, lang, route}`, без текста.
- Встраивается в `callback_data` кнопок: `f"{ACTION_FEEDBACK}_up_{request_id}"`
  / `f"{ACTION_FEEDBACK}_down_{request_id}"`.
- TTL и максимальный размер словаря ограничены (по аналогии с
  `StablePriorityQueue(maxsize=100)`, который уже bounded в этом кодбейзе) —
  старые записи вытесняются, а не растут бессрочно.
- После рестарта процесса словарь пуст: старые кнопки на старых сообщениях
  становятся "expired" при тапе — это ожидаемо, не бага, раз ничего не должно
  переживать рестарт как контент.
- После первого голоса по `request_id`: кнопки убираются
  (`edit_message_reply_markup`) и запись помечается voted (либо удаляется из
  словаря) — второй тап по тому же message не создаёт вторую log-строку.

## Privacy-риски и меры

1. **User/chat id в event.** Update-объект при тапе неизбежно содержит
   `chat_id`/`user_id` (нужны, чтобы отредактировать сообщение), но они не
   должны попасть в лог feedback-события — используются только транзитно для
   `edit_message_*` и не сохраняются. Тест должен фьюзить разные `chat_id` и
   проверять, что их значения не встречаются в тексте лога.
2. **Неограниченный рост metadata-словаря.** Пользователь может получить
   много ответов и никогда не проголосовать — записи копятся. Мера: bounded
   TTL + max size с вытеснением старых записей (тот же принцип, что уже
   применяется к `StablePriorityQueue`).
3. **Смешение с другими логами.** Если feedback-логгер использует тот же
   logger/format, что и остальной код, будущий грep для аудита может случайно
   зацепить строки с prompt/response из других частей системы. Мера:
   feedback-событие должно иметь фиксированный, легко фильтруемый префикс
   (`feedback_recorded ...`) и тест, который парсит **все** аргументы этой
   конкретной log-записи и проверяет их белый список полей — а не просто
   визуально проверяет, что "выглядит ок".
4. **Метаданные как побочный канал.** Всплеск 👎 для конкретной
   `provider`/`model` в конкретное время в принципе коррелируется с трафиком,
   как и уже существующий latency badge (`⚡2.8s`) в ответах — это тот же
   класс низкого риска, что уже принят в проекте, не новый прецедент.
5. **Спам тапов.** Повторные нажатия на одну и ту же кнопку не должны
   создавать повторные log-записи — обрабатывается идемпотентностью по
   `request_id` (см. выше), без отдельного rate-limiter.
6. **Запрет платной аналитики.** Ничего из этого не должно уходить в
   Mixpanel/Amplitude/PostHog Cloud/Segment или любой другой внешний sink —
   логи остаются локально на Mac mini (тот же файл/поток, что и остальные
   логи), в соответствии с нулевым бюджетом проекта. Если понадобится
   агрегация — локальный скрипт по логам, без сетевых вызовов.

## Локализация — 8 локалей (`de, en, es, fr, id, pt, ru, tr`)

Новые ключи (стиль/эмодзи соответствуют существующим в `translations.json`):

| Ключ | en |
|---|---|
| `feedback_thumbs_up_button` | `👍` |
| `feedback_thumbs_down_button` | `👎` |
| `feedback_recorded_up` | `Thanks for the feedback! 👍` |
| `feedback_recorded_down` | `Thanks — noted. 👎` |
| `feedback_expired` | `This vote is no longer available.` |

Кнопки-эмодзи можно оставить идентичными во всех локалях (эмодзи не требуют
перевода), но ключ всё равно должен существовать в каждой локали — так его
подхватывает существующий locale-parity тест
(`tests/test_localization.py`, `EXPECTED_KEYS`), который сейчас фиксирует
29 ключей и должен вырасти до 34 (29 + 5) с обновлённым множеством. Тексты
`feedback_recorded_*`/`feedback_expired` переводятся на все 8 языков перед
merge — без missing-key исключений, как и для остальных ключей.

## Acceptance tests

1. Каждый fast-reply ответ содержит inline-клавиатуру 👍/👎.
2. Тап 👍 или 👎: сообщение редактируется (клавиатура убирается, показывается
   локализованный `feedback_recorded_up/down`), и создаётся **ровно одна**
   log-запись `feedback_recorded` с полями из белого списка.
3. Property-тест: ни текст исходного запроса, ни текст ответа не встречаются
   подстрокой ни в одном аргументе log-записи `feedback_recorded`.
4. Повторный тап по тому же `request_id` после первого голоса не создаёт
   вторую log-запись; сообщение не падает, показывает уже отданный ответ или
   no-op edit.
5. Тап по `request_id`, которого нет в TTL-словаре (истёк/после рестарта):
   показывается `feedback_expired`, никакой лог не создаётся, исключение не
   всплывает.
6. TTL-словарь bounded: тест вставляет больше записей, чем максимальный
   размер, и проверяет, что размер никогда не превышает cap (вытесняются
   старые).
7. Locale-parity тест обновлён и проходит: все 8 локалей содержат все 5 новых
   ключей, ни одна не пропущена.
8. Fuzz-тест по разным `chat_id`/`user_id`: ни одно из значений не попадает в
   текст log-записи `feedback_recorded` ни при каком запуске.
9. Схема события содержит поле `route` (`local`/`web`) — тест проверяет, что
   design не хардкодит `"local"`, даже если Web ON UI появится позже другим
   slice.

## Минимальный slice

Границы (только это, ничего сверх):

- Bounded in-memory TTL store для feedback-metadata (`brainy_core/feedback.py`
  или аналог), без персистентности.
- `ACTION_FEEDBACK` callback prefix + клавиатура на fast-reply ответах.
- Одна log-строка `feedback_recorded` со white-list полей.
- 5 новых ключей × 8 локалей + обновление `EXPECTED_KEYS`/count в
  `tests/test_localization.py`.
- Тесты из раздела Acceptance tests.

Явно не входит: web-ответы, dashboard, экспорт, per-user история, rate
limiting сверх идемпотентности одного голоса на сообщение, любая внешняя
аналитика.

Commit slices (по конвенции `AGENTS.md`):

1. `feat: add bounded feedback metadata store`
2. `feat: attach thumbs up/down feedback to fast replies`
3. `i18n: add feedback locale keys for all 8 languages`
4. `test: cover feedback event privacy and idempotency`
