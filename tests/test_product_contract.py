from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProductContractTests(unittest.TestCase):
    def test_legacy_quality_assets_have_a_recovery_index(self) -> None:
        audit = (ROOT / "docs" / "LEGACY_QUALITY_AUDIT.md").read_text(encoding="utf-8")

        required = {
            "3275525",
            "get_sub_queries",
            "get_research_steps",
            "generate_summary_from_chunks",
            "polish_research_answer",
            "summarize_research_chunk",
            "EvidenceBundle",
            "citation_ids",
            "ResearchOverview",
            "40–80",
            "10–200",
            "100–300",
            "до 10 queries/steps",
            "Apriel Thinker 15B",
        }
        self.assertFalse(sorted(item for item in required if item not in audit))

    def test_web_on_plan_preserves_context_enrichment(self) -> None:
        plan = (ROOT / "docs" / "EXECUTION_PLAN.md").read_text(encoding="utf-8")
        strategy = (ROOT / "docs" / "PRODUCT_STRATEGY.md").read_text(encoding="utf-8")

        plan_requirements = {
            "EvidenceBundle",
            "SERP snippets",
            "spaCy",
            "Wikidata",
            "semantic rerank",
            "near duplicates",
            "token budget",
            "evidence IDs",
            "map/reduce",
            "Apriel Thinker 15B",
        }
        strategy_requirements = {
            "SearchGateway -> EvidenceBundle",
            "multilingual",
            "semantic rerank",
            "citation IDs",
            "Apriel Thinker 15B",
        }

        self.assertFalse(sorted(item for item in plan_requirements if item not in plan))
        self.assertFalse(sorted(item for item in strategy_requirements if item not in strategy))


if __name__ == "__main__":
    unittest.main()
