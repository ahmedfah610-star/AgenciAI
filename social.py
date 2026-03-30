"""
Social Media Agent — Flask Blueprint
Publish photos & videos to Facebook Page, Instagram, and TikTok.
AI-powered caption & hashtag generation via Anthropic Claude.
"""

import os
import base64
import json
import time
import secrets
import requests
from io import BytesIO
from flask import (Blueprint, request, jsonify, session)
from flask_login import login_required, current_user
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

social_bp = Blueprint("social", __name__)

# ─── Config ───────────────────────────────────────────────────────────────────

META_APP_ID       = os.getenv("META_APP_ID", "")
META_APP_SECRET   = os.getenv("META_APP_SECRET", "")
META_REDIRECT_URI = os.getenv("META_REDIRECT_URI",
                               "http://localhost:5000/api/social/meta/auth/callback")

TIKTOK_CLIENT_KEY    = os.getenv("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET", "")
TIKTOK_REDIRECT_URI  = os.getenv("TIKTOK_REDIRECT_URI",
                                  "http://localhost:5000/api/social/tiktok/auth/callback")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
anthropic_client  = Anthropic(api_key=ANTHROPIC_API_KEY)

# Meta Graph API version
META_GRAPH_VERSION = "v25.0"
META_GRAPH_BASE    = f"https://graph.facebook.com/{META_GRAPH_VERSION}"

# ─── Lazy DB access ──────────────────────────────────────────────────────────
# We import db and SocialToken at runtime to avoid circular imports.

def _get_db():
    from app import db
    return db

def _get_social_token_model():
    from app import SocialToken
    return SocialToken

def _get_app_logger():
    from app import app
    return app.logger


# ─── Helper: get / save token ────────────────────────────────────────────────

def _get_social_token(user_id: int, platform: str):
    SocialToken = _get_social_token_model()
    return SocialToken.query.filter_by(user_id=user_id, platform=platform).first()


def _save_social_token(user_id: int, platform: str, data: dict):
    db = _get_db()
    SocialToken = _get_social_token_model()
    token = SocialToken.query.filter_by(user_id=user_id, platform=platform).first()
    if not token:
        token = SocialToken(user_id=user_id, platform=platform)
        db.session.add(token)
    token.access_token  = data.get("access_token", "")
    token.refresh_token = data.get("refresh_token", "")
    token.page_id       = data.get("page_id", "")
    token.page_name     = data.get("page_name", "")
    token.ig_user_id    = data.get("ig_user_id", "")
    token.expires_at    = data.get("expires_at", 0.0)
    db.session.commit()
    return token


def _delete_social_token(user_id: int, platform: str):
    db = _get_db()
    SocialToken = _get_social_token_model()
    token = SocialToken.query.filter_by(user_id=user_id, platform=platform).first()
    if token:
        db.session.delete(token)
        db.session.commit()


# ═══════════════════════════════════════════════════════════════════════════════
#  AI ANALYZE — generate caption & hashtags
# ═══════════════════════════════════════════════════════════════════════════════

