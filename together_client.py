import httpx
import json
import logging
import re
from urllib.parse import urlparse, unquote
from functools import wraps
import asyncio
import math
import time
import config
from utils import detect_language, _filter_duplicate_chunks, strip_think
from httpx import ReadTimeout, ConnectTimeout, ConnectError, RemoteProtocolError


from together import AsyncTogether, error

logger = logging.getLogger(__name__)

THINKING_GUIDANCE = (
    "IMPORTANT: Your thinking MUST be completely contained within silent <think> tags. "
    "Begin your response with <think> and think through the task step-by-step. "
    "Do NOT repeat the user's question or any instructions in your thinking. "
    "Do NOT mention that you are thinking. "
    "Keep reasoning concise and focused. "
    "After completing your thinking, close with </think> and provide ONLY the final answer. "
)


# === Header-aware rate limiter & free-model fallback ===
LLAMA_FREE = config.TOGETHER_MODEL
DEEPSEEK_FREE = config.TOGETHER_DEEPSEEK
API_URL = "https://api.together.xyz/v1/chat/completions"

def _other_free(model_str: str) -> str:
    m = (model_str or "").lower()
    if "apriel-1.6" in m:
        return DEEPSEEK_FREE
    if "apriel-1.5" in m:
        return LLAMA_FREE
    if "deepseek" in m:
        return LLAMA_FREE
    if "llama" in m or "meta-llama" in m:
        return DEEPSEEK_FREE
    # default fallback if unknown
    return DEEPSEEK_FREE


_model_next_ok = {}
_model_lock = asyncio.Lock()

def _headers_lower(h):
    try:
        return {k.lower(): v for k, v in h.items()}
    except Exception:
        return {}

def _parse_rate_headers(h) -> dict:
    hl = _headers_lower(h)
    def _f(key, default=None, cast=float):
        try:
            return cast(hl.get(key, default))
        except Exception:
            return default
    return {
        "limit_rps": _f("x-ratelimit-limit", 0.05),
        "remaining": _f("x-ratelimit-remaining", 0, int),
        "reset_s":   _f("x-ratelimit-reset", None),
        "retry_after": _f("retry-after", None),
    }

def _get_next_ok(model: str) -> float:
    return _model_next_ok.get(model, 0.0)

def _choose_model_prefer_llama() -> str:
    """Choose the model with sooner availability; prefer Llama if both ready now."""
    now = asyncio.get_event_loop().time()
    llama_ready_in = max(0.0, _get_next_ok(LLAMA_FREE) - now)
    deep_ready_in  = max(0.0, _get_next_ok(DEEPSEEK_FREE) - now)
    if llama_ready_in == 0 and deep_ready_in == 0:
        return LLAMA_FREE
    return LLAMA_FREE if llama_ready_in <= deep_ready_in else DEEPSEEK_FREE

async def _wait_if_needed(model: str):
    now = asyncio.get_event_loop().time()
    async with _model_lock:
        t = _model_next_ok.get(model, 0.0)
    if t > now:
        await asyncio.sleep(t - now)

async def _respect_headers(model: str, headers, pace_after_success: bool = True):
    meta = _parse_rate_headers(headers)
    limit_rps = meta.get("limit_rps") or 0.05
    remaining = meta.get("remaining")
    reset_s   = meta.get("reset_s")
    retry_after = meta.get("retry_after")

    now = asyncio.get_event_loop().time()
    async with _model_lock:
        next_ok = _model_next_ok.get(model, now)

    delay = 0.0
    if remaining is not None and remaining <= 0:
        base = reset_s if reset_s is not None else (1.0/limit_rps if limit_rps > 0 else 2.0)
        delay = max(math.ceil(base) + 0.5, (retry_after or 0.0))
    elif pace_after_success and limit_rps and limit_rps > 0:
        delay = max(delay, 1.0/limit_rps)

    if delay > 0:
        async with _model_lock:
            _model_next_ok[model] = max(next_ok, now + delay)

async def _chat_once(*, model: str, messages: list, **kwargs) -> dict:
    """Raw Together call that obeys per-second headers. Returns JSON dict."""
    await _wait_if_needed(model)
    headers = {
        "Authorization": f"Bearer {config.TOGETHER_AI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"model": model, "messages": messages}
    payload.update(kwargs)
    timeout = kwargs.pop(
        "timeout",
        httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=60.0)
    )
    async with httpx.AsyncClient(timeout=timeout) as ac:
        resp = await ac.post(API_URL, headers=headers, json=payload)
        lr = {k:v for k,v in resp.headers.items() if k.lower().startswith(("x-rate","retry-after"))}
        logger.info(f"[LLM] <- {model} {resp.status_code}; rate_headers={lr}")
        await _respect_headers(model, resp.headers, pace_after_success=(resp.status_code==200))
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code in (429, 503):
            await _respect_headers(model, resp.headers, pace_after_success=False)
            try:
                body = resp.json()
            except Exception:
                body = {"text": resp.text[:500]}
            if resp.status_code == 429:
                raise error.RateLimitError(str(body))
            raise error.ServiceUnavailableError(str(body))
        else:
            resp.raise_for_status()

