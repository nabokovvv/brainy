from __future__ import annotations

import asyncio
import logging
import re
import socket
from urllib.parse import urljoin, urlparse, urlunparse

import config
from brainy_core.web_safety import is_global_ip_address, is_safe_public_http_url

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
MAX_PAGES_PER_REQUEST = 4
MAX_REDIRECTS = 3


class TextChunk:
    def __init__(self, text, source_url, index):
        self.text = text
        self.source_url = source_url
        self.index = index


class PageFetcher:
    """Lifespan-owned page loader for the Web ON evidence pipeline."""

    def __init__(self, session) -> None:
        self._session = session

    async def load(self, urls):
        return await fetch_and_process_pages(urls, session=self._session)


def _canonical_page_url(url: str) -> str:
    """Normalize a page URL before admission and fetching."""
    parsed = urlparse(url)
    return urlunparse(
        (
            parsed.scheme.lower(),
            (parsed.hostname or "").lower(),
            parsed.path or "/",
            "",
            parsed.query,
            "",
        )
    )


def _get_aiohttp():
    try:
        import aiohttp
    except ImportError as exc:
        raise RuntimeError(
            "aiohttp is not installed. Install the 'research' extra: pip install -e '.[research]'"
        ) from exc
    return aiohttp


def _get_beautifulsoup():
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise RuntimeError(
            "beautifulsoup4 is not installed. Install the 'research' extra: pip install -e '.[research]'"
        ) from exc
    return BeautifulSoup


async def _read_bounded_body(response, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            declared_size = int(declared)
            if declared_size < 0 or declared_size > limit:
                raise ValueError("response too large")
        except ValueError:
            raise ValueError("invalid or oversized Content-Length") from None

    chunks = []
    total = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > limit:
            raise ValueError("response too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _host_resolves_only_to_public_addresses(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname
    if host is None:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        return False
    return bool(addresses) and all(is_global_ip_address(sockaddr[0]) for *_, sockaddr in addresses)


async def fetch_page(session, url: str, retries: int = 2):
    if not is_safe_public_http_url(url) or not await _host_resolves_only_to_public_addresses(url):
        logger.warning("Rejected unsafe fetch URL")
        return None

    headers = {"User-Agent": config.CUSTOM_USER_AGENT}
    aiohttp = _get_aiohttp()
    timeout = aiohttp.ClientTimeout(total=10, connect=5)
    target_url = url
    redirect_count = 0
    for attempt in range(retries):
        host = urlparse(target_url).hostname or "unknown"
        try:
            async with session.get(
                target_url,
                timeout=timeout,
                headers=headers,
                allow_redirects=False,
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    redirect_count += 1
                    if not location or redirect_count > MAX_REDIRECTS:
                        logger.warning("Rejected redirect chain host=%s", host)
                        return None
                    target_url = urljoin(target_url, location)
                    if not is_safe_public_http_url(
                        target_url
                    ) or not await _host_resolves_only_to_public_addresses(target_url):
                        logger.warning("Rejected unsafe redirect host=%s", host)
                        return None
                    continue
                if response.status in {429, 503} and attempt < retries - 1:
                    await asyncio.sleep(2**attempt)
                    continue
                if response.status != 200:
                    logger.warning("Fetch failed host=%s status=%s", host, response.status)
                    return None

                content_type = response.headers.get("Content-Type", "")
                media_type = content_type.split(";", 1)[0].strip().lower()
                if media_type not in ALLOWED_CONTENT_TYPES:
                    logger.warning("Rejected fetch content type host=%s", host)
                    return None

                raw_body = await _read_bounded_body(response)
                encoding = response.charset or "utf-8"
                return raw_body.decode(encoding, errors="replace")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning("Fetch failed host=%s type=%s", host, type(exc).__name__)
            return None
    return None


def clean_html(html_content, url):
    host = urlparse(url).hostname or "unknown"
    try:
        BeautifulSoup = _get_beautifulsoup()
        soup = BeautifulSoup(html_content, "html.parser")
        for script_or_style in soup(["script", "style"]):
            script_or_style.decompose()

        title = soup.title.string if soup.title and soup.title.string else ""
        if not title:
            logger.debug("No title found host=%s", host)

        meta_description_tag = soup.find("meta", attrs={"name": "description"})
        meta_description = (
            meta_description_tag["content"]
            if meta_description_tag and "content" in meta_description_tag.attrs
            else ""
        )
        if not meta_description:
            logger.debug("No meta description found host=%s", host)

        paragraphs = soup.find_all("p")
        list_items = soup.find_all("li")
        tables = soup.find_all("table")

        p_text = " ".join(p.get_text() for p in paragraphs) if paragraphs else ""
        li_text = "\n".join(li.get_text() for li in list_items) if list_items else ""
        table_text = "\n".join(table.get_text() for table in tables) if tables else ""

        combined_body_text = "\n\n".join(filter(None, [p_text, li_text, table_text]))

        if not combined_body_text:
            logger.debug("No main body text found host=%s", host)

        cleaned_text = f"{title}\n{meta_description}\n{combined_body_text}"
        return cleaned_text
    except Exception as exc:
        logger.warning("HTML cleaning failed host=%s type=%s", host, type(exc).__name__)
        return ""


def _split_sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?。！？])\s+|\n+", text) if part.strip()]


def chunk_text(text, source_url, max_chunk_words=150):
    sentences = _split_sentences(text)
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
                chunks.append(
                    TextChunk(
                        text=" ".join(current_chunk_sentences),
                        source_url=source_url,
                        index=len(chunks),
                    )
                )
            current_chunk_sentences = [sentence]
            current_chunk_word_count = len(sentence_words)

    if current_chunk_sentences:
        chunks.append(
            TextChunk(
                text=" ".join(current_chunk_sentences), source_url=source_url, index=len(chunks)
            )
        )

    return chunks


async def fetch_and_process_pages(urls, session=None):
    """Fetch bounded public pages, reusing an injected lifespan session."""
    aiohttp = _get_aiohttp()
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        unique_urls = list(dict.fromkeys(_canonical_page_url(url) for url in urls))[
            :MAX_PAGES_PER_REQUEST
        ]
        tasks = [fetch_page(session, url) for url in unique_urls]
        html_contents = await asyncio.gather(*tasks)

        all_chunks = []
        for i, html in enumerate(html_contents):
            url = unique_urls[i]
            if html:
                clean_text = await asyncio.to_thread(clean_html, html, url)
                if clean_text:
                    chunks = await asyncio.to_thread(chunk_text, clean_text, source_url=url)
                    all_chunks.extend(chunks)
        return all_chunks
    finally:
        if own_session:
            await session.close()
