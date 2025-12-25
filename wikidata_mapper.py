import httpx
import json
import logging
import asyncio
import config

logger = logging.getLogger(__name__)

WIKIDATA_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

# Semaphore to rate limit P31 queries to prevent 429 errors
P31_SEMAPHORE = asyncio.Semaphore(5)

# Mapping spaCy entity labels to Wikidata 'instance of' (P31) Q-IDs with priority tiers
# Priority: 'high' > 'medium' > 'low'

SPACY_LABEL_TO_WIKIDATA_P31 = {
    "PERSON": {
        "high": ["Q5"],  # human
        "medium": ["Q15632617", "Q95074"],  # fictional human, mythological character
        "low": ["Q4271324"]  # mythical character
    },
    "ORG": {
        "high": ["Q6881511", "Q4830453", "Q783794", "Q2085381", "Q4438121"],  # enterprise, business, company, publisher, sports org
        "medium": ["Q43229", "Q7210356", "Q15265344"],  # organization, political org, broadcasting org
        "low": ["Q16917", "Q685"]  # hospital, library
    },
    "GPE": {
        "high": ["Q6256", "Q3624078", "Q515", "Q10864048", "Q15284"],  # country, sovereign state, city, constituent state, municipality
        "medium": ["Q28575", "Q82794"],  # county, geographic region
        "low": ["Q2221906"]  # geographic location (generic)
    },
    "LOC": {
        "high": ["Q515", "Q486972", "Q6256", "Q82794", "Q10864048"],  # city, human settlement, country, geographic region, constituent state
        "medium": ["Q23442", "Q4022", "Q8502", "Q13218391", "Q5107"],  # island, river, mountain, historical country, continent
        "low": ["Q2221906", "Q618123"]  # geographic location, geographical object (generic)
    },
    "FAC": {
        "high": ["Q811979", "Q13226383"],  # architectural structure, facility
        "medium": [],
        "low": []
    },
    "PRODUCT": {
        "high": ["Q40056", "Q7397", "Q571", "Q11424", "Q134556"],  # software, video game, book, film, single
        "medium": ["Q2424752", "Q47461344"],  # product, written work
        "low": ["Q24229398"]  # manufactured good
    },
    "EVENT": {
        "high": ["Q198", "Q18608583", "Q350604"],  # war, recurring event, armed conflict
        "medium": ["Q1190554", "Q46847"],  # event, disaster
        "low": ["Q1656682"]  # occurrence
    },
    "WORK_OF_ART": {
        "high": ["Q3305213", "Q860861", "Q207628", "Q11424"],  # painting, sculpture, musical composition, film
        "medium": ["Q838948"],  # work of art
        "low": ["Q17537576"]  # creative work
    },
    "LAW": {
        "high": ["Q828101"],  # law
        "medium": [],
        "low": []
    },
    "NORP": {
        "high": ["Q41710", "Q7278"],  # ethnic group, political party
        "medium": ["Q16334295", "Q9174"],  # human group, religion
        "low": []
    },
    "MISC": {
        "high": ["Q12136", "Q16521", "Q11173", "Q7187", "Q811430", "Q483247"],  # disease, taxon, chemical compound, gene, construction, phenomenon
        "medium": ["Q151885", "Q1047113"],  # concept, specialty
        "low": ["Q35120", "Q58778"]  # entity (very broad), system
    }
}

# Scientific Q-IDs that get boosted scoring
SCIENTIFIC_QIDS = {"Q12136", "Q16521", "Q11173", "Q7187", "Q483247"}

async def _get_p31_for_qid(client: httpx.AsyncClient, qid: str) -> list[str]:
    """Fetches P31 (instance of) values for a given QID with controlled P279 depth."""
    # Use limited P279 depth to prevent over-matching through distant ontological relationships
    max_depth = config.P279_MAX_DEPTH
    
    if max_depth == 0:
        # Direct P31 only, no subclass traversal
        query = f"""
        SELECT ?type WHERE {{
          wd:{qid} wdt:P31 ?type .
        }}
        """
    else:
        # Limited P279 traversal depth
        query = f"""
        SELECT ?type WHERE {{
          wd:{qid} wdt:P31/wdt:P279{{0,{max_depth}}} ?type .
        }}
        """
    
    headers = {
        'User-Agent': config.CUSTOM_USER_AGENT,
        'Accept': 'application/sparql-results+json'
    }
    
    # Use semaphore to rate limit P31 queries
    async with P31_SEMAPHORE:
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

