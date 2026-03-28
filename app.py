"""
WooCommerce + Allegro AI Product Agent — SaaS Backend
Stack: Python 3.11+, Flask, Flask-Login, SQLAlchemy, Anthropic SDK
"""

import os
import base64
import json
import re
import time
import secrets
import requests
from datetime import datetime
from flask import (Flask, request, jsonify, send_from_directory,
                   redirect, url_for, session)
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user,
                         logout_user, login_required, current_user)
from anthropic import Anthropic
from dotenv import load_dotenv
import bcrypt

load_dotenv()

app = Flask(__name__)
# SECRET_KEY musi byc staly — dodaj do .env!
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))

CORS(app, supports_credentials=True,
     origins=["http://localhost:5000", "http://127.0.0.1:5000", "null"])

# ─── Database ─────────────────────────────────────────────────────────────────

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///wooai.db")
# Railway uzywa postgres:// — SQLAlchemy wymaga postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login_page"

@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith("/api/"):
        return jsonify({"error": "Unauthorized", "login_required": True}), 401
    return redirect(url_for("login_page"))

# ─── Shared server config (.env) ──────────────────────────────────────────────

ANTHROPIC_API_KEY     = os.getenv("ANTHROPIC_API_KEY", "")
ALLEGRO_CLIENT_ID     = os.getenv("ALLEGRO_CLIENT_ID", "")
ALLEGRO_CLIENT_SECRET = os.getenv("ALLEGRO_CLIENT_SECRET", "")
ALLEGRO_SANDBOX       = os.getenv("ALLEGRO_SANDBOX", "true").lower() == "true"
ALLEGRO_REDIRECT_URI  = os.getenv(
    "ALLEGRO_REDIRECT_URI", "http://localhost:5000/api/allegro/auth/callback"
)

ALLEGRO_AUTH_BASE   = ("https://allegro.pl.allegrosandbox.pl" if ALLEGRO_SANDBOX
                        else "https://allegro.pl")
ALLEGRO_API_BASE    = ("https://api.allegro.pl.allegrosandbox.pl" if ALLEGRO_SANDBOX
                        else "https://api.allegro.pl")
ALLEGRO_UPLOAD_BASE = ("https://upload.allegro.pl.allegrosandbox.pl" if ALLEGRO_SANDBOX
                        else "https://upload.allegro.pl")

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)


# ─── Models ───────────────────────────────────────────────────────────────────

class User(db.Model, UserMixin):
    __tablename__ = "users"
    id            = db.Column(db.Integer, primary_key=True)
    email         = db.Column(db.String(200), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    _active       = db.Column("active", db.Boolean, default=True)

    settings      = db.relationship("UserSettings", backref="user",
                                    uselist=False, cascade="all, delete-orphan")
    allegro_token = db.relationship("AllegroToken", backref="user",
                                    uselist=False, cascade="all, delete-orphan")

    def get_id(self):
        return str(self.id)

    @property
    def is_active(self):
        return self._active


class UserSettings(db.Model):
    __tablename__      = "user_settings"
    id                 = db.Column(db.Integer, primary_key=True)
    user_id            = db.Column(db.Integer, db.ForeignKey("users.id"),
                                   unique=True, nullable=False)
    wc_store_url       = db.Column(db.String(500), default="")
    wc_consumer_key    = db.Column(db.String(200), default="")
    wc_consumer_secret = db.Column(db.String(200), default="")
    wp_username        = db.Column(db.String(200), default="")
    wp_app_password    = db.Column(db.String(200), default="")
    allegro_city     = db.Column(db.String(100), default="Warszawa")
    allegro_province = db.Column(db.String(100), default="MAZOWIECKIE")
    allegro_postcode = db.Column(db.String(20),  default="00-001")


class AllegroToken(db.Model):
    __tablename__  = "allegro_tokens"
    id             = db.Column(db.Integer, primary_key=True)
    user_id        = db.Column(db.Integer, db.ForeignKey("users.id"),
                               unique=True, nullable=False)
    access_token   = db.Column(db.Text,  default="")
    refresh_token  = db.Column(db.Text,  default="")
    expires_at     = db.Column(db.Float, default=0.0)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ─── Settings helpers ─────────────────────────────────────────────────────────

def _ensure_settings(user=None) -> UserSettings:
    u = user or current_user
    if not u.settings:
        s = UserSettings(user_id=u.id)
        db.session.add(s)
        db.session.commit()
        db.session.refresh(u)
    return u.settings


def _wc_auth(s: UserSettings):
    """WooCommerce auth — prefer App Password, fall back to API keys."""
    if s.wp_username and s.wp_app_password:
        return (s.wp_username, s.wp_app_password.replace(" ", ""))
    return (s.wc_consumer_key, s.wc_consumer_secret)


def _wp_auth(s: UserSettings):
    """WordPress media auth — same logic."""
    return _wc_auth(s)


# ─── Auth helpers ─────────────────────────────────────────────────────────────

def _hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def _check_pw(pw: str, hashed: str) -> bool:
    return bcrypt.checkpw(pw.encode(), hashed.encode())


# ─── Auth routes ──────────────────────────────────────────────────────────────

@app.route("/login")
def login_page():
    if current_user.is_authenticated:
        return redirect("/")
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "login.html")


