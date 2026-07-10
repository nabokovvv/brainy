from __future__ import annotations

import unittest
from pathlib import Path

from brainy_core.web_safety import is_safe_public_http_url
from wikidata_mapper import _escape_sparql_literal, _get_p31_for_qid, get_qid_from_entity


ROOT = Path(__file__).resolve().parents[1]


class FailingHttpClient:
    async def get(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("invalid input must fail before network I/O")


class ResearchContractTests(unittest.IsolatedAsyncioTestCase):
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
        }

        self.assertFalse([item for item in forbidden if item in source])
        self.assertFalse([item for item in required if item not in source])

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
