import httpx
import json
import logging
import re
from urllib.parse import urlparse

import config
from config import OLLAMA_ENDPOINT, OLLAMA_MODEL, FACTUAL_PARAMS, DEEP_SEARCH_STEP_ONE_MODEL, CREATIVE_PARAMS, FACTUAL_PARAMS_2, DEEP_SEARCH_STEP_ONE_MODEL, DEEP_SEARCH_STEP_FINAL_MODEL, OLLAMA_TIMEOUT
from utils import detect_language, _filter_duplicate_chunks

from together import Together

logger = logging.getLogger(__name__)

async def get_sub_queries(query: str, lang: str) -> list[str]:
    detected_user_lang = detect_language(query)
    if detected_user_lang == 'en':
        prompt_lang = 'en'
    else:
        prompt_lang = lang

    prompt = f"""Based on the following query, generate up to 10 sub-queries for a web search to gather the necessary information to provide a comprehensive answer. Try both shorter and longer search queries. The majority of them should be in "{prompt_lang}" language, and a couple - in English. Return the sub-queries as a clean JSON list of strings without comments.

Query from user: {query}"""
    payload = {"model": DEEP_SEARCH_STEP_ONE_MODEL, "prompt": prompt, **CREATIVE_PARAMS}
    logger.info(f"Ollama (sub-queries) - Prompt: {prompt}")
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            resp = await client.post(OLLAMA_ENDPOINT, json=payload)
            resp.raise_for_status()
    except httpx.RequestError as e:
        logger.error(f"Ollama (sub-queries) - Request failed: {e}")
        raise
    response_text = resp.json()["choices"][0]["text"].strip()
    logger.info(f"Ollama (sub-queries) - Raw Response: {response_text}")
    
    sub_queries = []
    json_string = ""
    try:
        # Extract content between the first '[' and the last ']'
        json_match = re.search(r'\[[\s\S]*\]', response_text)
        if json_match:
            json_string = json_match.group(0)
            
            # Remove line comments (//...)
            json_string = re.sub(r"//.*", "", json_string)
            
            # Remove trailing commas before closing brackets or braces
            json_string = re.sub(r",\s*([\}\]])", r"\1", json_string)

            sub_queries = json.loads(json_string)
        else:
             logger.warning("Ollama (sub-queries) - No JSON list found in the response.")

    except json.JSONDecodeError as e:
        logger.warning(f"Ollama (sub-queries) - Could not decode JSON: {e}. Raw string was: {json_string}")
        # Fallback to original regex if JSON parsing fails
        sub_queries = re.findall(r'\d+\.\s*"(.*?)"|\d+\.\s*(.*)', response_text)
        sub_queries = [item for sublist in sub_queries for item in sublist if item]

    logger.info(f"Ollama (sub-queries) - Parsed Sub-queries: {sub_queries}")
    return sub_queries