@app.route("/auth/register", methods=["POST"])
def register():
    data     = request.get_json() or {}
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not email or not password:
        return jsonify({"error": "Email i hasło są wymagane"}), 400
    if len(password) < 6:
        return jsonify({"error": "Hasło musi mieć minimum 6 znaków"}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({"error": "Ten e-mail jest już zarejestrowany"}), 400
    user = User(email=email, password_hash=_hash_pw(password))
    db.session.add(user)
    db.session.commit()
    login_user(user, remember=True)
    return jsonify({"success": True})


@app.route("/auth/login", methods=["POST"])
def login():
    data     = request.get_json() or {}
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    user     = User.query.filter_by(email=email).first()
    if not user or not _check_pw(password, user.password_hash):
        return jsonify({"error": "Nieprawidłowy email lub hasło"}), 401
    login_user(user, remember=True)
    return jsonify({"success": True})


@app.route("/auth/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return jsonify({"success": True})


@app.route("/auth/me")
@login_required
def me():
    s = _ensure_settings()
    return jsonify({
        "id":    current_user.id,
        "email": current_user.email,
        "settings_complete": bool(s.wc_store_url and (s.wc_consumer_key or s.wp_username)),
    })


# ─── Settings route ───────────────────────────────────────────────────────────

@app.route("/api/settings", methods=["GET", "POST"])
@login_required
def user_settings():
    s = _ensure_settings()
    if request.method == "GET":
        return jsonify({
            "wc_store_url":       s.wc_store_url,
            "wc_consumer_key":    s.wc_consumer_key,
            "wc_consumer_secret": s.wc_consumer_secret,
            "wp_username":        s.wp_username,
            "wp_app_password":    s.wp_app_password,
            "allegro_city":     s.allegro_city,
            "allegro_province": s.allegro_province,
            "allegro_postcode": s.allegro_postcode,
        })
    data = request.get_json() or {}
    for field in ["wc_store_url","wc_consumer_key","wc_consumer_secret",
                  "wp_username","wp_app_password",
                  "allegro_city","allegro_province","allegro_postcode"]:
        if field in data:
            setattr(s, field, str(data[field]).strip())
    db.session.commit()
    return jsonify({"success": True})


# ─── Strip HTML helper ────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text).strip()


# ─── 1. STORE ANALYZER ────────────────────────────────────────────────────────

def fetch_store_products(s: UserSettings, limit: int = 10) -> list[dict]:
    url  = f"{s.wc_store_url}/wp-json/wc/v3/products"
    resp = requests.get(url, auth=_wc_auth(s),
                        params={"per_page": limit, "status": "publish",
                                "orderby": "date", "order": "desc"},
                        timeout=15)
    resp.raise_for_status()
    return [
        {
            "name":              p.get("name", ""),
            "short_description": _strip_html(p.get("short_description", "")),
            "description":       _strip_html(p.get("description", ""))[:400],
            "categories":        [c["name"] for c in p.get("categories", [])],
            "tags":              [t["name"] for t in p.get("tags", [])],
            "price":             p.get("price", ""),
        }
        for p in resp.json()
    ]


# ─── 2. IMAGE ANALYZER ────────────────────────────────────────────────────────

def analyze_images(images: list[tuple[str, str]], topic: str = "") -> dict:
    content = []
    for image_b64, media_type in images:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": image_b64},
        })

    topic_hint = ""
    if topic:
        topic_hint = (
            f'\n\nSeller hint: "{topic}". '
            "If this contains a brand name, include it as \"Marka\". "
            "Use this to better understand product context and category."
        )

    content.append({
        "type": "text",
        "text": (
            "You are an expert product analyst for Polish Allegro e-commerce listings. "
            "Analyze the product image(s) thoroughly. "
            "Respond ONLY with valid JSON — no markdown fences, no preamble.\n\n"
            "Detect ALL visible and inferable Allegro-relevant attributes. "
            "These are used as search filters — be thorough and specific.\n\n"
            "JSON schema:\n"
            "{\n"
            '  "description": "Factual product description: product type, colors, material/texture, '
            'dimensions/size estimate, style, functions, use case, notable features, visible condition. '
            'No invented brand names or prices unless clearly visible.",\n'
            '  "suggested_topic": "1-3 word product topic hint, e.g. brand + type: Nike T-shirt, '
            'Kubek ceramiczny, Lampa LED — in Polish if possible",\n'
            '  "features": {\n'
            '    "Kolor": "dominant color(s) in Polish: Czarny / Biały / Czerwony / Niebieski / etc.",\n'
            '    "Materiał": "material if visible: Bawełna / Skóra / Plastik / Drewno / Metal / etc.",\n'
            '    "Stan": "Nowy — if packaged/unworn/sealed, Używany — if visibly worn/used",\n'
            '    "Marka": "brand name ONLY if logo/text visible on product — omit otherwise",\n'
            '    "Płeć": "Damski / Męski / Unisex — ONLY for clothing, shoes, accessories",\n'
            '    "Rodzaj": "product subtype: T-shirt / Bluza / Kubek / Poduszka / Lampa / etc.",\n'
            '    "Przeznaczenie": "primary use: Sport / Dom / Biuro / Dziecko / Outdoor / etc.",\n'
            '    "Styl": "Sportowy / Klasyczny / Casual / Elegancki / Nowoczesny / Rustykalny / etc.",\n'
            '    "Wzór": "Jednolity / W paski / W kratę / Nadruk / Kwiatowy — if applicable",\n'
            '    "Rozmiar": "size or dimensions if estimable",\n'
            '    "Wiek": "Dorosły / Dziecko / Niemowlę — if product targets specific age",\n'
            '    "Funkcja": "key function(s): Dekoracyjna / Ochronna / Sportowa / Edukacyjna / etc."\n'
            "    // add any other clearly visible product attribute\n"
            "  }\n"
            "}\n\n"
            "Rules:\n"
            "- All feature VALUES must be in Polish\n"
            "- Only include keys you are confident about — skip uncertain ones\n"
            "- Stan must be exactly 'Nowy' or 'Używany' (no other values)\n"
            "- Kolor: use simple Polish color names only"
            + topic_hint
        ),
    })
    resp = anthropic_client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": content}],
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        result = json.loads(raw)
        if "description" not in result:
            result = {"description": raw, "features": {}, "suggested_topic": ""}
    except json.JSONDecodeError:
        result = {"description": raw, "features": {}, "suggested_topic": ""}
    return result


# ─── 3. PRODUCT GENERATOR ─────────────────────────────────────────────────────

PRODUCT_PROMPT = """\
Jesteś doświadczonym ekspertem ds. marketingu e-commerce i copywritingu dla polskich sklepów internetowych. \
Specjalizujesz się w pisaniu opisów produktów, które sprzedają — łącząc psychologię zakupową, \
SEO i perswazję. Twoje opisy trafiają w potrzeby klientów, budują zaufanie i zwiększają konwersję.

## ANALIZA ZDJĘCIA PRODUKTU
{image_description}{topic_block}

## WYKRYTE CECHY PRODUKTU
{features_block}

## PRZYKŁADOWE PRODUKTY ZE SKLEPU (styl i ton do dopasowania)
{store_examples}

## TWOJE ZADANIE
Stwórz profesjonalny listing produktu dla polskiego sklepu WooCommerce:

1. **Tytuł** — chwytliwy, konkretny, zawiera główne słowo kluczowe. Klient musi wiedzieć co kupuje.
2. **Opis** — pisz językiem korzyści, nie cech. Zamiast "wykonany z drewna" → "trwały i naturalny materiał, który posłuży latami". \
Używaj krótkich akapitów. Zakończ wezwaniem do działania (CTA). HTML dozwolony: <p>, <ul>, <li>, <strong>.
3. **Krótki opis** — jedna mocna, sprzedażowa fraza na kartę produktu. Max 25 słów.
4. **SEO title** — dokładnie 50-60 znaków, główne słowo kluczowe na początku.
5. **Meta description** — 150-160 znaków, zachęca do kliknięcia, zawiera benefit i CTA.
6. **Tagi** — precyzyjne słowa kluczowe po polsku, które klienci wpisują w wyszukiwarce.
7. **Kategoria** — dopasuj do istniejących w sklepie lub stwórz logiczną nową.
8. **Cena** — na podstawie przedziału cenowego sklepu dobierz odpowiednią cenę. Jeśli sklep ma produkty w przedziale 20-50 zł, ustaw cenę w tym zakresie odpowiednio do produktu.

## ZASADY
- Odpowiedz WYŁĄCZNIE prawidłowym JSON — bez markdown, bez preambuły
- Pisz wyłącznie po polsku (opisy, tagi, kategorie)
- Opis: 100-180 słów, angażujący i sprzedażowy
- Nie wymyślaj cen jeśli nie są widoczne na zdjęciu
- `is_bundle`: true tylko gdy zdjęcie wyraźnie pokazuje zestaw kilku produktów

## SCHEMAT JSON
{{
  "name": "string — tytuł produktu, 5-10 słów, Title Case",
  "seo_title": "string — SEO title, 50-60 znaków",
  "meta_description": "string — meta description, 150-160 znaków",
  "description": "string — pełny opis HTML, sprzedażowy i SEO",
  "short_description": "string — jedna mocna fraza sprzedażowa, max 25 słów",
  "category": "string — kategoria po polsku",
  "tags": ["tablica", "tagów", "po", "polsku"],
  "price_suggestion": "string lub null",
  "stock_quantity": "integer",
  "is_bundle": "boolean"
}}
"""


