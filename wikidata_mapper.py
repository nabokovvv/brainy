from __future__ import annotations

import httpx
import json
import logging
import asyncio
import re

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
        "low": ["Q4271324"],  # mythical character
    },
    "ORG": {
        "high": [
            "Q6881511",
            "Q4830453",
            "Q783794",
            "Q2085381",
            "Q4438121",
        ],  # enterprise, business, company, publisher, sports org
        "medium": [
            "Q43229",
            "Q7210356",
            "Q15265344",
        ],  # organization, political org, broadcasting org
        "low": ["Q16917", "Q685"],  # hospital, library
    },
    "GPE": {
        "high": [
            "Q6256",
            "Q3624078",
            "Q515",
            "Q10864048",
            "Q15284",
        ],  # country, sovereign state, city, constituent state, municipality
        "medium": ["Q28575", "Q82794"],  # county, geographic region
        "low": ["Q2221906"],  # geographic location (generic)
    },
    "LOC": {
        "high": [
            "Q515",
            "Q486972",
            "Q6256",
            "Q82794",
            "Q10864048",
        ],  # city, human settlement, country, geographic region, constituent state
        "medium": [
            "Q23442",
            "Q4022",
            "Q8502",
            "Q13218391",
            "Q5107",
        ],  # island, river, mountain, historical country, continent
        "low": ["Q2221906", "Q618123"],  # geographic location, geographical object (generic)
    },
    "FAC": {
        "high": ["Q811979", "Q13226383"],  # architectural structure, facility
        "medium": [],
        "low": [],
    },
    "PRODUCT": {
        "high": [
            "Q40056",
            "Q7397",
            "Q571",
            "Q11424",
            "Q134556",
        ],  # software, video game, book, film, single
        "medium": ["Q2424752", "Q47461344"],  # product, written work
        "low": ["Q24229398"],  # manufactured good
    },
    "EVENT": {
        "high": ["Q198", "Q18608583", "Q350604"],  # war, recurring event, armed conflict
        "medium": ["Q1190554", "Q46847"],  # event, disaster
        "low": ["Q1656682"],  # occurrence
    },
    "WORK_OF_ART": {
        "high": [
            "Q3305213",
            "Q860861",
            "Q207628",
            "Q11424",
        ],  # painting, sculpture, musical composition, film
        "medium": ["Q838948"],  # work of art
        "low": ["Q17537576"],  # creative work
    },
    "LAW": {
        "high": ["Q828101"],  # law
        "medium": [],
        "low": [],
    },
    "NORP": {
        "high": ["Q41710", "Q7278"],  # ethnic group, political party
        "medium": ["Q16334295", "Q9174"],  # human group, religion
        "low": [],
    },
    "MISC": {
        "high": [
            "Q12136",
            "Q16521",
            "Q11173",
            "Q7187",
            "Q811430",
            "Q483247",
        ],  # disease, taxon, chemical compound, gene, construction, phenomenon
        "medium": ["Q151885", "Q1047113"],  # concept, specialty
        "low": ["Q35120", "Q58778"],  # entity (very broad), system
    },
}

# Scientific Q-IDs that get boosted scoring
SCIENTIFIC_QIDS = {"Q12136", "Q16521", "Q11173", "Q7187", "Q483247"}
QID_PATTERN = re.compile(r"^Q[1-9]\d*$")
LANGUAGE_PATTERN = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})?$")


