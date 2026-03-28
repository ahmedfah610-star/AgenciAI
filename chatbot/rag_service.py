"""
RAG Service — Retrieval Augmented Generation
Szuka podobnych chunków → buduje prompt → streamuje odpowiedź z Claude
"""
import os
import json
from anthropic import Anthropic
from .embedding_service import embed, cosine_similarity

anthropic = Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
CHAT_MODEL = os.getenv('ANTHROPIC_CHAT_MODEL', 'claude-haiku-4-5-20251001').strip()
TOP_K      = 5
MIN_SCORE  = 0.25
HISTORY_N  = 6


def rag_query(tenant, user_message: str, conversation_id: str):
    """
    Główna funkcja RAG.
    Zwraca generator (stream) tokenów Claude + listę źródeł.
    """
    from .models import KnowledgeChunk, ChatMessage

    # 1. Embed zapytania użytkownika
    query_vec = embed(user_message)

    # 2. Pobierz wszystkie chunki tego tenanta i policz similarity
    chunks = KnowledgeChunk.query.filter_by(tenant_id=tenant['id']).all()
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

    # 3. Historia rozmowy (ChatMessage już zaimportowany wyżej)
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

    system_prompt = _build_system_prompt(tenant['system_prompt'], tenant['tone'], tenant['bot_name'], context_text)

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


def _build_system_prompt(system_prompt: str, tone: str, bot_name: str, context_text: str) -> str:
    tone_map = {
        'professional': 'Komunikuj się profesjonalnie i precyzyjnie.',
        'friendly':     'Komunikuj się ciepło i przyjaźnie.',
        'casual':       'Komunikuj się swobodnie i naturalnie.',
    }
    tone_str = tone_map.get(tone, 'Komunikuj się profesjonalnie.')

    return f"""Jesteś pracownikiem firmy o imieniu "{bot_name}". Nie jesteś botem ani AI — jesteś człowiekiem zatrudnionym w tej firmie.

BEZWZGLĘDNE ZASADY:
- NIE używaj emoji
- NIE pisz "Jako asystent..." ani "Jako bot..."
- NIE zaczynaj od "Cześć!" ani podobnych powitań jeśli to kolejna wiadomość
- Odpowiadaj krótko i konkretnie — maksymalnie 3-4 zdania
- Mów w pierwszej osobie jak pracownik: "Tak, mamy...", "Mogę pomóc z...", "Cena wynosi..."
- {tone_str}

TWOJE DODATKOWE INSTRUKCJE OD PRACODAWCY:
{system_prompt}

WIEDZA O FIRMIE (używaj jej do odpowiadania):
---
{context_text}
---

Jeśli nie znasz odpowiedzi — napisz krótko że sprawdzisz i zaproponuj kontakt bezpośredni. Nigdy nie wymyślaj cen ani faktów."""