@social_bp.route("/api/social/analyze", methods=["POST"])
@login_required
def social_analyze():
    """Analyze uploaded image/video and generate social media caption + hashtags."""
    logger = _get_app_logger()

    media_file = request.files.get("media")
    topic      = request.form.get("topic", "").strip()
    platform   = request.form.get("platform", "all").strip()  # hint for style

    if not media_file:
        return jsonify({"error": "Brak pliku media"}), 400

    filename  = media_file.filename or ""
    mime_type = media_file.content_type or ""
    raw_data  = media_file.read()

    is_video = mime_type.startswith("video/") or filename.lower().endswith(
        (".mp4", ".mov", ".avi", ".mkv", ".webm")
    )
    is_image = mime_type.startswith("image/") or filename.lower().endswith(
        (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
    )

    if not is_video and not is_image:
        return jsonify({"error": "Nieobsługiwany format pliku. Wyślij zdjęcie lub wideo."}), 400

    # Build Claude prompt
    topic_hint = ""
    if topic:
        topic_hint = f'\n\nDodatkowy kontekst od użytkownika: "{topic}". Użyj tego do lepszego zrozumienia produktu/treści.'

    platform_hint = ""
    if platform and platform != "all":
        platform_hint = f"\nOptymalizuj styl pod platformę: {platform}."

    content_parts = []

    if is_image:
        # Convert to appropriate MIME for Claude
        claude_mime = mime_type
        if claude_mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            claude_mime = "image/jpeg"

        image_b64 = base64.standard_b64encode(raw_data).decode()
        content_parts.append({
            "type": "image",
            "source": {"type": "base64", "media_type": claude_mime, "data": image_b64},
        })
        media_instruction = "Analizujesz ZDJĘCIE do posta na social media."
    else:
        # For video: we can't send video to Claude directly, so we describe context
        media_instruction = (
            "Użytkownik chce opublikować WIDEO na social media. "
            "Nie widzisz wideo, ale na podstawie kontekstu podanego przez użytkownika, "
            "wygeneruj angażujący opis i hashtagi."
        )

    content_parts.append({
        "type": "text",
        "text": (
            f"{media_instruction}{topic_hint}{platform_hint}\n\n"
            "Jesteś ekspertem od social media marketingu w Polsce. "
            "Twórz treści, które generują zaangażowanie.\n\n"
            "Wygeneruj TYLKO prawidłowy JSON (bez markdown, bez preambuły):\n"
            "{\n"
            '  "caption": "Angażujący opis posta po polsku (2-4 zdania, z emoji). '
            'Powinien wzbudzać ciekawość i zachęcać do interakcji.",\n'
            '  "hashtags": ["hashtag1", "hashtag2", ...],\n'
            '  "media_type": "image" lub "video",\n'
            '  "suggested_cta": "Sugerowane call-to-action (np. Kliknij link w bio!)"\n'
            "}\n\n"
            "Zasady:\n"
            "- Hashtagi bez znaku # (sam tekst)\n"
            "- 5-15 hashtagów, mix popularnych i niche\n"
            "- Caption ma być naturalny, nie reklamowy\n"
            "- Dodaj emoji strategicznie\n"
            "- Jeśli widzisz produkt — opisz go atrakcyjnie\n"
            "- Sugeruj CTA dopasowane do treści"
        ),
    })

    try:
        resp = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[{"role": "user", "content": content_parts}],
        )
        raw_text = resp.content[0].text.strip()
        # Strip markdown fences if present
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[1] if "\n" in raw_text else raw_text[3:]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3]
        raw_text = raw_text.strip()

        result = json.loads(raw_text)
        result["media_type"] = "video" if is_video else "image"
        return jsonify(result)

    except json.JSONDecodeError:
        logger.error("AI returned non-JSON for social analyze: %s", raw_text[:500])
        return jsonify({
            "caption": raw_text[:300] if raw_text else "Sprawdź nasz najnowszy post! 🔥",
            "hashtags": ["nowość", "polskamarka", "musthave"],
            "media_type": "video" if is_video else "image",
            "suggested_cta": "Link w bio! 👆",
        })
    except Exception as e:
        logger.error("Social analyze error: %s", e)
        return jsonify({"error": f"Błąd analizy AI: {e}"}), 500


# ═══════════════════════════════════════════════════════════════════════════════
#  STATUS — which platforms are connected
# ═══════════════════════════════════════════════════════════════════════════════