def generate_product(image_description: str, store_products: list[dict], features: dict, topic: str = "") -> dict:
    store_examples = ""
    prices = [float(p["price"]) for p in store_products if p.get("price") and str(p["price"]).replace(".","").isdigit()]
    avg_price = sum(prices) / len(prices) if prices else None
    price_hint = f"\nCeny produktów w sklepie: min {min(prices):.2f} zł, max {max(prices):.2f} zł, średnia {avg_price:.2f} zł." if prices else ""

    for i, p in enumerate(store_products[:5], 1):
        store_examples += (
            f"Produkt {i}: {p['name']} | cena: {p['price']} zł\n"
            f"  Opis: {p['short_description'][:100]}\n"
            f"  Kategorie: {', '.join(p['categories'])}\n"
            f"  Tagi: {', '.join(p['tags'][:5])}\n\n"
        )
    features_block = "\n".join(f"- {k}: {v}" for k, v in features.items()) if features else "Brak wykrytych cech."
    topic_block = f"\n## TEMAT PODANY PRZEZ SPRZEDAWCĘ\n{topic}\n⚠️ Ten temat MUSI znaleźć się w tytule i opisie. Buduj cały opis wokół niego." if topic else ""

    prompt = PRODUCT_PROMPT.format(
        image_description=image_description,
        features_block=features_block,
        store_examples=(store_examples + price_hint) if store_examples else "Brak produktów w sklepie — użyj własnej oceny.",
        topic_block=topic_block,
    )
    resp = anthropic_client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)


# ─── 4. WORDPRESS MEDIA UPLOADER ──────────────────────────────────────────────

def upload_image_to_wordpress(s: UserSettings, image_b64: str,
                               filename: str, media_type: str = "image/jpeg") -> dict:
    image_bytes = base64.standard_b64decode(image_b64)
    ext_map = {"image/jpeg": "jpg", "image/png": "png",
               "image/webp": "webp", "image/gif": "gif"}
    ext     = ext_map.get(media_type, "jpg")
    wp_url  = f"{s.wc_store_url}/wp-json/wp/v2/media"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}.{ext}"',
        "Content-Type":        media_type,
    }
    resp = requests.post(wp_url, auth=_wp_auth(s), headers=headers,
                         data=image_bytes, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return {"id": data["id"], "url": data.get("source_url") or data.get("link") or ""}


# ─── 5. WOOCOMMERCE UPLOADER ──────────────────────────────────────────────────

def get_or_create_category(s: UserSettings, category_name: str) -> int:
    url  = f"{s.wc_store_url}/wp-json/wc/v3/products/categories"
    resp = requests.get(url, auth=_wc_auth(s), params={"search": category_name}, timeout=10)
    resp.raise_for_status()
    items = resp.json()
    if items:
        return items[0]["id"]
    resp = requests.post(url, auth=_wc_auth(s), json={"name": category_name}, timeout=10)
    resp.raise_for_status()
    return resp.json()["id"]


def get_or_create_tags(s: UserSettings, tag_names: list[str]) -> list[int]:
    url = f"{s.wc_store_url}/wp-json/wc/v3/products/tags"
    ids = []
    for tag in tag_names:
        resp = requests.get(url, auth=_wc_auth(s), params={"search": tag}, timeout=10)
        resp.raise_for_status()
        items = resp.json()
        if items and items[0]["name"].lower() == tag.lower():
            ids.append(items[0]["id"])
        else:
            r = requests.post(url, auth=_wc_auth(s), json={"name": tag}, timeout=10)
            r.raise_for_status()
            ids.append(r.json()["id"])
    return ids


def upload_product(s: UserSettings, product_data: dict, image_ids: list[int] = None) -> dict:
    url     = f"{s.wc_store_url}/wp-json/wc/v3/products"
    cat_id  = get_or_create_category(s, product_data.get("category", "Uncategorized"))
    tag_ids = get_or_create_tags(s, product_data.get("tags", []))

    product_type   = "grouped" if product_data.get("is_bundle") else "simple"
    stock_quantity = product_data.get("stock_quantity")
    try:
        stock_quantity = int(stock_quantity) if stock_quantity is not None else None
    except (ValueError, TypeError):
        stock_quantity = None

    seo_title        = product_data.get("seo_title", "")
    meta_description = product_data.get("meta_description", "")

    payload = {
        "name":              product_data["name"],
        "type":              product_type,
        "status":            "draft",
        "description":       product_data.get("description", ""),
        "short_description": product_data.get("short_description", ""),
        "regular_price":     product_data.get("price_suggestion") or "",
        "categories":        [{"id": cat_id}],
        "tags":              [{"id": t} for t in tag_ids],
        "meta_data": [
            {"key": "_yoast_wpseo_title",    "value": seo_title},
            {"key": "_yoast_wpseo_metadesc", "value": meta_description},
            {"key": "rank_math_title",        "value": seo_title},
            {"key": "rank_math_description",  "value": meta_description},
        ],
    }
    if stock_quantity is not None:
        payload["manage_stock"]   = True
        payload["stock_quantity"] = stock_quantity
        payload["stock_status"]   = "instock" if stock_quantity > 0 else "outofstock"
    if image_ids:
        payload["images"] = [{"id": img_id} for img_id in image_ids]

    resp = requests.post(url, auth=_wc_auth(s), json=payload, timeout=20)
    resp.raise_for_status()
    return resp.json()


# ─── 6. ALLEGRO TOKEN MANAGEMENT (per user, stored in DB) ─────────────────────

_ALLEGRO_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _allegro_basic_auth_header() -> dict:
    creds = base64.b64encode(f"{ALLEGRO_CLIENT_ID}:{ALLEGRO_CLIENT_SECRET}".encode()).decode()
    return {
        "Authorization": f"Basic {creds}",
        "User-Agent":    _ALLEGRO_UA,
        "Content-Type":  "application/x-www-form-urlencoded",
        "Accept":        "application/json",
    }


def _allegro_bearer_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept":        "application/vnd.allegro.public.v1+json",
        "Content-Type":  "application/vnd.allegro.public.v1+json",
    }


def _save_allegro_token(user: User, data: dict) -> None:
    expires_at = time.time() + data.get("expires_in", 3600) - 60
    token = user.allegro_token
    if not token:
        token = AllegroToken(user_id=user.id)
        db.session.add(token)
    token.access_token  = data.get("access_token", "")
    token.refresh_token = data.get("refresh_token", "")
    token.expires_at    = expires_at
    db.session.commit()


def _get_valid_access_token(user: User) -> str | None:
    token = user.allegro_token
    if not token or not token.access_token:
        return None
    if time.time() < token.expires_at:
        return token.access_token
    # Try refresh
    resp = requests.post(
        f"{ALLEGRO_AUTH_BASE}/auth/oauth/token",
        headers=_allegro_basic_auth_header(),
        data={"grant_type": "refresh_token", "refresh_token": token.refresh_token},
        timeout=15,
    )
    if resp.ok:
        _save_allegro_token(user, resp.json())
        return resp.json()["access_token"]
    return None


# ─── 7. ALLEGRO OFFER HELPERS ─────────────────────────────────────────────────

def _allegro_upload_raw(access_token: str, index: int,
                         data: bytes, content_type: str) -> tuple[str, str | None]:
    """Upload raw image bytes to Allegro CDN. Returns (cdn_url, error)."""
    app.logger.info("Allegro img %d upload — %s, %d bytes", index + 1, content_type, len(data))
    up_resp = requests.post(
        f"{ALLEGRO_UPLOAD_BASE}/sale/images",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type":  content_type,
            "Accept":        "application/vnd.allegro.public.v1+json",
        },
        data=data,
        timeout=60,
    )
    if up_resp.ok:
        cdn_url = up_resp.json().get("location", "")
        if cdn_url:
            app.logger.info("Allegro img %d OK: %s", index + 1, cdn_url)
            return cdn_url, None
        return "", f"Zdjęcie {index+1}: brak 'location' w odpowiedzi"
    err = f"Zdjęcie {index+1}: upload {up_resp.status_code} — {up_resp.text[:200]}"
    app.logger.warning(err)
    return "", err


