"""
Flask Blueprint — wszystkie endpointy chatbota
"""
import os
import json
import uuid
import secrets
import hashlib
from datetime import datetime
from functools import wraps
from flask import (Blueprint, request, Response, jsonify,
                   render_template, stream_with_context, current_app, send_from_directory)
from flask_login import login_required, current_user
import werkzeug.utils

chatbot_bp = Blueprint('chatbot', __name__, url_prefix='/chatbot')

UPLOAD_FOLDER = os.getenv('UPLOAD_DIR', './uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

CORS_HEADERS = {
    'Access-Control-Allow-Origin':  '*',
    'Access-Control-Allow-Headers': 'Content-Type, X-Api-Key',
    'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
}

@chatbot_bp.after_request
def add_cors(response):
    for k, v in CORS_HEADERS.items():
        response.headers[k] = v
    return response

@chatbot_bp.route('/api/<path:path>', methods=['OPTIONS'])
@chatbot_bp.route('/api/message', methods=['OPTIONS'])
@chatbot_bp.route('/api/config', methods=['OPTIONS'])
@chatbot_bp.route('/api/feedback', methods=['OPTIONS'])
def handle_options(path=''):
    return Response('', status=204, headers=CORS_HEADERS)


# ── Helpers ───────────────────────────────────────────────────

def get_db():
    from app import db
    return db

def get_models():
    from chatbot import models as m
    return m

def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# ── Auth ──────────────────────────────────────────────────────

def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get('X-Api-Key') or request.args.get('key')
        if not key:
            return jsonify({'error': 'Brak klucza API'}), 401
        m = get_models()
        api_key = m.ChatApiKey.query.filter_by(
            key_value=_hash_key(key), is_active=True
        ).first()
        if not api_key:
            return jsonify({'error': 'Nieprawidłowy klucz API'}), 401
        api_key.last_used = datetime.utcnow()
        get_db().session.commit()
        request.tenant = api_key.tenant
        return f(*args, **kwargs)
    return decorated


def get_or_create_tenant():
    m = get_models()
    db = get_db()
    tenant = m.ChatTenant.query.filter_by(user_id=current_user.id).first()
    if not tenant:
        tenant = m.ChatTenant(user_id=current_user.id)
        db.session.add(tenant)
        db.session.commit()
    return tenant


# ══════════════════════════════════════════════════════════════
# WIDGET (publiczne)
# ══════════════════════════════════════════════════════════════

@chatbot_bp.route('/widget.js')
def serve_widget():
    return send_from_directory(
        os.path.join(os.path.dirname(__file__), '..', 'static'),
        'widget.js', mimetype='application/javascript'
    )


@chatbot_bp.route('/api/config')
@require_api_key
def get_config():
    t = request.tenant
    return jsonify({
        'botName':        t.bot_name,
        'primaryColor':   t.primary_color,
        'welcomeMessage': t.welcome_message,
    })


@chatbot_bp.route('/api/message', methods=['POST'])
@require_api_key
def send_message():
    from .rag_service import rag_query
    data       = request.get_json()
    message    = (data.get('message') or '').strip()
    visitor_id = data.get('visitorId', '')
    conv_id    = data.get('conversationId')
    page_url   = data.get('pageUrl', '')
    tenant     = request.tenant
    m          = get_models()
    db         = get_db()

    if not message:
        return jsonify({'error': 'Wiadomość wymagana'}), 400

    if not conv_id:
        conv = m.Conversation(tenant_id=tenant.id, visitor_id=visitor_id, page_url=page_url)
        db.session.add(conv)
        db.session.commit()
        conv_id = conv.id
    else:
        conv = m.Conversation.query.filter_by(id=conv_id, tenant_id=tenant.id).first()
        if not conv:
            return jsonify({'error': 'Nieznana konwersacja'}), 404

    user_msg = m.ChatMessage(conversation_id=conv_id, tenant_id=tenant.id,
                             role='user', content=message)
    db.session.add(user_msg)
    db.session.commit()

    def generate():
        full_response = ''
        sources = []
        start = datetime.utcnow()
        yield f'data: {json.dumps({"conversationId": conv_id})}\n\n'
        try:
            stream, sources = rag_query(tenant, message, conv_id)
            with stream as s:
                for text in s.text_stream:
                    full_response += text
                    yield f'data: {json.dumps({"text": text})}\n\n'
        except Exception as e:
            yield f'data: {json.dumps({"error": str(e)})}\n\n'
            return

        latency = int((datetime.utcnow() - start).total_seconds() * 1000)
        asst = m.ChatMessage(conversation_id=conv_id, tenant_id=tenant.id,
                             role='assistant', content=full_response, latency_ms=latency)
        db.session.add(asst)
        db.session.commit()
        yield f'event: done\ndata: {json.dumps({"messageId": asst.id, "sources": sources})}\n\n'

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no',
                             'Access-Control-Allow-Origin': '*'})