async def chat_with_fallback(*, model: str | None = None, messages: list, immediate_on_429: bool = True, **gen_kwargs) -> dict:
    """Use the caller-provided `model` as PRIMARY; on 429/503/timeout, immediately try the OTHER free model.
    If `model` is None, choose based on readiness (preferring Llama when both are free)."""
    # Pop model from kwargs if it came via **payload
    if model is None and 'model' in gen_kwargs:
        model = gen_kwargs.pop('model')
    primary = model or _choose_model_prefer_llama()
    secondary = _other_free(primary)
    try:
        logger.info(f"[LLM] primary={primary} secondary={secondary}")
        return await _chat_once(model=primary, messages=messages, **gen_kwargs)
    except (error.RateLimitError,
            error.ServiceUnavailableError,
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
            httpx.ConnectError,
            httpx.RemoteProtocolError):
        # помечаем первичную модель как «занятую» на пару секунд, чтобы планировщик не дёргал её тут же
        now = asyncio.get_event_loop().time()
        async with _model_lock:
            _model_next_ok[primary] = max(_model_next_ok.get(primary, 0.0), now + 3.0)
        if immediate_on_429:
            logger.warning(f"[LLM] {primary} limited/unavailable; fallback -> {secondary}")
            return await _chat_once(model=secondary, messages=messages, **gen_kwargs)
        raise
# === End header-aware layer ===
# Initialize the asynchronous client
client = AsyncTogether(api_key=config.TOGETHER_AI_API_KEY)

# Shared state for rate limiting
_rate_limit_state = {
    "lock": asyncio.Lock(),
    "until": 0,
}

def retry_on_server_error(retries=4, delay=2, backoff=2):
    """A decorator to retry a function call on server-side (5xx) or rate limit errors."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Check and wait if we are in a cooldown period
            async with _rate_limit_state["lock"]:
                wait_duration = _rate_limit_state["until"] - asyncio.get_event_loop().time()
            
            if wait_duration > 0:
                logger.warning(f"Rate limit cooldown is active. Waiting for {wait_duration:.2f}s.")
                await asyncio.sleep(wait_duration)

            # Proceed with the retry loop
            _retries = retries
            _delay = delay

            for attempt in range(_retries):
                try:
                    return await func(*args, **kwargs)
                except (error.RateLimitError, error.ServiceUnavailableError, error.APIError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError) as e:
                    # If this was the last attempt, re-raise the exception
                    if attempt == _retries - 1:
                        logger.error(f"Final attempt failed for {func.__name__}. Raising error.", exc_info=True)
                        raise

                    wait_time = _delay

                    # For the second-to-last attempt, set a long delay
                    if attempt == _retries - 2:
                        wait_time = 60

                    log_message = f"API error in `{func.__name__}`. Retrying in {wait_time:.2f}s... (Attempt {attempt + 1}/{_retries})"

                    if isinstance(e, error.RateLimitError):
                        wait_time = 60  # Rate limit wait always overrides other delays
                        log_message = f"Rate limit hit for {func.__name__}. Waiting {wait_time}s... (Attempt {attempt + 1}/{_retries})"
                        # Also set the shared cooldown
                        async with _rate_limit_state["lock"]:
                            _rate_limit_state["until"] = asyncio.get_event_loop().time() + wait_time
                    
                    logger.warning(log_message, exc_info=True)
                    await asyncio.sleep(wait_time)

                    # Only apply backoff for the initial, shorter retries
                    if attempt < _retries - 2:
                        _delay *= backoff
        return wrapper
    return decorator

@retry_on_server_error()
async def get_sub_queries(query: str, lang: str) -> list[str]:
    detected_user_lang = detect_language(query)
    prompt_lang = 'en' if detected_user_lang == 'en' else lang

    prompt = f"""Based on the following query, generate up to 4 sub-queries for a web search to gather the necessary information to provide a comprehensive answer. Try both shorter and longer search queries. Three of them should be in "{prompt_lang}" language, and one - in English. Return the sub-queries as a clean JSON list of strings without any comments.

{THINKING_GUIDANCE}

