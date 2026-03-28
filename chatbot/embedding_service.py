"""
Voyage AI — embeddingi dla RAG pipeline
"""
import os
import json
import requests

VOYAGE_API_URL = 'https://api.voyageai.com/v1/embeddings'
MODEL          = os.getenv('VOYAGE_EMBEDDING_MODEL', 'voyage-3-lite')
BATCH_SIZE     = 128


def embed(text: str) -> list[float]:
    """Embed jednego tekstu."""
    clean = text.replace('\n', ' ').strip()[:16000]
    return _voyage_request([clean])[0]


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed wielu tekstów z automatycznym batchingiem."""
    clean = [t.replace('\n', ' ').strip()[:16000] for t in texts]
    results = []
    for i in range(0, len(clean), BATCH_SIZE):
        batch = clean[i:i + BATCH_SIZE]
        results.extend(_voyage_request(batch))
    return results


def _voyage_request(inputs: list[str]) -> list[list[float]]:
    resp = requests.post(
        VOYAGE_API_URL,
        json={'input': inputs, 'model': MODEL},
        headers={
            'Authorization': f'Bearer {os.getenv("VOYAGE_API_KEY")}',
            'Content-Type': 'application/json',
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()['data']
    data.sort(key=lambda x: x['index'])
    return [d['embedding'] for d in data]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Podobieństwo cosinusowe — do szukania w pamięci jeśli nie ma pgvector."""
    import math
    dot   = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(y * y for y in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)