async def get_research_steps(query: str, lang: str, entities_info: list) -> list[str]:
    detected_user_lang = detect_language(query)
    if detected_user_lang == 'en':
        prompt_lang = 'en'
    else:
        prompt_lang = lang

    entity_context = ""
    if entities_info:
        entity_context = "\n\nDiscovered Entities:\n"
        for entity in entities_info:
            entity_context += f"- {entity['entity']}\n"

    prompt = f"""You are a researcher. Break down the user's question into several logical research steps. Use the provided entity details to create more accurate and specific steps. In each step, instead of pronouns, be sure to indicate the full name of the object(s) of research. Also, in each step, keep the general context of the research (user's request). Do not refer to other steps or to the future results of other steps. If it is absolutely necessary to refer to other steps, then repeat the context of what was in the previous steps.

Your response must be in the "{prompt_lang}" language.

Return the steps as a clean JSON list of strings, with a maximum of 10 items in {prompt_lang} language. For example:
[
  "Check A",
  "Review B",
  "Compare A and B"
]

Query from user: {query}
"""
    if entity_context:
        prompt += f"{entity_context} EACH QUESTION SHOULD CONTAIN AT LEAST ONE ENTITY NAME"
    payload = {"model": "qwen2.5:14b-instruct-q4_K_M", "prompt": prompt, **CREATIVE_PARAMS}
    logger.info(f"Ollama (research-steps) - Prompt: {prompt}")
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            resp = await client.post(OLLAMA_ENDPOINT, json=payload)
            resp.raise_for_status()
    except httpx.RequestError as e:
        logger.error(f"Ollama (research-steps) - Request failed: {e}")
        raise
    response_text = resp.json()["choices"][0]["text"].strip()
    logger.info(f"Ollama (research-steps) - Raw Response: {response_text}")
    
    steps = []
    json_string = ""
    try:
        # Extract content between the first '[' and the last ']'
        json_match = re.search(r'\[[\s\S]*\]', response_text)
        if json_match:
            json_string = json_match.group(0)
            
            # Remove line comments (//...)
            json_string = re.sub(r"//.*", "", json_string)
            
            # Remove trailing commas before closing brackets or braces
            json_string = re.sub(r",\s*([\}\]])", r"\1", json_string)

            steps = json.loads(json_string)
        else:
             logger.warning("Ollama (research-steps) - No JSON list found in the response.")

    except json.JSONDecodeError as e:
        logger.warning(f"Ollama (research-steps) - Could not decode JSON: {e}. Raw string was: {json_string}")
        # Fallback to original regex if JSON parsing fails
        steps = re.findall(r'\d+\.\s*"(.*?)"|\d+\.\s*(.*)', response_text)
        steps = [item for sublist in sub_queries for item in sublist if item]

    logger.info(f"Ollama (research-steps) - Parsed Steps: {steps}")
    return steps

async def synthesize_research_answer(query: str, research_data: dict, lang: str) -> str:
    detected_user_lang = detect_language(query)
    if detected_user_lang == 'en':
        prompt_lang = 'en'
    else:
        prompt_lang = lang

    # Format research_data for the LLM prompt
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
"""
    payload = {"model": "qwen2.5:3b-instruct", "prompt": prompt, **FACTUAL_PARAMS_2}
    logger.info(f"Ollama (research-synthesis) - Prompt: {prompt}")
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            resp = await client.post(OLLAMA_ENDPOINT, json=payload)
            resp.raise_for_status()
    except httpx.RequestError as e:
        logger.error(f"Ollama (research-synthesis) - Request failed: {e}")
        raise
    response_text = resp.json()["choices"][0]["text"].strip()
    logger.info(f"Ollama (research-synthesis) - Response: {response_text}")
    return response_text

async def synthesize_answer(query: str, snippets: list, lang: str) -> str:
    # Filter out duplicate snippets
    unique_snippets = _filter_duplicate_chunks(snippets)
    # Assuming snippets is a list of TextChunk objects
    snippet_text = "\n".join([f"- {s.text}" for s in unique_snippets])
    logger.info(f"Ollama (synthesis) - Snippets: {snippet_text}")

    detected_user_lang = detect_language(query)
    if detected_user_lang == 'en':
        prompt_lang = 'en'
    else:
        prompt_lang = lang

    prompt = f"""You are an expert. Your task is to synthesize a comprehensive, well-structered, and detailed answer based *only* on the provided web search snippets.

**Instructions:**
1. **Your response MUST be in the "{prompt_lang}" language despite the language of snippets.**
2. Stick strictly to the information given in the 'Snippets'. Do not add any information that is not present in them.
3. In your output make sure you split your text by paragraphs, 40 - 80 words each.

**Question:** {query}