def _upload_images_to_allegro(access_token: str,
                               image_urls: list[str] | None = None,
                               image_bytes_list: list[tuple[bytes, str]] | None = None,
                               ) -> tuple[list[str], list[str]]:
    """Upload images to Allegro CDN from URLs or raw bytes (fallback)."""
    allegro_urls = []
    errors       = []
    idx          = 0

    for url in (image_urls or []):
        if not url:
            continue
        try:
            img_resp     = requests.get(url, timeout=30)
            img_resp.raise_for_status()
            content_type = img_resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
            cdn_url, err = _allegro_upload_raw(access_token, idx, img_resp.content, content_type)
            if cdn_url:
                allegro_urls.append(cdn_url)
            elif err:
                errors.append(err)
        except Exception as e:
            errors.append(f"Zdjęcie {idx+1}: {type(e).__name__}: {e}")
            app.logger.warning("Allegro img %d from URL failed: %s", idx + 1, e)
        idx += 1

    # If no images uploaded via URL, try direct bytes upload
    if not allegro_urls and image_bytes_list:
        for raw_bytes, mime in image_bytes_list:
            try:
                cdn_url, err = _allegro_upload_raw(access_token, idx, raw_bytes,
                                                    mime or "image/jpeg")
                if cdn_url:
                    allegro_urls.append(cdn_url)
                elif err:
                    errors.append(err)
            except Exception as e:
                errors.append(f"Zdjęcie {idx+1}: {type(e).__name__}: {e}")
                app.logger.warning("Allegro img %d direct upload failed: %s", idx + 1, e)
            idx += 1

    return allegro_urls, errors


def _sanitize_allegro_html(html: str) -> str:
    replacements = [
        (r'<strong(\s[^>]*)?>', '<b>'),  (r'</strong>', '</b>'),
        (r'<em(\s[^>]*)?>',    '<i>'),  (r'</em>',     '</i>'),
        (r'<h1(\s[^>]*)?>',    '<h2>'), (r'</h1>',     '</h2>'),
        (r'<h4(\s[^>]*)?>',    '<h3>'), (r'</h4>',     '</h3>'),
        (r'<h5(\s[^>]*)?>',    '<h3>'), (r'</h5>',     '</h3>'),
        (r'<h6(\s[^>]*)?>',    '<h3>'), (r'</h6>',     '</h3>'),
        (r'<div(\s[^>]*)?>', '<p>'),    (r'</div>',    '</p>'),
        (r'<span[^>]*>',       ''),      (r'</span>',   ''),
    ]
    for pattern, replacement in replacements:
        html = re.sub(pattern, replacement, html, flags=re.IGNORECASE)
    allowed = {'p', 'b', 'i', 'ul', 'ol', 'li', 'br', 'h2', 'h3'}
    def strip_tag(m):
        tag = re.match(r'</?(\w+)', m.group(0))
        return m.group(0) if (tag and tag.group(1).lower() in allowed) else ''
    html = re.sub(r'<[^>]+>', strip_tag, html)
    html = re.sub(r'&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)', '&amp;', html)
    return html.strip()


def _build_tax_rates(access_token: str, category_id: str, vat: dict) -> dict | None:
    """
    Build taxSettings payload using rates validated against /sale/tax-settings.
    vat = {"pl": "23", "sk": "20", "hu": "27", "cz": "21"}
    Returns taxSettings dict or None if nothing to set.

    /sale/tax-settings response structure:
    {
      "subjects": [{"label": "Goods", "value": "GOODS"}, ...],
      "rates": [{"countryCode": "PL", "values": [{"label":"23%","value":"23.00",...}]}, ...],
      "exemptions": [{"label":"...", "value":"..."}, ...]
    }
    """
    _country_map = {"pl": "PL", "sk": "SK", "hu": "HU", "cz": "CZ"}

    # Fetch valid settings — note: correct param is "category.id"
    resp = requests.get(
        f"{ALLEGRO_API_BASE}/sale/tax-settings",
        headers=_allegro_bearer_headers(access_token),
        params={"category.id": category_id},
        timeout=10,
    )
    api_data = {}
    if resp.ok:
        api_data = resp.json()
        app.logger.info("Allegro tax-settings: %s", json.dumps(api_data)[:600])
    else:
        app.logger.warning("tax-settings fetch failed %s: %s", resp.status_code, resp.text[:200])

    # Pick subject — use first available from API, default "GOODS"
    subjects = api_data.get("subjects", [])
    subject = subjects[0]["value"] if subjects else "GOODS"

    # Build a lookup: countryCode → list of valid rate strings
    api_rates_by_country: dict[str, list[str]] = {}
    for entry in api_data.get("rates", []):
        cc = (entry.get("countryCode") or "").upper()
        values = [v.get("value", "") for v in entry.get("values", []) if v.get("value")]
        if cc and values:
            api_rates_by_country[cc] = values

    app.logger.info("API rates by country: %s", api_rates_by_country)

    tax_rates = []
    for vat_key, country_code in _country_map.items():
        rate_val = vat.get(vat_key, "")
        if not rate_val or rate_val in ("EXEMPT", "0", ""):
            continue
        try:
            desired = float(str(rate_val).replace("%", "").strip())
        except (ValueError, TypeError):
            continue

        # Find closest valid rate for this country from API
        available = api_rates_by_country.get(country_code, [])
        if available:
            best = min(available, key=lambda r: abs(float(r) - desired) if r else 999)
            tax_rates.append({"rate": best, "countryCode": country_code})
        else:
            # No API data for this country — use user value in "23.00" format
            tax_rates.append({"rate": f"{desired:.2f}", "countryCode": country_code})

    if not tax_rates:
        return None

    return {"rates": tax_rates, "subject": subject}


def _get_allegro_shipping_rate_id(access_token: str) -> str | None:
    resp = requests.get(
        f"{ALLEGRO_API_BASE}/sale/shipping-rates",
        headers=_allegro_bearer_headers(access_token),
        timeout=10,
    )
    if resp.ok:
        rates = resp.json().get("shippingRates", [])
        if rates:
            return rates[0]["id"]
    return None


def _ensure_leaf_category(access_token: str, category_id: str) -> str:
    """
    Given a category ID, return it if it's a leaf.
    If not a leaf, find the first leaf child (BFS one level deep).
    """
    headers = _allegro_bearer_headers(access_token)
    resp = requests.get(f"{ALLEGRO_API_BASE}/sale/categories/{category_id}",
                        headers=headers, timeout=10)
    if resp.ok:
        cat = resp.json()
        if cat.get("leaf"):
            return category_id
        # Not a leaf — fetch children and pick first leaf
        children_resp = requests.get(f"{ALLEGRO_API_BASE}/sale/categories",
                                     headers=headers,
                                     params={"parent.id": category_id}, timeout=10)
        if children_resp.ok:
            children = children_resp.json().get("categories", [])
            for child in children:
                if child.get("leaf"):
                    app.logger.info("Category %s not leaf → using child %s", category_id, child["id"])
                    return str(child["id"])
            # No leaf child found at first level — recurse into first child
            if children:
                return _ensure_leaf_category(access_token, str(children[0]["id"]))
    return category_id  # return as-is if we can't verify


