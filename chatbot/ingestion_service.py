"""
Ingestion Service — wczytuje treści i tworzy embeddingi
Obsługuje: URL, PDF, CSV, TXT, ręczne Q&A
"""
import os
import io
import json
import threading
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from app import db
from .models import KnowledgeSource, KnowledgeChunk
from .embedding_service import embed_batch

CHUNK_SIZE    = int(os.getenv('CHUNK_SIZE', 800))
CHUNK_OVERLAP = int(os.getenv('CHUNK_OVERLAP', 100))


# ── Publiczne funkcje — uruchamiają w tle ────────────────────

def ingest_url_async(source_id: str, tenant_id: str, url: str, app):
    """Uruchamia ingestion URL w osobnym wątku."""
    t = threading.Thread(
        target=_run_in_context,
        args=(app, _ingest_url, source_id, tenant_id, url),
        daemon=True,
    )
    t.start()


def ingest_file_async(source_id: str, tenant_id: str, file_path: str, file_type: str, app):
    """Uruchamia ingestion pliku w osobnym wątku."""
    t = threading.Thread(
        target=_run_in_context,
        args=(app, _ingest_file, source_id, tenant_id, file_path, file_type),
        daemon=True,
    )
    t.start()


def ingest_manual_async(source_id: str, tenant_id: str, text: str, app):
    """Uruchamia ingestion ręcznego Q&A w osobnym wątku."""
    t = threading.Thread(
        target=_run_in_context,
        args=(app, _ingest_manual, source_id, tenant_id, text),
        daemon=True,
    )
    t.start()


# ── Wewnętrzne funkcje ───────────────────────────────────────

def _run_in_context(app, fn, *args):
    """Uruchamia funkcję w kontekście aplikacji Flask (wymagane dla db)."""
    with app.app_context():
        try:
            fn(*args)
        except Exception as e:
            print(f'[Ingestion] Błąd: {e}')


def _ingest_url(source_id, tenant_id, url):
    _set_status(source_id, 'processing')
    try:
        text = _scrape_url(url)
        _process_text(source_id, tenant_id, text, {'url': url})
    except Exception as e:
        _set_status(source_id, 'failed', str(e))


def _ingest_file(source_id, tenant_id, file_path, file_type):
    _set_status(source_id, 'processing')
    try:
        if file_type == 'pdf':
            text = _extract_pdf(file_path)
        elif file_type == 'csv':
            text = _extract_csv(file_path)
        else:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
        _process_text(source_id, tenant_id, text, {'file': os.path.basename(file_path)})
        # Usuń plik tymczasowy
        try:
            os.unlink(file_path)
        except Exception:
            pass
    except Exception as e:
        _set_status(source_id, 'failed', str(e))


def _ingest_manual(source_id, tenant_id, text):
    _set_status(source_id, 'processing')
    try:
        _process_text(source_id, tenant_id, text, {'type': 'manual'})
    except Exception as e:
        _set_status(source_id, 'failed', str(e))


def _process_text(source_id, tenant_id, text, metadata):
    """Chunk → embed → zapisz do bazy."""
    chunks = _split_chunks(text, CHUNK_SIZE, CHUNK_OVERLAP)
    if not chunks:
        raise ValueError('Brak treści do przetworzenia')

    # Usuń stare chunki (re-sync)
    KnowledgeChunk.query.filter_by(source_id=source_id).delete()
    db.session.commit()

    # Batch embedding
    BATCH = 50
    total = 0
    for i in range(0, len(chunks), BATCH):
        batch = chunks[i:i + BATCH]
        embeddings = embed_batch(batch)
        for j, (chunk_text, vec) in enumerate(zip(batch, embeddings)):
            chunk = KnowledgeChunk(
                tenant_id=tenant_id,
                source_id=source_id,
                content=chunk_text,
                metadata_={**metadata, 'index': i + j},
                embedding=json.dumps(vec),
            )
            db.session.add(chunk)
            total += 1
        db.session.commit()

    # Zaktualizuj status źródła
    source = KnowledgeSource.query.get(source_id)
    if source:
        source.status = 'ready'
        source.chunk_count = total
        source.last_synced_at = datetime.utcnow()
        source.error_message = None
        db.session.commit()