@chatbot_bp.route('/api/feedback', methods=['POST'])
@require_api_key
def submit_feedback():
    data     = request.get_json()
    feedback = data.get('feedback')
    if feedback not in ('thumbs_up', 'thumbs_down'):
        return jsonify({'error': 'Nieprawidłowa wartość'}), 400
    m  = get_models()
    db = get_db()
    msg = m.ChatMessage.query.filter_by(
        id=data.get('messageId'), tenant_id=request.tenant.id
    ).first()
    if msg:
        msg.feedback = feedback
        db.session.commit()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════
# DASHBOARD (wymaga logowania)
# ══════════════════════════════════════════════════════════════

@chatbot_bp.route('/dashboard')
@login_required
def dashboard():
    tenant        = get_or_create_tenant()
    m             = get_models()
    api_keys      = m.ChatApiKey.query.filter_by(tenant_id=tenant.id, is_active=True).all()
    sources       = m.KnowledgeSource.query.filter_by(tenant_id=tenant.id).order_by(
                        m.KnowledgeSource.created_at.desc()).all()
    conversations = m.Conversation.query.filter_by(tenant_id=tenant.id).order_by(
                        m.Conversation.created_at.desc()).limit(20).all()
    return render_template('chatbot_dashboard.html', tenant=tenant,
                           api_keys=api_keys, sources=sources, conversations=conversations)


@chatbot_bp.route('/dashboard/settings', methods=['POST'])
@login_required
def save_settings():
    tenant = get_or_create_tenant()
    data   = request.get_json()
    for key in ['bot_name', 'primary_color', 'welcome_message', 'system_prompt', 'tone']:
        if key in data:
            setattr(tenant, key, data[key])
    get_db().session.commit()
    return jsonify({'ok': True})


@chatbot_bp.route('/dashboard/api-keys', methods=['POST'])
@login_required
def create_api_key():
    tenant   = get_or_create_tenant()
    m        = get_models()
    db       = get_db()
    raw_key  = f'sk_live_{secrets.token_hex(20)}'
    prefix   = raw_key[:12]
    api_key  = m.ChatApiKey(tenant_id=tenant.id, key_value=_hash_key(raw_key),
                            key_prefix=prefix,
                            label=request.get_json().get('label', 'Widget'))
    db.session.add(api_key)
    db.session.commit()
    return jsonify({'key': raw_key, 'prefix': prefix}), 201


@chatbot_bp.route('/dashboard/api-keys/<key_id>', methods=['DELETE'])
@login_required
def revoke_api_key(key_id):
    tenant = get_or_create_tenant()
    m      = get_models()
    key    = m.ChatApiKey.query.filter_by(id=key_id, tenant_id=tenant.id).first_or_404()
    key.is_active = False
    get_db().session.commit()
    return jsonify({'ok': True})