**Snippets:**
{snippet_text}"""
    payload = {"model": DEEP_SEARCH_STEP_FINAL_MODEL, "prompt": prompt, **FACTUAL_PARAMS_2}
    logger.info(f"Ollama (synthesis) - Prompt: {prompt}")
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            resp = await client.post(OLLAMA_ENDPOINT, json=payload)
            resp.raise_for_status()
    except httpx.RequestError as e:
        logger.error(f"Ollama (synthesis) - Request failed: {e}")
        raise
    response_text = resp.json()["choices"][0]["text"].strip()
    logger.info(f"Ollama (synthesis) - Response: {response_text}")

    return response_text

def contains_chinese(text: str) -> bool:
    """Checks if the string contains Chinese characters."""
    return bool(re.search(r'[\u4e00-\u9fff]', text))

async def translate_if_needed(query: str, original_answer: str) -> str:
    """Translates the answer if it contains Chinese characters."""
    if not contains_chinese(original_answer):
        return original_answer

    logger.warning(f"Ollama (prompt_without_context) - Chinese detected in response: {original_answer}")

    # Step 1: Detect the language of the original query
    detected_language = detect_language(query)
    logger.info(f"Detected query language: {detected_language}")

    # Step 2: Translate the answer to the detected language
    translation_prompt = f'''Answer the user\'s question in the {detected_language} language. User\'s question: "{query}".'''
    payload = {"model": "llama3:8b-instruct-q4_K_M", "prompt": translation_prompt, **FACTUAL_PARAMS}
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            resp = await client.post(OLLAMA_ENDPOINT, json=payload)
            resp.raise_for_status()
        translated_answer = resp.json()["choices"][0]["text"].strip()
        logger.info(f"Ollama (prompt_without_context) - Translated answer: {translated_answer}")
        return translated_answer
    except httpx.RequestError as e:
        logger.error(f"Ollama (prompt_without_context) - Translation failed: {e}")
        raise

async def prompt_without_context(query: str, lang: str, model: str = None, params: dict = None) -> str:
    detected_user_lang = detect_language(query)
    if detected_user_lang == 'en':
        prompt_lang = 'en'
    else:
        prompt_lang = lang

    prompt = f"""You are a helpful AI assistant. Always answer in the "{prompt_lang}" language!
    
Question from the user: {query}
"""
    
    # Use default model and params if not provided
    final_model = model if model is not None else OLLAMA_MODEL
    final_params = params if params is not None else FACTUAL_PARAMS

    payload = {"model": final_model, "prompt": prompt, **final_params}
    logger.info(f"Ollama (prompt_without_context-fallback-no-context) - Prompt: {prompt}")
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            resp = await client.post(OLLAMA_ENDPOINT, json=payload)
            resp.raise_for_status()
    except httpx.RequestError as e:
        logger.error(f"Ollama (prompt_without_context) - Request failed: {e}")
        raise
    response_text = resp.json()["choices"][0]["text"].strip()
    logger.info(f"Ollama (prompt_without_context) - Response: {response_text}")

    # Translate if necessary
    final_answer = await translate_if_needed(query, response_text)

    return final_answer

async def fast_reply(query: str, lang: str, available_modes: list, translated_mode_names: dict) -> str:
    detected_user_lang = detect_language(query)
    if detected_user_lang == 'en':
        prompt_lang = 'en'
    else:
        prompt_lang = lang

    system_prompt = f"""Your name is Brainy. Your website is https://askbrainy.com. You are a helpful AI assistant built on free, open-source tools. Your creator's Telegram nickname is @bonbekon, and you will always be accessible for free. The core idea behind you is to combine a fast, open-source Large Language Model (QWEN 2.5 7B Instruct) with real-time context from the internet (a technique called RAG) to provide answers comparable in quality to proprietary models like ChatGPT 3.5 and sometimes even ChatGPT 4o. Your advantages vs other free AI tools: fast responses take less than 5 seconds on average, actual and unbiased information in other modes, you have a free unlimited deep research feauture.

Your goal is to give the shortest and most precise answer possible in the current 'Fast Answer' mode. Always answer in the "{prompt_lang}" language.

