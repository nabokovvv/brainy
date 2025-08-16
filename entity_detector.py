import spacy
import re

# A regex to find leading conjunctions and similar words in different languages
# Covers: and, y, und, et, и, serta, dan, ve, etc.
LEADING_CONJUNCTION_REGEX = re.compile(r"^(\s*(and|y|und|et|и|serta|dan|ve)\s+)", re.IGNORECASE)

# Map language codes to spaCy model names
LANG_MODEL_MAP = {
    "en": "en_core_web_sm",
    "es": "es_core_news_sm",
    "pt": "pt_core_news_sm",
    "fr": "fr_core_news_sm",
    "ru": "ru_core_news_sm",
    "de": "de_core_news_sm",
    "tr": "xx_ent_wiki_sm", # No dedicated small model for Turkish
    "id": "xx_ent_wiki_sm", # No dedicated small model for Indonesian
}

# Cache loaded NLP models
_nlp_models = {}

def load_nlp_model(lang: str):
    """Loads and caches the spaCy NLP model for the given language."""
    model_name = LANG_MODEL_MAP.get(lang, "xx_ent_wiki_sm") # Fallback to multilingual

    if model_name not in _nlp_models:
        try:
            _nlp_models[model_name] = spacy.load(model_name)
        except OSError:
            print(f"\n{'-'*60}")
            print(f"ERROR: SpaCy model '{model_name}' not found.")
            print(f"Please install it by running: python -m spacy download {model_name}")
            print(f"{'-'*60}\n")
            raise
    return _nlp_models[model_name]

def _clean_entity_text(text: str) -> str:
    """Removes leading conjunctions from the detected entity text."""
    return LEADING_CONJUNCTION_REGEX.sub("", text).strip()

def detect_entities(text: str, lang: str) -> list:
    """
    Detects named entities in two passes: first with a precise language-specific model,
    then with a broad multilingual model to catch additional entities.
    """
    # --- First Pass: Language-Specific Model ---
    nlp_specific = load_nlp_model(lang)
    doc_specific = nlp_specific(text)
    entities = []
    found_entity_texts = set()

    for ent in doc_specific.ents:
        cleaned_text = _clean_entity_text(ent.text)
        if not cleaned_text:
            continue

        # Re-process the cleaned text to get the correct lemma
        cleaned_doc = nlp_specific(cleaned_text)
        lemmatized_parts = [
            token.lemma_
            for token in cleaned_doc
            if token.pos_ in ['NOUN', 'PROPN', 'ADJ']
        ]
        lemmatized_entity_text = " ".join(lemmatized_parts).strip()

        if lemmatized_entity_text and lemmatized_entity_text not in found_entity_texts:
            # Use the original label but the cleaned text and lemma
            entities.append({"text": cleaned_text, "label": ent.label_, "lemma": lemmatized_entity_text})
            found_entity_texts.add(lemmatized_entity_text)

    # --- Second Pass: Multilingual Model for Fallback ---
    specific_model_name = LANG_MODEL_MAP.get(lang, "xx_ent_wiki_sm")
    if specific_model_name != "xx_ent_wiki_sm":
        nlp_multi = load_nlp_model('xx') # 'xx' will load 'xx_ent_wiki_sm'
        doc_multi = nlp_multi(text)
        for ent in doc_multi.ents:
            cleaned_text = _clean_entity_text(ent.text)
            if not cleaned_text:
                continue

            # Attempt to lemmatize the multilingual entity for better deduplication
            multi_lemmatized_text = " ".join([token.lemma_ for token in nlp_multi(cleaned_text) if token.pos_ in ['NOUN', 'PROPN', 'ADJ']]).strip()
            if not multi_lemmatized_text: # Fallback if lemmatization fails
                multi_lemmatized_text = cleaned_text

            if cleaned_text and multi_lemmatized_text.lower() not in {e.lower() for e in found_entity_texts}:
                is_new = True
                for found_lemma in found_entity_texts:
                    if multi_lemmatized_text in found_lemma or found_lemma in multi_lemmatized_text:
                        is_new = False
                        break
                if is_new:
                    entities.append({"text": cleaned_text, "label": ent.label_, "lemma": multi_lemmatized_text})
                    found_entity_texts.add(multi_lemmatized_text)
    return entities