Query from user: {query}"""
    
    logger.info(f"Together AI (sub-queries) - Prompt: {prompt}")
    try:
        response = data = await chat_with_fallback(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7, 
            top_p=0.9
        )
        response_text = strip_think(data['choices'][0]['message']['content']).strip()
    except Exception as e:
        logger.error(f"Together AI (sub-queries) - Request failed: {e}")
        raise

    logger.info(f"Together AI (sub-queries) - Raw Response: {response_text}")
    
    sub_queries = []
    try:
        json_match = re.search(r'\[[\s\S]*\]', response_text)
        if json_match:
            json_string = json_match.group(0)
            json_string = re.sub(r"//.*", "", json_string)
            json_string = re.sub(r",\s*([\}\]])", r"\1", json_string)
            sub_queries = json.loads(json_string)
        else:
             logger.warning("Together AI (sub-queries) - No JSON list found in the response.")
    except json.JSONDecodeError as e:
        logger.warning(f"Together AI (sub-queries) - Could not decode JSON: {e}. Raw string was: {json_string if 'json_string' in locals() else ''}")
        sub_queries = re.findall(r'\d+\.\s*"(.*?)"|\d+\.\s*(.*)', response_text)
        sub_queries = [item for sublist in sub_queries for item in sublist if item]

    logger.info(f"Together AI (sub-queries) - Parsed Sub-queries: {sub_queries}")
    return sub_queries


@retry_on_server_error()
async def get_research_steps(query: str, lang: str, entities_info: list) -> list[str]:
    detected_user_lang = detect_language(query)
    prompt_lang = 'en' if detected_user_lang == 'en' else lang

    entity_context = ""
    if entities_info:
        entity_context = "\n\nDiscovered Entities:\n"
        for entity in entities_info:
            entity_context += f"- {entity['entity']}\n"

    prompt = f"""You are a researcher. Break down the user's question into several logical research steps. Use the provided entity details to create more accurate and specific steps. In each step, instead of pronouns, be sure to indicate the full name of the object(s) of research. Also, in each step, keep the general context of the research (user's request). Do not refer to other steps or to the future results of other steps. If it is absolutely necessary to refer to other steps, then repeat the context of what was in the previous steps.

Your response must be in the "{prompt_lang}" language.

Return the steps as a clean JSON list of strings, with a maximum of 6 items in {prompt_lang} language. For example:
[
  "Check A",
  "Review B",
  "Compare A and B"
]

{THINKING_GUIDANCE}

