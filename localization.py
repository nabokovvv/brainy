import json
from typing import Dict, Any

class Translator:
    def __init__(self, translations_file: str):
        with open(translations_file, 'r', encoding='utf-8') as f:
            self.translations: Dict[str, Dict[str, str]] = json.load(f)
        self.supported_languages = list(self.translations.keys())

    def get_string(self, key: str, lang_code: str, **kwargs: Any) -> str:
        """Gets a translated string, falling back to English if the language or key is not found."""
        # Fallback to English if the language is not supported
        if lang_code not in self.supported_languages:
            lang_code = 'en'

        # Get the string, falling back to the English version of the string if the key is missing
        string = self.translations.get(lang_code, {}).get(key) or self.translations.get('en', {}).get(key)

        if not string:
            # Ultimate fallback if a key is not in English either
            return f"<translation_missing: {key}>"

        return string.format(**kwargs)
