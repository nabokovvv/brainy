from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from brainy_core.web_safety import is_safe_public_http_url
from brainy_core.search import SearchQuery, SearchResult
from page_processor import _canonical_page_url, _read_bounded_body, chunk_text, fetch_page
from wikidata_mapper import _escape_sparql_literal, _get_p31_for_qid, get_qid_from_entity


ROOT = Path(__file__).resolve().parents[1]


class FailingHttpClient:
    async def get(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("invalid input must fail before network I/O")


class FixtureContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            yield chunk


class FixtureResponse:
    def __init__(self, status, headers=None, body=b""):
        self.status = status
        self.headers = headers or {}
        self.charset = "utf-8"
        self.content = FixtureContent([body])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class FixtureSession:
    def __init__(self, responses):
        self.responses = responses
        self.requested = []

    def get(self, url, **_kwargs):
        self.requested.append(url)
        return self.responses[url]


class ResearchContractTests(unittest.IsolatedAsyncioTestCase):
    def test_search_contract_normalizes_safe_result_urls(self) -> None:
        request = SearchQuery(query="latest local news", language="en", limit=3)
        result = SearchResult(
            title="Example",
            url="HTTPS://Example.COM/story/?q=1#tracking",
            snippet="A bounded snippet.",
            rank=1,
            provider="fake",
        )

        self.assertEqual(request.limit, 3)
        self.assertEqual(result.canonical_url, "https://example.com/story?q=1")

    def test_search_contract_rejects_unbounded_or_unsafe_inputs(self) -> None:
        with self.assertRaises(ValueError):
            SearchQuery(query="x", language="en", limit=11)
        with self.assertRaises(ValueError):
            SearchResult("Title", "javascript:alert(1)", "Snippet", 1, "fake")

    def test_public_url_admission_rejects_local_and_credentialed_targets(self) -> None:
        rejected = {
            "ftp://example.com/file",
            "http://localhost/admin",
            "http://localhost.localdomain/admin",
            "http://127.0.0.1/admin",
            "http://10.0.0.1/admin",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]/admin",
            "https://user:secret@example.com/",
            "https://example.com/page#fragment",
            "https://intranet/path",
        }
        for url in rejected:
            with self.subTest(url=url):
                self.assertFalse(is_safe_public_http_url(url))

        self.assertTrue(is_safe_public_http_url("https://example.com/page?q=1"))
        self.assertTrue(is_safe_public_http_url("https://8.8.8.8/"))

    def test_page_fetcher_has_bounded_transparent_offline_contract(self) -> None:
        source = (ROOT / "page_processor.py").read_text(encoding="utf-8")
        forbidden = {
            "ssl=False",
            "nltk.download",
            "USER_AGENTS",
            "random.choice",
            "cleaned_text[:200]",
        }
        required = {
            "allow_redirects=False",
            "MAX_RESPONSE_BYTES",
            "ALLOWED_CONTENT_TYPES",
            "_host_resolves_only_to_public_addresses",
            "is_safe_public_http_url",
            "MAX_REDIRECTS",
            "urljoin",
        }

        self.assertFalse([item for item in forbidden if item in source])
        self.assertFalse([item for item in required if item not in source])

    def test_page_urls_are_canonicalized_before_dedupe(self) -> None:
        self.assertEqual(
            _canonical_page_url("HTTPS://Example.COM/story/#tracking"),
            "https://example.com/story/",
        )

    def test_chunking_preserves_multilingual_sentence_boundaries(self) -> None:
        chunks = chunk_text(
            "Первое предложение. Второе предложение! ثالثة جملة؟", "https://example.com"
        )
        self.assertEqual(len(chunks), 1)
        self.assertIn("ثالثة جملة", chunks[0].text)

    def test_chunking_covers_all_product_locales(self) -> None:
        samples = {
            "en": "First sentence. Second sentence!",
            "es": "Primera frase. Segunda frase!",
            "ru": "Первое предложение. Второе предложение!",
            "pt": "Primeira frase. Segunda frase!",
            "fr": "Première phrase. Deuxième phrase !",
            "de": "Erster Satz. Zweiter Satz!",
            "tr": "İlk cümle. İkinci cümle!",
            "id": "Kalimat pertama. Kalimat kedua!",
        }
        for language, sample in samples.items():
            with self.subTest(language=language):
                chunks = chunk_text(sample, "https://example.com")
                self.assertTrue(chunks)
                self.assertIn(" ".join(sample.split()), " ".join(chunk.text for chunk in chunks))

    def test_sparql_literal_escaping_removes_control_characters(self) -> None:
        escaped = _escape_sparql_literal('name" }\nSERVICE <https://example.com>')

        self.assertNotIn("\n", escaped)
        self.assertIn('\\"', escaped)
        self.assertIn("\\n", escaped)

    async def test_invalid_wikidata_inputs_do_not_reach_network(self) -> None:
        client = FailingHttpClient()

        self.assertIsNone(await get_qid_from_entity(client, "entity", "../../en"))
        self.assertIsNone(await get_qid_from_entity(client, "x" * 257, "en"))
        self.assertEqual(await _get_p31_for_qid(client, "not-a-qid"), [])

    async def test_redirect_fixture_revalidates_target_before_following(self) -> None:
        session = FixtureSession(
            {
                "https://example.com/start": FixtureResponse(
                    302, {"Location": "http://localhost/private"}
                )
            }
        )
        with patch("page_processor._host_resolves_only_to_public_addresses", return_value=True):
            self.assertIsNone(await fetch_page(session, "https://example.com/start"))
        self.assertEqual(session.requested, ["https://example.com/start"])

    async def test_oversized_body_fixture_is_rejected(self) -> None:
        response = SimpleNamespace(
            headers={"Content-Length": "11"},
            content=FixtureContent([b"01234567890"]),
        )
        with self.assertRaises(ValueError):
            await _read_bounded_body(response, limit=10)

    def test_research_logs_do_not_include_response_or_page_content(self) -> None:
        sources = "\n".join(
            (ROOT / name).read_text(encoding="utf-8")
            for name in ("page_processor.py", "entity_lookup.py", "wikidata_fetcher.py")
        )

        self.assertNotIn("response.text", sources)
        self.assertNotIn("cleaned_text[:", sources)
        self.assertNotIn("query: '{query}'", sources)


if __name__ == "__main__":
    unittest.main()