def _find_allegro_category(access_token: str, category_name: str,
                           product_name: str = "") -> str | None:
    """Find best Allegro leaf category. Uses matching-categories first (most accurate)."""
    headers = _allegro_bearer_headers(access_token)

    def _to_leaf(cat_id: str) -> str:
        return _ensure_leaf_category(access_token, cat_id)

    # 1. matching-categories by product name — most semantic, Allegro recommends this
    if product_name:
        resp = requests.get(f"{ALLEGRO_API_BASE}/sale/matching-categories",
                            headers=headers, params={"name": product_name}, timeout=10)
        if resp.ok:
            cats = resp.json().get("matchingCategories", [])
            if cats:
                cat_id = str(cats[0]["id"])
                leaf_id = _to_leaf(cat_id)
                app.logger.info("matching-categories: %s → leaf: %s", cat_id, leaf_id)
                return leaf_id

    def _search_by_name(name: str) -> str | None:
        resp = requests.get(f"{ALLEGRO_API_BASE}/sale/categories",
                            headers=headers, params={"name": name}, timeout=10)
        if resp.ok:
            cats = resp.json().get("categories", [])
            # prefer already-leaf categories
            for cat in cats:
                if cat.get("leaf"):
                    return str(cat["id"])
            # fallback: take first and ensure leaf
            if cats:
                return _to_leaf(str(cats[0]["id"]))
        return None

    # 2. AI-suggested category name
    cat_id = _search_by_name(category_name)
    if cat_id:
        return cat_id

    # 3. Fallback: "Inne"
    cat_id = _search_by_name("Inne")
    if cat_id:
        return cat_id

    # 4. Last resort: first root category → leaf
    resp = requests.get(f"{ALLEGRO_API_BASE}/sale/categories",
                        headers=headers, timeout=10)
    if resp.ok:
        cats = resp.json().get("categories", [])
        if cats:
            return _to_leaf(str(cats[0]["id"]))

    return None


def _fill_allegro_parameters(access_token: str, category_id: str,
                              features: dict, vat_rate: str = "",
                              product_name: str = "",
                              product_description: str = "") -> list:
    """Use Claude to intelligently fill all available category parameters."""
    headers = _allegro_bearer_headers(access_token)
    resp = requests.get(
        f"{ALLEGRO_API_BASE}/sale/categories/{category_id}/parameters",
        headers=headers, timeout=10,
    )
    if not resp.ok:
        app.logger.warning("Could not fetch parameters for category %s: %s",
                           category_id, resp.text[:200])
        return []

    params_def = resp.json().get("parameters", [])
    if not params_def:
        return []

    merged_features = dict(features or {})
    if vat_rate and vat_rate != "EXEMPT":
        merged_features.setdefault("Stawka VAT", f"{vat_rate}%")

    # ── Key feature names that map to Allegro parameter names ──
    # Polish param name → feature key
    _DIRECT_MAP = {
        "Marka":   merged_features.get("Marka", ""),
        "Rozmiar": merged_features.get("Rozmiar", ""),
        "Kolor":   merged_features.get("Kolor", ""),
        "Stan":    merged_features.get("Stan", ""),
    }

    def _fuzzy_match_value(user_val: str, dict_values: list) -> str | None:
        """Find best matching valueId from dictionaryValues for user_val (case-insensitive)."""
        if not user_val or not dict_values:
            return None
        uv = user_val.strip().lower()
        # 1. Exact match
        for v in dict_values:
            if v.get("value", "").lower() == uv:
                return v["id"]
        # 2. Starts-with match
        for v in dict_values:
            if v.get("value", "").lower().startswith(uv):
                return v["id"]
        # 3. Contains match
        for v in dict_values:
            if uv in v.get("value", "").lower():
                return v["id"]
        # 4. User value contains the dict entry
        for v in dict_values:
            label = v.get("value", "").lower()
            if label and label in uv:
                return v["id"]
        return None

    # Build compact param list for Claude (non-priority params)
    params_for_ai = []
    valid_value_ids: dict[str, set] = {}
    # Direct matches we handle ourselves (skip from Claude)
    direct_results: list[dict] = []
    direct_param_ids: set[str] = set()

    for p in params_def:
        param_id   = str(p["id"])
        param_name = p.get("name", "")
        ptype      = p["type"]
        dict_vals  = p.get("dictionaryValues", [])

        # ── Direct handling for Marka, Rozmiar, Kolor, Stan ──
        # NOTE: we include ALL params regardless of offerScope —
        # /sale/product-offers handles routing to product vs offer automatically.
        user_val = _DIRECT_MAP.get(param_name, "")
        if user_val:
            app.logger.info("Direct param [%s] type=%s dict_vals=%d user_val=%s",
                            param_name, ptype, len(dict_vals), user_val)
            if ptype == "string":
                direct_results.append({"id": param_id, "values": [user_val]})
                direct_param_ids.add(param_id)
                app.logger.info("→ string set: %s = %s", param_name, user_val)
                continue
            elif ptype == "dictionary":
                if dict_vals:
                    best_id = _fuzzy_match_value(user_val, dict_vals)
                    if best_id:
                        direct_results.append({"id": param_id, "valuesIds": [best_id]})
                        direct_param_ids.add(param_id)
                        app.logger.info("→ dict matched: %s = valueId %s", param_name, best_id)
                        continue
                    else:
                        app.logger.warning("→ dict NO MATCH for %s=%s in %d values (sample: %s)",
                                           param_name, user_val, len(dict_vals),
                                           [v["value"] for v in dict_vals[:5]])
                else:
                    app.logger.warning("→ dict EMPTY values for %s, trying string fallback", param_name)
                # Fallback: send as open text value (works for open-dictionary params)
                direct_results.append({"id": param_id, "values": [user_val]})
                direct_param_ids.add(param_id)
                app.logger.info("→ dict fallback to values[]: %s = %s", param_name, user_val)
                continue

        # ── Everything else: only offer-scope params go to Claude ──
        # NOTE: cannot use `or` chain — False or None = None, not False!
        def _offer_scope(param):
            for val in [
                param.get("offerScope"),
                param.get("options", {}).get("offerScope"),
                param.get("restrictions", {}).get("offerScope"),
            ]:
                if val is not None:
                    return val
            return None
        if _offer_scope(p) is False:
            continue  # skip product-catalog-only params for Claude

        entry: dict = {"id": param_id, "name": param_name, "type": ptype,
                       "required": p.get("required", False)}
        if dict_vals:
            entry["options"] = [{"valueId": v["id"], "label": v["value"]}
                                 for v in dict_vals[:40]]
            valid_value_ids[param_id] = {v["id"] for v in dict_vals}
        if p.get("restrictions", {}).get("maxLength"):
            entry["maxLength"] = p["restrictions"]["maxLength"]
        params_for_ai.append(entry)

    # ── Claude fills the rest ──
    ai_results: list[dict] = []
    if params_for_ai:
        prompt = (
            "Fill Allegro listing parameters for a Polish e-commerce product.\n\n"
            f"Product name: {product_name}\n"
            f"Short description: {product_description[:400]}\n"
            f"Detected features: {json.dumps(merged_features, ensure_ascii=False)}\n\n"
            "Parameters to fill:\n"
            f"{json.dumps(params_for_ai, ensure_ascii=False)[:4000]}\n\n"
            "CRITICAL RULES:\n"
            "1. For 'dictionary' type: use ONLY a 'valueId' from the 'options' list.\n"
            "   NEVER use the label text. Output: {\"id\":\"param_id\",\"valuesIds\":[\"valueId\"]}\n"
            "2. For 'string' type: {\"id\":\"param_id\",\"values\":[\"text\"]}\n"
            "3. For 'integer'/'float' type: {\"id\":\"param_id\",\"values\":[\"42\"]}\n"
            "4. Only include parameters you can confidently fill from the product info.\n"
            "5. Respond ONLY with a JSON array — no markdown, no explanation.\n"
        )
        try:
            ai_resp = anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = ai_resp.content[0].text.strip()
            raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
            raw_list = json.loads(raw)
            if isinstance(raw_list, list):
                for item in raw_list:
                    if not isinstance(item, dict) or not item.get("id"):
                        continue
                    param_id = str(item["id"])
                    if param_id in direct_param_ids:
                        continue  # already handled directly
                    clean: dict = {"id": param_id}
                    if item.get("valuesIds") and isinstance(item["valuesIds"], list):
                        allowed = valid_value_ids.get(param_id, set())
                        valid = [str(v) for v in item["valuesIds"]
                                 if v and (not allowed or str(v) in allowed)]
                        if not valid:
                            continue
                        clean["valuesIds"] = valid
                    elif item.get("values") and isinstance(item["values"], list):
                        clean["values"] = [str(v) for v in item["values"] if v is not None]
                        if not clean["values"]:
                            continue
                    else:
                        continue
                    ai_results.append(clean)
        except Exception as e:
            app.logger.warning("AI parameter fill failed: %s", e)

    result = direct_results + ai_results
    app.logger.info("Allegro params: %d direct + %d AI = %d total",
                    len(direct_results), len(ai_results), len(result))
    return result


