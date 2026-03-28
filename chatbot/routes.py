"""
Flask Blueprint — wszystkie endpointy chatbota
Rejestruj w app.py: app.register_blueprint(chatbot_bp)
"""
import os
import json
import uuid
import secrets
import hashlib
from datetime import datetime
from functools import wraps
from flask import (
    Blueprint, request, Response, jsonify,
    render_template, stream_with_context, current_app, send_from_directory
)
from flask_login import login_required, current_user
from app import db
from .models import ChatTenant, ChatApiKey, KnowledgeSource, Conversation, ChatMessage
from .rag_service import rag_query
from .ingestion_service import ingest_url_async, ingest_file_async, ingest_manual_async
import werkzeug.utils

chatbot_bp = Blueprint('chatbot', __name__, url_prefix='/chatbot')

UPLOAD_FOLDER = os.getenv('UPLOAD_DIR', './uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# ══════════════════════════════════════════════════════════════
# AUTH HELPERS
# ══════════════════════════════════════════════════════════════

def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def require_api_key(f):
    """Dekorator — weryfikuje API key z widgetu."""
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get('X-Api-Key') or request.args.get('key')
        if not key:
            return jsonify({'error': 'Brak klucza API'}), 401

        key_hash = _hash_key(key)
        api_key = ChatApiKey.query.filter_by(key_value=key_hash, is_active=True).first()
        if not api_key:
            return jsonify({'error': 'Nieprawidłowy klucz API'}), 401

        api_key.last_used = datetime.utcnow()
        db.session.commit()

        request.tenant = api_key.tenant
        return f(*args, **kwargs)
    return decorated


def get_or_create_tenant():
    """Pobierz lub stwórz tenant dla zalogowanego użytkownika."""
    tenant = ChatTenant.query.filter_by(user_id=current_user.id).first()
    if not tenant:
        tenant = ChatTenant(user_id=current_user.id)
        db.session.add(tenant)
        db.session.commit()
    return tenant


# ══════════════════════════════════════════════════════════════
# WIDGET ENDPOINTS (publiczne — używa API key)
# ══════════════════════════════════════════════════════════════

@chatbot_bp.route('/widget.js')
def serve_widget():
    """Serwuje widget.js jako plik statyczny."""
    return send_from_directory(
        os.path.join(os.path.dirname(__file__), '..', 'static'),
        'widget.js',
        mimetype='application/javascript'
    )


@chatbot_bp.route('/api/config')
@require_api_key
def get_config():
    """Widget pobiera konfigurację przy starcie."""
    t = request.tenant
    return jsonify({
        'botName':        t.bot_name,
        'primaryColor':   t.primary_color,
        'welcomeMessage': t.welcome_message,
    })


@chatbot_bp.route('/api/message', methods=['POST'])
@require_api_key
def send_message():
    """
    Główny endpoint czatu — streamuje odpowiedź Claude przez SSE.
    Widget wywołuje POST, dostaje stream text/event-stream.
    """
    data       = request.get_json()
    message    = (data.get('message') or '').strip()
    visitor_id = data.get('visitorId', '')
    conv_id    = data.get('conversationId')
    page_url   = data.get('pageUrl', '')
    tenant     = request.tenant

    if not message:
        return jsonify({'error': 'Wiadomość jest wymagana'}), 400

    # Utwórz lub pobierz konwersację
    if not conv_id:
        conv = Conversation(
            tenant_id=tenant.id,
            visitor_id=visitor_id,
            page_url=page_url,
        )
        db.session.add(conv)
        db.session.commit()
        conv_id = conv.id
    else:
        conv = Conversation.query.get(conv_id)
        if not conv or conv.tenant_id != tenant.id:
            return jsonify({'error': 'Nieznana konwersacja'}), 404

    # Zapisz wiadomość użytkownika
    user_msg = ChatMessage(
        conversation_id=conv_id,
        tenant_id=tenant.id,
        role='user',
        content=message,
    )
    db.session.add(user_msg)
    db.session.commit()

    def generate():
        full_response = ''
        start_ms = datetime.utcnow()

        yield f'event: meta\ndata: {json.dumps({"conversationId": conv_id})}\n\n'

        try:
            stream, sources = rag_query(tenant, message, conv_id)

            with stream as s:
                for event in s:
                    if (event.type == 'content_block_delta'
                            and hasattr(event.delta, 'text')):
                        token = event.delta.text
                        full_response += token
                        yield f'data: {json.dumps({"text": token})}\n\n'

        except Exception as e:
            yield f'event: error\ndata: {json.dumps({"message": str(e)})}\n\n'
            return

        # Zapisz odpowiedź asystenta
        latency = int((datetime.utcnow() - start_ms).total_seconds() * 1000)
        asst_msg = ChatMessage(
            conversation_id=conv_id,
            tenant_id=tenant.id,
            role='assistant',
            content=full_response,
            latency_ms=latency,
        )
        db.session.add(asst_msg)
        db.session.commit()

        yield f'event: done\ndata: {json.dumps({"messageId": asst_msg.id, "sources": sources})}\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':               'no-cache',
            'X-Accel-Buffering':           'no',
            'Access-Control-Allow-Origin': '*',
        },
    )