If you cannot provide a short and precise answer, you MUST explicitly state that you cannot and advise the user to use other available modes. Here is a description of the modes to help you guide the user:
- **{translated_mode_names['web_search']}:** Use this for questions that need up-to-date information. It provides actual, not outdated, information in about 20 seconds.
- **{translated_mode_names['deep_search']}:** This mode provides actual and unbiased information by searching more thoroughly.
- **{translated_mode_names['deep_research']}:** For complex questions, this mode reads hundreds of websites to produce the most relevant and comprehensive answer.

Your available modes are: {', '.join(available_modes)}."""
    user_prompt = f"{query}"

    payload = {
        "model": config.FAST_REPLY_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.3,
        "top_k": 50,
        "top_p": 0.9,
        "repetition_penalty": 1.1,
        "max_tokens": 400
    }
    logger.info(f"Ollama (fast-reply) - System Prompt: {system_prompt}")
    logger.info(f"Ollama (fast-reply) - User Prompt: {user_prompt}")
    try:
        async with httpx.AsyncClient(timeout=config.OLLAMA_TIMEOUT) as client:
            resp = await client.post(config.OLLAMA_CHAT_ENDPOINT, json=payload)
            resp.raise_for_status()
    except httpx.RequestError as e:
        logger.error(f"Ollama (fast-reply) - Request failed: {e}")
        raise
    response_json = resp.json()
    if "message" in response_json:
        response_text = response_json["message"]["content"].strip()
    elif "choices" in response_json and len(response_json["choices"]) > 0:
        # Corrected fallback for OpenAI-compatible chat completions
        if "message" in response_json["choices"][0]:
            response_text = response_json["choices"][0]["message"]["content"].strip()
        elif "text" in response_json["choices"][0]: # Keep this for older OpenAI completions
            response_text = response_json["choices"][0]["text"].strip()
        else:
            raise ValueError(f"Unexpected response format within choices from Ollama: {response_json['choices'][0]}")
    else:
        raise ValueError(f"Unexpected top-level response format from Ollama: {response_json}")
    logger.info(f"Ollama (fast-reply) - Response: {response_text}")

    return response_text

async def generate_answer_from_serp(query: str, snippets: list, lang: str, translator, entities_info: list) -> str:
    detected_user_lang = detect_language(query)
    if detected_user_lang == 'en':
        prompt_lang = 'en'
    else:
        prompt_lang = lang
    # Log received snippets
    logger.info(f"Received snippets for LLM: {snippets}")

    # Filter out duplicate snippets and store them for source selection
    unique_snippets_for_ranking = _filter_duplicate_chunks(snippets)

    # Sort snippets by text length to find the "top" sources
    sorted_snippets = sorted(unique_snippets_for_ranking, key=lambda s: len(s.text), reverse=True)

    # Get top 3 unique source URLs
    top_sources = []
    seen_urls = set()
    for s in sorted_snippets:
        if s.source_url not in seen_urls:
            top_sources.append(s.source_url)
            seen_urls.add(s.source_url)
        if len(top_sources) >= 3:
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

    # Prepare entity information for the prompt
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

    prompt = f"""You are a skilled researcher. You are able to pick the most relevant data from a very broad context to answer the user's query in a short and precise way. Write a complete, coherent, and fact-rich answer to the user's query from context snippets and discovered entities. Keep only unique and valuable information (guidance, facts, numbers, addresses, characteristics) related to the user's query. The user's query: "{query}".\n{entity_context}\n\nRules: 1. Max output should be around 10-200 words. 2. Double check you don't repeat yourself and provide only unique and detailed information. 3. Answer in the "{prompt_lang}" language. 4. Stick closer to the language and style of provided context snippets. 5. Information discovered in "Discovered entities and their details" is the most reliable, and it is your final source of truth. 6. If the user query implies a short answer (facts, dates, quick advice etc), keep you answer very short. 7. If the user query implies a long answer (e.g. comparisons, lists, coding, analysis, research etc) provide a detailed answer.\nContext snippets: {snippet_text}"""
    payload = {"model": "qwen2.5:3b-instruct",
               "prompt": prompt,
               "temperature": 0.2,
               "max_tokens": 550,
}

    logger.info(f"Ollama (generate_answer_from_serp) - Prompt: {prompt}")
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            resp = await client.post(OLLAMA_ENDPOINT, json=payload)
            resp.raise_for_status()
    except httpx.RequestError as e:
        logger.error(f"Ollama (generate_answer_from_serp) - Request failed: {e}")
        raise
    response_text = resp.json()["choices"][0]["text"].strip()
    logger.info(f"Ollama (generate_answer_from_serp) - Response: {response_text}")

    final_answer = response_text

    if top_sources:
        final_answer += f"\n\n{translator.get_string("sources_label", lang)}:\n"
        for i, url in enumerate(top_sources):
            final_answer += f"{i+1}. {url}\n"

    return final_answer

