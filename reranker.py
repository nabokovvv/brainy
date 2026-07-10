from __future__ import annotations

import torch
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
import logging

logger = logging.getLogger(__name__)
MAX_RERANK_CHUNKS = 64
MAX_QUERY_CHARS = 2_000
MAX_CHUNK_CHARS = 4_000


class Reranker:
    def __init__(self, model_name):
        # Auto-detect the best available device to make the script universal.
        if torch.backends.mps.is_available():
            device = "mps"
            logger.info("Reranker is using MPS device (Apple Silicon GPU).")
        elif torch.cuda.is_available():
            device = "cuda"
            logger.info("Reranker is using CUDA device (Nvidia GPU).")
        else:
            device = "cpu"
            logger.info("Reranker is using CPU.")

        self.model = SentenceTransformer(model_name, device=device)

    def rerank(self, query: str, chunks: list, top_n: int, threshold: float = 0.0) -> list:
        if not chunks or top_n <= 0:
            return []
        candidates = chunks[:MAX_RERANK_CHUNKS]
        query_embedding = self.model.encode(
            [query[:MAX_QUERY_CHARS]],
            show_progress_bar=False,
        )
        chunk_embeddings = self.model.encode(
            [chunk.text[:MAX_CHUNK_CHARS] for chunk in candidates],
            show_progress_bar=False,
        )

        similarities = cosine_similarity(query_embedding, chunk_embeddings)[0]

        ranked_chunks = sorted(
            [(chunk, sim) for chunk, sim in zip(candidates, similarities) if sim >= threshold],
            key=lambda x: x[1],
            reverse=True,
        )

        return [chunk for chunk, sim in ranked_chunks[:top_n]]
