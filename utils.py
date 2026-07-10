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
    },
}


def translate_string(text_key: str, lang: str) -> str:
    """
    Translates a given text key based on the specified language.
    Falls back to English if the language or key is not found.
    """
    return TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(text_key, text_key)


# Допустимые имена "мысленных" тегов
_THINK_TAGS = r"(think|analysis|reasoning|scratchpad|chain[_\-\s]?of[_\-\s]?thought)"

# Полноценный блок: <think ...> ... </think> (регистр/пробелы/переносы не мешают)
_THINK_BLOCK = re.compile(
    rf"<\s*{_THINK_TAGS}\b[^>]*>\s*.*?\s*<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)

# Осиротевший открывающий тег до конца текста
_THINK_OPEN_TO_EOF = re.compile(
    rf"<\s*{_THINK_TAGS}\b[^>]*>\s*.*\Z",
    re.IGNORECASE | re.DOTALL,
)

# Одиночный открывающий тег
_THINK_OPEN = re.compile(
    rf"<\s*{_THINK_TAGS}\b[^>]*>",
    re.IGNORECASE,
)


def strip_think(text: str) -> str:
    if not isinstance(text, str):
        return text
    # 1) Снимаем корректно закрытые блоки. Повторяем, если их несколько.
    prev = None
    while prev != text:
        prev = text
        text = _THINK_BLOCK.sub("", text)
    # 2) Если остался осиротевший открывающий тег — срезаем до конца
    text = _THINK_OPEN_TO_EOF.sub("", text)
    # 3) Убираем одиночные открывающие теги, если вдруг остались
    text = _THINK_OPEN.sub("", text)
    return text.strip()
