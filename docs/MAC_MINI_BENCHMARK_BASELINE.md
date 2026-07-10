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

## Остаётся

- synthetic full-context fill для 32K/64K;
- 15-question multilingual fixture; реальный прогон и ручная оценка pending;
- 3 concurrent-user benchmark;
- combined-memory benchmark с рабочей моделью Whisper; FFmpeg и `whisper.cpp` есть,
  встроенный tiny-model smoke прошёл, но Brainy-adapter ещё не подключён.