Query from user: {query}
"""
    if entity_context:
        prompt += f"{entity_context} EACH QUESTION SHOULD CONTAIN AT LEAST ONE ENTITY NAME"
    
    logger.info(f"Together AI (research-steps) - Prompt: {prompt}")
    try:
        response = data = await chat_with_fallback(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            top_p=0.9
        )
        response_text = strip_think(data['choices'][0]['message']['content']).strip()
    except Exception as e:
        logger.error(f"Together AI (research-steps) - Request failed: {e}")
        raise
    
    logger.info(f"Together AI (research-steps) - Raw Response: {response_text}")
    
    steps = []
    try:
        json_match = re.search(r'\[[\s\S]*\]', response_text)
        if json_match:
            json_string = json_match.group(0)
            json_string = re.sub(r"//.*", "", json_string)
            json_string = re.sub(r",\s*([\}\]])", r"\1", json_string)
            steps = json.loads(json_string)
        else:
             logger.warning("Together AI (research-steps) - No JSON list found in the response.")
    except json.JSONDecodeError as e:
        logger.warning(f"Together AI (research-steps) - Could not decode JSON: {e}. Raw string was: {json_string if 'json_string' in locals() else ''}")
        steps = re.findall(r'\d+\.\s*"(.*?)"|\d+\.\s*(.*)', response_text)
        steps = [item for sublist in steps for item in sublist if item]

    logger.info(f"Together AI (research-steps) - Parsed Steps: {steps}")
    return steps

@retry_on_server_error()
async def synthesize_research_answer(query: str, research_data: dict, lang: str) -> str:
    detected_user_lang = detect_language(query)
    prompt_lang = 'en' if detected_user_lang == 'en' else lang

    formatted_research_data = ""
    for step, summary in research_data.items():
        formatted_research_data += f"### {step}\n"
        formatted_research_data += f"{summary}\n\n"

    prompt = f"""You are a chief editor. Your task is to generate an engaging introduction where you highlight some findings of the presented research and a concise TL;DR (Too Long; Didn't Read) summary for a research report based on the provided research items. Your ultimate goal is to help answer the user's query: "{query}".

Your response MUST be in the "{prompt_lang}" language.

Output your response in JSON format with two keys: "intro" and "tldr".

Example JSON output:
```json
{{
  "intro": "Your introduction here",
  "tldr": "Your TL;DR summary here"
}}
```

**Research Data (Summaries of each research item):**
{formatted_research_data}

{THINKING_GUIDANCE}
"""
    logger.info(f"Together AI (research-synthesis) - Prompt: {prompt}")
    try:
        response = data = await chat_with_fallback(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        response_text = strip_think(data['choices'][0]['message']['content']).strip()
    except Exception as e:
        logger.error(f"Together AI (research-synthesis) - Request failed: {e}")
        raise
    
    logger.info(f"Together AI (research-synthesis) - Response: {response_text}")
    return response_text

@retry_on_server_error()
async def synthesize_answer(query: str, research_data: list, lang: str, entities_info: list) -> str:
    # Define model and token limits
    MODEL_CONTEXT_WINDOW = 12000
    MAX_OUTPUT_TOKENS = 4000  # Reduced to leave more buffer
    CHAR_PER_TOKEN_ESTIMATE = 3  # More conservative estimate

    # --- 1. Construct the static parts of the prompt ---
    detected_user_lang = detect_language(query)
    prompt_lang = 'en' if detected_user_lang == 'en' else lang

    entity_context = ""
    if entities_info:
        entity_context = "\n\nDiscovered Entities and their details:\n"
        for entity in entities_info:
            entity_context += f"- Entity: {entity['entity']}\n"
            if entity.get('description'):
                entity_context += f"  Description: {entity['description']}\n"
            if entity.get('lead_paragraph'):
                entity_context += f"  Lead Paragraph: {entity['lead_paragraph']}\n"
            entity_context += f"  QID: {entity['qid']}\n"

    # Base prompt template without the dynamic context
    base_prompt_template = f"""You are a skilled researcher. You are able to pick the most relevant data from a very broad context to answer the user's query in a detailed, structured, and precise way. Write a complete, coherent, and fact-rich answer to the user's query from context snippets and discovered entities. Keep only unique and valuable information (guidance, facts, numbers, addresses, characteristics) related to the user's query.\n\n**Instructions:**\n1. **Your response MUST be in the \"{prompt_lang}\" language, regardless of the language of the snippets.**\n2. Synthesize the information from all sub-queries to create a single, coherent answer to the main question.\n3. Information discovered in \"Discovered entities and their details\" is the most reliable, and it is your final source of truth.\n4. {THINKING_GUIDANCE}\n\n**Main Question:** {query}\n{entity_context}\n\n**Context from search results:**\n"""

    # --- 2. Calculate available space for dynamic context ---
    base_prompt_char_len = len(base_prompt_template) + len(query) # Also account for user query in messages
    # Calculate the character limit for the snippets
    max_context_char_limit = (MODEL_CONTEXT_WINDOW - MAX_OUTPUT_TOKENS) * CHAR_PER_TOKEN_ESTIMATE - base_prompt_char_len

    if max_context_char_limit <= 0:
        raise ValueError("The base prompt, query, and entities are too long to fit any context.")

    # --- 3. Build and truncate the dynamic context ---
    contexts = []
    for item in research_data:
        sub_query = item['query']
        snippets = item['snippets']
        if not snippets:
            continue
        
        unique_snippets = _filter_duplicate_chunks(snippets)
        snippet_text = "\n".join([f"- {s.text}" for s in unique_snippets])
        contexts.append(f"Sub-query: {sub_query}\nSnippets:\n{snippet_text}")

    if not contexts:
        return "Could not find relevant information."

    formatted_contexts = "\n\n".join(contexts)
    
    if len(formatted_contexts) > max_context_char_limit:
        logger.warning(f"Context length ({len(formatted_contexts)}) exceeds limit ({max_context_char_limit}). Truncating.")
        formatted_contexts = formatted_contexts[:max_context_char_limit]
        last_newline = formatted_contexts.rfind('\n')
        if last_newline != -1:
            formatted_contexts = formatted_contexts[:last_newline]

    # --- 4. Assemble the final prompt and make the API call ---
    final_prompt = base_prompt_template + formatted_contexts
    
    logger.info(f"Together AI (synthesis) - Final Prompt Length: {len(final_prompt)} chars")
    try:
        response = data = await _chat_once(
            model=config.TOGETHER_WEB_SEARCH,
            messages=[
                {"role": "system", "content": final_prompt},
                {"role": "user", "content": query}
            ],
            temperature=0.3,
            max_tokens=MAX_OUTPUT_TOKENS,
            reasoning_effort = "medium"
        )
        response_text = strip_think(data['choices'][0]['message']['content']).strip()
    except Exception as e:
        logger.error(f"Together AI (synthesis) - Request failed: {e}")
        raise
    
    logger.info(f"Together AI (synthesis) - Response: {response_text}")
    return response_text

def contains_chinese(text: str) -> bool:
    return bool(re.search(r'[\u4e00-\u9fff]', text))

async def translate_if_needed(query: str, original_answer: str) -> str:
    if not contains_chinese(original_answer):
        return original_answer

    logger.warning(f"Together AI (prompt_without_context) - Chinese detected in response: {original_answer}")
    detected_language = detect_language(query)
    logger.info(f"Detected query language: {detected_language}")

    translation_prompt = f'''Answer the user\'s question in the {detected_language} language. {THINKING_GUIDANCE} User\'s question: "{query}".'''
    try:
        response = data = await chat_with_fallback(
            messages=[{"role": "user", "content": translation_prompt}],
            temperature=0.3,
            top_k=50,
            top_p=0.9,
            frequency_penalty=0.2,
            repetition_penalty=1.1,
            max_tokens=1024
        )
        translated_answer = response.choices[0].message.content.strip()
        logger.info(f"Together AI (prompt_without_context) - Translated answer: {translated_answer}")
        return translated_answer
    except Exception as e:
        logger.error(f"Together AI (prompt_without_context) - Translation failed: {e}")
        raise

@retry_on_server_error()
async def prompt_without_context(query: str, lang: str, model: str = None, params: dict = None) -> str:
    detected_user_lang = detect_language(query)
    prompt_lang = 'en' if detected_user_lang == 'en' else lang

    prompt = f"""You are a helpful AI assistant. Always answer in the "{prompt_lang}" language!
{THINKING_GUIDANCE}
    
Question from the user: {query}
"""
    
    final_model = model if model is not None else config.TOGETHER_MODEL
    final_params = params if params is not None else {
        "temperature": 0.3, "top_k": 50, "top_p": 0.9, 
        "frequency_penalty": 0.2, "max_tokens": 1024, "repetition_penalty": 1.1
    }

    logger.info(f"Together AI (prompt_without_context-fallback-no-context) - Prompt: {prompt}")
    try:
        # Use a dictionary to hold keyword arguments for the API call
        api_params = {
            "model": final_model,
            "messages": [{"role": "user", "content": prompt}],
            **final_params
        }
        response = data = await _chat_once(
            **api_params
        )
        response_text = strip_think(data['choices'][0]['message']['content']).strip()
    except Exception as e:
        logger.error(f"Together AI (prompt_without_context) - Request failed: {e}")
        raise
    
    logger.info(f"Together AI (prompt_without_context) - Response: {response_text}")
    final_answer = await translate_if_needed(query, response_text)
    return final_answer

@retry_on_server_error()
async def fast_reply(query: str, lang: str, available_modes: list, translated_mode_names: dict) -> str:
    detected_user_lang = detect_language(query)
    prompt_lang = 'en' if detected_user_lang == 'en' else lang

    system_prompt = f"""Your name is Brainy. You are a Telegram bot and a helpful AI assistant built with free, open-source tools. Your creator's Telegram nickname is @bonbekon. You will always be accessible for free. The core idea behind you is to combine a fast, open-source Large Language Models with real-time context from the internet (a technique called RAG) to provide answers comparable in quality to proprietary models like ChatGPT. Your advantages vs other free AI tools: fast responses to easy everyday questions, actual and unbiased information, free unlimited deep research.

{THINKING_GUIDANCE}

Your goal is to give a short and precise answer. If a more detailed answer is absolutely required, suggest using other modes. Always answer in the "{prompt_lang}" language.

If you cannot provide a short and precise answer, you MUST explicitly state that you cannot and advise the user to use a more suitable mode:
- **{translated_mode_names['web_search']}:** Use this for easy questions that need up-to-date information.
- **{translated_mode_names['deep_search']}:** For more complex questions that do not require deep analysis.
- **{translated_mode_names['deep_research']}:** For complex research or analysis.
"""
    user_prompt = f"{query}"

    payload = {
        "model": config.TOGETHER_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.3,
        "top_k": 50,
        "top_p": 0.9,
        "repetition_penalty": 1.1,
        "max_tokens": 2000,
        "reasoning_effort": "low",
    }
    logger.info(f"Together AI (fast-reply) - System Prompt: {system_prompt}")
    logger.info(f"Together AI (fast-reply) - User Prompt: {user_prompt}")
    try:
        response = data = await chat_with_fallback(**payload)
        response_text = strip_think(data['choices'][0]['message']['content']).strip()
    except Exception as e:
        logger.error(f"Together AI (fast-reply) - Request failed: {e}")
        raise
    
    logger.info(f"Together AI (fast-reply) - Response: {response_text}")
    return response_text

@retry_on_server_error()
async def generate_answer_from_serp(query: str, snippets: list, lang: str, translator, entities_info: list) -> str:
    detected_user_lang = detect_language(query)
    prompt_lang = 'en' if detected_user_lang == 'en' else lang
    logger.info(f"Received snippets for LLM: {snippets}")

    unique_snippets_for_ranking = _filter_duplicate_chunks(snippets)
    sorted_snippets = sorted(unique_snippets_for_ranking, key=lambda s: len(s.text), reverse=True)

    # Fallback sources extracted from snippets
    fallback_sources = []
    seen_urls = set()
    for s in sorted_snippets:
        if s.source_url not in seen_urls:
            fallback_sources.append(s.source_url)
            seen_urls.add(s.source_url)
        if len(fallback_sources) >= 3:
            break

    snippets_by_domain = {}
    for s in snippets:
        if len(s.text) < 70:
            continue
        domain = urlparse(s.source_url).netloc
        if domain not in snippets_by_domain:
            snippets_by_domain[domain] = []
        snippets_by_domain[domain].append(s.text)

    snippet_texts = []
    for domain, texts in snippets_by_domain.items():
        combined_text = " ".join(texts)
        snippet_texts.append(f"- {combined_text} [{domain}]")

    snippet_text = "\n\n".join(snippet_texts)

    entity_context = ""
    if entities_info:
        entity_context = "\n\nDiscovered Entities and their details:\n"
        for entity in entities_info:
            entity_context += f"- Entity: {entity['entity']}\n"
            if entity['description']:
                entity_context += f"  Description: {entity['description']}\n"
            if entity['lead_paragraph']:
                entity_context += f"  Lead Paragraph: {entity['lead_paragraph']}\n"
            entity_context += f"  QID: {entity['qid']}\n"

    # JSON-formatted prompt - removed THINKING_GUIDANCE and added JSON instructions
    prompt = f"""You are a skilled researcher. You are able to pick the most relevant data from a very broad context to answer the user's query in a short and precise way. Write a complete, coherent, and fact-rich answer to the user's query from context snippets and discovered entities. Keep only unique and valuable information (guidance, facts, numbers, addresses, characteristics) related to the user's query. The user's query: "{query}".\n{entity_context}\n\nRules: 1. Double check you don't repeat yourself and provide only unique and detailed information. 2. Answer in the "{prompt_lang}" language. 3. Stick closer to the language and style of provided context snippets. 4. Information discovered in "Discovered entities and their details" is the most reliable, and it is your final source of truth. 5. If the user query implies a short answer (facts, dates, quick advice etc), keep you answer very short. 6. If the user query implies a long answer (e.g. comparisons, lists, coding, analysis, research etc) provide a detailed answer.\n\nContext snippets: {snippet_text}\n\nIMPORTANT: You MUST respond with ONLY a valid JSON object (no markdown code blocks, no explanatory text). Use this exact format:\n{{\n  "thinking": "Your internal analysis of the query and context here",\n  "final": "Your complete answer to the user in {prompt_lang} language",\n  "sources": ["https://full-url-1.com", "https://full-url-2.com", "https://full-url-3.com"]\n}}\n\nInclude up to 5 most relevant source URLs (complete URLs, not domains) from the context snippets."""
    
    logger.info(f"Together AI (generate_answer_from_serp) - Prompt: {prompt}")
    try:
        response = data = await chat_with_fallback(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=4000
        )
        response_text = data['choices'][0]['message']['content'].strip()
    except Exception as e:
        logger.error(f"Together AI (generate_answer_from_serp) - Request failed: {e}")
        raise
    
    logger.info(f"Together AI (generate_answer_from_serp) - Raw Response: {response_text}")
    
    # Attempt to parse JSON response
    parsed_json = None
    final_answer_text = ""
    sources_from_json = []
    
    try:
        # Try direct JSON parsing
        parsed_json = json.loads(response_text)
        logger.info(f"Together AI (generate_answer_from_serp) - Successfully parsed JSON directly")
    except json.JSONDecodeError:
        logger.warning(f"Together AI (generate_answer_from_serp) - Direct JSON parse failed, attempting regex extraction")
        # Try regex extraction
        json_pattern = re.compile(r'\{[\s\S]*?"thinking"[\s\S]*?"final"[\s\S]*?"sources"[\s\S]*?\}', re.DOTALL)
        match = json_pattern.search(response_text)
        if match:
            try:
                parsed_json = json.loads(match.group(0))
                logger.info(f"Together AI (generate_answer_from_serp) - Successfully extracted JSON via regex")
            except json.JSONDecodeError:
                logger.error(f"Together AI (generate_answer_from_serp) - Regex extraction found JSON-like structure but parsing failed")
        else:
            logger.error(f"Together AI (generate_answer_from_serp) - No JSON structure found in response")
    
    # Process JSON if successfully parsed
    if parsed_json:
        # Log full JSON response
        logger.info(f"Together AI (generate_answer_from_serp) - Full JSON Response:\n{json.dumps(parsed_json, indent=2, ensure_ascii=False)}")
        
        # Extract fields with validation
        thinking = parsed_json.get('thinking', '')
        final_answer_text = parsed_json.get('final', '')
        sources_from_json = parsed_json.get('sources', [])
        
        # Log extracted field info
        logger.info(f"Together AI (generate_answer_from_serp) - Extracted fields: thinking_length={len(thinking)}, final_length={len(final_answer_text)}, sources_count={len(sources_from_json) if isinstance(sources_from_json, list) else 0}")
        
        # Validate required fields
        if not final_answer_text:
            logger.warning(f"Together AI (generate_answer_from_serp) - Missing or empty 'final' field, using thinking as fallback")
            final_answer_text = thinking if thinking else response_text
        
        if not thinking:
            logger.warning(f"Together AI (generate_answer_from_serp) - Missing 'thinking' field")
        
        # Validate sources field
        if not isinstance(sources_from_json, list):
            logger.warning(f"Together AI (generate_answer_from_serp) - 'sources' field is not an array, converting to empty array")
            sources_from_json = []
        
        # Validate and filter sources - must be valid URLs
        validated_sources = []
        for src in sources_from_json:
            if isinstance(src, str) and (src.startswith('http://') or src.startswith('https://')):
                if src not in validated_sources:  # Deduplicate
                    validated_sources.append(src)
        
        sources_from_json = validated_sources
        logger.info(f"Together AI (generate_answer_from_serp) - Validated {len(sources_from_json)} sources from JSON")
        
    else:
        # Fallback to legacy strip_think method
        logger.error(f"Together AI (generate_answer_from_serp) - Falling back to legacy strip_think method")
        final_answer_text = strip_think(response_text)
    
    # Determine final sources to display (max 3)
    top_sources = []
    if sources_from_json:
        # Use sources from JSON, limit to 3
        top_sources = sources_from_json[:3]
        logger.info(f"Together AI (generate_answer_from_serp) - Using {len(top_sources)} sources from JSON")
    else:
        # Fallback to snippet-based sources
        top_sources = fallback_sources
        logger.info(f"Together AI (generate_answer_from_serp) - Using {len(top_sources)} fallback sources from snippets")
    
    # Construct final answer with sources
    final_answer = final_answer_text
    
    if top_sources:
        final_answer += f"\n\n{translator.get_string('sources_label', lang)}:\n"
        for i, url in enumerate(top_sources):
            final_answer += f"{i+1}. {unquote(url)}\n"

    return final_answer

@retry_on_server_error()
async def generate_summary_from_chunks(query: str, snippets: list, lang: str, translator, entities_info: list) -> str:
    detected_user_lang = detect_language(query)
    prompt_lang = 'en' if detected_user_lang == 'en' else lang
    logger.info(f"Received snippets for LLM summary: {snippets}")

    unique_snippets_for_ranking = _filter_duplicate_chunks(snippets)

    snippets_by_domain = {}
    for s in unique_snippets_for_ranking:
        if len(s.text) < 70:
            continue
        source_identifier = s.source_url # Use the full URL
        if source_identifier not in snippets_by_domain:
            snippets_by_domain[source_identifier] = []
        snippets_by_domain[source_identifier].append(s.text)

    snippet_texts = []
    for source_id, texts in snippets_by_domain.items():
        combined_text = " ".join(texts)
        snippet_texts.append(f"- {combined_text} [Source: {source_id}]") # Use the full URL in the prompt

    snippet_text = "\n\n".join(snippet_texts)

    entity_context = ""
    if entities_info:
        entity_context = "\n\nDiscovered Entities and their details:\n"
        for entity in entities_info:
            entity_context += f"- Entity: {entity['entity']}\n"
            if entity['description']:
                entity_context += f"  Description: {entity['description']}\n"
            if entity['lead_paragraph']:
                entity_context += f"  Lead Paragraph: {entity['lead_paragraph']}\n"
            entity_context += f"  QID: {entity['qid']}\n"

    prompt = f"""You are a skilled researcher. You are able to pick the most relevant data from a very broad context to answer the user's query in a detailed and precise way. Write a complete, coherent, and fact-rich answer to the user's query from context snippets and discovered entities. Keep only unique and valuable information (guidance, facts, numbers, addresses, characteristics) related to the user's query.\n{entity_context}\n\nRules: 1. Double check you don't repeat yourself and provide only unique and detailed information. 2. Answer in the "{prompt_lang}" language. 3. Do not add any information not present in the snippets. 4. Stick closer to the language and style of provided context snippets. 5. Information discovered in "Discovered entities and their details" is the most reliable, and it is your final source of truth. 6. **Crucially, cite your sources in square brackets (strictly follow this format: "[[https://www.kommersant.ru/doc/7566968](https://www.kommersant.ru/doc/7566968)]") directly within the text where the information is used.** 7. {THINKING_GUIDANCE}\nContext snippets: {snippet_text}"""
    
    logger.info(f"Together AI (generate_summary_from_chunks) - Prompt: {prompt}")
    try:
        response = data = await chat_with_fallback(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": query}
            ],
            temperature=0.2,
            max_tokens=2400
        )
        response_text = strip_think(data['choices'][0]['message']['content']).strip()
    except Exception as e:
        logger.error(f"Together AI (generate_summary_from_chunks) - Request failed: {e}")
        raise
    
    logger.info(f"Together AI (generate_summary_from_chunks) - Response: {response_text}")
    return response_text

