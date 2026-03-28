"""
RAG Service — Retrieval Augmented Generation
Szuka podobnych chunków → buduje prompt → streamuje odpowiedź z Claude
"""
import os
import json
from anthropic import Anthropic
from .embedding_service import embed, cosine_similarity
from .models import KnowledgeChunk, ChatMessage, Conversation

anthropic = Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
CHAT_MODEL = os.getenv('ANTHROPIC_CHAT_MODEL', 'claude-haiku-4-5-20251001')
TOP_K      = 5
MIN_SCORE  = 0.25
HISTORY_N  = 6


def rag_query(tenant, user_message: str, conversation_id: str):
    """
    Główna funkcja RAG.
    Zwraca generator (stream) tokenów Claude + listę źródeł.
    """
    # 1. Embed zapytania użytkownika
    query_vec = embed(user_message)

    # 2. Pobierz wszystkie chunki tego tenanta i policz similarity
    chunks = KnowledgeChunk.query.filter_by(tenant_id=tenant.id).all()
    scored = []
    for chunk in chunks:
        if not chunk.embedding:
            continue
        chunk_vec = json.loads(chunk.embedding)
        score = cosine_similarity(query_vec, chunk_vec)
        if score >= MIN_SCORE:
            scored.append((score, chunk))

    # Posortuj i weź TOP_K
    scored.sort(key=lambda x: x[0], reverse=True)
    top_chunks = scored[:TOP_K]

    # 3. Historia rozmowy
    history_msgs = ChatMessage.query.filter_by(
        conversation_id=conversation_id
    ).order_by(ChatMessage.created_at.desc()).limit(HISTORY_N).all()

    history = [
        {'role': m.role, 'content': m.content}
        for m in reversed(history_msgs)
        if m.role in ('user', 'assistant')
    ]

    # 4. Zbuduj system prompt z kontekstem
    context_text = '\n\n'.join(
        f'[{i+1}] {chunk.content}'
        for i, (_, chunk) in enumerate(top_chunks)
    ) or 'Brak dokumentów w bazie wiedzy.'

    system_prompt = _build_system_prompt(tenant, context_text)

    # 5. Stream z Claude
    stream = anthropic.messages.stream(
        model=CHAT_MODEL,
        max_tokens=800,
        system=system_prompt,
        messages=[
            *history,
            {'role': 'user', 'content': user_message},
        ],
    )

    sources = [
        {
            'score': round(score, 3),
            'preview': chunk.content[:120],
        }
        for score, chunk in top_chunks
    ]

    return stream, sources


def _build_system_prompt(tenant, context_text: str) -> str:
    tone_map = {
        'professional': 'Komunikuj się profesjonalnie i precyzyjnie.',
        'friendly':     'Komunikuj się ciepło i przyjaźnie.',
        'casual':       'Komunikuj się swobodnie i naturalnie.',
    }
    tone = tone_map.get(tenant.tone, 'Komunikuj się profesjonalnie.')

    return f"""{tenant.system_prompt}

{tone}

KONTEKST Z BAZY WIEDZY:
---
{context_text}
---

ZASADY:
- Odpowiadaj TYLKO na podstawie podanego kontekstu jeśli to możliwe.
- Jeśli nie znasz odpowiedzi, powiedz to szczerze i zaproponuj kontakt z obsługą.
- Bądź zwięzły — max 150 słów chyba że pytanie wymaga szczegółów.
- Nie wymyślaj cen, faktów ani polityk których nie ma w kontekście.
- Jesteś "{tenant.bot_name}", asystentem tej firmy."""
