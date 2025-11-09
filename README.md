# Whop Pulse Hook

A minimal FastAPI project bundled with a Playwright helper that injects the exact WebSocket hook you provided into [`https://whop.com/pulse/`](https://whop.com/pulse/). The API proves the service is reachable, while the script opens Chromium with the hook so you can watch every Pulse frame live.

## Requirements

- Python 3.11+
- Playwright browsers (`playwright install chromium`)

Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## FastAPI app

Start the barebones API:

```bash
uvicorn app.main:app --reload
```

`GET /` responds with a short status message plus a reminder on how to run the hook script.

## WebSocket hook script

Launch the watcher exactly as in your snippet:

```bash
python -m app.pulse_client
```

The script injects a tiny snippet that overrides `window.WebSocket`, logs every connection, and prints both text and base64-encoded binary payloads directly to your terminal. Keep the browser open and interact with the page to trigger more activity.

Set `PULSE_PLAYWRIGHT_HEADLESS=true` (in your shell or `.env`) to launch Chromium without a visible window; leave it unset/`false` for the default visible mode.

When protobuf frames include pricing data, the decoder now echoes friendly summary lines that list the amount, listing name, and the full marketplace URL (built from the slug embedded in the payload) so you can jump straight to products worth tracking.

## Listing extractor

Need the structured details for a specific marketplace page such as [`https://whop.com/iris-out-5c`](https://whop.com/iris-out-5c)? Use the scraper CLI:

```bash
python -m app.listing_scraper whop.com/iris-out-5c
```

The scraper now launches headless Chromium via Playwright right away so JavaScript-driven pricing widgets render before any DOM parsing happens. It still performs the plain HTTP request pair (HTML page + Next.js data file) and dumps a JSON document that contains:

- Basic page metadata (title, descriptions, OG tags, HTTP status, final URL)
- Every JSON-LD block parsed into products/offers/organizations so you always get prices, billing cadence, feature lists, etc.
- A `content` snapshot derived from the Next.js payload (hero title/subtitle, CTA button, feature sections, and the primary body copy) plus a backup scraper that walks `<h2>` headings labeled “Features” or “FAQs” directly in the HTML to capture the on-page bullet lists.
- If you point the CLI at a seller profile like `whop.com/username`, it automatically discovers every linked `whop.com/username/product` page (up to eight) and returns the full product snapshots for each one.

Use `--timeout` if you need to tweak the request deadline. Because the helper depends on Playwright's Chromium build, ensure you've run `playwright install chromium` beforehand (as shown in the requirements section).