@retry_on_server_error()
async def polish_research_answer(summaries: str, query: str, lang: str, translator) -> str:
    """Takes a list of summaries and synthesizes the final answer, truncating if necessary."""
    # --- 1. Define constants and strip think tags ---
    summaries = strip_think(summaries)  # CRITICAL: Strip think tags first
    MODEL_CONTEXT_WINDOW = 10000
    MAX_OUTPUT_TOKENS = 5000
    CHAR_PER_TOKEN_ESTIMATE = 3  # Conservative estimate

    # --- 2. Calculate available space for summaries ---
    prompt_template = f"""You are a chief researcher. Answer the user's query based on the research data provided to you. 

**User Query:** {query}

**Research Data (Summaries):**
{summaries}

**Rules:**
1. Crucially, cite your sources in the following format "[https://example.com/page](https://example.com/page)" directly within the text where the information is used.
2. List facts from junior researchers, check them for any contradictions, and only then compose the detailed final answer.
3. Your final answer should be very detailed, complete, coherent, and well-structured
4. Minimal desired output is 500 words. The more the better. Max allowed output is 4000 words.
5. Stick closer to the language and style of provided context snippets.
6. Readability: One to three lines per paragraph. One idea per sentence. Don’t be afraid of sentence fragments. (e.g., “It’s more effective. And easier to read.”). Use punchy phrases that grab a skimming reader and hand them off to the next line to keep people engaged:
In other words…
Which means:
Why?
Why not?
Here’s why.
For example:
Like this:
However:
On the other hand…
Important:

EXAMPLE:

Not great:

“Adding recognizable customer logos, star ratings, and short testimonials to a landing page builds trust and reduces perceived risk, which lowers friction in the decision process and typically improves click-through rates on calls-to-action.”

Better:

"Social proof lowers friction on landing pages.

Here’s why:

Logos, ratings, and quick testimonials answer “Is this legit?” fast—so more visitors keep reading and more of them click the CTA."

7. Your final answer must be in the "{lang}" language.
8. {THINKING_GUIDANCE}"""
    
    base_prompt_len = len(prompt_template.format(summaries=''))
    max_summaries_len = (MODEL_CONTEXT_WINDOW - MAX_OUTPUT_TOKENS) * CHAR_PER_TOKEN_ESTIMATE - base_prompt_len

    # --- 3. Truncate summaries if they are too long ---
    if len(summaries) > max_summaries_len:
        logger.warning(f"Summaries length ({len(summaries)}) exceeds limit ({max_summaries_len}). Truncating.")
        summaries = summaries[:max_summaries_len]

    # --- 4. Final API Call ---
    final_prompt = prompt_template.format(summaries=summaries)

    logger.info(f"Together AI (polish-research) - Final prompt to be sent: {final_prompt}")
    logger.info(f"Together AI (polish-research) - Prompting to synthesize final answer. Final summaries length: {len(summaries)} chars.")
    try:
        data = await chat_with_fallback(
            model=config.TOGETHER_DEEPSEEK,
            messages=[{"role": "user", "content": final_prompt}],
            temperature=0.5,
            max_tokens=MAX_OUTPUT_TOKENS
        )
        # ВАЖНО: теперь ответ — dict; берём content и срезаем <think>
        polished_text = strip_think(data["choices"][0]["message"]["content"]).strip()
    except Exception as e:
        logger.error(f"Together AI (polish-research) - Request failed: {e}")
        return "Error: Could not generate the final research answer."
    
    logger.info(f"Together AI (polish-research) - Final answer received.")
    return polished_text