@chatbot_bp.route('/api/feedback', methods=['POST'])
@require_api_key
def submit_feedback():
    data     = request.get_json()
    msg_id   = data.get('messageId')
    feedback = data.get('feedback')

    if feedback not in ('thumbs_up', 'thumbs_down'):
        return jsonify({'error': 'Nieprawidłowa wartość'}), 400

    msg = ChatMessage.query.filter_by(id=msg_id, tenant_id=request.tenant.id).first()
    if msg:
        msg.feedback = feedback
        db.session.commit()

    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════
# DASHBOARD (wymagane logowanie)
# ══════════════════════════════════════════════════════════════

@chatbot_bp.route('/dashboard')
@login_required
def dashboard():
    """Strona zarządzania chatbotem."""
    tenant = get_or_create_tenant()
    api_keys = ChatApiKey.query.filter_by(tenant_id=tenant.id, is_active=True).all()
    sources  = KnowledgeSource.query.filter_by(tenant_id=tenant.id).order_by(
        KnowledgeSource.created_at.desc()
    ).all()
    conversations = Conversation.query.filter_by(tenant_id=tenant.id).order_by(
        Conversation.created_at.desc()
    ).limit(20).all()

    return render_template(
        'chatbot_dashboard.html',
        tenant=tenant,
        api_keys=api_keys,
        sources=sources,
        conversations=conversations,
    )


@chatbot_bp.route('/dashboard/settings', methods=['POST'])
@login_required
def save_settings():
    tenant = get_or_create_tenant()
    data   = request.get_json()

    allowed = ['bot_name', 'primary_color', 'welcome_message', 'system_prompt', 'tone']
    for key in allowed:
        if key in data:
            setattr(tenant, key, data[key])

    db.session.commit()
    return jsonify({'ok': True})


@chatbot_bp.route('/dashboard/api-keys', methods=['POST'])
@login_required
def create_api_key():
    tenant = get_or_create_tenant()

    raw_key = f'sk_live_{secrets.token_hex(20)}'
    prefix  = raw_key[:12]
    key_hash = _hash_key(raw_key)

    api_key = ChatApiKey(
        tenant_id=tenant.id,
        key_value=key_hash,
        key_prefix=prefix,
        label=request.get_json().get('label', 'Domyślny'),
    )
    db.session.add(api_key)
    db.session.commit()

    # Klucz zwracamy TYLKO RAZ — nie jest przechowywany w plaintext
    return jsonify({'key': raw_key, 'prefix': prefix}), 201


@chatbot_bp.route('/dashboard/api-keys/<key_id>', methods=['DELETE'])
@login_required
def revoke_api_key(key_id):
    tenant = get_or_create_tenant()
    key = ChatApiKey.query.filter_by(id=key_id, tenant_id=tenant.id).first_or_404()
    key.is_active = False
    db.session.commit()
    return jsonify({'ok': True})


