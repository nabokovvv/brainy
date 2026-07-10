# Brainy

Brainy is a fast, multilingual Telegram chat built around local-first inference. The
product direction is one chat with an explicit `Web OFF/ON` switch:

- `Web OFF` sends the request directly to a local Ollama model.
- `Web ON` will add a zero-cost search path with verifiable sources.

The project is at the **Stage 0 / early Stage 1 checkpoint**. The runtime now exposes
one chat with an explicit `Web OFF/ON` route switch; the old Deep/Web modes and their
provider stack have been removed. The spaCy/reranker/page/Wikidata research utilities
are preserved as a dormant optional extra for Stage 2. Web ON search is not
implemented yet and therefore fails closed instead of silently returning a local
answer as fresh.

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
SEARCH_BACKEND=disabled
```

`OLLAMA_MODEL=gemma4:e2b` is confirmed on the target Mac mini. Its first 8K/32K/64K
single-user baseline is recorded in [the Mac mini benchmark](docs/MAC_MINI_BENCHMARK_BASELINE.md).
Do not enable web yet: a production-safe, zero-cost backend has not been integrated.

Never commit `.env`, tokens, API keys, or real user data.

### 3. Run the bot

With Ollama App running and the exact model tag configured:

```bash
uv run --env-file .env python bot.py
```

The Telegram process requires `TELEGRAM_TOKEN`; disabled external providers do not
require their keys.

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
- Web search is disabled until a safe zero-cost provider contract is implemented.
- The exact Gemma Ollama tag and 8K/32K/64K performance still need verification on
  the target Mac mini M4 with 16 GB unified memory.
- The repository is under active Stage 0 cleanup and is not production-ready.

## License

Brainy is licensed under the MIT License. See [LICENSE](LICENSE).