@retry_on_server_error()
async def summarize_research_chunk(chunk: str, query: str, lang: str) -> str:
    """Summarizes a single chunk of research data in the context of the user's query."""
    prompt = f"""You are a research assistant. Analyze this piece of the research draft and summarize in a detailed and well-structured way the key information that can help partly or fully answer the user's main query, which is: '{query}'.

Provide only the summary of the text below, with no extra comments or introductions. Stick closer to the language and style of provided context snippets. The summary must be in the "{lang}" language. Don't forget to cite sources (if any) in square brackets: their domains or full urls if available. {THINKING_GUIDANCE}

**Research Draft Chunk:**

{chunk}"""

    logger.info(f"Together AI (summarize-chunk) - Prompting to summarize chunk of length {len(chunk)}.")
    try:
        response = data = await chat_with_fallback(model=config.TOGETHER_DEEPSEEK, # Use the specified summarizer model
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2, # Factual summarization
            max_tokens=4000 # Allow for a decent summary length, but not too long
        )
        summary = strip_think(data['choices'][0]['message']['content']).strip()
    except Exception as e:
        logger.error(f"Together AI (summarize-chunk) - Request failed: {e}")
        return "" # Return an empty string if summarization fails
    
    logger.info(f"Together AI (summarize-chunk) - Summary received.")
    return summary
