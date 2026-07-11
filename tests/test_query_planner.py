from __future__ import annotations

import unittest

from brainy_core.evidence import SearchGateway
from brainy_core.inference import ChatResult, ProviderModel
from brainy_core.query_planner import (
    MAX_QUERY_CHARS,
    build_planner_request,
    fallback_query,
    is_passthrough_query,
    parse_planned_queries,
    plan_search_queries,
)
from brainy_core.search import SearchQuery, SearchResult


class FakeInference:
    model = ProviderModel("fake", "test", True)

    def __init__(self, answer: str):
        self.answer = answer
        self.calls = 0

    async def chat(self, request):
        self.calls += 1
        self.last_request = request
        return ChatResult(self.answer, self.model, 1)


class FailingInference:
    model = ProviderModel("fake", "test", True)

    async def chat(self, request):
        raise RuntimeError("provider down")


class QueryPlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_message_passes_through_without_llm_call(self):
        provider = FakeInference("should not be used")
        queries = await plan_search_queries("погода в Лиссабоне завтра", "ru", provider)
        self.assertEqual(queries, ("погода в Лиссабоне завтра",))
        self.assertEqual(provider.calls, 0)

    def test_multi_sentence_message_is_not_passthrough(self):
        self.assertFalse(is_passthrough_query("Привет! Расскажи про погоду."))
        self.assertTrue(is_passthrough_query("best pizza in Lisbon?"))

    async def test_long_message_is_rewritten_to_one_query(self):
        provider = FakeInference('  "Mac mini M4 16GB llama.cpp benchmark"  ')
        message = (
            "Слушай, у меня есть Mac mini M4 с 16 гигабайтами памяти, и я хочу понять, "
            "какие бенчмарки llama.cpp на нем показывают в интернете."
        )
        queries = await plan_search_queries(message, "ru", provider)
        self.assertEqual(queries, ("Mac mini M4 16GB llama.cpp benchmark",))
        self.assertEqual(provider.calls, 1)

    async def test_second_query_requires_very_long_message(self):
        provider = FakeInference("first query\nsecond query")
        medium = "x" * 200 + ". И еще вопрос про другое."
        self.assertEqual(await plan_search_queries(medium, "ru", provider), ("first query",))
        long_message = "y" * 501 + ". И еще вопрос."
        self.assertEqual(
            await plan_search_queries(long_message, "ru", provider),
            ("first query", "second query"),
        )

    async def test_planner_failure_falls_back_to_capped_raw_message(self):
        message = "слово " * 100
        queries = await plan_search_queries(message, "ru", FailingInference())
        self.assertEqual(len(queries), 1)
        self.assertLessEqual(len(queries[0]), MAX_QUERY_CHARS)
        self.assertTrue(queries[0].startswith("слово"))

    def test_parse_strips_think_blocks_numbering_and_duplicates(self):
        parsed = parse_planned_queries(
            "<think>reasoning</think>\n1. eiffel tower height\n2) Eiffel Tower Height\n- other",
            "original message",
            allow_second=True,
        )
        self.assertEqual(parsed, ("eiffel tower height", "other"))

    def test_parse_empty_output_falls_back(self):
        parsed = parse_planned_queries("<think>only thoughts</think>", "raw text", allow_second=False)
        self.assertEqual(parsed, ("raw text",))

    def test_fallback_query_normalizes_whitespace(self):
        self.assertEqual(fallback_query("  a\n b   c "), "a b c")

    def test_planner_request_is_bounded_and_cold(self):
        request = build_planner_request("message", "en", allow_second=False)
        self.assertLessEqual(request.max_output_tokens, 100)
        self.assertEqual(request.temperature, 0.0)


class RecordingSearch:
    def __init__(self):
        self.queries: list[str] = []

    async def search(self, request: SearchQuery):
        self.queries.append(request.query)
        index = len(self.queries)
        return (
            SearchResult(
                f"Title {index}",
                f"https://example.com/{request.query.replace(' ', '-')}",
                f"Fact for {request.query}.",
                1,
                "fake",
            ),
        )


class OneFailingSearch(RecordingSearch):
    async def search(self, request: SearchQuery):
        if request.query == "bad":
            raise RuntimeError("backend down")
        return await super().search(request)


class MultiQueryGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_merges_and_dedupes_two_queries(self):
        search = RecordingSearch()
        bundle = await SearchGateway(search).build_bundle(
            SearchQuery("first", "en"), (SearchQuery("second", "en"),)
        )
        self.assertEqual(sorted(search.queries), ["first", "second"])
        self.assertEqual(
            sorted(item.canonical_url for item in bundle.items),
            ["https://example.com/first", "https://example.com/second"],
        )

    async def test_gateway_survives_one_failed_query(self):
        bundle = await SearchGateway(OneFailingSearch()).build_bundle(
            SearchQuery("bad", "en"), (SearchQuery("good", "en"),)
        )
        self.assertEqual(
            [item.canonical_url for item in bundle.items], ["https://example.com/good"]
        )


if __name__ == "__main__":
    unittest.main()