def _get_priority_tier(p31_values: list[str], spacy_label: str) -> tuple[str | None, list[str]]:
    """Determines the priority tier of an entity based on its P31 values.
    
    Returns:
        tuple: (priority_tier, matched_qids) where priority_tier is 'high', 'medium', 'low', or None
    """
    if spacy_label not in SPACY_LABEL_TO_WIKIDATA_P31:
        return (None, [])
    
    priority_map = SPACY_LABEL_TO_WIKIDATA_P31[spacy_label]
    
    # Check high priority first
    high_matches = [qid for qid in p31_values if qid in priority_map.get('high', [])]
    if high_matches:
        return ('high', high_matches)
    
    # Check medium priority
    medium_matches = [qid for qid in p31_values if qid in priority_map.get('medium', [])]
    if medium_matches:
        return ('medium', medium_matches)
    
    # Check low priority
    low_matches = [qid for qid in p31_values if qid in priority_map.get('low', [])]
    if low_matches:
        return ('low', low_matches)
    
    return (None, [])

def _calculate_candidate_score(qid: str, sitelinks: int, priority_tier: str | None, matched_p31_qids: list[str]) -> float:
    """Calculates a composite score for a candidate entity.
    
    Args:
        qid: Wikidata Q-ID
        sitelinks: Number of Wikipedia sitelinks
        priority_tier: 'high', 'medium', 'low', or None
        matched_p31_qids: List of matched P31 Q-IDs
    
    Returns:
        float: Composite score (higher is better)
    """
    # Base weight by priority tier
    if priority_tier == 'high':
        priority_weight = config.HIGH_PRIORITY_WEIGHT
    elif priority_tier == 'medium':
        priority_weight = config.MEDIUM_PRIORITY_WEIGHT
    elif priority_tier == 'low':
        priority_weight = config.LOW_PRIORITY_WEIGHT
    else:
        priority_weight = 0
    
    # Normalize sitelinks (cap at 100 to prevent extremely popular entities from dominating)
    sitelinks_score = min(sitelinks, 100)
    
    # Apply scientific term boost if applicable
    boost = 1.0
    if any(qid in SCIENTIFIC_QIDS for qid in matched_p31_qids):
        boost = config.SCIENTIFIC_TERM_BOOST
    
    # Final score = (priority_weight + sitelinks_score) * boost
    score = (priority_weight + sitelinks_score) * boost
    
    return score