def _escape_sparql_literal(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


async def _get_p31_for_qid(client: httpx.AsyncClient, qid: str) -> list[str]:
    """Fetches P31 (instance of) values for a given QID with controlled P279 depth."""
    if not QID_PATTERN.fullmatch(qid):
        return []
    # Use limited P279 depth to prevent over-matching through distant ontological relationships
    max_depth = config.P279_MAX_DEPTH

    if max_depth == 0:
        # Direct P31 only, no subclass traversal
        property_path = "wdt:P31"
    elif max_depth == 1:
        # P31 OR (P31 followed by one P279 hop)
        property_path = "(wdt:P31|wdt:P31/wdt:P279)"
    else:
        # Build cumulative alternative paths for depth >= 2
        # Example for depth=2: (wdt:P31|wdt:P31/wdt:P279|wdt:P31/wdt:P279/wdt:P279)
        paths = ["wdt:P31"]
        for i in range(1, max_depth + 1):
            path_segment = "wdt:P31" + "/wdt:P279" * i
            paths.append(path_segment)
        property_path = "(" + "|".join(paths) + ")"

    query = f"""
    SELECT ?type WHERE {{
      wd:{qid} {property_path} ?type .
    }}
    """

    headers = {"User-Agent": config.CUSTOM_USER_AGENT, "Accept": "application/sparql-results+json"}

    # Use semaphore to rate limit P31 queries
    async with P31_SEMAPHORE:
        try:
            response = await client.get(
                WIKIDATA_SPARQL_ENDPOINT, headers=headers, params={"query": query}
            )
            response.raise_for_status()
            data = response.json()
            bindings = data.get("results", {}).get("bindings", [])
            return [b["type"]["value"].split("/")[-1] for b in bindings]
        except httpx.RequestError as exc:
            logger.warning("Wikidata P31 request failed qid=%s type=%s", qid, type(exc).__name__)
            return []
        except json.JSONDecodeError:
            logger.warning("Wikidata P31 returned invalid JSON qid=%s", qid)
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
    high_matches = [qid for qid in p31_values if qid in priority_map.get("high", [])]
    if high_matches:
        return ("high", high_matches)

    # Check medium priority
    medium_matches = [qid for qid in p31_values if qid in priority_map.get("medium", [])]
    if medium_matches:
        return ("medium", medium_matches)

    # Check low priority
    low_matches = [qid for qid in p31_values if qid in priority_map.get("low", [])]
    if low_matches:
        return ("low", low_matches)

    return (None, [])


def _calculate_candidate_score(
    qid: str, sitelinks: int, priority_tier: str | None, matched_p31_qids: list[str]
) -> float:
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
    if priority_tier == "high":
        priority_weight = config.HIGH_PRIORITY_WEIGHT
    elif priority_tier == "medium":
        priority_weight = config.MEDIUM_PRIORITY_WEIGHT
    elif priority_tier == "low":
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


async def get_qid_from_entity(
    client: httpx.AsyncClient, search_term: str, lang: str, spacy_label: str | None = None
) -> str | None:
    """Searches Wikidata for the Q-ID of a given entity text with priority-based disambiguation.

    Args:
        client: HTTP client for API requests
        search_term: Entity text to search for
        lang: Language code (e.g., 'ru', 'en')
        spacy_label: spaCy entity label (e.g., 'LOC', 'PERSON')

    Returns:
        Best matching Q-ID or None if not found
    """
    normalized_search = search_term.strip()
    if (
        not normalized_search
        or len(normalized_search) > 256
        or not LANGUAGE_PATTERN.fullmatch(lang)
    ):
        return None
    escaped_search = _escape_sparql_literal(normalized_search)

    # Enhanced query to fetch sitelinks count along with Q-IDs
    query = f'''SELECT ?item ?sitelinks WHERE {{
  SERVICE wikibase:mwapi {{
    bd:serviceParam wikibase:api "EntitySearch" .
    bd:serviceParam wikibase:endpoint "www.wikidata.org" .
    bd:serviceParam mwapi:search "{escaped_search}" .
    bd:serviceParam mwapi:language "{lang}" .
    ?item wikibase:apiOutputItem mwapi:item .
  }}
  ?item wikibase:sitelinks ?sitelinks .
}} ORDER BY DESC(?sitelinks) LIMIT {config.ENTITY_SEARCH_LIMIT}'''

    headers = {"User-Agent": config.CUSTOM_USER_AGENT, "Accept": "application/sparql-results+json"}

    try:
        response = await client.get(
            WIKIDATA_SPARQL_ENDPOINT, headers=headers, params={"query": query}
        )
        response.raise_for_status()
        data = response.json()

        bindings = data.get("results", {}).get("bindings", [])

        if not bindings:
            logger.debug("Wikidata entity candidates count=0 language=%s", lang)
            return None

        # Extract candidates with sitelinks
        candidates = []
        for binding in bindings:
            item_uri = binding.get("item", {}).get("value")
            sitelinks_value = binding.get("sitelinks", {}).get("value")
            if item_uri and sitelinks_value:
                qid = item_uri.split("/")[-1]
                sitelinks = int(sitelinks_value)
                candidates.append({"qid": qid, "sitelinks": sitelinks})

        if not candidates:
            logger.debug("Wikidata valid candidates count=0 language=%s", lang)
            return None

        logger.info("Wikidata candidates count=%s language=%s", len(candidates), lang)

        # If no spacy_label provided, return the most popular candidate (highest sitelinks)
        if not spacy_label or spacy_label not in SPACY_LABEL_TO_WIKIDATA_P31:
            best_candidate = max(candidates, key=lambda x: x["sitelinks"])
            logger.info("Wikidata entity selected qid=%s tier=unfiltered", best_candidate["qid"])
            return best_candidate["qid"]

        # Fetch P31 values and score all candidates
        scored_candidates = []
        for candidate in candidates:
            qid = candidate["qid"]
            sitelinks = candidate["sitelinks"]

            # Fetch P31 values for this candidate
            p31_values = await _get_p31_for_qid(client, qid)

            # Determine priority tier
            priority_tier, matched_p31_qids = _get_priority_tier(p31_values, spacy_label)

            # Calculate score
            score = _calculate_candidate_score(qid, sitelinks, priority_tier, matched_p31_qids)

            scored_candidates.append(
                {
                    "qid": qid,
                    "sitelinks": sitelinks,
                    "p31_values": p31_values,
                    "priority_tier": priority_tier,
                    "matched_p31_qids": matched_p31_qids,
                    "score": score,
                }
            )

            # Log candidate details
            if priority_tier:
                logger.debug("Wikidata candidate qid=%s tier=%s", qid, priority_tier)
            else:
                logger.debug("Wikidata candidate qid=%s tier=none", qid)

        # Sort by score (descending)
        scored_candidates.sort(key=lambda x: x["score"], reverse=True)

        # Select best candidate based on priority tiers and threshold
        best_candidate = None

        # Try to find high-priority match
        high_priority_candidates = [c for c in scored_candidates if c["priority_tier"] == "high"]
        if high_priority_candidates:
            best_candidate = high_priority_candidates[0]
            logger.info("Wikidata entity selected qid=%s tier=high", best_candidate["qid"])
            return best_candidate["qid"]

        # Try to find medium-priority match
        medium_priority_candidates = [
            c for c in scored_candidates if c["priority_tier"] == "medium"
        ]
        if medium_priority_candidates:
            best_candidate = medium_priority_candidates[0]
            logger.info("Wikidata entity selected qid=%s tier=medium", best_candidate["qid"])
            return best_candidate["qid"]

        # Try to find low-priority match with sufficient sitelinks
        low_priority_candidates = [
            c
            for c in scored_candidates
            if c["priority_tier"] == "low" and c["sitelinks"] >= config.MIN_SITELINKS_LOW_PRIORITY
        ]
        if low_priority_candidates:
            best_candidate = low_priority_candidates[0]
            logger.info("Wikidata entity selected qid=%s tier=low", best_candidate["qid"])
            return best_candidate["qid"]

        # Fallback: return highest-sitelinks candidate if it meets minimum threshold
        fallback_candidates = [
            c for c in scored_candidates if c["sitelinks"] >= config.MIN_SITELINKS_THRESHOLD
        ]
        if fallback_candidates:
            # Sort by sitelinks for fallback
            fallback_candidates.sort(key=lambda x: x["sitelinks"], reverse=True)
            best_candidate = fallback_candidates[0]
            logger.info("Wikidata entity selected qid=%s tier=fallback", best_candidate["qid"])
            return best_candidate["qid"]

        # Last resort: return most popular candidate even if below threshold
        if scored_candidates:
            best_candidate = max(scored_candidates, key=lambda x: x["sitelinks"])
            logger.info("Wikidata entity selected qid=%s tier=last-resort", best_candidate["qid"])
            return best_candidate["qid"]

        logger.debug("No suitable Wikidata entity language=%s", lang)
        return None

    except httpx.RequestError as exc:
        logger.warning("Wikidata entity request failed type=%s", type(exc).__name__)
    except json.JSONDecodeError:
        logger.warning("Wikidata entity response was invalid JSON")
    except Exception as exc:
        logger.warning("Wikidata entity lookup failed type=%s", type(exc).__name__)

    logger.debug("Wikidata entity lookup returned no result language=%s", lang)
    return None
