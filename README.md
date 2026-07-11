# Brainy

Brainy is a fast, multilingual Telegram chat built around local-first inference. The
product direction is one chat with an explicit `Web OFF/ON` switch:

- `Web OFF` sends the request directly to a local Ollama model.
- `Web ON` uses the configured zero-cost provider rotation and returns verifiable
  sources when at least one provider has usable quota and responds successfully.

The project is at the **Stage 0 / early Stage 1 checkpoint**. The runtime now exposes
one chat with an explicit `Web OFF/ON` route switch; the old Deep/Web modes and their
provider stack have been removed. The spaCy/reranker/page/Wikidata research utilities
are preserved as a dormant optional extra for Stage 2. Web ON rotates configured
zero-cost Brave Search, Tavily, and SerpAPI adapters with a persistent monthly
quota ledger. Providers are queried in parallel; when every configured provider is
exhausted or unavailable, Web ON is disabled until the next UTC calendar month.

## Product boundaries

- Local inference is the default and must work without external provider keys.
- Voice input with Whisper is a supported feature and will be preserved.
- All eight existing locales remain supported: `de`, `en`, `es`, `fr`, `id`, `pt`,
  `ru`, and `tr`.
- The operating budget is zero. Paid models, paid search, chargeable fallbacks,
  auto-top-ups, and Telegram Stars are not allowed.
- Private conversations and voice transcripts must not be exported to Markdown or
  otherwise persisted without an explicit retention decision.
- Charts, standalone Deep Search, and Deep Research are outside the current MVP.

See [the product strategy](docs/PRODUCT_STRATEGY.md) and
[the execution plan](docs/EXECUTION_PLAN.md) for the approved scope and rollout.

## Local setup

The supported development setup uses [uv](https://docs.astral.sh/uv/) and Python
3.12. Ollama App must be running locally. FFmpeg must also be available for Whisper
voice input.

### 1. Install uv and Python 3.12

On macOS:

```bash
brew install uv
uv python install 3.12
```

On other systems, use the installation method from the official uv documentation.

Then install the project and development dependencies:

```bash
uv sync --python 3.12
```

The preserved spaCy/reranker/page utilities are optional and are not needed for the
fast chat. Install them only while working on the future research path:

```bash
uv sync --python 3.12 --extra research
```

spaCy language models are intentionally not downloaded at import time.

### 2. Configure the local runtime

Copy the safe example configuration:

```bash
cp .env.example .env
```

Edit `.env` and set `TELEGRAM_TOKEN` when you want to run the Telegram bot. Keep
these local defaults during Stage 0:

```dotenv
LLM_CLIENT=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1
WEB_ENABLED_DEFAULT=false
SEARCH_BACKEND=rotation
TELEGRAM_RICH_MESSAGES=true
```

`OLLAMA_MODEL=gemma4:e2b` is confirmed on the target Mac mini. Its first 8K/32K/64K
single-user baseline is recorded in [the Mac mini benchmark](docs/MAC_MINI_BENCHMARK_BASELINE.md).
Keep `WEB_ENABLED_DEFAULT=false` until the Web ON evidence/synthesis slice is wired
into the Telegram runtime. `SEARCH_BACKEND=rotation` enables only providers with
configured keys. The quota ledger is stored at
`~/.local/state/brainy/search_quota.json` by default and contains counters/status,
never prompts, answers, or search content. The monthly limits are configured with
`BRAVE_SEARCH_MONTHLY_LIMIT=900`, `TAVILY_MONTHLY_LIMIT=900`, and
`SERPAPI_MONTHLY_LIMIT=200`.

The rotation order is Brave Search API, Tavily, then SerpAPI. A request reserves
one monthly slot from every configured provider and runs those calls concurrently;
successful results are merged and deduplicated by the evidence gateway. An API
failure marks that provider unavailable for the current month. If no provider
remains, Web ON is disabled until the next UTC month.

`OLLAMA_TIMEOUT` (seconds, default `120`, must stay within `0-120`) and
`OLLAMA_CONTEXT_TOKENS` (default `65536`, must stay within `1-65536`) control the
request deadline and the dynamic context ceiling described in the benchmark above;
lower `OLLAMA_CONTEXT_TOKENS` if you need to trade context length for latency/memory
on a smaller machine.

For voice input, `WHISPER_BACKEND=python` (default) uses `openai-whisper`, which
shells out to whatever `ffmpeg` is on your `PATH`. The target Mac mini instead uses
the owner's verified whisper.cpp large-v3 model:

```dotenv
WHISPER_BACKEND=cpp
WHISPER_CPP_EXECUTABLE=/opt/homebrew/bin/whisper-cli
WHISPER_CPP_FFMPEG=/opt/homebrew/bin/ffmpeg
WHISPER_CPP_MODEL=~/Library/Application Support/Brainy/models/whisper/ggml-large-v3.bin
```

`WHISPER_CPP_EXECUTABLE` and `WHISPER_CPP_FFMPEG` point at binaries that must be
installed separately (e.g. via Homebrew) and are not managed by `uv sync`;
`WHISPER_CPP_FFMPEG` is read from this exact path, not from `PATH`. The default
`WHISPER_BACKEND=python` remains available for development machines.

Never commit `.env`, tokens, API keys, or real user data.

### 3. Run the bot

With Ollama App running and the exact model tag configured:

```bash
uv run --env-file .env python bot.py
```

The Telegram process requires `TELEGRAM_TOKEN`; disabled external providers do not
require their keys. Rich Bot API 10.1 finals are attempted through PTB's raw API
escape hatch; unsupported or rejected rich payloads automatically use the regular
MarkdownV2/plain persistent-message path. A transport timeout is inherently ambiguous
because Bot API sends have no idempotency key; Brainy prioritizes delivery, so that rare
case may produce a duplicate final instead of silently losing the answer.

## Local quality gates

Run the complete Stage 0 gate before committing code:

```bash
PYTHONPYCACHEPREFIX=/tmp/brainy-pycache uv run python -m compileall -q brainy_core tests *.py
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

Normal tests must not contact Telegram, Ollama, search backends, or external model
providers.

## Current limitations

- The route switch is session-persistent and captured with each buffered request;
  durable persistence across bot restarts is not implemented yet.
- Search providers are best-effort and can still fail or report exhausted quotas;
  provider errors rotate to the remaining configured providers, while all-provider
  failure disables Web ON until the next UTC calendar month.
- Telegram progressive delivery, safe rich finals, EvidenceBundle synthesis, and
  citation validation are implemented; a real search result is still required for
  a live citation smoke.
- The repository is under active Stage 0 cleanup and is not production-ready.

## License

Brainy is licensed under the MIT License. See [LICENSE](LICENSE).
