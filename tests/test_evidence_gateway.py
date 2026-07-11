from __future__ import annotations

import unittest

from brainy_core.evidence import GroundedSynthesizer, SearchGateway
from brainy_core.inference import ChatResult, ProviderModel
from brainy_core.search import SearchQuery, SearchResult


class FakeSearch:
    async def search(self, request: SearchQuery):
        return (
            SearchResult("One", "https://example.com/a#x", "First fact.", 2, "fake"),
            SearchResult("Duplicate", "https://example.com/a", "Same source.", 1, "fake"),
            SearchResult("Two", "https://example.org/b", "Second fact.", 3, "fake"),
        )


class FailingSearch:
    async def search(self, request):
        raise RuntimeError("primary unavailable")


class FallbackSearch:
    async def search(self, request):
        return (
            SearchResult("Fallback", "https://fallback.example/a", "Fallback fact.", 1, "fallback"),
        )


class AlsoFailingSearch:
    async def search(self, request):
        raise RuntimeError("fallback unavailable")


class FakeInference:
    model = ProviderModel("fake", "test", True)

    def __init__(self, citation_id: str = "E1", wrapped: bool = False):
        self.citation_id = citation_id
        self.wrapped = wrapped

    async def chat(self, request):
        payload = f'{{"answer":"Ответ","citation_ids":["{self.citation_id}","unknown","{self.citation_id}"]}}'
        if self.wrapped:
            payload = f"Some preface.```json\n{payload}\n```"
        return ChatResult(
            payload,
            self.model,
            1,
        )


class FakePageChunk:
    def __init__(self, text, source_url):
        self.text = text
        self.source_url = source_url


class EvidenceGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_deduplicates_and_assigns_stable_ids(self):
        bundle = await SearchGateway(FakeSearch(), token_budget=100).build_bundle(
            SearchQuery("question", "ru", 3)
        )
        repeat = await SearchGateway(FakeSearch(), token_budget=100).build_bundle(
            SearchQuery("question", "ru", 3)
        )
        self.assertEqual(
            [item.evidence_id for item in bundle.items],
            [item.evidence_id for item in repeat.items],
        )
        self.assertEqual(
            [item.canonical_url for item in bundle.items],
            ["https://example.com/a", "https://example.org/b"],
        )
        self.assertLessEqual(bundle.estimated_tokens, 100)

    async def test_synthesis_filters_unknown_and_duplicate_citations(self):
        bundle = await SearchGateway(FakeSearch()).build_bundle(SearchQuery("question", "ru"))
        answer = await GroundedSynthesizer(FakeInference(bundle.items[0].evidence_id)).synthesize(
            "question", "ru", bundle
        )
        self.assertEqual(answer.citation_ids, (bundle.items[0].evidence_id,))
        self.assertEqual(answer.citations[0].canonical_url, "https://example.com/a")

    async def test_synthesis_accepts_wrapped_json_without_accepting_unstructured_text(self):
        bundle = await SearchGateway(FakeSearch()).build_bundle(SearchQuery("question", "ru"))
        answer = await GroundedSynthesizer(
            FakeInference(bundle.items[0].evidence_id, wrapped=True)
        ).synthesize("question", "ru", bundle)
        self.assertEqual(answer.citation_ids, (bundle.items[0].evidence_id,))

    async def test_gateway_packs_page_chunks_with_provenance_and_trust(self):
        async def load_pages(urls):
            self.assertEqual(urls, ("https://example.com/a", "https://example.org/b"))
            return [FakePageChunk("Full page context.", urls[0])]

        bundle = await SearchGateway(
            FakeSearch(), token_budget=100, page_loader=load_pages
        ).build_bundle(SearchQuery("question", "en"))
        page = bundle.items[-1]
        self.assertEqual(page.provenance, "page_chunk")
        self.assertEqual(page.trust, "page_content")
        self.assertLessEqual(bundle.estimated_tokens, 100)

    async def test_page_chunks_apply_near_dedupe_and_source_diversity(self):
        async def load_pages(_urls):
            return [
                FakePageChunk("Question answer current policy details.", "https://example.com/a"),
                FakePageChunk("Question answer current policy details.", "https://example.com/b"),
                FakePageChunk(
                    "Independent source reports a different detail.", "https://other.org/c"
                ),
                FakePageChunk("Third same-host context.", "https://example.com/d"),
            ]

        bundle = await SearchGateway(FakeSearch(), page_loader=load_pages).build_bundle(
            SearchQuery("question policy", "en")
        )
        page_items = [item for item in bundle.items if item.provenance == "page_chunk"]
        self.assertEqual(len(page_items), 3)
        self.assertEqual(len({item.canonical_url for item in page_items}), 3)

    async def test_gateway_uses_fallback_only_after_primary_failure(self):
        bundle = await SearchGateway(
            FailingSearch(), fallback_provider=FallbackSearch()
        ).build_bundle(SearchQuery("question", "en"))
        self.assertEqual(bundle.items[0].provenance, "fallback")

    async def test_gateway_surfaces_full_search_failure(self):
        with self.assertRaisesRegex(RuntimeError, "fallback unavailable"):
            await SearchGateway(
                FailingSearch(), fallback_provider=AlsoFailingSearch()
            ).build_bundle(SearchQuery("question", "en"))


if __name__ == "__main__":
    unittest.main()
