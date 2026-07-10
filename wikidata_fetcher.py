from __future__ import annotations

import httpx
import json
import logging
import re
from urllib.parse import quote

import config

logger = logging.getLogger(__name__)

WIKIDATA_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
WIKIPEDIA_REST_API_BASE = "https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"
QID_PATTERN = re.compile(r"^Q[1-9]\d*$")
LANGUAGE_PATTERN = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})?$")

async def _execute_sparql_query(client: httpx.AsyncClient, query: str) -> dict | None:
    """Helper function to execute SPARQL queries against Wikidata asynchronously."""
    headers = {
        'User-Agent': config.CUSTOM_USER_AGENT,
        'Accept': 'application/sparql-results+json'
    }
    try:
        response = await client.get(WIKIDATA_SPARQL_ENDPOINT, headers=headers, params={'query': query})
        response.raise_for_status()
        return response.json()
    except httpx.RequestError as exc:
        logger.warning("Wikidata request failed type=%s", type(exc).__name__)
        return None
    except json.JSONDecodeError:
        logger.warning("Wikidata returned invalid JSON")
        return None

async def get_wikidata_description(client: httpx.AsyncClient, qid: str, lang: str) -> str | None:
    """Fetches the description for a given Q-ID from Wikidata in a specific language."""
    if not QID_PATTERN.fullmatch(qid) or not LANGUAGE_PATTERN.fullmatch(lang):
        return None
    query = f'''SELECT ?desc WHERE {{
  wd:{qid} schema:description ?desc .
  FILTER(LANG(?desc) = "{lang}")
}} LIMIT 1'''

    data = await _execute_sparql_query(client, query)
    if data:
        bindings = data.get('results', {}).get('bindings', [])
        if bindings:
            description = bindings[0].get('desc', {}).get('value')
            if description:
                logger.info("Wikidata description found qid=%s language=%s", qid, lang)
                return description
    logger.debug("No Wikidata description qid=%s language=%s", qid, lang)
    return None

async def get_wikipedia_lead_paragraph(client: httpx.AsyncClient, qid: str, lang: str) -> str | None:
    """Fetches the lead paragraph for a given Q-ID from Wikipedia, with English fallback."""
    if not QID_PATTERN.fullmatch(qid) or not LANGUAGE_PATTERN.fullmatch(lang):
        return None

    async def fetch_title(target_lang: str):
        title_query = f'''SELECT ?articleTitle WHERE {{
          ?article schema:about wd:{qid} ;
                   schema:isPartOf <https://{target_lang}.wikipedia.org/> ;
                   schema:name ?articleTitle .
        }} LIMIT 1'''
        title_data = await _execute_sparql_query(client, title_query)
        if title_data:
            bindings = title_data.get('results', {}).get('bindings', [])
            if bindings:
                return bindings[0].get('articleTitle', {}).get('value')
        return None

    page_title = await fetch_title(lang)
    api_lang = lang

    if not page_title:
        logger.debug("Wikipedia title missing qid=%s language=%s", qid, lang)
        page_title = await fetch_title('en')
        api_lang = 'en'

    if not page_title:
        logger.debug("Wikipedia title missing after fallback qid=%s", qid)
        return None

    url = WIKIPEDIA_REST_API_BASE.format(lang=api_lang, title=quote(page_title, safe=""))
    headers = {'User-Agent': config.CUSTOM_USER_AGENT}
    try:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        extract = data.get('extract')
        if extract:
            logger.info("Wikipedia summary found qid=%s language=%s", qid, api_lang)
            return extract
    except httpx.RequestError as exc:
        logger.warning(
            "Wikipedia request failed qid=%s language=%s type=%s",
            qid,
            api_lang,
            type(exc).__name__,
        )
    except json.JSONDecodeError:
        logger.warning("Wikipedia returned invalid JSON qid=%s language=%s", qid, api_lang)

    logger.debug("Wikipedia summary missing qid=%s language=%s", qid, api_lang)
    return None
