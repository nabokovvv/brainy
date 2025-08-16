import asyncio
import aiohttp
from bs4 import BeautifulSoup
import logging
import random
import config

logger = logging.getLogger(__name__)

# Custom User-Agent for transparency


# List of common User-Agent strings for rotation (used as fallback)
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/91.0.864.59',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Firefox/89.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15',
    'Mozilla/5.0 (Linux; Android 10; SM-G973F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Mobile Safari/537.36',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1',
]

class TextChunk:
    def __init__(self, text, source_url, index):
        self.text = text
        self.source_url = source_url
        self.index = index

async def fetch_page(session, url, retries=3):
    for attempt in range(retries):
        if attempt == 0:
            headers = {'User-Agent': config.CUSTOM_USER_AGENT}
        else:
            headers = {'User-Agent': random.choice(USER_AGENTS)}
        try:
            async with session.get(url, timeout=10, headers=headers, ssl=False) as response:
                response.raise_for_status()
                raw_body = await response.read()
                try:
                    encoding = response.get_encoding()
                    return raw_body.decode(encoding)
                except UnicodeDecodeError:
                    logger.warning(f"UnicodeDecodeError for {url}. Trying 'utf-8' with replacement characters.")
                    return raw_body.decode('utf-8', errors='replace')
        except aiohttp.ClientResponseError as e:
            if e.status in [429, 503] and attempt < retries - 1:
                delay = 2 ** attempt  # Exponential backoff
                logger.warning(f"Received {e.status} for {url}. Retrying in {delay} seconds...")
                await asyncio.sleep(delay)
            elif e.status in [401, 403] and attempt < retries - 1: # Retry with new User-Agent
                logger.warning(f"Received {e.status} for {url}. Retrying with new User-Agent...")
                # No delay here, as User-Agent change is immediate
            else:
                logger.error(f"Error fetching {url}: {e}")
                return None
        except aiohttp.ClientError as e:
            logger.error(f"Error fetching {url}: {e}")
            return None
        except asyncio.TimeoutError:
            logger.warning(f"Timeout fetching {url}")
            return None
        except Exception as e:
            logger.error(f"An unexpected error occurred while fetching {url}: {e}")
            return None
    return None # All retries failed

def clean_html(html_content, url):
    logger.info(f"Cleaning HTML for URL: {url}")
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        for script_or_style in soup(["script", "style"]):
            script_or_style.decompose()
        
        title = soup.title.string if soup.title and soup.title.string else ''
        if not title:
            logger.warning(f"No title found for {url}")

        meta_description_tag = soup.find('meta', attrs={'name': 'description'})
        meta_description = meta_description_tag['content'] if meta_description_tag and 'content' in meta_description_tag.attrs else ''
        if not meta_description:
            logger.warning(f"No meta description found for {url}")

        paragraphs = soup.find_all('p')
        list_items = soup.find_all('li')
        tables = soup.find_all('table')

        p_text = ' '.join(p.get_text() for p in paragraphs) if paragraphs else ''
        li_text = '\n'.join(li.get_text() for li in list_items) if list_items else ''
        table_text = '\n'.join(table.get_text() for table in tables) if tables else ''

        combined_body_text = '\n\n'.join(filter(None, [p_text, li_text, table_text]))

        if not combined_body_text:
            logger.warning(f"No main body text (paragraphs, lists, tables) found for {url}")

        cleaned_text = f"{title}\n{meta_description}\n{combined_body_text}"
        logger.info(f"Cleaned text for {url} (first 200 chars): {cleaned_text[:200]}")
        return cleaned_text
    except Exception as e:
        logger.error(f"Error cleaning HTML for {url}: {e}")
        return ""

import nltk
try:
    nltk.data.find('tokenizers/punkt')
except nltk.downloader.DownloadError:
    nltk.download('punkt')

def chunk_text(text, source_url, max_chunk_words=150):
    sentences = nltk.sent_tokenize(text)
    chunks = []
    current_chunk_sentences = []
    current_chunk_word_count = 0

    for sentence in sentences:
        sentence_words = sentence.split()
        if current_chunk_word_count + len(sentence_words) <= max_chunk_words:
            current_chunk_sentences.append(sentence)
            current_chunk_word_count += len(sentence_words)
        else:
            if current_chunk_sentences:
                chunks.append(TextChunk(text=' '.join(current_chunk_sentences), source_url=source_url, index=len(chunks)))
            current_chunk_sentences = [sentence]
            current_chunk_word_count = len(sentence_words)
    
    if current_chunk_sentences:
        chunks.append(TextChunk(text=' '.join(current_chunk_sentences), source_url=source_url, index=len(chunks)))
    
    return chunks

from urllib.parse import urlparse

async def fetch_and_process_pages(urls):
    async with aiohttp.ClientSession() as session:
        tasks = []
        last_domain = None
        # Use a set to keep track of unique URLs and avoid processing duplicates
        unique_urls = list(dict.fromkeys(urls))
        
        for url in unique_urls:
            domain = urlparse(url).netloc
            if domain == last_domain:
                logger.info(f"Waiting 2 seconds before fetching from same domain: {domain}")
                await asyncio.sleep(2)
            tasks.append(fetch_page(session, url))
            last_domain = domain

        html_contents = await asyncio.gather(*tasks)
        
        all_chunks = []
        for i, html in enumerate(html_contents):
            url = unique_urls[i] # Get the URL for logging
            if html:
                clean_text = await asyncio.to_thread(clean_html, html, url)
                if clean_text: # Only chunk if there's actual cleaned text
                    chunks = await asyncio.to_thread(chunk_text, clean_text, source_url=url)
                    all_chunks.extend(chunks)
                else:
                    logger.warning(f"Skipping chunking for {url} due to empty cleaned text.")
            else:
                logger.warning(f"Skipping processing for {url} due to empty HTML content.")
        return all_chunks
