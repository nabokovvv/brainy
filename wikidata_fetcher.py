import httpx
import json
import logging
import config

logger = logging.getLogger(__name__)

WIKIDATA_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
WIKIPEDIA_REST_API_BASE = "https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"

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
    except httpx.RequestError as e:
        logger.error(f"Error executing SPARQL query: {e}")
        return None
    except json.JSONDecodeError:
        logger.error(f"Failed to decode JSON from SPARQL query. Response: {response.text}")
        return None

async def get_wikidata_description(client: httpx.AsyncClient, qid: str, lang: str) -> str | None:
    """Fetches the description for a given Q-ID from Wikidata in a specific language."""
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
                logger.info(f"Fetched Wikidata description for {qid} ({lang}): {description[:50]}...")
                return description
    logger.warning(f"No Wikidata description found for {qid} in language {lang}")
    return None

async def get_wikipedia_lead_paragraph(client: httpx.AsyncClient, qid: str, lang: str) -> str | None:
    """Fetches the lead paragraph for a given Q-ID from Wikipedia, with English fallback."""
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
        logger.warning(f"No Wikipedia page title found for {qid} in language {lang}. Trying English fallback.")
        page_title = await fetch_title('en')
        api_lang = 'en'

    if not page_title:
        logger.warning(f"No Wikipedia page title found for {qid} even with English fallback.")
        return None

    url = WIKIPEDIA_REST_API_BASE.format(lang=api_lang, title=page_title)
    headers = {'User-Agent': config.CUSTOM_USER_AGENT}
    try:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        extract = data.get('extract')
        if extract:
            logger.info(f"Fetched Wikipedia lead paragraph for {qid} ({api_lang}): {extract[:50]}...")
            return extract
    except httpx.RequestError as e:
        logger.error(f"Error fetching Wikipedia summary for {qid} ({api_lang}): {e}")
    except json.JSONDecodeError:
        logger.error(f"Failed to decode JSON from Wikipedia for {qid} ({api_lang}). Response: {response.text}")

    logger.warning(f"No Wikipedia lead paragraph found for {qid} in language {api_lang}")
    return None