def publish_to_allegro(user: User, product_data: dict,
                       image_urls: list[str] | None = None,
                       image_bytes_list: list[tuple[bytes, str]] | None = None,
                       features: dict | None = None,
                       topic: str = "",
                       allegro_options: dict | None = None) -> dict:
    s            = _ensure_settings(user)
    access_token = _get_valid_access_token(user)
    if not access_token:
        return {"error": "Nie zalogowano do Allegro — kliknij 'Autoryzuj Allegro'."}

    category_id = _find_allegro_category(
        access_token,
        product_data.get("category", "Inne"),
        product_name=product_data.get("name", ""),
    )
    if not category_id:
        return {"error": "Nie znaleziono kategorii Allegro. Ustaw domyślną kategorię w Ustawieniach."}

    shipping_rate_id = _get_allegro_shipping_rate_id(access_token)

    try:
        price = float(str(product_data.get("price_suggestion") or "0").replace(",", "."))
    except (ValueError, TypeError):
        price = 0.0
    stock = int(product_data.get("stock_quantity") or 10)

    description_html = _sanitize_allegro_html(
        product_data.get("description") or f"<p>{product_data.get('short_description', '')}</p>"
    )

    # Supplement features with brand from topic if not already present
    merged_features = dict(features or {})
    # Extract brand from topic: first capitalized word that looks like a proper noun
    # e.g. "Nike bluza czerwona" → "Nike", "Adidas Originals hoodie" → "Adidas Originals"
    if topic and not merged_features.get("Marka"):
        words = topic.split()
        brand_words = []
        for w in words:
            clean = w.strip(",.!?\"'")
            if clean and clean[0].isupper() and len(clean) > 1:
                brand_words.append(clean)
            elif brand_words:
                break  # stop at first non-capitalized word after brand started
        if brand_words:
            merged_features["Marka"] = " ".join(brand_words)

    opts    = allegro_options or {}
    vat     = opts.get("vat", {})
    pl_vat  = vat.get("pl", "23")

    # Fill parameters via Claude — uses description + features for comprehensive filling
    parameters = _fill_allegro_parameters(
        access_token, category_id, merged_features,
        vat_rate=pl_vat,
        product_name=product_data.get("name", ""),
        product_description=product_data.get("short_description", "") or
                            _strip_html(product_data.get("description", ""))[:400],
    )

    invoice = "WITHOUT_VAT" if pl_vat == "EXEMPT" else "VAT"

    # Build taxSettings from valid API rates
    tax_settings = _build_tax_rates(access_token, category_id, vat)

    offer: dict = {
        "name":     product_data["name"][:75],
        "category": {"id": category_id},
        "description": {
            "sections": [{"items": [{"type": "TEXT", "content": description_html}]}]
        },
        "sellingMode": {
            "format": "BUY_NOW",
            "price":  {"amount": f"{price:.2f}", "currency": "PLN"},
        },
        "stock":    {"available": stock, "unit": "UNIT"},
        "payments": {"invoice": invoice},
        "location": {
            "countryCode": "PL",
            "province":    s.allegro_province,
            "city":        s.allegro_city,
            "postCode":    s.allegro_postcode,
        },
        "delivery":    {"handlingTime": opts.get("handlingTime", "PT24H")},
        "publication": {"status": "INACTIVE"},
    }
    if shipping_rate_id:
        offer["delivery"]["shippingRates"] = {"id": shipping_rate_id}

    # Tax settings — declare VAT rate per country
    if tax_settings:
        offer["taxSettings"] = tax_settings
        app.logger.info("Allegro taxSettings: %s", json.dumps(tax_settings))

    # Fulfillment — only send for ONE_FULFILLMENT; omitting = Allegro treats as self-managed
    if opts.get("fulfillment") == "ONE_FULFILLMENT":
        offer["fulfillment"] = {"availabilityCode": "ONE_FULFILLMENT"}

    # Parameters (VAT + product features matched to category params)
    if parameters:
        offer["parameters"] = parameters

    image_upload_errors = []
    allegro_image_urls, image_upload_errors = _upload_images_to_allegro(
        access_token,
        image_urls=(image_urls or [])[:5],
        image_bytes_list=(image_bytes_list or [])[:5],
    )
    if allegro_image_urls:
        offer["images"] = allegro_image_urls
        app.logger.info("Allegro offer images: %s", allegro_image_urls)

    def _post_offer(o: dict):
        app.logger.info("Allegro offer payload: %s", json.dumps(o, ensure_ascii=False)[:2000])
        r = requests.post(
            f"{ALLEGRO_API_BASE}/sale/product-offers",
            headers=_allegro_bearer_headers(access_token),
            json=o, timeout=20,
        )
        app.logger.info("Allegro POST → %s: %s", r.status_code, r.text[:1000])
        return r

    resp = _post_offer(offer)

    # Retry logic — strip individual bad parameters one at a time, up to 8 attempts
    for _attempt in range(8):
        if resp.status_code != 422:
            break
        try:
            body = resp.json()
            errs = body.get("errors", [])
            if not errs:
                break

            app.logger.warning("422 attempt %d errors: %s", _attempt, json.dumps(errs)[:600])
            fixed = False

            # ── Extract bad param IDs from every error field ──
            bad_param_ids: set[str] = set()
            for e in errs:
                # From path like "parameters[2]"
                path = e.get("path") or ""
                m = re.match(r"parameters\[(\d+)\]", path)
                if m and offer.get("parameters"):
                    idx = int(m.group(1))
                    params = offer["parameters"]
                    if idx < len(params):
                        bad_param_ids.add(str(params[idx].get("id", "")))

                # From userMessage / message — extract ONLY the specific param ID
                # e.g. "Parameter `1294:Rodzaj` should not..." → only 1294, not all params
                for field in ("userMessage", "message", "details"):
                    text = e.get(field) or ""
                    for match in re.finditer(r"\b(\d{2,})\s*:", text):
                        bad_param_ids.add(match.group(1))

            bad_param_ids.discard("")

            if bad_param_ids and offer.get("parameters"):
                before = len(offer["parameters"])
                offer["parameters"] = [
                    p for p in offer["parameters"]
                    if str(p.get("id", "")) not in bad_param_ids
                ]
                after = len(offer["parameters"])
                app.logger.warning("Attempt %d: removed params %s (%d→%d)",
                                   _attempt, bad_param_ids, before, after)
                if after == before:
                    # ID not found in our list — drop all to avoid infinite loop
                    offer.pop("parameters", None)
                fixed = True

            # Tax error
            elif offer.get("taxSettings") and any(
                "tax" in (e.get("code") or "").lower() or
                (e.get("path") or "").startswith("taxSettings")
                for e in errs
            ):
                app.logger.warning("Attempt %d: tax error — dropping taxSettings", _attempt)
                offer.pop("taxSettings", None)
                fixed = True

            # Any other parameter error — drop all
            elif offer.get("parameters") and any(
                "parameter" in (e.get("code") or "").lower() or
                (e.get("path") or "").startswith("parameters")
                for e in errs
            ):
                app.logger.warning("Attempt %d: dropping all parameters", _attempt)
                offer.pop("parameters", None)
                fixed = True

            if not fixed:
                break
            resp = _post_offer(offer)
        except Exception as retry_err:
            app.logger.warning("Retry attempt %d failed: %s", _attempt, retry_err)
            break

    if resp.ok:
        data      = resp.json()
        offer_id  = data.get("id", "")
        offer_url = f"{ALLEGRO_AUTH_BASE}/oferta/{offer_id}" if offer_id else ""
        result    = {"success": True, "offer_id": offer_id, "offer_url": offer_url}
        if image_upload_errors:
            result["image_warnings"] = image_upload_errors
        return result

    try:
        err_body = resp.json()
        errors   = err_body.get("errors", [])
        def _fmt(e):
            parts = [f"[{e.get('code','?')}] {e.get('message','?')}"]
            if e.get("path"):  parts.append(f"path={e['path']}")
            if e.get("details"): parts.append(f"details={e['details']}")
            if e.get("userMessage"): parts.append(f"userMsg={e['userMessage']}")
            return " | ".join(parts)
        msg = ("; ".join(_fmt(e) for e in errors)
               if errors else json.dumps(err_body)[:800])
    except Exception:
        msg = resp.text[:800]
    return {"error": f"Allegro {resp.status_code}: {msg}"}


