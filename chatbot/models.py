"""
Modele SQLAlchemy dla chatbota.
Dorzuć do istniejącej bazy — używają tego samego db co app.py
"""

import uuid
from datetime import datetime
from app import db  # importujemy istniejący db z app.py


def gen_uuid():
    return str(uuid.uuid4())


class ChatTenant(db.Model):
    """Każdy klient który kupił chatbota = jeden tenant"""
    __tablename__ = 'chat_tenants'

    id              = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    user_id         = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    bot_name        = db.Column(db.String(100), default='Asystent')
    primary_color   = db.Column(db.String(7), default='#0071e3')
    welcome_message = db.Column(db.Text, default='Cześć! W czym mogę pomóc?')
    system_prompt   = db.Column(db.Text, default='Jesteś pomocnym asystentem obsługi klienta.')
    tone            = db.Column(db.String(20), default='professional')
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    api_keys        = db.relationship('ChatApiKey', backref='tenant', cascade='all, delete-orphan')
    sources         = db.relationship('KnowledgeSource', backref='tenant', cascade='all, delete-orphan')
    conversations   = db.relationship('Conversation', backref='tenant', cascade='all, delete-orphan')


class ChatApiKey(db.Model):
    """Klucze API do widgetu — jeden tenant może mieć kilka"""
    __tablename__ = 'chat_api_keys'

    id          = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    tenant_id   = db.Column(db.String(36), db.ForeignKey('chat_tenants.id'), nullable=False)
    key_value   = db.Column(db.String(64), unique=True, nullable=False)  # przechowujemy zahashowany
    key_prefix  = db.Column(db.String(16), nullable=False)               # sk_live_XXXX do wyświetlania
    label       = db.Column(db.String(100), default='Domyślny')
    is_active   = db.Column(db.Boolean, default=True)
    last_used   = db.Column(db.DateTime)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)


class KnowledgeSource(db.Model):
    """Źródła wiedzy — URL, PDF, CSV, ręczne Q&A"""
    __tablename__ = 'knowledge_sources'

    id             = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    tenant_id      = db.Column(db.String(36), db.ForeignKey('chat_tenants.id'), nullable=False)
    type           = db.Column(db.String(20), nullable=False)  # url | pdf | csv | txt | manual
    name           = db.Column(db.String(255), nullable=False)
    source_url     = db.Column(db.String(1000))
    file_path      = db.Column(db.String(500))
    status         = db.Column(db.String(20), default='pending')  # pending | processing | ready | failed
    chunk_count    = db.Column(db.Integer, default=0)
    error_message  = db.Column(db.Text)
    last_synced_at = db.Column(db.DateTime)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    chunks = db.relationship('KnowledgeChunk', backref='source', cascade='all, delete-orphan')


class KnowledgeChunk(db.Model):
    """Fragmenty tekstu z embeddingami (pgvector)"""
    __tablename__ = 'knowledge_chunks'

    id          = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    tenant_id   = db.Column(db.String(36), db.ForeignKey('chat_tenants.id'), nullable=False)
    source_id   = db.Column(db.String(36), db.ForeignKey('knowledge_sources.id'), nullable=False)
    content     = db.Column(db.Text, nullable=False)
    metadata_   = db.Column(db.JSON, default=dict)
    # Embedding jako JSON array — pgvector obsługujemy przez raw SQL
    embedding   = db.Column(db.Text)  # przechowujemy jako JSON string
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)


class Conversation(db.Model):
    """Jedna sesja czatu = jedna rozmowa"""
    __tablename__ = 'conversations'

    id          = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    tenant_id   = db.Column(db.String(36), db.ForeignKey('chat_tenants.id'), nullable=False)
    visitor_id  = db.Column(db.String(100), nullable=False)
    page_url    = db.Column(db.String(500))
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    messages = db.relationship('ChatMessage', backref='conversation', cascade='all, delete-orphan',
                               order_by='ChatMessage.created_at')


class ChatMessage(db.Model):
    """Pojedyncza wiadomość w rozmowie"""
    __tablename__ = 'chat_messages'

    id              = db.Column(db.String(36), primary_key=True, default=gen_uuid)
    conversation_id = db.Column(db.String(36), db.ForeignKey('conversations.id'), nullable=False)
    tenant_id       = db.Column(db.String(36), db.ForeignKey('chat_tenants.id'), nullable=False)
    role            = db.Column(db.String(20), nullable=False)  # user | assistant
    content         = db.Column(db.Text, nullable=False)
    feedback        = db.Column(db.String(20))  # thumbs_up | thumbs_down
    latency_ms      = db.Column(db.Integer)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