async def get_qid_from_entity(client: httpx.AsyncClient, search_term: str, lang: str, spacy_label: str | None = None) -> str | None:
    """Searches Wikidata for the Q-ID of a given entity text with priority-based disambiguation.
    
    Args:
        client: HTTP client for API requests
        search_term: Entity text to search for
        lang: Language code (e.g., 'ru', 'en')
        spacy_label: spaCy entity label (e.g., 'LOC', 'PERSON')
    
    Returns:
        Best matching Q-ID or None if not found
    """
    # Enhanced query to fetch sitelinks count along with Q-IDs
    query = f'''SELECT ?item ?sitelinks WHERE {{
  SERVICE wikibase:mwapi {{
    bd:serviceParam wikibase:api "EntitySearch" .
    bd:serviceParam wikibase:endpoint "www.wikidata.org" .
    bd:serviceParam mwapi:search "{search_term}" .
    bd:serviceParam mwapi:language "{lang}" .
    ?item wikibase:apiOutputItem mwapi:item .
  }}
  ?item wikibase:sitelinks ?sitelinks .
}} ORDER BY DESC(?sitelinks) LIMIT {config.ENTITY_SEARCH_LIMIT}'''

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

        # Extract candidates with sitelinks
        candidates = []
        for binding in bindings:
            item_uri = binding.get('item', {}).get('value')
            sitelinks_value = binding.get('sitelinks', {}).get('value')
            if item_uri and sitelinks_value:
                qid = item_uri.split('/')[-1]
                sitelinks = int(sitelinks_value)
                candidates.append({'qid': qid, 'sitelinks': sitelinks})
        
        if not candidates:
            logger.warning(f"No valid candidates found for entity: {search_term}")
            return None
        
        logger.info(f"Retrieved {len(candidates)} candidates for '{search_term}': {[(c['qid'], c['sitelinks']) for c in candidates]}")

        # If no spacy_label provided, return the most popular candidate (highest sitelinks)
        if not spacy_label or spacy_label not in SPACY_LABEL_TO_WIKIDATA_P31:
            best_candidate = max(candidates, key=lambda x: x['sitelinks'])
            logger.info(f"Mapped entity '{search_term}' to Q-ID: {best_candidate['qid']} (no P31 filter, highest sitelinks: {best_candidate['sitelinks']})")
            return best_candidate['qid']
        
        # Fetch P31 values and score all candidates
        scored_candidates = []
        for candidate in candidates:
            qid = candidate['qid']
            sitelinks = candidate['sitelinks']
            
            # Fetch P31 values for this candidate
            p31_values = await _get_p31_for_qid(client, qid)
            
            # Determine priority tier
            priority_tier, matched_p31_qids = _get_priority_tier(p31_values, spacy_label)
            
            # Calculate score
            score = _calculate_candidate_score(qid, sitelinks, priority_tier, matched_p31_qids)
            
            scored_candidates.append({
                'qid': qid,
                'sitelinks': sitelinks,
                'p31_values': p31_values,
                'priority_tier': priority_tier,
                'matched_p31_qids': matched_p31_qids,
                'score': score
            })
            
            # Log candidate details
            if priority_tier:
                logger.info(f"Candidate {qid}: P31 values {matched_p31_qids}, priority tier {priority_tier}")
                logger.debug(f"Candidate {qid} score: {score} (priority={priority_tier}, sitelinks={sitelinks})")
            else:
                logger.debug(f"Candidate {qid}: No P31 match, sitelinks={sitelinks}")
        
        # Sort by score (descending)
        scored_candidates.sort(key=lambda x: x['score'], reverse=True)
        
        # Select best candidate based on priority tiers and threshold
        best_candidate = None
        
        # Try to find high-priority match
        high_priority_candidates = [c for c in scored_candidates if c['priority_tier'] == 'high']
        if high_priority_candidates:
            best_candidate = high_priority_candidates[0]
            logger.info(f"Selected {best_candidate['qid']} for '{search_term}': score={best_candidate['score']:.1f}, "
                       f"type={best_candidate['matched_p31_qids']}, sitelinks={best_candidate['sitelinks']} (high priority)")
            return best_candidate['qid']
        
        # Try to find medium-priority match
        medium_priority_candidates = [c for c in scored_candidates if c['priority_tier'] == 'medium']
        if medium_priority_candidates:
            best_candidate = medium_priority_candidates[0]
            logger.info(f"Selected {best_candidate['qid']} for '{search_term}': score={best_candidate['score']:.1f}, "
                       f"type={best_candidate['matched_p31_qids']}, sitelinks={best_candidate['sitelinks']} (medium priority)")
            return best_candidate['qid']
        
        # Try to find low-priority match with sufficient sitelinks
        low_priority_candidates = [c for c in scored_candidates 
                                  if c['priority_tier'] == 'low' and c['sitelinks'] >= config.MIN_SITELINKS_LOW_PRIORITY]
        if low_priority_candidates:
            best_candidate = low_priority_candidates[0]
            logger.info(f"Selected {best_candidate['qid']} for '{search_term}': score={best_candidate['score']:.1f}, "
                       f"type={best_candidate['matched_p31_qids']}, sitelinks={best_candidate['sitelinks']} (low priority)")
            return best_candidate['qid']
        
        # Fallback: return highest-sitelinks candidate if it meets minimum threshold
        fallback_candidates = [c for c in scored_candidates if c['sitelinks'] >= config.MIN_SITELINKS_THRESHOLD]
        if fallback_candidates:
            # Sort by sitelinks for fallback
            fallback_candidates.sort(key=lambda x: x['sitelinks'], reverse=True)
            best_candidate = fallback_candidates[0]
            logger.warning(f"No P31 match for '{search_term}', using highest-sitelinks result: {best_candidate['qid']} "
                          f"(sitelinks={best_candidate['sitelinks']})")
            return best_candidate['qid']
        
        # Last resort: return most popular candidate even if below threshold
        if scored_candidates:
            best_candidate = max(scored_candidates, key=lambda x: x['sitelinks'])
            logger.warning(f"All candidates below threshold for '{search_term}', returning best available: {best_candidate['qid']} "
                          f"(sitelinks={best_candidate['sitelinks']})")
            return best_candidate['qid']
        
        logger.warning(f"Could not find suitable Q-ID for entity: {search_term}")
        return None

    except httpx.RequestError as e:
        logger.error(f"Error querying Wikidata for entity '{search_term}': {e}")
    except json.JSONDecodeError:
        logger.error(f"Failed to decode JSON from Wikidata for entity '{search_term}'. Response: {response.text}")
    except Exception as e:
        logger.error(f"Unexpected error in get_qid_from_entity for '{search_term}': {e}", exc_info=True)

    logger.warning(f"Could not find Q-ID for entity: {search_term} in language: {lang} with spacy_label: {spacy_label}")
    return None