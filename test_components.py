"""
test_components.py — Run each component in isolation to verify setup.
Usage: python test_components.py
"""

import os
import sys
import json
import base64
import requests
from dotenv import load_dotenv

load_dotenv()

WC_STORE_URL       = os.getenv("WC_STORE_URL")
WC_CONSUMER_KEY    = os.getenv("WC_CONSUMER_KEY")
WC_CONSUMER_SECRET = os.getenv("WC_CONSUMER_SECRET")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")
WC_AUTH = (WC_CONSUMER_KEY, WC_CONSUMER_SECRET)


def test_woocommerce_connection():
    print("\n[1] Testing WooCommerce API connection...")
    url = f"{WC_STORE_URL}/wp-json/wc/v3/products"
    try:
        resp = requests.get(url, auth=WC_AUTH, params={"per_page": 3}, timeout=10)
        resp.raise_for_status()
        products = resp.json()
        print(f"    ✓ Connected. Found {len(products)} products.")
        for p in products:
            print(f"      - {p['name']} (${p.get('price', 'N/A')})")
        return True
    except Exception as e:
        print(f"    ✗ Failed: {e}")
        return False


def test_anthropic_vision():
    print("\n[2] Testing Anthropic Vision API...")
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    # Download a sample product image for testing
    img_url = "https://images.unsplash.com/photo-1523275335684-37898b6baf30?w=400"
    try:
        img_resp = requests.get(img_url, timeout=15)
        img_b64 = base64.standard_b64encode(img_resp.content).decode()

        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                    {"type": "text", "text": "Describe this product in 2 sentences for an e-commerce listing."}
                ]
            }]
        )
        print(f"    ✓ Vision working. Response: {resp.content[0].text[:150]}...")
        return True
    except Exception as e:
        print(f"    ✗ Failed: {e}")
        return False


def test_product_generation():
    print("\n[3] Testing product generation prompt...")
    from anthropic import Anthropic
    import re
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    sample_desc = "A luxury minimalist wristwatch with a brushed silver stainless steel case, black leather strap, and clean white dial with Roman numerals. Swiss quartz movement. Case diameter 40mm."
    sample_store = "Product 1: Premium Leather Wallet\n  Short desc: Hand-crafted full-grain leather wallet\n  Categories: Accessories\n  Tags: leather, wallet, handmade\n\n"

    prompt = f"""
You are an expert WooCommerce copywriter.

IMAGE: {sample_desc}

STORE STYLE: {sample_store}

Output ONLY valid JSON (no markdown):
{{"name":"...", "description":"...", "short_description":"...", "category":"...", "tags":["..."], "price_suggestion":"..."}}
"""
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        print(f"    ✓ Generated product:")
        print(f"      Name: {data.get('name')}")
        print(f"      Category: {data.get('category')}")
        print(f"      Tags: {data.get('tags')}")
        print(f"      Price suggestion: {data.get('price_suggestion')}")
        return True
    except Exception as e:
        print(f"    ✗ Failed: {e}")
        return False


if __name__ == "__main__":
    results = []
    results.append(test_woocommerce_connection())
    results.append(test_anthropic_vision())
    results.append(test_product_generation())

    print(f"\n{'='*40}")
    passed = sum(results)
    print(f"Results: {passed}/{len(results)} tests passed")
    if passed == len(results):
        print("✓ All systems go — run `python app.py` to start the server")
    else:
        print("✗ Fix the failing tests before starting the server")
    sys.exit(0 if passed == len(results) else 1)
