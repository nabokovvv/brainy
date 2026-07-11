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

Проверенная large-v3 модель находится в
`~/Library/Application Support/Brainy/models/whisper/ggml-large-v3.bin`; executable —
`/opt/homebrew/bin/whisper-cli`. Fingerprint модели сохранён в combined benchmark
review, сама модель не копируется в репозиторий.

Повторная проверка 2026-07-11 через non-login SSH shell подтвердила, что
абсолютные пути работают независимо от PATH: ffmpeg и whisper-cli успешно
обработали существующий локальный audio fixture с large-v3 примерно за 6 секунд.

## Web ON: состояние remote smoke

Проверка 2026-07-11 выполнялась из отдельного `~/Brainy-runtime`, развёрнутого
из локального checkout `ec735b1` без Git-метаданных, `.env` и кэшей. Не трогать
CI checkout в `~/actions-runner/_work/brainy/brainy`: он находится на старом
commit `3cf255f` и не содержит Web ON evidence modules.

Ollama loopback API на target готов. Однако живой Telegram runtime на target не
найден: в runtime-каталоге отсутствует `.env`, поэтому нельзя запускать Bot API
smoke или отправлять реальные сообщения без явного provisioning token.

Legacy DuckDuckGo path удалён. Новый Web ON runtime использует sequential rotation
Tavily → Brave Search API → SerpAPI с лимитами 900/900/200 запросов в UTC
месяц. Счётчики хранятся в `SEARCH_QUOTA_STATE_PATH` и не содержат пользовательский
контент. При ошибках всех доступных API или исчерпании всех лимитов Web ON
отключается до следующего UTC месяца.

После перехода на sequential rotation реальный smoke получил 3 результата за 0.53 s;
quota delta составил `tavily=1`, `brave=0`, `serpapi=0`. Это подтверждает, что
успешный запрос расходует только один provider quota. Query, snippets и ответ в
метрики/логи не записывались.

### Дальнейшие шаги

1. Добавить Telegram token в защищённый runtime `.env` (не добавлять в Git), если
   нужен живой Bot API smoke; search keys уже сохранены с правами `600`.
2. Запустить один нейтральный Telegram Web ON
   smoke, проверить canonical citation links и замерить search/synthesis/Telegram
   latency без сохранения текста пользователя или ответа.
3. Отдельно повторить smoke при недоступности всех search APIs и подтвердить
   локализованный `web_unavailable` без local fallback.
