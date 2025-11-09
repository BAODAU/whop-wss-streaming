from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
from typing import Any

from playwright.async_api import async_playwright

from .listing_scraper import fetch_listing_snapshot
from .settings import HEADLESS_DEFAULT

try:  # optional helper
    import blackboxprotobuf  # type: ignore
except Exception:  # pragma: no cover
    blackboxprotobuf = None

URL = "https://whop.com/pulse/"

WS_HOOK = """
    const NativeWebSocket = window.WebSocket;
    window.WebSocket = class extends NativeWebSocket {
        constructor(url, protocols) {
            super(url, protocols);

            this.addEventListener('open', () => {
                console.log('[WS-HOOK] Connection opened to:' + url);
            });

            this.addEventListener('message', (event) => {
                if (event.data instanceof ArrayBuffer) {
                    const bytes = new Uint8Array(event.data);
                    let binary = '';
                    for (let i = 0; i < bytes.byteLength; i++) {
                        binary += String.fromCharCode(bytes[i]);
                    }
                    const b64 = btoa(binary);
                    console.log('[WS-HOOK] BINARY payload:', b64);
                } else {
                    console.log('[WS-HOOK] TEXT payload:', event.data);
                }
            });
        }
    };
"""


def _json_fallback(obj):
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except UnicodeDecodeError:
            return f"[HEX] {obj.hex()}"
    return str(obj)


def _format_product_details(details: dict[str, object], mapping: dict[str, str]) -> dict[str, object]:
    """Return a human-friendly view of the product detail fields, omitting image URLs."""

    if not isinstance(details, dict):
        return {}

    formatted: dict[str, object] = {}
    for field_id, value in details.items():
        if field_id == "18":  # nested media, skip image URLs
            continue
        label = mapping.get(field_id, f"field_{field_id}")
        formatted[label] = value
    return formatted


# Map field paths to protobuf types we already know.
# Field paths are tuples of string field numbers from the root down.
FIELD_TYPE_HINTS: dict[tuple[str, ...], str] = {
    ("11", "1"): "double",
}

MARKETPLACE_BASE_URL = "https://whop.com/marketplace/"

SHOW_RAW_PAYLOAD = bool(os.environ.get("PULSE_SHOW_RAW"))


_FETCHED_LISTING_URLS: set[str] = set()
_FETCHING_LISTING_URLS: set[str] = set()


PRODUCT_DETAIL_FIELD_NAMES: dict[str, str] = {
    "1": "product_id",
    "2": "slug",
    "3": "vendor_handle",
    "4": "title",
    "6": "store_name",
}


def _collect_priced_products(obj) -> list[dict[str, object]]:
    """Walk the decoded protobuf and capture listed products, even if a price is missing."""

    results: list[dict[str, object]] = []

    def _extract(node):
        if not isinstance(node, dict):
            return None
        price = node.get("1")
        details = node.get("4")
        if not isinstance(details, dict):
            return None
        slug = details.get("2")
        if not isinstance(slug, str):
            return None
        name_val = details.get("3")
        name = name_val if isinstance(name_val, str) else None
        currency_val = node.get("2")
        currency = currency_val if isinstance(currency_val, str) else None
        vendor_val = node.get("3")
        vendor = vendor_val if isinstance(vendor_val, str) else None
        price_value = None
        if isinstance(price, (int, float)):
            price_value = float(price)
        return {
            "price": price_value,
            "currency": currency,
            "slug": slug,
            "name": name,
            "vendor": vendor,
            "url": f"{MARKETPLACE_BASE_URL}{slug}",
            "details": _format_product_details(details, PRODUCT_DETAIL_FIELD_NAMES),
        }

    def _visit(node):
        info = _extract(node)
        if info:
            results.append(info)
        if isinstance(node, dict):
            for child in node.values():
                _visit(child)
        elif isinstance(node, list):
            for child in node:
                _visit(child)

    _visit(obj)
    return results


def _print_priced_products_summary(obj) -> list[dict[str, object]]:
    entries = _collect_priced_products(obj)
    printable: dict[str, object] = {}
    if isinstance(obj, dict):
        for key in ("query", "purchase"):
            if key in obj:
                printable[key] = obj[key]
    if not entries and not printable:
        return
    if printable:
        print(json.dumps(printable, indent=2, default=_json_fallback))
    for entry in entries:
        print(json.dumps(entry, indent=2, default=_json_fallback))
    return entries


def _fixed64_int_to_double(value: int) -> float:
    """Convert a 64-bit little-endian int (wire type 1) into IEEE-754 double."""

    try:
        return struct.unpack("<d", value.to_bytes(8, "little", signed=False))[0]
    except OverflowError:  # pragma: no cover - safeguard for unexpected widths
        return float(value)


