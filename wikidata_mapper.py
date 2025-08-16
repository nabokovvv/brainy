import httpx
import json
import logging
import config

logger = logging.getLogger(__name__)

WIKIDATA_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

# Mapping spaCy entity labels to Wikidata 'instance of' (P31) Q-IDs

SPACY_LABEL_TO_WIKIDATA_P31 = {
    # Core “named entity” types
    "PERSON": ["Q5"],                                 # human
    "ORG": ["Q43229"],                                # organization
    "GPE": [
        "Q6256",     # country
        "Q515",      # city
        "Q10864048", # constituent state of a country
        "Q15284",    # municipality
        "Q28575",    # county
        "Q3624078"   # sovereign state
    ],
    "LOC": [
        "Q2221906",  # geographic location
        "Q618123"    # geographical object/feature
    ],
    "FAC": [
        "Q811979",   # architectural structure
        "Q13226383"  # facility
    ],
    "PRODUCT": [
        "Q2424752",  # product
        "Q24229398"  # manufactured good / product (keep both if you already rely on it)
    ],
    "EVENT": ["Q1190554"],                             # event
    "WORK_OF_ART": ["Q838948"],                        # work of art
    "LAW": ["Q828101"],                                # law

    # NORP = nationalities, religious & political groups.
    # A single class won’t cover it; go broad and rely on P279*.
    "NORP": [
        "Q16334295",  # human group
        "Q41710",     # ethnic group
        "Q7278",      # political party
        "Q9174"       # religion
    ],

    # Catch‑all
    "MISC": ["Q35120"]                                 # entity (very broad)
}

async def _get_p31_for_qid(client: httpx.AsyncClient, qid: str) -> list[str]:
    """Fetches P31 (instance of) values for a given QID."""
    query = f"""
    SELECT ?type WHERE {{
      wd:{qid} wdt:P31/wdt:P279* ?type .
    }}
    """
    headers = {
        'User-Agent': config.CUSTOM_USER_AGENT,
        'Accept': 'application/sparql-results+json'
    }
    try:
        response = await client.get(WIKIDATA_SPARQL_ENDPOINT, headers=headers, params={'query': query})
        response.raise_for_status()
        data = response.json()
        bindings = data.get('results', {}).get('bindings', [])
        return [b['type']['value'].split('/')[-1] for b in bindings]
    except httpx.RequestError as e:
        logger.error(f"Error fetching P31 for QID {qid}: {e}")
        return []
    except json.JSONDecodeError:
        logger.error(f"Failed to decode JSON from Wikidata for P31 of QID {qid}. Response: {response.text}")
        return []

async def get_qid_from_entity(client: httpx.AsyncClient, search_term: str, lang: str, spacy_label: str | None = None) -> str | None:
    """Searches Wikidata for the Q-ID of a given entity text asynchronously, with improved disambiguation."""
    query_parts = [
        f'''SELECT ?item WHERE {{
  SERVICE wikibase:mwapi {{
    bd:serviceParam wikibase:api "EntitySearch" .
    bd:serviceParam wikibase:endpoint "www.wikidata.org" .
    bd:serviceParam mwapi:search "{search_term}" .
    bd:serviceParam mwapi:language "{lang}" .
    ?item wikibase:apiOutputItem mwapi:item .
  }}
}} LIMIT 5''' # Fetch more results
    ]

    query = "\n".join(query_parts)

    headers = {
        'User-Agent': config.CUSTOM_USER_AGENT,
        'Accept': 'application/sparql-results+json'
    }

    logger.info(f"Querying Wikidata for entity '{search_term}' (lang: {lang}, spacy_label: {spacy_label})")
    try:
        response = await client.get(WIKIDATA_SPARQL_ENDPOINT, headers=headers, params={'query': query})
        response.raise_for_status()
        data = response.json()

        bindings = data.get('results', {}).get('bindings', [])
        
        if not bindings:
            logger.warning(f"Could not find Q-ID for entity: {search_term} in language: {lang} with spacy_label: {spacy_label}")
            return None

        candidate_qids = []
        for binding in bindings:
            item_uri = binding.get('item', {}).get('value')
            if item_uri:
                candidate_qids.append(item_uri.split('/')[-1])

        # If a spacy_label is provided, try to find a QID that matches its P31
        if spacy_label and spacy_label in SPACY_LABEL_TO_WIKIDATA_P31:
            expected_p31_qids = SPACY_LABEL_TO_WIKIDATA_P31[spacy_label]
            
            p31_matching_qids = []
            for qid in candidate_qids:
                p31_values = await _get_p31_for_qid(client, qid)
                # Check if any of the candidate's P31 values are in the expected list
                if any(p in expected_p31_qids for p in p31_values):
                    p31_matching_qids.append(qid)
            
            if p31_matching_qids:
                # Return the first QID that matches the P31 filter
                logger.info(f"Mapped entity '{search_term}' to Q-ID: {p31_matching_qids[0]} (P31 match) with spacy_label {spacy_label}")
                return p31_matching_qids[0]
            else:
                logger.warning(f"No Q-ID found matching P31 filter for entity: {search_term} in language: {lang} with spacy_label: {spacy_label}. Falling back to first result.")
                # If no perfect P31 match, fall back to the first result from the initial search
                first_qid = candidate_qids[0]
                logger.info(f"Mapped entity '{search_term}' to Q-ID: {first_qid} (first result) with spacy_label {spacy_label}")
                return first_qid
        else:
            # If no spacy_label or no P31 mapping, return the first result
            first_qid = candidate_qids[0]
            logger.info(f"Mapped entity '{search_term}' to Q-ID: {first_qid} (no P31 filter) with spacy_label {spacy_label}")
            return first_qid

    except httpx.RequestError as e:
        logger.error(f"Error querying Wikidata for entity '{search_term}': {e}")
    except json.JSONDecodeError:
        logger.error(f"Failed to decode JSON from Wikidata for entity '{search_term}'. Response: {response.text}")

    logger.warning(f"Could not find Q-ID for entity: {search_term} in language: {lang} with spacy_label: {spacy_label}")
    return None