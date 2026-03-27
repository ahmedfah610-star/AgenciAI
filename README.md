# WooAI — Product Agent MVP

Turn a product photo into a fully-written WooCommerce listing in ~10 seconds.

## How it works

```
Image → Claude Vision → Store style analysis → LLM generates JSON → WooCommerce API
```

## Setup (5 minutes)

### 1. Clone & install

```bash
cd woo_ai_agent
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your actual values
```

| Variable | Where to find it |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys |
| `WC_STORE_URL` | Your WordPress site URL (no trailing slash) |
| `WC_CONSUMER_KEY` | WooCommerce → Settings → Advanced → REST API → Add Key |
| `WC_CONSUMER_SECRET` | Same as above — copy immediately, shown once |

**WooCommerce API permissions needed:** Read/Write

### 3. Test components

```bash
python test_components.py
```

All 3 tests should pass before you continue.

### 4. Start the server

```bash
python app.py
# Server running at http://localhost:5000
```

### 5. Open the UI

Open `index.html` in your browser (double-click or use a local server).

Set the API endpoint to `http://localhost:5000` (default).

## Usage

1. **Upload** a product image (JPG/PNG/WEBP)
2. Click **Analyze & Generate** — takes ~5-10s
3. **Review and edit** the generated title, description, category, tags, price
4. Click **Save to WooCommerce** — creates a Draft product
5. Review the draft in WooCommerce admin, then publish

## Cost estimate (per product)

| Step | Model | Approx cost |
|---|---|---|
| Vision analysis | claude-sonnet-4 | ~$0.003 |
| Product generation | claude-sonnet-4 | ~$0.005 |
| WooCommerce API | — | free |
| **Total** | | **~$0.01 per product** |

## Project structure

```
woo_ai_agent/
├── app.py              # Flask backend (all business logic)
├── index.html          # Single-file frontend UI
├── test_components.py  # Component-level tests
├── requirements.txt
└── .env.example
```

## API reference

### POST /api/analyze
```
multipart/form-data: image (file)
→ { success, image_description, product: { name, description, short_description, category, tags, price_suggestion } }
```

### POST /api/publish
```
JSON: { product: { name, description, short_description, category, tags, price_suggestion } }
→ { success, product_id, edit_url }
```

## Next improvements (post-MVP)

- [ ] Image upload to WooCommerce media library (attach image to product)
- [ ] Batch processing — upload a zip of images, generate all
- [ ] Price suggestion from competitor scraping
- [ ] Auto-detect variants (colors/sizes from image)
- [ ] Webhook trigger from Google Drive / Dropbox folder
- [ ] Fine-tune prompt with store-specific examples
- [ ] Slack/email notification when product is generated
