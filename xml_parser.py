import xml.etree.ElementTree as ET
from typing import List
import logging
import re

logger = logging.getLogger(__name__)

class YandexChunk:
    def __init__(self, text: str, url: str, source_type: str):
        self.text = text
        self.url = url
        self.source_type = source_type

def clean_hlword_tags(text: str) -> str:
    """Removes <hlword> tags from the text."""
    return re.sub(r'<hlword>(.*?)</hlword>', r'\1', text)

def parse_yandex_xml(xml_string: str) -> List[YandexChunk]:
    chunks = []
    if not xml_string:
        return chunks
    try:
        root = ET.fromstring(xml_string)
        for doc in root.findall('.//doc'):
            url_element = doc.find('url')
            url = url_element.text if url_element is not None else None

            if url:
                # Extract and clean passage
                passage_element = doc.find('.//passage')
                if passage_element is not None and passage_element.text:
                    cleaned_passage = clean_hlword_tags(passage_element.text)
                    chunks.append(YandexChunk(text=cleaned_passage, url=url, source_type='passage'))
                    logger.info(f"XML Parser - Parsed Passage: URL={url}, Text={cleaned_passage[:100]}...")

                # Extract and clean extended-text
                extended_text_element = doc.find('.//extended-text')
                if extended_text_element is not None and extended_text_element.text:
                    cleaned_extended_text = clean_hlword_tags(extended_text_element.text)
                    chunks.append(YandexChunk(text=cleaned_extended_text, url=url, source_type='extended-text'))
                    logger.info(f"XML Parser - Parsed Extended Text: URL={url}, Text={cleaned_extended_text[:100]}...")

    except ET.ParseError as e:
        logger.error(f"XML Parse Error: {e} in xml_string: {xml_string[:500]}")

    return chunks