def recursive_decode(data: bytes, prefix: tuple[str, ...] = ()) -> object:
    """
    Attempt to decode bytes as a Protobuf message. If nested binary blobs are encountered,
    recursively decode them; otherwise render them as text/hex.
    """

    if blackboxprotobuf is None:
        return f"<BINARY_HEX: {data.hex()}>"
    try:
        message, _typedef = blackboxprotobuf.decode_message(data)  # type: ignore[attr-defined]
    except Exception:
        return f"<BINARY_HEX: {data.hex()}>"

    def _process(field_path: tuple[str, ...], value):
        hint = FIELD_TYPE_HINTS.get(field_path)

        if isinstance(value, list):
            return [_process(field_path, item) for item in value]

        if hint == "double" and isinstance(value, int):
            return _fixed64_int_to_double(value)

        if isinstance(value, bytes):
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError:
                nested = recursive_decode(value, field_path)
                return nested
        if isinstance(value, dict):
            return {k: _process(field_path + (str(k),), v) for k, v in value.items()}
        return value

    return {
        field_id: _process(prefix + (str(field_id),), val)
        for field_id, val in message.items()
    }


def decode_whop_protobuf(base64_data: str) -> list[dict[str, object]] | None:
    """Decode base64 protobuf blobs printed by the WS hook, handling mixed payloads."""

    if blackboxprotobuf is None:
        print("BROWSER: [DECODE] Install 'blackboxprotobuf' to decode binary frames.")
        return
    try:
        proto_data = base64.b64decode(base64_data)
        decoded = recursive_decode(proto_data)
        normalized = _normalize_decoded_payload(decoded)
        if normalized is None:
            return None
        decoded = normalized
        if SHOW_RAW_PAYLOAD:
            print("\n--- DECODED PROTOBUF ---")
            if isinstance(decoded, (dict, list)):
                print(json.dumps(decoded, indent=2, default=_json_fallback))
            else:
                print(decoded)
            print("------------------------\n")
        return _print_priced_products_summary(decoded)
    except Exception as exc:
        print(f"[!] Protobuf decode error: {exc}")
    return None


def _normalize_decoded_payload(decoded: object) -> object | None:
    """Handle known Protobuf payload shapes before printing."""

    if not isinstance(decoded, dict):
        return decoded

    if len(decoded) == 1:
        field_id, payload = next(iter(decoded.items()))

        if field_id in {"10", "13"}:
            return None

        if field_id == "8" and isinstance(payload, dict):
            inner = payload.get("1")
            if (
                isinstance(inner, dict)
                and {"1", "2"} <= set(inner.keys())
                and all(isinstance(inner.get(key), int) for key in ("1", "2"))
            ):
                return None

        if field_id == "42" and isinstance(payload, dict):
            query = payload.get("1")
            if isinstance(query, str):
                return {"query": query}

    return decoded


async def _fetch_listing_snapshot_for_target(target: str) -> None:
    snapshot: dict[str, Any] | None = None
    try:
        snapshot = await asyncio.to_thread(fetch_listing_snapshot, target)
    except Exception as exc:
        print(f"[LISTING] Failed to fetch '{target}': {exc}")
        return
    finally:
        _FETCHING_LISTING_URLS.discard(target)
        _FETCHED_LISTING_URLS.add(target)

    info_url = snapshot.get("final_url") or snapshot.get("requested_url") or target
    print(f"[LISTING] Snapshot for {info_url}")

    content = snapshot.get("content")
    payload = content if content else snapshot
    print(json.dumps(payload, indent=2, default=_json_fallback))


def _schedule_listing_snapshot_fetch(target_url: str) -> None:
    if (
        not target_url
        or target_url in _FETCHED_LISTING_URLS
        or target_url in _FETCHING_LISTING_URLS
    ):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _FETCHING_LISTING_URLS.add(target_url)
    loop.create_task(_fetch_listing_snapshot_for_target(target_url))


async def run(url: str = URL, headless: bool | None = None) -> None:
    """Launch Chromium with the injected WS hook and print console output."""

    resolved_headless = HEADLESS_DEFAULT if headless is None else headless

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=resolved_headless)
        page = await browser.new_page()
        await page.add_init_script(WS_HOOK)

        def _handle_console(msg) -> None:
            text = msg.text
            if "[WS-HOOK]" not in text:
                return
            marker = "[WS-HOOK] BINARY payload:"
            if marker in text:
                b64 = text.split(marker, 1)[1].strip()
                entries = decode_whop_protobuf(b64)
                if entries:
                    for entry in entries:
                        price = entry.get("price")
                        url = entry.get("url")
                        if price is None:
                            continue
                        if isinstance(url, str):
                            _schedule_listing_snapshot_fetch(url)
                return
            # print(f"BROWSER: {text}")

        page.on("console", _handle_console)

        print("🔥 Hook injected. Navigating to page...")
        await page.goto(url, wait_until="domcontentloaded")
        print("👀 Watching for data... (Interact with the page to trigger more)")

        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            pass
        finally:
            try:
                page.off("console", _handle_console)
            except Exception:
                pass
            await page.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(run())
