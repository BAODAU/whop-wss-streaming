# Whop Pulse Tools

Headless Playwright utilities to watch the Whop Pulse WebSocket feed and scrape individual listing pages with full JavaScript rendering.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (for dependency and run management)
- Playwright browsers (`playwright install chromium`)

## Setup with uv

```bash
uv venv
source .venv/bin/activate
uv pip sync uv.lock
playwright install chromium
```

Prefer not to activate the environment? Use `uv run <command>` and uv will automatically build the isolated interpreter before executing the command.

## Pulse WebSocket watcher

Inject the hook into [`https://whop.com/pulse/`](https://whop.com/pulse/) exactly like your original snippet, but with automatic protobuf decoding and listing fetches:

```bash
uv run python -m app.pulse_client
```

The script overrides `window.WebSocket`, prints text/binary payloads, and reports human-friendly summaries (price, listing name, vendor, and canonical URL). Set `PULSE_PLAYWRIGHT_HEADLESS=true` (shell or `.env`) to force Chromium to run headlessly—containers now default to headless mode.

When protobuf frames include pricing data, the decoder emits readable JSON and automatically schedules a listing snapshot fetch for any unseen product URLs.

## Listing extractor

Need structured details for a marketplace page such as [`https://whop.com/iris-out-5c`](https://whop.com/iris-out-5c)? Run the scraper CLI:

```bash
uv run python -m app.listing_scraper whop.com/iris-out-5c
```

The scraper launches headless Chromium immediately so JavaScript-driven pricing widgets render before DOM parsing. It still performs the plain HTTP request pair (HTML page + Next.js data file) and returns a JSON document with:

- Basic page metadata (title, descriptions, OG tags, HTTP status, final URL)
- Every JSON-LD block parsed into products/offers/organizations so you always get prices, billing cadence, feature lists, etc.
- A `content` snapshot derived from the Next.js payload (hero title/subtitle, CTA button, feature sections, and the primary body copy) plus a backup scraper that walks `<h2>` headings labeled “Features” or “FAQs” directly in the HTML to capture the on-page bullet lists.
- Automatic discovery of seller profiles (e.g., `whop.com/username`) with recursive fetching of up to eight linked product pages.

Use `--timeout` to tweak the HTTP deadline. Ensure Playwright's Chromium build is installed before running the scraper.

## Docker image

Build and run the watcher inside the bundled Playwright image via uv:

```bash
docker build -t whop-pulse .
docker run --rm -it whop-pulse
```

The container installs dependencies with `uv pip sync`, forces headless Chromium, and starts `python -m app.pulse_client` automatically. Provide environment variables (e.g., `PULSE_SHOW_RAW=1`) at `docker run` time if you need to tweak runtime behavior.
