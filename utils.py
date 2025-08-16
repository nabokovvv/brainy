import py3langid
import re

# Hardcoded translations for specific strings
TRANSLATIONS = {
    "en": {
        "Author_Title": "AI-Powered Expert Researcher",
        "Research Statistics:": "Research Statistics:",
        "Websites Visited:": "Websites Visited:",
        "Chunks Analyzed:": "Chunks Analyzed:",
        "Total Characters Read:": "Total Characters Read:",
    },
    "es": {
        "Author_Title": "Investigador Experto con IA",
        "Research Statistics:": "Estadísticas de Investigación:",
        "Websites Visited:": "Sitios Web Visitados:",
        "Chunks Analyzed:": "Fragmentos Analizados:",
        "Total Characters Read:": "Total de Caracteres Leídos:",
    },
    "ru": {
        "Author_Title": "Эксперт-исследователь на базе ИИ",
        "Research Statistics:": "Статистика исследования:",
        "Websites Visited:": "Посещенные веб-сайты:",
        "Chunks Analyzed:": "Проанализированные фрагменты:",
        "Total Characters Read:": "Всего прочитанных символов:",
    },
    "pt": {
        "Author_Title": "Pesquisador Especialista com IA",
        "Research Statistics:": "Estatísticas da Pesquisa:",
        "Websites Visited:": "Sites Visitados:",
        "Chunks Analyzed:": "Fragmentos Analisados:",
        "Total Characters Read:": "Total de Caracteres Lidos:",
    },
    "de": {
        "Author_Title": "KI-gestützter Expertenforscher",
        "Research Statistics:": "Forschungsstatistiken:",
        "Websites Visited:": "Besuchte Websites:",
        "Chunks Analyzed:": "Analysierte Abschnitte:",
        "Total Characters Read:": "Gelesene Zeichen insgesamt:",
    },
    "tr": {
        "Author_Title": "Yapay Zeka Destekli Uzman Araştırmacı",
        "Research Statistics:": "Araştırma İstatistikleri:",
        "Websites Visited:": "Ziyaret Edilen Web Siteleri:",
        "Chunks Analyzed:": "Analiz Edilen Parçalar:",
        "Total Characters Read:": "Toplam Okunan Karakter Sayısı:",
    },
    "id": {
        "Author_Title": "Peneliti Ahli Bertenaga AI",
        "Research Statistics:": "Statistik Penelitian:",
        "Websites Visited:": "Situs Web yang Dikunjungi:",
        "Chunks Analyzed:": "Potongan yang Dianalisis:",
        "Total Characters Read:": "Total Karakter yang Dibaca:",
    }
}

def translate_string(text_key: str, lang: str) -> str:
    """
    Translates a given text key based on the specified language.
    Falls back to English if the language or key is not found.
    """
    return TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(text_key, text_key)

def strip_think(text: str) -> str:
    """Removes <think> tags from a string."""
    return re.sub(r'<think>.*?</think>', '', text, flags=re.S | re.I).strip()

def detect_language(text: str) -> str:
    """Detects the language of the given text using py3langid."""
    lang, _ = py3langid.classify(text)
    return lang

def _filter_duplicate_chunks(chunks: list) -> list:
    """Filters out duplicate chunks based on their text content."""
    seen_texts = set()
    unique_chunks = []
    for chunk in chunks:
        # This check assumes chunk has a .text attribute.
        # It might need to be adapted if chunks are of different types.
        if hasattr(chunk, 'text') and chunk.text not in seen_texts:
            seen_texts.add(chunk.text)
            unique_chunks.append(chunk)
        elif isinstance(chunk, dict) and chunk.get('text') not in seen_texts:
            seen_texts.add(chunk['text'])
            unique_chunks.append(chunk)
    return unique_chunks