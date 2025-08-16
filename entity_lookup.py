import logging
import asyncio
import httpx
from typing import List, Dict, Any

from entity_detector import detect_entities, load_nlp_model
from wikidata_mapper import get_qid_from_entity, _get_p31_for_qid
from wikidata_fetcher import get_wikidata_description, get_wikipedia_lead_paragraph

logger = logging.getLogger(__name__)

# Define a semaphore to limit concurrent requests to Wikidata
# This helps prevent "429 Too Many Requests" errors.
WIKIDATA_SEMAPHORE = asyncio.Semaphore(3)

async def _process_single_entity(client, search_term: str, lang: str) -> Dict[str, Any]:
    """Processes a single entity, trying user's lang first, then falling back to English."""
    async with WIKIDATA_SEMAPHORE:
        # Try to find the entity in the user's language first
        qid = await get_qid_from_entity(client, search_term, lang)

        # If not found and the user's language is not English, try English as a fallback
        if not qid and lang != 'en':
            qid = await get_qid_from_entity(client, search_term, 'en')

        if qid:
            description = await get_wikidata_description(client, qid, lang)
            lead_paragraph = await get_wikipedia_lead_paragraph(client, qid, lang)
            return {
                "entity": search_term,
                "description": description,
                "qid": qid,
                "wikipedia_url": None, # This function does not return a URL
                "lead_paragraph": lead_paragraph,
            }
    return None

async def get_entity_info(query: str, lang: str) -> List[Dict]:
    """
    Asynchronously detects entities using spaCy's built-in NER and fetches their details.
    This is the most robust method for handling multi-word entities.
    """
    nlp = load_nlp_model(lang)
    doc = nlp(query)

    if not doc.ents:
        logger.info(f"No entities found by spaCy NER in query: '{query}'")
        return []

    # Use the entities directly identified by spaCy
    detected_entities = list(doc.ents)
    logger.info(f"Successfully detected entities via spaCy NER: {[ent.text for ent in detected_entities]}")

    async with httpx.AsyncClient() as client:
        # Create a list of unique entity texts to avoid duplicate lookups
        unique_entity_texts = sorted(list(set([ent.text for ent in detected_entities])))
        tasks = [_process_single_entity(client, text, lang) for text in unique_entity_texts]
        results = await asyncio.gather(*tasks)
    
    # Filter out None results for entities that were not found in Wikidata
    return [info for info in results if info is not None]