# ─── API ROUTES ────────────────────────────────────────────────────────────────

@app.route("/api/analyze", methods=["POST"])
@login_required
def analyze():
    files = request.files.getlist("images")
    if not files or not any(f.filename for f in files):
        single = request.files.get("image")
        if not single:
            return jsonify({"error": "No image provided"}), 400
        files = [single]

    images = []
    for f in files:
        if f and f.filename:
            image_b64 = base64.standard_b64encode(f.read()).decode()
            images.append((image_b64, f.content_type or "image/jpeg"))

    if not images:
        return jsonify({"error": "No valid images provided"}), 400

    try:
        s              = _ensure_settings()
        topic          = (request.form.get("topic") or "").strip()
        store_products = []
        if s.wc_store_url and (s.wc_consumer_key or s.wp_username):
            try:
                store_products = fetch_store_products(s, limit=10)
            except Exception as e:
                app.logger.warning("Could not fetch store products: %s", e)

        vision_result   = analyze_images(images, topic=topic)
        features        = dict(vision_result.get("features", {}))
        # Only suggest topic when user left the field empty
        suggested_topic = "" if topic else vision_result.get("suggested_topic", "")

        # If user provided a topic and it contains a likely brand (capitalized word(s)),
        # supplement features["Marka"] when AI didn't detect one from the image
        if topic and not features.get("Marka"):
            brand_candidate = " ".join(
                w for w in topic.split()
                if w and w[0].isupper() and len(w) > 1
            )
            if brand_candidate:
                features["Marka"] = brand_candidate

        product_data = generate_product(
            vision_result["description"],
            store_products,
            features,
            topic=topic,
        )
        return jsonify({
            "success":           True,
            "image_description": vision_result["description"],
            "features":          features,
            "suggested_topic":   suggested_topic,
            "product":           product_data,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/publish", methods=["POST"])
@login_required
def publish():
    product_json = request.form.get("product")
    if not product_json:
        return jsonify({"error": "Missing product data"}), 400
    try:
        product_data = json.loads(product_json)
    except json.JSONDecodeError:
        return jsonify({"error": "Invalid product JSON"}), 400

    targets_raw = request.form.get("targets", '["woocommerce","allegro"]')
    try:
        targets = json.loads(targets_raw)
    except Exception:
        targets = ["woocommerce", "allegro"]

    s      = _ensure_settings()
    result = {"success": True, "woocommerce": None, "allegro": None}

    # Parse features and topic from form
    features_raw = request.form.get("features", "{}")
    try:
        features = json.loads(features_raw) if features_raw else {}
    except Exception:
        features = {}
    topic = (request.form.get("topic") or "").strip()
    allegro_opts_raw = request.form.get("allegro_options", "{}")
    try:
        allegro_opts = json.loads(allegro_opts_raw)
    except Exception:
        allegro_opts = {}

    # Read all image files into memory first (so we can use them for both WP and Allegro)
    raw_images: list[tuple[bytes, str, str]] = []  # (bytes, mime, filename_stem)
    for i, f in enumerate(request.files.getlist("images")):
        if f and f.filename:
            data      = f.read()
            mime      = f.content_type or "image/jpeg"
            stem      = re.sub(r"[^a-zA-Z0-9_-]", "-",
                               os.path.splitext(f.filename)[0]) or f"product-{i+1}"
            raw_images.append((data, mime, stem))

    # Upload images to WordPress
    image_ids  = []
    image_urls = []
    for i, (data, mime, stem) in enumerate(raw_images):
        try:
            image_b64 = base64.standard_b64encode(data).decode()
            media     = upload_image_to_wordpress(s, image_b64, stem, mime)
            image_ids.append(media["id"])
            image_urls.append(media["url"])
            app.logger.info("WP image uploaded: id=%s url=%s", media["id"], media["url"])
        except Exception as e:
            app.logger.warning("Image %d WP upload failed: %s", i, e)

    result["images_uploaded"] = len(image_ids)
    result["image_urls"]      = image_urls

    if "woocommerce" in targets:
        try:
            wc_product = upload_product(s, product_data, image_ids)
            result["woocommerce"] = {
                "success":    True,
                "product_id": wc_product["id"],
                "edit_url":   f"{s.wc_store_url}/wp-admin/post.php?post={wc_product['id']}&action=edit",
            }
        except requests.exceptions.HTTPError as e:
            try:
                wc_body = e.response.json()
                msg     = f"WooCommerce {e.response.status_code}: {wc_body.get('message', str(wc_body))}"
            except Exception:
                msg = f"WooCommerce {e.response.status_code}: {e.response.text[:400]}"
            app.logger.error("WC error: %s", msg)
            result["woocommerce"] = {"success": False, "error": msg}
        except Exception as e:
            result["woocommerce"] = {"success": False, "error": f"{type(e).__name__}: {e}"}

    if "allegro" in targets:
        # Pass raw bytes as fallback when WP image upload failed (no image_urls)
        image_bytes_list = [(d, m) for d, m, _ in raw_images] if not image_urls else []
        result["allegro"] = publish_to_allegro(
            current_user, product_data,
            image_urls=image_urls,
            image_bytes_list=image_bytes_list,
            features=features,
            topic=topic,
            allegro_options=allegro_opts,
        )

    wc_ok = result["woocommerce"] and result["woocommerce"].get("success")
    al_ok = result["allegro"]     and result["allegro"].get("success")
    result["success"] = bool(wc_ok or al_ok)
    return jsonify(result)


# ─── ALLEGRO SUGGEST OPTIONS ──────────────────────────────────────────────────

# VAT parameter names used by Allegro in category parameters
_ALLEGRO_VAT_PARAM_NAMES = {"Stawka VAT", "VAT rate", "Podatek VAT", "VAT"}

# Country default VAT rates (when Allegro doesn't specify)
_COUNTRY_DEFAULT_VAT = {"pl": "23", "sk": "20", "hu": "27", "cz": "21"}


def _parse_vat_value(raw: str) -> str | None:
    """Convert Allegro dict value like '23%' or '0.23' to plain integer string '23'."""
    raw = raw.strip().replace(",", ".")
    if raw.endswith("%"):
        try:
            return str(int(float(raw[:-1])))
        except ValueError:
            return None
    try:
        val = float(raw)
        # If it looks like a fraction (0.23) convert to percent
        if 0 < val <= 1:
            return str(int(round(val * 100)))
        if val > 1:
            return str(int(val))
    except ValueError:
        pass
    return None


@app.route("/api/allegro/suggest-vat", methods=["POST"])
@login_required
def allegro_suggest_vat():
    """Return suggested VAT rates for a product's Allegro category."""
    access_token = _get_valid_access_token(current_user)
    if not access_token:
        return jsonify({"error": "not_authorized"}), 401

    data         = request.get_json() or {}
    product_name = (data.get("product_name") or "").strip()
    category_name = (data.get("category") or "").strip()

    category_id = _find_allegro_category(access_token, category_name, product_name)
    if not category_id:
        return jsonify({"vat": _COUNTRY_DEFAULT_VAT, "category_id": None})

    # Fetch category parameters — look for a VAT parameter
    headers = _allegro_bearer_headers(access_token)
    resp = requests.get(
        f"{ALLEGRO_API_BASE}/sale/categories/{category_id}/parameters",
        headers=headers, timeout=10,
    )

    suggested_rate = None
    if resp.ok:
        for param in resp.json().get("parameters", []):
            if param.get("name") in _ALLEGRO_VAT_PARAM_NAMES:
                values = param.get("dictionaryValues", [])
                if values:
                    suggested_rate = _parse_vat_value(values[0].get("value", ""))
                    break

    if not suggested_rate:
        # Fallback: fetch seller tax-settings and find first rate
        ts_resp = requests.get(
            f"{ALLEGRO_API_BASE}/sale/tax-settings",
            headers=headers, timeout=10,
        )
        if ts_resp.ok:
            settings = ts_resp.json().get("taxSettings", [])
            for ts in settings:
                rate_str = ts.get("rate") or ts.get("percentage") or ts.get("name", "")
                parsed = _parse_vat_value(str(rate_str))
                if parsed and parsed != "0":
                    suggested_rate = parsed
                    break

    if not suggested_rate:
        suggested_rate = "23"   # universal fallback

    # Build per-country response using same suggested rate as base,
    # adjusted to nearest valid rate for each country
    def _nearest(rate: str, options: list[str]) -> str:
        try:
            r = int(rate)
            return min(options, key=lambda x: abs(int(x) - r))
        except (ValueError, TypeError):
            return options[0]

    vat = {
        "pl": _nearest(suggested_rate, ["23", "8", "5", "0"]),
        "sk": _nearest(suggested_rate, ["20", "10", "0"]),
        "hu": _nearest(suggested_rate, ["27", "18", "5", "0"]),
        "cz": _nearest(suggested_rate, ["21", "15", "10", "0"]),
    }

    return jsonify({"vat": vat, "category_id": category_id,
                    "suggested_rate": suggested_rate})


# ─── ALLEGRO AUTH ROUTES ───────────────────────────────────────────────────────

@app.route("/api/allegro/status")
@login_required
def allegro_status():
    if not ALLEGRO_CLIENT_ID:
        return jsonify({"authorized": False, "reason": "ALLEGRO_CLIENT_ID not configured"})
    token = current_user.allegro_token
    if not token or not token.access_token:
        return jsonify({"authorized": False, "reason": "no_token"})
    if time.time() < token.expires_at:
        return jsonify({"authorized": True})
    new_token = _get_valid_access_token(current_user)
    return jsonify({"authorized": bool(new_token)})


@app.route("/api/allegro/auth/start")
@login_required
def allegro_auth_start():
    if not ALLEGRO_CLIENT_ID:
        return jsonify({"error": "ALLEGRO_CLIENT_ID nie ustawiony w .env"}), 400
    import urllib.parse
    # Encode user_id in state for security + multi-user support
    state  = base64.urlsafe_b64encode(
        json.dumps({"user_id": current_user.id, "nonce": secrets.token_hex(8)}).encode()
    ).decode()
    session["allegro_oauth_state"] = state
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id":     ALLEGRO_CLIENT_ID,
        "redirect_uri":  ALLEGRO_REDIRECT_URI,
        "state":         state,
    })
    return jsonify({"auth_url": f"{ALLEGRO_AUTH_BASE}/auth/oauth/authorize?{params}"})