@social_bp.route("/api/social/status")
@login_required
def social_status():
    fb_token = _get_social_token(current_user.id, "facebook")
    ig_token = _get_social_token(current_user.id, "instagram")
    tk_token = _get_social_token(current_user.id, "tiktok")

    return jsonify({
        "facebook": {
            "connected": bool(fb_token and fb_token.access_token),
            "page_name": fb_token.page_name if fb_token else "",
            "configured": bool(META_APP_ID),
        },
        "instagram": {
            "connected": bool(ig_token and ig_token.access_token),
            "configured": bool(META_APP_ID),
        },
        "tiktok": {
            "connected": bool(tk_token and tk_token.access_token),
            "configured": bool(TIKTOK_CLIENT_KEY),
        },
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  META OAUTH (Facebook + Instagram share same flow)
# ═══════════════════════════════════════════════════════════════════════════════

@social_bp.route("/api/social/meta/auth/start")
@login_required
def meta_auth_start():
    if not META_APP_ID:
        return jsonify({"error": "META_APP_ID nie ustawiony w .env"}), 400

    import urllib.parse
    state = base64.urlsafe_b64encode(
        json.dumps({"user_id": current_user.id, "nonce": secrets.token_hex(8)}).encode()
    ).decode()
    session["meta_oauth_state"] = state

    scopes = "public_profile"

    params = urllib.parse.urlencode({
        "client_id":    META_APP_ID,
        "redirect_uri": META_REDIRECT_URI,
        "state":        state,
        "scope":        scopes,
        "response_type": "code",
    })
    return jsonify({
        "auth_url": f"https://www.facebook.com/{META_GRAPH_VERSION}/dialog/oauth?{params}"
    })


@social_bp.route("/api/social/meta/auth/callback")
def meta_auth_callback():
    code  = request.args.get("code")
    error = request.args.get("error")
    state = request.args.get("state", "")
    logger = _get_app_logger()

    def _html_close(status: str, msg: str) -> str:
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
        <style>body{{font-family:'Inter',sans-serif;display:flex;align-items:center;justify-content:center;
        height:100vh;background:#fbfbfd;color:#1d1d1f;margin:0;}}
        .box{{text-align:center;padding:40px;border:1px solid #e8e8ed;
        border-radius:16px;background:#fff;}}</style></head>
        <body><div class="box"><h2>{msg}</h2><p style="color:#6e6e73;margin-top:8px;">Możesz zamknąć to okno.</p></div>
        <script>window.opener&&window.opener.postMessage({{metaAuth:'{status}'}}, '*');
        setTimeout(()=>window.close(),2000);</script></body></html>"""

    if error:
        return _html_close("error", f"Błąd: {error}"), 400
    if not code:
        return _html_close("error", "Brak kodu autoryzacyjnego."), 400

    # Decode user from state
    try:
        payload = json.loads(base64.urlsafe_b64decode(state.encode()).decode())
        user_id = payload["user_id"]
    except Exception:
        return _html_close("error", "Nieprawidłowy state."), 400

    # Exchange code for short-lived token
    resp = requests.get(f"{META_GRAPH_BASE}/oauth/access_token", params={
        "client_id":     META_APP_ID,
        "client_secret": META_APP_SECRET,
        "redirect_uri":  META_REDIRECT_URI,
        "code":          code,
    }, timeout=15)

    if not resp.ok:
        logger.error("Meta token exchange failed: %s", resp.text[:400])
        return _html_close("error", f"Błąd wymiany tokena ({resp.status_code})."), 500

    short_token = resp.json().get("access_token", "")

    # Exchange for long-lived token (60 days)
    ll_resp = requests.get(f"{META_GRAPH_BASE}/oauth/access_token", params={
        "grant_type":    "fb_exchange_token",
        "client_id":     META_APP_ID,
        "client_secret": META_APP_SECRET,
        "fb_exchange_token": short_token,
    }, timeout=15)

    if ll_resp.ok:
        ll_data = ll_resp.json()
        user_token = ll_data.get("access_token", short_token)
        expires_in = ll_data.get("expires_in", 5184000)
    else:
        user_token = short_token
        expires_in = 3600

    # Get user's Facebook Pages
    pages_resp = requests.get(f"{META_GRAPH_BASE}/me/accounts", params={
        "access_token": user_token,
        "fields": "id,name,access_token,instagram_business_account",
    }, timeout=15)

    if not pages_resp.ok:
        logger.error("Meta pages fetch failed: %s", pages_resp.text[:400])
        return _html_close("error", "Nie udało się pobrać stron."), 500

    pages = pages_resp.json().get("data", [])
    if not pages:
        return _html_close("error", "Brak stron firmowych na tym koncie. Utwórz Facebook Page."), 400

    # Use first page (user can change later)
    page = pages[0]
    page_token = page.get("access_token", "")
    page_id    = page.get("id", "")
    page_name  = page.get("name", "")

    # Save Facebook token
    _save_social_token(user_id, "facebook", {
        "access_token":  page_token,
        "refresh_token": user_token,  # store user token for refresh
        "page_id":       page_id,
        "page_name":     page_name,
        "expires_at":    time.time() + expires_in,
    })

    # Check for Instagram Business Account linked to this page
    ig_account = page.get("instagram_business_account")
    if ig_account:
        ig_id = ig_account.get("id", "")
        _save_social_token(user_id, "instagram", {
            "access_token": page_token,  # IG uses page token
            "page_id":      page_id,
            "ig_user_id":   ig_id,
            "page_name":    page_name,
            "expires_at":   time.time() + expires_in,
        })
        return _html_close("success", f"✓ Połączono Facebook ({page_name}) + Instagram!")

    return _html_close("success", f"✓ Połączono Facebook Page: {page_name}")


@social_bp.route("/api/social/meta/disconnect", methods=["POST"])
@login_required
def meta_disconnect():
    _delete_social_token(current_user.id, "facebook")
    _delete_social_token(current_user.id, "instagram")
    return jsonify({"success": True})


# ═══════════════════════════════════════════════════════════════════════════════
#  TIKTOK OAUTH
# ═══════════════════════════════════════════════════════════════════════════════

@social_bp.route("/api/social/tiktok/auth/start")
@login_required
def tiktok_auth_start():
    if not TIKTOK_CLIENT_KEY:
        return jsonify({"error": "TIKTOK_CLIENT_KEY nie ustawiony w .env"}), 400

    import urllib.parse
    state = base64.urlsafe_b64encode(
        json.dumps({"user_id": current_user.id, "nonce": secrets.token_hex(8)}).encode()
    ).decode()
    session["tiktok_oauth_state"] = state

    # PKCE code challenge (S256) — required by TikTok
    code_verifier  = secrets.token_urlsafe(64)
    session["tiktok_code_verifier"] = code_verifier

    import hashlib
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).decode().rstrip("=")

    params = urllib.parse.urlencode({
        "client_key":             TIKTOK_CLIENT_KEY,
        "response_type":          "code",
        "scope":                  "video.publish,video.upload,user.info.basic",
        "redirect_uri":           TIKTOK_REDIRECT_URI,
        "state":                  state,
        "code_challenge":         code_challenge,
        "code_challenge_method":  "S256",
    })
    return jsonify({
        "auth_url": f"https://www.tiktok.com/v2/auth/authorize/?{params}"
    })


@social_bp.route("/api/social/tiktok/auth/callback")
def tiktok_auth_callback():
    code  = request.args.get("code")
    error = request.args.get("error")
    state = request.args.get("state", "")
    logger = _get_app_logger()

    def _html_close(status: str, msg: str) -> str:
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
        <style>body{{font-family:'Inter',sans-serif;display:flex;align-items:center;justify-content:center;
        height:100vh;background:#fbfbfd;color:#1d1d1f;margin:0;}}
        .box{{text-align:center;padding:40px;border:1px solid #e8e8ed;
        border-radius:16px;background:#fff;}}</style></head>
        <body><div class="box"><h2>{msg}</h2><p style="color:#6e6e73;margin-top:8px;">Możesz zamknąć to okno.</p></div>
        <script>window.opener&&window.opener.postMessage({{tiktokAuth:'{status}'}}, '*');
        setTimeout(()=>window.close(),2000);</script></body></html>"""

    if error:
        return _html_close("error", f"Błąd: {error}"), 400
    if not code:
        return _html_close("error", "Brak kodu autoryzacyjnego."), 400

    try:
        payload = json.loads(base64.urlsafe_b64decode(state.encode()).decode())
        user_id = payload["user_id"]
    except Exception:
        return _html_close("error", "Nieprawidłowy state."), 400

    code_verifier = session.get("tiktok_code_verifier", "")

    # Exchange code for token
    resp = requests.post("https://open.tiktokapis.com/v2/oauth/token/", data={
        "client_key":     TIKTOK_CLIENT_KEY,
        "client_secret":  TIKTOK_CLIENT_SECRET,
        "code":           code,
        "grant_type":     "authorization_code",
        "redirect_uri":   TIKTOK_REDIRECT_URI,
        "code_verifier":  code_verifier,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15)

    if not resp.ok:
        logger.error("TikTok token exchange failed: %s", resp.text[:400])
        return _html_close("error", f"Błąd wymiany tokena ({resp.status_code})."), 500

    token_data = resp.json()
    access_token  = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    expires_in    = token_data.get("expires_in", 86400)
    open_id       = token_data.get("open_id", "")

    _save_social_token(user_id, "tiktok", {
        "access_token":  access_token,
        "refresh_token": refresh_token,
        "page_id":       open_id,
        "expires_at":    time.time() + expires_in,
    })

    return _html_close("success", "✓ Połączono z TikTok!")


@social_bp.route("/api/social/tiktok/disconnect", methods=["POST"])
@login_required
def tiktok_disconnect():
    _delete_social_token(current_user.id, "tiktok")
    return jsonify({"success": True})


# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLISH — FACEBOOK
# ═══════════════════════════════════════════════════════════════════════════════

@social_bp.route("/api/social/publish/facebook", methods=["POST"])
@login_required
def publish_facebook():
    logger = _get_app_logger()
    token  = _get_social_token(current_user.id, "facebook")

    if not token or not token.access_token:
        return jsonify({"success": False, "error": "Facebook nie podłączony"}), 401

    caption    = request.form.get("caption", "")
    media_file = request.files.get("media")
    media_type = request.form.get("media_type", "image")

    page_id      = token.page_id
    access_token = token.access_token

    try:
        if media_type == "video" and media_file:
            # Upload video to Facebook Page
            resp = requests.post(
                f"https://graph-video.facebook.com/{META_GRAPH_VERSION}/{page_id}/videos",
                data={"description": caption, "access_token": access_token},
                files={"source": (media_file.filename, media_file.read(), media_file.content_type)},
                timeout=120,
            )
        elif media_file:
            # Upload photo to Facebook Page
            resp = requests.post(
                f"{META_GRAPH_BASE}/{page_id}/photos",
                data={"caption": caption, "access_token": access_token},
                files={"source": (media_file.filename, media_file.read(), media_file.content_type)},
                timeout=60,
            )
        else:
            # Text-only post
            resp = requests.post(
                f"{META_GRAPH_BASE}/{page_id}/feed",
                data={"message": caption, "access_token": access_token},
                timeout=30,
            )

        if resp.ok:
            post_data = resp.json()
            post_id = post_data.get("id") or post_data.get("post_id", "")
            return jsonify({
                "success": True,
                "post_id": post_id,
                "platform": "facebook",
                "page_name": token.page_name,
            })
        else:
            error_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            error_msg = error_data.get("error", {}).get("message", resp.text[:300])
            logger.error("Facebook publish failed: %s", error_msg)
            return jsonify({"success": False, "error": f"Facebook: {error_msg}"})

    except Exception as e:
        logger.error("Facebook publish exception: %s", e)
        return jsonify({"success": False, "error": f"Facebook: {e}"})


# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLISH — INSTAGRAM
# ═══════════════════════════════════════════════════════════════════════════════

@social_bp.route("/api/social/publish/instagram", methods=["POST"])
@login_required
def publish_instagram():
    logger = _get_app_logger()
    token  = _get_social_token(current_user.id, "instagram")

    if not token or not token.access_token or not token.ig_user_id:
        return jsonify({"success": False, "error": "Instagram nie podłączony"}), 401

    caption    = request.form.get("caption", "")
    image_url  = request.form.get("image_url", "")  # Must be public URL
    media_type = request.form.get("media_type", "image")

    ig_user_id   = token.ig_user_id
    access_token = token.access_token

    if not image_url:
        return jsonify({
            "success": False,
            "error": "Instagram wymaga publicznego URL zdjęcia. Skonfiguruj WooCommerce lub wgraj zdjęcie na serwer."
        })

    try:
        # Step 1: Create media container
        container_params = {
            "caption":      caption,
            "access_token": access_token,
        }

        if media_type == "video":
            container_params["video_url"]  = image_url
            container_params["media_type"] = "VIDEO"
        else:
            container_params["image_url"] = image_url

        container_resp = requests.post(
            f"{META_GRAPH_BASE}/{ig_user_id}/media",
            data=container_params,
            timeout=30,
        )

        if not container_resp.ok:
            err = container_resp.json().get("error", {}).get("message", container_resp.text[:300])
            return jsonify({"success": False, "error": f"Instagram container: {err}"})

        creation_id = container_resp.json().get("id")
        if not creation_id:
            return jsonify({"success": False, "error": "Instagram: brak creation_id"})

        # Step 2: Wait for container to be ready (poll)
        for _ in range(10):
            time.sleep(2)
            status_resp = requests.get(
                f"{META_GRAPH_BASE}/{creation_id}",
                params={"fields": "status_code", "access_token": access_token},
                timeout=10,
            )
            if status_resp.ok:
                status = status_resp.json().get("status_code", "")
                if status == "FINISHED":
                    break
                elif status == "ERROR":
                    return jsonify({"success": False, "error": "Instagram: błąd przetwarzania media"})

        # Step 3: Publish the container
        publish_resp = requests.post(
            f"{META_GRAPH_BASE}/{ig_user_id}/media_publish",
            data={"creation_id": creation_id, "access_token": access_token},
            timeout=30,
        )

        if publish_resp.ok:
            media_id = publish_resp.json().get("id", "")
            return jsonify({
                "success": True,
                "post_id": media_id,
                "platform": "instagram",
            })
        else:
            err = publish_resp.json().get("error", {}).get("message", publish_resp.text[:300])
            return jsonify({"success": False, "error": f"Instagram publish: {err}"})

    except Exception as e:
        logger.error("Instagram publish exception: %s", e)
        return jsonify({"success": False, "error": f"Instagram: {e}"})


# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLISH — TIKTOK
# ═══════════════════════════════════════════════════════════════════════════════

@social_bp.route("/api/social/publish/tiktok", methods=["POST"])
@login_required
def publish_tiktok():
    logger = _get_app_logger()
    token  = _get_social_token(current_user.id, "tiktok")

    if not token or not token.access_token:
        return jsonify({"success": False, "error": "TikTok nie podłączony"}), 401

    caption    = request.form.get("caption", "")
    media_file = request.files.get("media")

    if not media_file:
        return jsonify({"success": False, "error": "TikTok wymaga pliku wideo"}), 400

    access_token = token.access_token
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type":  "application/json; charset=UTF-8",
    }

    try:
        video_data = media_file.read()
        video_size = len(video_data)

        # Step 1: Initialize upload
        init_body = {
            "post_info": {
                "title":          caption[:150],
                "privacy_level":  "SELF_ONLY",  # Safe default; change after audit
                "disable_duet":   False,
                "disable_stitch": False,
                "disable_comment": False,
            },
            "source_info": {
                "source":            "FILE_UPLOAD",
                "video_size":        video_size,
                "chunk_size":        video_size,
                "total_chunk_count": 1,
            },
        }

        init_resp = requests.post(
            "https://open.tiktokapis.com/v2/post/publish/video/init/",
            headers=headers,
            json=init_body,
            timeout=30,
        )

        if not init_resp.ok:
            err_data = init_resp.json() if init_resp.headers.get("content-type", "").startswith("application/json") else {}
            error_msg = err_data.get("error", {}).get("message", init_resp.text[:300])
            return jsonify({"success": False, "error": f"TikTok init: {error_msg}"})

        init_data  = init_resp.json().get("data", {})
        publish_id = init_data.get("publish_id", "")
        upload_url = init_data.get("upload_url", "")

        if not upload_url:
            return jsonify({"success": False, "error": "TikTok: brak upload_url"})

        # Step 2: Upload video chunk
        upload_headers = {
            "Content-Type":  "video/mp4",
            "Content-Length": str(video_size),
            "Content-Range":  f"bytes 0-{video_size - 1}/{video_size}",
        }
        upload_resp = requests.put(upload_url, data=video_data,
                                   headers=upload_headers, timeout=120)

        if not upload_resp.ok:
            return jsonify({"success": False, "error": f"TikTok upload: {upload_resp.status_code}"})

        # Step 3: Poll for publish status
        for attempt in range(15):
            time.sleep(3)
            status_resp = requests.post(
                "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
                headers=headers,
                json={"publish_id": publish_id},
                timeout=10,
            )
            if status_resp.ok:
                status_data = status_resp.json().get("data", {})
                pub_status  = status_data.get("status", "")
                if pub_status == "PUBLISH_COMPLETE":
                    return jsonify({
                        "success": True,
                        "post_id": publish_id,
                        "platform": "tiktok",
                    })
                elif pub_status in ("FAILED", "PUBLISH_FAILED"):
                    fail_reason = status_data.get("fail_reason", "Unknown")
                    return jsonify({"success": False, "error": f"TikTok: {fail_reason}"})

        return jsonify({
            "success": True,
            "post_id": publish_id,
            "platform": "tiktok",
            "note": "Wideo przesłane — TikTok przetwarza publikację.",
        })

    except Exception as e:
        logger.error("TikTok publish exception: %s", e)
        return jsonify({"success": False, "error": f"TikTok: {e}"})


# ═══════════════════════════════════════════════════════════════════════════════
#  UNIFIED PUBLISH — publish to all selected platforms at once
# ═══════════════════════════════════════════════════════════════════════════════

@social_bp.route("/api/social/publish", methods=["POST"])
@login_required
def social_publish_all():
    """Publish to multiple selected platforms at once."""
    logger = _get_app_logger()

    caption    = request.form.get("caption", "")
    platforms  = json.loads(request.form.get("platforms", "[]"))
    media_file = request.files.get("media")
    media_type = request.form.get("media_type", "image")

    results = {}

    for platform in platforms:
        if platform == "facebook":
            # Re-read file for each platform
            if media_file:
                media_file.seek(0)
            with _make_request_context(request, media_file, caption, media_type) as ctx:
                results["facebook"] = _publish_to_facebook(ctx)

        elif platform == "instagram":
            results["instagram"] = {"success": False,
                                     "error": "Instagram wymaga publicznego URL — użyj dedykowanego przycisku."}

        elif platform == "tiktok":
            if media_file:
                media_file.seek(0)
            with _make_request_context(request, media_file, caption, media_type) as ctx:
                results["tiktok"] = _publish_to_tiktok(ctx)

    any_ok = any(r.get("success") for r in results.values())
    return jsonify({"success": any_ok, "results": results})


# Internal helper — just reuses the individual endpoint logic inline
# For a cleaner approach, we call the publish functions directly

def _publish_fb_direct(user_id, caption, media_file, media_type):
    """Direct Facebook publish without going through Flask route."""
    logger = _get_app_logger()
    token  = _get_social_token(user_id, "facebook")
    if not token or not token.access_token:
        return {"success": False, "error": "Facebook nie podłączony"}

    page_id      = token.page_id
    access_token = token.access_token

    try:
        if media_type == "video" and media_file:
            resp = requests.post(
                f"https://graph-video.facebook.com/{META_GRAPH_VERSION}/{page_id}/videos",
                data={"description": caption, "access_token": access_token},
                files={"source": (media_file.filename, media_file.read(), media_file.content_type)},
                timeout=120,
            )
        elif media_file:
            resp = requests.post(
                f"{META_GRAPH_BASE}/{page_id}/photos",
                data={"caption": caption, "access_token": access_token},
                files={"source": (media_file.filename, media_file.read(), media_file.content_type)},
                timeout=60,
            )
        else:
            resp = requests.post(
                f"{META_GRAPH_BASE}/{page_id}/feed",
                data={"message": caption, "access_token": access_token},
                timeout=30,
            )

        if resp.ok:
            post_data = resp.json()
            return {"success": True, "post_id": post_data.get("id", ""), "platform": "facebook",
                    "page_name": token.page_name}
        else:
            error_data = resp.json() if "application/json" in resp.headers.get("content-type", "") else {}
            error_msg = error_data.get("error", {}).get("message", resp.text[:300])
            return {"success": False, "error": f"Facebook: {error_msg}"}
    except Exception as e:
        return {"success": False, "error": f"Facebook: {e}"}


@social_bp.route("/api/social/publish-all", methods=["POST"])
@login_required
def publish_all():
    """Unified publish to all selected platforms."""
    logger = _get_app_logger()

    caption    = request.form.get("caption", "")
    platforms  = json.loads(request.form.get("platforms", "[]"))
    media_file = request.files.get("media")
    media_type = request.form.get("media_type", "image")

    results = {}

    for platform in platforms:
        if media_file:
            media_file.seek(0)

        if platform == "facebook":
            results["facebook"] = _publish_fb_direct(
                current_user.id, caption, media_file, media_type)

        elif platform == "instagram":
            results["instagram"] = {
                "success": False,
                "error": "Instagram wymaga publicznego URL zdjęcia. Opublikuj najpierw na WooCommerce lub podaj URL."
            }

        elif platform == "tiktok":
            tk_token = _get_social_token(current_user.id, "tiktok")
            if not tk_token or not tk_token.access_token:
                results["tiktok"] = {"success": False, "error": "TikTok nie podłączony"}
            elif media_type != "video":
                results["tiktok"] = {"success": False, "error": "TikTok obsługuje tylko wideo"}
            elif not media_file:
                results["tiktok"] = {"success": False, "error": "Brak pliku wideo"}
            else:
                # Direct TikTok publish
                try:
                    video_data = media_file.read()
                    video_size = len(video_data)
                    headers_tk = {
                        "Authorization": f"Bearer {tk_token.access_token}",
                        "Content-Type":  "application/json; charset=UTF-8",
                    }
                    init_body = {
                        "post_info": {
                            "title": caption[:150],
                            "privacy_level": "SELF_ONLY",
                            "disable_duet": False,
                            "disable_stitch": False,
                            "disable_comment": False,
                        },
                        "source_info": {
                            "source": "FILE_UPLOAD",
                            "video_size": video_size,
                            "chunk_size": video_size,
                            "total_chunk_count": 1,
                        },
                    }
                    init_resp = requests.post(
                        "https://open.tiktokapis.com/v2/post/publish/video/init/",
                        headers=headers_tk, json=init_body, timeout=30)
                    if init_resp.ok:
                        init_data = init_resp.json().get("data", {})
                        upload_url = init_data.get("upload_url", "")
                        publish_id = init_data.get("publish_id", "")
                        if upload_url:
                            upload_resp = requests.put(upload_url, data=video_data,
                                headers={"Content-Type": "video/mp4",
                                         "Content-Length": str(video_size),
                                         "Content-Range": f"bytes 0-{video_size-1}/{video_size}"},
                                timeout=120)
                            results["tiktok"] = {
                                "success": upload_resp.ok,
                                "post_id": publish_id,
                                "platform": "tiktok",
                                "note": "Wideo przesłane — TikTok przetwarza." if upload_resp.ok else f"Upload error: {upload_resp.status_code}",
                            }
                        else:
                            results["tiktok"] = {"success": False, "error": "TikTok: brak upload_url"}
                    else:
                        results["tiktok"] = {"success": False, "error": f"TikTok init: {init_resp.text[:200]}"}
                except Exception as e:
                    results["tiktok"] = {"success": False, "error": f"TikTok: {e}"}

    any_ok = any(r.get("success") for r in results.values())
    return jsonify({"success": any_ok, "results": results})