@chatbot_bp.route('/dashboard/knowledge/url', methods=['POST'])
@login_required
def add_url():
    from .ingestion_service import ingest_url_async
    tenant = get_or_create_tenant()
    m      = get_models()
    db     = get_db()
    data   = request.get_json()
    url    = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'URL wymagany'}), 400
    source = m.KnowledgeSource(tenant_id=tenant.id, type='url',
                                name=data.get('name') or url, source_url=url)
    db.session.add(source)
    db.session.commit()
    ingest_url_async(source.id, tenant.id, url, current_app._get_current_object())
    return jsonify({'sourceId': source.id}), 202


@chatbot_bp.route('/dashboard/knowledge/upload', methods=['POST'])
@login_required
def upload_file():
    from .ingestion_service import ingest_file_async
    tenant = get_or_create_tenant()
    m      = get_models()
    db     = get_db()
    file   = request.files.get('file')
    if not file:
        return jsonify({'error': 'Plik wymagany'}), 400
    ext = file.filename.rsplit('.', 1)[-1].lower()
    if ext not in ('pdf', 'txt', 'csv'):
        return jsonify({'error': 'Dozwolone: PDF, TXT, CSV'}), 400
    filename  = werkzeug.utils.secure_filename(f'{uuid.uuid4()}.{ext}')
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(file_path)
    source = m.KnowledgeSource(tenant_id=tenant.id, type=ext,
                                name=file.filename, file_path=file_path)
    db.session.add(source)
    db.session.commit()
    ingest_file_async(source.id, tenant.id, file_path, ext, current_app._get_current_object())
    return jsonify({'sourceId': source.id}), 202


@chatbot_bp.route('/dashboard/knowledge/manual', methods=['POST'])
@login_required
def add_manual():
    from .ingestion_service import ingest_manual_async
    tenant   = get_or_create_tenant()
    m        = get_models()
    db       = get_db()
    data     = request.get_json()
    question = data.get('question', '').strip()
    answer   = data.get('answer', '').strip()
    if not question or not answer:
        return jsonify({'error': 'Pytanie i odpowiedź wymagane'}), 400
    source = m.KnowledgeSource(tenant_id=tenant.id, type='manual',
                                name=f'Q&A: {question[:60]}')
    db.session.add(source)
    db.session.commit()
    ingest_manual_async(source.id, tenant.id, f'Pytanie: {question}\nOdpowiedź: {answer}',
                        current_app._get_current_object())
    return jsonify({'sourceId': source.id}), 202


@chatbot_bp.route('/dashboard/knowledge/<source_id>', methods=['DELETE'])
@login_required
def delete_source(source_id):
    tenant = get_or_create_tenant()
    m      = get_models()
    source = m.KnowledgeSource.query.filter_by(
        id=source_id, tenant_id=tenant.id).first_or_404()
    get_db().session.delete(source)
    get_db().session.commit()
    return jsonify({'ok': True})


@chatbot_bp.route('/dashboard/knowledge/sources')
@login_required
def list_sources():
    tenant  = get_or_create_tenant()
    m       = get_models()
    sources = m.KnowledgeSource.query.filter_by(tenant_id=tenant.id).order_by(
                  m.KnowledgeSource.created_at.desc()).all()
    return jsonify([{
        'id': s.id, 'type': s.type, 'name': s.name, 'status': s.status,
        'chunk_count': s.chunk_count, 'error_message': s.error_message,
        'last_synced_at': s.last_synced_at.isoformat() if s.last_synced_at else None,
    } for s in sources])


@chatbot_bp.route('/dashboard/conversations/<conv_id>/messages')
@login_required
def get_messages(conv_id):
    tenant = get_or_create_tenant()
    m      = get_models()
    conv   = m.Conversation.query.filter_by(id=conv_id, tenant_id=tenant.id).first_or_404()
    return jsonify([{
        'id': msg.id, 'role': msg.role, 'content': msg.content,
        'feedback': msg.feedback, 'latency_ms': msg.latency_ms,
        'created_at': msg.created_at.isoformat(),
    } for msg in conv.messages])
