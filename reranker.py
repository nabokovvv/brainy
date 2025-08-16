import torch
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
from page_processor import TextChunk
import logging

logger = logging.getLogger(__name__)

class Reranker:
    def __init__(self, model_name):
        # Auto-detect the best available device to make the script universal.
        if torch.backends.mps.is_available():
            device = 'mps'
            logger.info("Reranker is using MPS device (Apple Silicon GPU).")
        elif torch.cuda.is_available():
            device = 'cuda'
            logger.info("Reranker is using CUDA device (Nvidia GPU).")
        else:
            device = 'cpu'
            logger.info("Reranker is using CPU.")
            
        self.model = SentenceTransformer(model_name, device=device)

    def rerank(self, query: str, chunks: list, top_n: int, threshold: float = 0.0) -> list:
        if not chunks:
            return []
        query_embedding = self.model.encode([query])
        chunk_embeddings = self.model.encode([chunk.text for chunk in chunks])

        similarities = cosine_similarity(query_embedding, chunk_embeddings)[0]

        ranked_chunks = sorted(
            [(chunk, sim) for chunk, sim in zip(chunks, similarities) if sim >= threshold],
            key=lambda x: x[1],
            reverse=True
        )

        return [chunk for chunk, sim in ranked_chunks[:top_n]]
