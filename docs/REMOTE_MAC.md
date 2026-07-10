# Доступ к Mac mini для Brainy

Статус: локальный key-based доступ подтверждён 2026-07-10.

## Подключение

Использовать Bonjour-имя в локальной сети, а не изменчивый IP:

```bash
ssh lobster@Mac.local
```

Не хранить здесь пароль, внешний IP, private key или токены. Для будущих сессий
использовать только key-based SSH; публичный ключ разработческого Mac уже добавлен в
`~/.ssh/authorized_keys` пользователя `lobster`.

## Проверка target перед benchmark

```bash
hostname
ipconfig getifaddr en1
curl --fail --max-time 3 http://127.0.0.1:11434/api/tags
/opt/homebrew/bin/ffmpeg -version
```

Ollama App может не добавлять CLI `ollama` в shell `PATH`, поэтому readiness
проверяется локальным API, а не наличием команды. Не запускать скачивание моделей,
внешние inference-запросы или benchmark до подтверждения exact model tag из
`/api/tags`.

На target подтверждён `gemma4:e2b` (Ollama metadata: 4.65B, 131072 context). Для
fast path передавать `reasoning_effort: "none"` через OpenAI-compatible endpoint
или `think: false` через native `/api/chat`: иначе Gemma может тратить ответ на
скрытый thinking trace вместо видимого Telegram текста.

Для synthetic, non-user benchmark использовать
`tools/benchmark_ollama.py`; он допускает только loopback Ollama API и печатает
метрики без prompt/response text. Скрипт копируется во временную директорию target,
а JSON-результат добавляется в документацию только после review. Всегда задавать
`--max-output-tokens`, чтобы ответы были сопоставимы с коротким Telegram UX.

Первый verified single-user baseline находится в `MAC_MINI_BENCHMARK_BASELINE.md`.
Key-based SSH через `lobster@Mac.local` подтверждён; для будущих сессий не нужны
пароль или IP.

## Voice status

`/opt/homebrew/bin/ffmpeg` и Homebrew `whisper.cpp` (`whisper-cli` 1.9.1) уже
установлены. Встроенный публичный tiny-model smoke прошёл 2026-07-10. Brainy пока
использует lazy Python `openai-whisper`, поэтому это не означает готовой интеграции:
не скачивать модели и не ставить зависимости без отдельного разрешения владельца.