@app.route("/api/allegro/auth/callback")
def allegro_auth_callback():
    code  = request.args.get("code")
    error = request.args.get("error")
    state = request.args.get("state", "")

    def _html_close(status: str, msg: str) -> str:
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
        <style>body{{font-family:sans-serif;display:flex;align-items:center;justify-content:center;
        height:100vh;background:#0f0f11;color:#e8e8ec;margin:0;}}
        .box{{text-align:center;padding:40px;border:1px solid rgba(255,255,255,.1);
        border-radius:16px;background:#17171a;}}</style></head>
        <body><div class="box"><h2>{msg}</h2><p style="color:#7c7c8a;margin-top:8px;">Możesz zamknąć to okno.</p></div>
        <script>window.opener&&window.opener.postMessage({{allegroAuth:'{status}'}}, '*');
        setTimeout(()=>window.close(),2000);</script></body></html>"""

    if error:
        return _html_close("error", f"Błąd: {error}"), 400

    if not code:
        return _html_close("error", "Brak kodu autoryzacyjnego."), 400

    # Decode user from state
    try:
        payload = json.loads(base64.urlsafe_b64decode(state.encode()).decode())
        user_id = payload["user_id"]
        user    = db.session.get(User, user_id)
        if not user:
            raise ValueError("Unknown user")
    except Exception:
        return _html_close("error", "Nieprawidłowy state parametr."), 400

    resp = requests.post(
        f"{ALLEGRO_AUTH_BASE}/auth/oauth/token",
        headers=_allegro_basic_auth_header(),
        data={"grant_type": "authorization_code", "code": code,
              "redirect_uri": ALLEGRO_REDIRECT_URI},
        timeout=15,
    )
    if not resp.ok:
        return _html_close("error", f"Błąd wymiany tokena ({resp.status_code})."), 500

    _save_allegro_token(user, resp.json())
    return _html_close("success", "✓ Autoryzacja zakończona!")


@app.route("/api/allegro/categories")
@login_required
def allegro_categories():
    name  = request.args.get("name", "")
    token = _get_valid_access_token(current_user)
    if not token:
        return jsonify({"error": "Not authorized"}), 401
    resp = requests.get(
        f"{ALLEGRO_API_BASE}/sale/categories",
        headers=_allegro_bearer_headers(token),
        params={"name": name} if name else {},
        timeout=10,
    )
    return (jsonify(resp.json()) if resp.ok
            else jsonify({"error": resp.text[:300]}), resp.status_code)


# ─── WC TEST ──────────────────────────────────────────────────────────────────

@app.route("/api/test-wc")
@login_required
def test_wc():
    try:
        s    = _ensure_settings()
        url  = f"{s.wc_store_url}/wp-json/wc/v3/products"
        resp = requests.get(url, auth=_wc_auth(s), params={"per_page": 1}, timeout=10)
        resp.raise_for_status()
        return jsonify({"success": True, "status": resp.status_code, "store": s.wc_store_url})
    except requests.exceptions.HTTPError as e:
        try:
            body = e.response.json()
        except Exception:
            body = e.response.text[:200]
        return jsonify({"success": False, "status": e.response.status_code, "error": body})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ─── Static / Index ───────────────────────────────────────────────────────────

@app.route("/")
def landing():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "landing.html")

@app.route("/app")
@login_required
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ─── ChatBot Module ───────────────────────────────────────────────────────────

from chatbot.models import init_models
init_models(db)   # rejestruje modele używając istniejącego db (bez circular import)

from chatbot import chatbot_bp
app.register_blueprint(chatbot_bp)

# ─── Init DB + Run ────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