@chatbot_bp.route('/dashboard/knowledge/url', methods=['POST'])
@login_required
def add_url():
    tenant = get_or_create_tenant()
    data   = request.get_json()
    url    = data.get('url', '').strip()

    if not url:
        return jsonify({'error': 'URL jest wymagany'}), 400

    source = KnowledgeSource(
        tenant_id=tenant.id,
        type='url',
        name=data.get('name') or url,
        source_url=url,
        status='pending',
    )
    db.session.add(source)
    db.session.commit()

    ingest_url_async(source.id, tenant.id, url, current_app._get_current_object())
    return jsonify({'sourceId': source.id, 'status': 'pending'}), 202


@chatbot_bp.route('/dashboard/knowledge/upload', methods=['POST'])
@login_required
def upload_file():
    tenant = get_or_create_tenant()
    file   = request.files.get('file')

    if not file:
        return jsonify({'error': 'Plik jest wymagany'}), 400

    ext = file.filename.rsplit('.', 1)[-1].lower()
    if ext not in ('pdf', 'txt', 'csv'):
        return jsonify({'error': 'Dozwolone formaty: PDF, TXT, CSV'}), 400

    filename = werkzeug.utils.secure_filename(f'{uuid.uuid4()}.{ext}')
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(file_path)

    source = KnowledgeSource(
        tenant_id=tenant.id,
        type=ext,
        name=file.filename,
        file_path=file_path,
        status='pending',
    )
    db.session.add(source)
    db.session.commit()

    ingest_file_async(source.id, tenant.id, file_path, ext, current_app._get_current_object())
    return jsonify({'sourceId': source.id, 'status': 'pending'}), 202


@chatbot_bp.route('/dashboard/knowledge/manual', methods=['POST'])
@login_required
def add_manual():
    tenant   = get_or_create_tenant()
    data     = request.get_json()
    question = data.get('question', '').strip()
    answer   = data.get('answer', '').strip()

    if not question or not answer:
        return jsonify({'error': 'Pytanie i odpowiedź są wymagane'}), 400

    source = KnowledgeSource(
        tenant_id=tenant.id,
        type='manual',
        name=f'Q&A: {question[:60]}',
        status='pending',
    )
    db.session.add(source)
    db.session.commit()

    text = f'Pytanie: {question}\nOdpowiedź: {answer}'
    ingest_manual_async(source.id, tenant.id, text, current_app._get_current_object())
    return jsonify({'sourceId': source.id}), 202


@chatbot_bp.route('/dashboard/knowledge/<source_id>', methods=['DELETE'])
@login_required
def delete_source(source_id):
    tenant = get_or_create_tenant()
    source = KnowledgeSource.query.filter_by(
        id=source_id, tenant_id=tenant.id
    ).first_or_404()
    db.session.delete(source)
    db.session.commit()
    return jsonify({'ok': True})


@chatbot_bp.route('/dashboard/conversations/<conv_id>/messages')
@login_required
def get_messages(conv_id):
    tenant = get_or_create_tenant()
    conv = Conversation.query.filter_by(id=conv_id, tenant_id=tenant.id).first_or_404()
    return jsonify([
        {
            'id':         m.id,
            'role':       m.role,
            'content':    m.content,
            'feedback':   m.feedback,
            'latency_ms': m.latency_ms,
            'created_at': m.created_at.isoformat(),
        }
        for m in conv.messages
    ])


@chatbot_bp.route('/dashboard/knowledge/sources')
@login_required
def list_sources():
    tenant  = get_or_create_tenant()
    sources = KnowledgeSource.query.filter_by(tenant_id=tenant.id).order_by(
        KnowledgeSource.created_at.desc()
    ).all()
    return jsonify([
        {
            'id':            s.id,
            'type':          s.type,
            'name':          s.name,
            'status':        s.status,
            'chunk_count':   s.chunk_count,
            'error_message': s.error_message,
            'last_synced_at': s.last_synced_at.isoformat() if s.last_synced_at else None,
        }
        for s in sources
    ])