# ── Ekstrakcja tekstu ────────────────────────────────────────

def _normalize_url(href: str) -> str:
    """Usuwa trailing slash i fragment żeby uniknąć duplikatów."""
    return href.rstrip('/')


def _scrape_url(url: str, max_pages: int = 10) -> str:
    """Scrapuje stronę główną + podstrony (max_pages)."""
    from urllib.parse import urljoin, urlparse

    base = urlparse(url)
    base_origin = f'{base.scheme}://{base.netloc}'
    visited = set()
    product_queue = []   # strony produktów — priorytet
    other_queue = [_normalize_url(url)]
    all_texts = []

    headers = {'User-Agent': 'Mozilla/5.0 (compatible; AgenciAI-Bot/1.0)'}

    def next_url():
        if product_queue:
            return product_queue.pop(0)
        if other_queue:
            return other_queue.pop(0)
        return None

    while len(visited) < max_pages:
        current = next_url()
        if current is None:
            break
        if current in visited:
            continue
        visited.add(current)

        try:
            resp = requests.get(current, timeout=15, headers=headers, allow_redirects=True)
            if not resp.ok:
                continue
            ct = resp.headers.get('Content-Type', '')
            if 'text/html' not in ct:
                continue

            soup = BeautifulSoup(resp.text, 'html.parser')

            # Zbierz linki wewnętrzne
            for a in soup.find_all('a', href=True):
                href = _normalize_url(
                    urljoin(current, a['href']).split('#')[0].split('?')[0]
                )
                if not href.startswith(base_origin):
                    continue
                if href in visited:
                    continue
                # Produkty i ważne podstrony na przód kolejki
                if any(seg in href for seg in ['/produkt', '/product', '/sklep', '/shop', '/kontakt', '/contact', '/cennik', '/dostawa']):
                    if href not in product_queue:
                        product_queue.append(href)
                else:
                    if href not in other_queue:
                        other_queue.append(href)

            # Wyciągnij tekst
            for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
                tag.decompose()
            title = soup.title.string.strip() if soup.title else current
            body = soup.get_text(separator='\n', strip=True)
            all_texts.append(f'=== {title} ===\nURL: {current}\n{body}')
            print(f'[Crawler] {len(visited)}/{max_pages} — {current}')

        except Exception as e:
            print(f'[Crawler] Błąd {current}: {e}')
            continue

    print(f'[Crawler] Zakończono — przeskanowano {len(visited)} stron')
    return '\n\n'.join(all_texts)


def _extract_pdf(file_path: str) -> str:
    import pdf2image
    import pytesseract
    from PIL import Image
    # Prosta ekstrakcja przez pdfminer jeśli dostępna
    try:
        from pdfminer.high_level import extract_text
        return extract_text(file_path)
    except ImportError:
        return ''


def _extract_csv(file_path: str) -> str:
    import csv
    rows = []
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(' | '.join(f'{k}: {v}' for k, v in row.items()))
    return '\n'.join(rows)


# ── Chunking ─────────────────────────────────────────────────

def _split_chunks(text: str, size: int, overlap: int) -> list[str]:
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = ' '.join(words[i:i + size])
        if len(chunk.strip()) > 20:
            chunks.append(chunk)
        i += size - overlap
        if i + size > len(words) and i < len(words):
            last = ' '.join(words[i:])
            if len(last.strip()) > 20:
                chunks.append(last)
            break
    return chunks


# ── Helper ───────────────────────────────────────────────────

def _set_status(source_id: str, status: str, error: str = None):
    source = KnowledgeSource.query.get(source_id)
    if source:
        source.status = status
        source.error_message = error
        db.session.commit()
