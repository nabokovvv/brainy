# Mac mini M4 16 GB: local baseline

Дата: 2026-07-10. Target: `Mac.local`, Ollama local API, model `gemma4:e2b`.

## Метод

- Один synthetic, неперсональный запрос; результат не сохранялся.
- Native Ollama `/api/chat`, `think: false`, один запрос одновременно.
- Короткий ответ ограничен 32 или 128 output tokens.
- Сначала cold context allocation, затем немедленный warm повтор.
- RSS — сумма процессов Ollama; swap — системный счётчик macOS.

## Warm результаты

| Context | Output cap | TTFT | Generation | Total | Ollama RSS | Swap change |
|---|---:|---:|---:|---:|---:|---:|
| 8K | 128 | 355 ms | 40.6 tok/s | 2.25 s | 6.05 GB | 0 |
| 32K | 32 | 361 ms | 42.0 tok/s | 790 ms | 3.56 GB | 0 |
| 64K | 32 | 360 ms | 41.7 tok/s | 840 ms | 6.40 GB | 0 |

Перед/после всех проб использованный swap оставался 1.17 GB из 2 GB. Cold allocation
при переключении context window занимала около 3.4–4.7 s; это не user-facing warm
TTFT. На target Ollama metadata заявляет 131072-token context, но продуктовый ceiling
пока остаётся 64K до full-context и combined-memory проверок.

## Вывод

`gemma4:e2b` проходит initial single-user fast-path baseline на 8K/32K/64K: observed
generation выше исходного ориентира владельца 30 tok/s, новый swap не появился.
Это не означает готовность к public beta: пробы использовали короткий input и одного
пользователя.

## Три одновременных пользователя

2026-07-11 три synthetic-запроса одновременно поступили в один сериализованный
generation slot с 8K context и output cap 64. Cold запрос завершился за 5.87 s,
два warm — примерно за 1.93 s каждый; максимальная end-to-end задержка с очередью
составила 9.95 s. Все три запроса завершились, generation держалась около 40 tok/s,
RSS стабилизировался около 5.9 GB, использованный swap не изменился (1063.38 MB).
Сырые метрики сохранены в `tests/results/gemma4-e2b-concurrency-2026-07-11.json`.
Этот bounded batch не является длительным fairness/load-тестом.

## Full-context retention smoke

2026-07-11 synthetic prompts с маркерами в начале и конце проверены при 32K и 64K
context. Фактические `prompt_eval_count` — 32,000 и 64,000; оба ответа сохранили оба
маркера. Новый swap не появился. 32K: TTFT 69.84 s, input 469 tok/s, RSS 6.44 GB.
64K: TTFT 191.78 s, input 337 tok/s, RSS 6.82 GB. Это подтверждает доступность и
базовое удержание контекста, но не качество длинных ответов. Число filler-токенов
эвристическое; authoritative значение — `prompt_eval_count` из Ollama. Метрики:
`tests/results/gemma4-e2b-full-context-{32k,64k}-2026-07-11.json`.

## Multilingual quality

15-question synthetic run дал 15/15 non-empty и 14/15 по ручной оценке. Единственная
ошибка — некорректная формулировка об изоляции дерева в индонезийском ответе.
Response и review fixtures сохранены в `tests/results/`.

## Whisper large-v3 combined memory

На target найдена существующая `whisper.cpp` large-v3 модель размером 2.9 GB. При
persistent `whisper-server` её RSS составил 3.36 GB; вместе с warm Gemma — 9.17 GB.
Gemma сохранила TTFT 358 ms и total 386 ms, а тестовая транскрипция была непустой.
Однако загрузка Whisper увеличила использованный swap примерно с 1055 MB до 1801 MB.
Сам Gemma-запрос нового swap не добавил.

Итог владельца: рост swap примерно на 746 MB допустим ради качества large-v3 и не
блокирует MVP; наблюдаем длительный memory pressure, но не заменяем модель только
ради нулевого swap. Метрики сохранены в
`tests/results/gemma4-e2b-whisper-large-v3-combined-2026-07-11*.json`.
Model file перенесён в постоянный
`~/Library/Application Support/Brainy/models/whisper/ggml-large-v3.bin`; сама модель
не хранится в Git.

## Остаётся

- подключить whisper.cpp large-v3 к Brainy;
- UI tests для progressive Telegram delivery и message limits.
