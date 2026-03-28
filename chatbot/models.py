"""
Modele SQLAlchemy dla chatbota.
"""

import uuid
from datetime import datetime


def gen_uuid():
    return str(uuid.uuid4())


def init_models(db):
    """
    Tworzy klasy modeli używając przekazanego db.
    Wywołane z app.py po inicjalizacji db.
    """

    class ChatTenant(db.Model):
        __tablename__ = 'chat_tenants'

        id              = db.Column(db.String(36), primary_key=True, default=gen_uuid)
        user_id         = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
        bot_name        = db.Column(db.String(100), default='Specjalista ds. obsługi klienta')
        primary_color   = db.Column(db.String(7), default='#0071e3')
        welcome_message = db.Column(db.Text, default='Cześć! W czym mogę pomóc?')
        system_prompt   = db.Column(db.Text, default='Jesteś pomocnym asystentem obsługi klienta.')
        tone            = db.Column(db.String(20), default='professional')
        created_at      = db.Column(db.DateTime, default=datetime.utcnow)

        api_keys      = db.relationship('ChatApiKey',      backref='tenant', cascade='all, delete-orphan')
        sources       = db.relationship('KnowledgeSource', backref='tenant', cascade='all, delete-orphan')
        conversations = db.relationship('Conversation',    backref='tenant', cascade='all, delete-orphan')

    class ChatApiKey(db.Model):
        __tablename__ = 'chat_api_keys'

        id          = db.Column(db.String(36), primary_key=True, default=gen_uuid)
        tenant_id   = db.Column(db.String(36), db.ForeignKey('chat_tenants.id'), nullable=False)
        key_value   = db.Column(db.String(64), unique=True, nullable=False)
        key_prefix  = db.Column(db.String(16), nullable=False)
        label       = db.Column(db.String(100), default='Domyślny')
        is_active   = db.Column(db.Boolean, default=True)
        last_used   = db.Column(db.DateTime)
        created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    class KnowledgeSource(db.Model):
        __tablename__ = 'knowledge_sources'

        id             = db.Column(db.String(36), primary_key=True, default=gen_uuid)
        tenant_id      = db.Column(db.String(36), db.ForeignKey('chat_tenants.id'), nullable=False)
        type           = db.Column(db.String(20), nullable=False)
        name           = db.Column(db.String(255), nullable=False)
        source_url     = db.Column(db.String(1000))
        file_path      = db.Column(db.String(500))
        status         = db.Column(db.String(20), default='pending')
        chunk_count    = db.Column(db.Integer, default=0)
        error_message  = db.Column(db.Text)
        last_synced_at = db.Column(db.DateTime)
        created_at     = db.Column(db.DateTime, default=datetime.utcnow)

        chunks = db.relationship('KnowledgeChunk', backref='source', cascade='all, delete-orphan')

    class KnowledgeChunk(db.Model):
        __tablename__ = 'knowledge_chunks'

        id          = db.Column(db.String(36), primary_key=True, default=gen_uuid)
        tenant_id   = db.Column(db.String(36), db.ForeignKey('chat_tenants.id'), nullable=False)
        source_id   = db.Column(db.String(36), db.ForeignKey('knowledge_sources.id'), nullable=False)
        content     = db.Column(db.Text, nullable=False)
        metadata_   = db.Column(db.JSON, default=dict)
        embedding   = db.Column(db.Text)   # JSON string z listą floatów
        created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    class Conversation(db.Model):
        __tablename__ = 'conversations'

        id          = db.Column(db.String(36), primary_key=True, default=gen_uuid)
        tenant_id   = db.Column(db.String(36), db.ForeignKey('chat_tenants.id'), nullable=False)
        visitor_id  = db.Column(db.String(100), nullable=False)
        page_url    = db.Column(db.String(500))
        created_at  = db.Column(db.DateTime, default=datetime.utcnow)

        messages = db.relationship('ChatMessage', backref='conversation',
                                   cascade='all, delete-orphan',
                                   order_by='ChatMessage.created_at')

    class ChatMessage(db.Model):
        __tablename__ = 'chat_messages'

        id              = db.Column(db.String(36), primary_key=True, default=gen_uuid)
        conversation_id = db.Column(db.String(36), db.ForeignKey('conversations.id'), nullable=False)
        tenant_id       = db.Column(db.String(36), db.ForeignKey('chat_tenants.id'), nullable=False)
        role            = db.Column(db.String(20), nullable=False)
        content         = db.Column(db.Text, nullable=False)
        feedback        = db.Column(db.String(20))
        latency_ms      = db.Column(db.Integer)
        created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    # Udostępnij klasy globalnie w module
    import chatbot.models as _m
    _m.ChatTenant      = ChatTenant
    _m.ChatApiKey      = ChatApiKey
    _m.KnowledgeSource = KnowledgeSource
    _m.KnowledgeChunk  = KnowledgeChunk
    _m.Conversation    = Conversation
    _m.ChatMessage     = ChatMessage


# Placeholdery — zostaną zastąpione przez init_models()
ChatTenant      = None
ChatApiKey      = None
KnowledgeSource = None
KnowledgeChunk  = None
Conversation    = None
ChatMessage     = None
