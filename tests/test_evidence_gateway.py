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

    def __init__(self, answer: str = "Ответ на основе контекста."):
        self.answer = answer
        self.last_request = None

    async def chat(self, request):
        self.last_request = request
        return ChatResult(self.answer, self.model, 1)


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

    async def test_synthesis_returns_prose_with_top_ranked_citations(self):
        bundle = await SearchGateway(FakeSearch()).build_bundle(SearchQuery("question", "ru"))
        synthesizer = GroundedSynthesizer(FakeInference("Краткий ответ."))
        answer = await synthesizer.synthesize("question", "ru", bundle)
        self.assertEqual(answer.answer, "Краткий ответ.")
        # Citations are the top-ranked retrieved sources, chosen by the app.
        expected = synthesizer.select_citations(bundle)
        self.assertEqual(answer.citations, expected)
        self.assertEqual(answer.citations[0].canonical_url, "https://example.com/a")

    async def test_synthesis_prompt_excludes_ids_and_urls(self):
        bundle = await SearchGateway(FakeSearch()).build_bundle(SearchQuery("question", "ru"))
        request = GroundedSynthesizer(FakeInference()).build_request("question", "ru", bundle)
        context = request.messages[-1].content
        for item in bundle.items:
            self.assertIn(item.text, context)
            self.assertNotIn(item.evidence_id, context)
            self.assertNotIn(item.canonical_url, context)

    async def test_history_is_inserted_between_system_and_web_context_turn(self):
        from brainy_core.inference import ChatMessage

        bundle = await SearchGateway(FakeSearch()).build_bundle(SearchQuery("question", "en"))
        history = (
            ChatMessage(role="user", content="earlier question"),
            ChatMessage(role="assistant", content="earlier answer"),
        )
        request = GroundedSynthesizer(FakeInference()).build_request(
            "question", "en", bundle, history=history
        )

        roles = [m.role for m in request.messages]
        self.assertEqual(roles[0], "system")
        self.assertEqual(roles[1:3], ["user", "assistant"])
        self.assertEqual(request.messages[1].content, "earlier question")
        self.assertEqual(request.messages[2].content, "earlier answer")
        self.assertEqual(request.messages[-1].role, "user")
        self.assertIn("Web context:", request.messages[-1].content)

    async def test_detailed_synthesis_has_a_larger_bounded_output(self):
        bundle = await SearchGateway(FakeSearch()).build_bundle(SearchQuery("question", "en"))
        synthesizer = GroundedSynthesizer(FakeInference())

        regular = synthesizer.build_request("question", "en", bundle)
        detailed = synthesizer.build_request("question", "en", bundle, detailed=True)

        self.assertEqual(regular.max_output_tokens, 500)
        self.assertEqual(detailed.max_output_tokens, 900)
        self.assertIn("Compare sources", detailed.messages[0].content)

    async def test_synthesis_strips_think_trace_and_rejects_empty(self):
        bundle = await SearchGateway(FakeSearch()).build_bundle(SearchQuery("question", "ru"))
        answer = await GroundedSynthesizer(
            FakeInference("<think>hidden</think>Видимый ответ.")
        ).synthesize("question", "ru", bundle)
        self.assertEqual(answer.answer, "Видимый ответ.")
        with self.assertRaises(ValueError):
            await GroundedSynthesizer(FakeInference("<think>only</think>")).synthesize(
                "question", "ru", bundle
            )

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