async def generate_summary_from_chunks(query: str, snippets: list, lang: str, translator, entities_info: list) -> str:
    detected_user_lang = detect_language(query)
    if detected_user_lang == 'en':
        prompt_lang = 'en'
    else:
        prompt_lang = lang
    logger.info(f"Received snippets for LLM summary: {snippets}")

    unique_snippets_for_ranking = _filter_duplicate_chunks(snippets)

    sorted_snippets = sorted(unique_snippets_for_ranking, key=lambda s: len(s.text), reverse=True)

    top_sources = []
    seen_urls = set()
    for s in sorted_snippets:
        if s.source_url not in seen_urls:
            top_sources.append(s.source_url)
            seen_urls.add(s.source_url)
        if len(top_sources) >= 3:
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

    prompt = f"""You are a skilled researcher. You are able to pick the most relevant data from a very broad context to answer the user's query in a detailed and precise way. Write a complete, coherent, and fact-rich answer to the user's query from context snippets and discovered entities. Keep only unique and valuable information (guidance, facts, numbers, addresses, characteristics) related to the user's query. The user's query: "{query}".\n{entity_context}\n\nRules: 1. Max output should be around 100-300 words. 2. Double check you don't repeat yourself and provide only unique and detailed information. 3. Answer in the "{prompt_lang}" language. 4. Do not add any information not present in the snippets. 4. Stick closer to the language and style of provided context snippets. 5. Information discovered in "Discovered entities and their details" is the most reliable, and it is your final source of truth.\nContext snippets: {snippet_text}"""
    payload = {"model": "qwen2.5:3b-instruct", # Can be tweaked later
               "prompt": prompt,
               "temperature": 0.2,
               "max_tokens": 800, # Adjusted for summary length
}

    logger.info(f"Ollama (generate_summary_from_chunks) - Prompt: {prompt}")
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            resp = await client.post(OLLAMA_ENDPOINT, json=payload)
            resp.raise_for_status()
    except httpx.RequestError as e:
        logger.error(f"Ollama (generate_summary_from_chunks) - Request failed: {e}")
        raise
    response_text = resp.json()["choices"][0]["text"].strip()
    logger.info(f"Ollama (generate_summary_from_chunks) - Response: {response_text}")

    #if top_sources:
        #final_answer += f"\n\n{translator.get_string("sources_label", lang)}:\n"
        #for i, url in enumerate(top_sources):
            #final_answer += f"{i+1}. {url}\n"

    return response_text

async def deepseek_r1_reply(query: str, lang: str) -> str:
    client = Together(api_key=config.TOGETHER_AI_API_KEY)
    try:
        system_prompt = f"You are a helpful AI assistant. Always respond in the {lang} language."
        response = client.chat.completions.create(
            model=config.DEEPSEEK_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": query
                }
            ]
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Together AI (DeepSeek R1) - Request failed: {e}")
        raise