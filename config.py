import os

# Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable not set.")

# LLM Client Configuration
LLM_CLIENT = os.getenv("LLM_CLIENT", "together")  # Can be "ollama" or "together"

# Ollama
OLLAMA_ENDPOINT = os.getenv("OLLAMA_ENDPOINT", "http://localhost:11434/v1/completions")
OLLAMA_CHAT_ENDPOINT = os.getenv("OLLAMA_CHAT_ENDPOINT", "http://localhost:11434/v1/chat/completions")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:14b-instruct-q4_K_M")
FAST_REPLY_MODEL = os.getenv("FAST_REPLY_MODEL", "qwen2.5:7b-instruct")
FACTUAL_PARAMS = {"temperature": 0.3, "top_k": 50, "top_p": 0.9, "frequency_penalty": 0.2, "max_tokens": 1024, "repetition_penalty": 1.1}
FACTUAL_PARAMS_2 = {"temperature": 0.3}
# Parameters for creative tasks like generating sub-queries
CREATIVE_PARAMS = {"temperature": 0.7, "top_p": 0.9}
DEEP_SEARCH_STEP_ONE_MODEL = os.getenv("DEEP_SEARCH_STEP_ONE_MODEL", "llama3:8b-instruct-q4_K_M")
DEEP_SEARCH_STEP_SIX_MODEL = os.getenv("DEEP_SEARCH_STEP_SIX_MODEL", "deepseek-r1:7b")
DEEP_SEARCH_STEP_FINAL_MODEL = os.getenv("DEEP_SEARCH_STEP_FINAL_MODEL", "mistral:7b-instruct-q4_K_M")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", 900)) # 15 minutes

# Together AI
TOGETHER_AI_API_KEY = os.getenv("TOGETHER_AI_API_KEY")
if not TOGETHER_AI_API_KEY:
    raise ValueError("TOGETHER_AI_API_KEY environment variable not set.")

TOGETHER_MODEL = os.getenv("TOGETHER_MODEL", "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free")
TOGETHER_DEEPSEEK = os.getenv("TOGETHER_DEEPSEEK", "deepseek-ai/DeepSeek-R1-Distill-Llama-70B-free")
TOGETHER_WEB_SEARCH = os.getenv("TOGETHER_WEB_SEARCH", "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free")
TOGETHER_FAST = os.getenv("TOGETHER_FAST", "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free")
TOGETHER_SUMMARY = os.getenv("TOGETHER_SUMMARY", "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free")
TOGETHER_QUERIES = os.getenv("TOGETHER_QUERIES", "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free")


# Search
SEARCH_BACKEND = os.getenv("SEARCH_BACKEND", "yandex") # or another supported backend
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
if not YANDEX_API_KEY:
    raise ValueError("YANDEX_API_KEY environment variable not set.")

# Reranker
RERANK_MODEL = os.getenv("RERANK_MODEL", "sentence-transformers/paraphrase-multilingual-mpnet-base-v2")
RERANK_THRESHOLD = float(os.getenv("RERANK_THRESHOLD", 0.69))
TOP_N = int(os.getenv("TOP_N", 5))

WIKIDATA_ACCESS_TOKEN = os.getenv("WIKIDATA_ACCESS_TOKEN")
if not WIKIDATA_ACCESS_TOKEN:
    raise ValueError("WIKIDATA_ACCESS_TOKEN environment variable not set.")

# Output Directories
MD_OUTPUT_DIR = os.getenv("MD_OUTPUT_DIR", "md")
CHARTS_OUTPUT_DIR = os.getenv("CHARTS_OUTPUT_DIR", "charts")

# Custom User-Agent
CUSTOM_USER_AGENT = os.getenv("CUSTOM_USER_AGENT", "BrainyBot/1.0 (https://askbrainy.com)")
