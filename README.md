# Brainy Bot

A Telegram bot powered by Large Language Models (LLMs) for deep search and research, providing comprehensive answers and generating charts. Visit our website at https://askbrainy.com.

## Features

*   **Web Search:** Fast answers with real-time context from the internet.
*   **Deep Search:** For more complex questions, synthesizing information from multiple sources.
*   **Deep Research:** Conducts multi-step research and provides detailed reports.
*   **Chart Generation:** Visualizes key findings from research data.
*   **Multilingual Support:** Communicates in multiple languages.

## Setup and Installation

### Prerequisites

*   Python 3.8+
*   Telegram Bot Token (from BotFather)
*   Yandex Search API Key
*   Together AI API Key (or Ollama setup)

### Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/YOUR_USERNAME/brainy.git
    cd brainy
    ```
2.  **Create a virtual environment (recommended):**
    ```bash
    python3 -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```
3.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

### Configuration

Create a `config.py` file (or set environment variables) with your API keys and other settings.
**IMPORTANT:** Never commit your actual API keys to version control. Use environment variables or a `.env` file.

Example `config.py` (if you choose not to use environment variables directly):

```python
# config.py
TELEGRAM_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
YANDEX_API_KEY = "YOUR_YANDEX_API_KEY"
TOGETHER_AI_API_KEY = "YOUR_TOGETHER_AI_API_KEY" # Or relevant API key for Ollama

# LLM Client (choose "together" or "ollama")
LLM_CLIENT = "together" 

# Other configurations (e.g., model names, thresholds)
TOGETHER_MODEL = "mistralai/Mixtral-8x7B-Instruct-v0.1"
TOGETHER_DEEPSEEK = "deepseek-ai/deepseek-coder-6.7b-instruct"
TOGETHER_WEB_SEARCH = "mistralai/Mixtral-8x7B-Instruct-v0.1"
TOGETHER_DEEPSEEK = "deepseek-ai/deepseek-coder-6.7b-instruct"
DEEP_SEARCH_STEP_SIX_MODEL = "mistralai/Mixtral-8x7B-Instruct-v0.1"
CREATIVE_PARAMS = {"temperature": 0.7, "top_k": 50, "top_p": 0.9, "frequency_penalty": 0.2, "repetition_penalty": 1.1}

SEARCH_BACKEND = "yandex" # or "google" if you implement it
RERANK_MODEL = "BAAI/bge-reranker-base"

TOP_N = 5
RERANK_THRESHOLD = 0.5
```

*   `MD_OUTPUT_DIR`: Directory for generated Markdown files (default: `md`).
*   `CHARTS_OUTPUT_DIR`: Directory for generated chart images (default: `charts`).

Alternatively, set these as environment variables:

```bash
export TELEGRAM_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"
export YANDEX_API_KEY="YOUR_YANDEX_API_KEY"
export TOGETHER_AI_API_KEY="YOUR_TOGETHER_AI_API_KEY"
# ... other variables
export MD_OUTPUT_DIR="your_md_folder"
export CHARTS_OUTPUT_DIR="your_charts_folder"
```

### Running the Bot

```bash
python bot.py
```

## Contributing

Contributions are welcome! Please open an issue or submit a pull request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
