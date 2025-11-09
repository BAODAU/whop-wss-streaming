from __future__ import annotations

import argparse
import contextlib
import json
import re
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

SCALAR_TYPES = (str, int, float, bool)
DEFAULT_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
}
TITLE_KEYS = ("title", "heading", "headline", "name", "label")
DESC_KEYS = ("subtitle", "description", "body", "summary", "text", "copy", "details", "tagline")
CTA_KEYS = ("cta", "ctaText", "ctaLabel", "ctaButton", "primaryCta", "ctaPrimary", "action")
FEATURE_KEYS = {
    "features",
    "featurelist",
    "items",
    "perks",
    "benefits",
    "bullets",
    "sellingpoints",
    "highlights",
    "points",
    "listitems",
}
FAQ_QUESTION_TAGS = {"h3", "h4", "summary", "button", "dt"}
FAQ_ANSWER_TAGS = {"p", "div", "span", "li", "dd", "ul", "ol", "section", "article", "blockquote"}
FAQ_ANSWER_FALLBACK_MIN_LEN = 24
FAQ_ENTRY_LIMIT = 12
NEXT_FLIGHT_NEEDLE = "self.__next_f.push(["


def _normalize_target(target: str) -> tuple[str, str]:
    candidate = target.strip()
    if not candidate:
        raise ValueError("Target URL or slug cannot be empty.")
    if "://" not in candidate:
        normalized = candidate.lstrip("/")
        if normalized.lower().startswith("whop.com/"):
            normalized = normalized.split("/", 1)[1]
        candidate = f"https://whop.com/{normalized.lstrip('/')}"
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported scheme in target: {candidate}")
    path = parsed.path.strip("/")
    slug = path.split("/")[-1] if path else parsed.netloc
    return candidate, slug


class _HTMLSnapshotParser(HTMLParser):
    """Grab meta tags, JSON-LD blobs, and __NEXT_DATA__ without third-party deps."""

    META_MAP = {
        ("name", "description"): "description",
        ("property", "og:title"): "ogTitle",
        ("property", "og:description"): "ogDescription",
        ("property", "og:url"): "ogUrl",
    }

    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str | None] = {
            "title": None,
            "description": None,
            "ogTitle": None,
            "ogDescription": None,
            "ogUrl": None,
        }
        self.jsonld_scripts: list[str] = []
        self.next_data_script: str | None = None
        self._in_title = False
        self._title_parts: list[str] = []
        self._script_context: str | None = None
        self._script_buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): (value or "") for key, value in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = attr_map.get("name")
            prop = attr_map.get("property")
            content = attr_map.get("content", "").strip()
            if not content:
                return
            key = None
            if name:
                key = self.META_MAP.get(("name", name.lower()))
            if key is None and prop:
                key = self.META_MAP.get(("property", prop.lower()))
            if key:
                self.meta[key] = content
        elif tag == "script":
            script_id = attr_map.get("id")
            script_type = attr_map.get("type", "").lower()
            if script_id == "__next_data__":
                self._script_context = "next"
                self._script_buffer = []
            elif script_type == "application/ld+json":
                self._script_context = "jsonld"
                self._script_buffer = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._script_context:
            self._script_buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            title = "".join(self._title_parts).strip()
            if title:
                self.meta["title"] = title
            self._in_title = False
            self._title_parts.clear()
        elif tag == "script" and self._script_context:
            content = "".join(self._script_buffer).strip()
            if self._script_context == "next" and content and self.next_data_script is None:
                self.next_data_script = content
            elif self._script_context == "jsonld" and content:
                self.jsonld_scripts.append(content)
            self._script_context = None
            self._script_buffer = []


class _DOMNode:
    __slots__ = ("tag", "attrs", "children", "text_parts", "parent")

    def __init__(self, tag: str, attrs: dict[str, str], parent: _DOMNode | None = None) -> None:
        self.tag = tag.lower()
        self.attrs = {k.lower(): v for k, v in attrs.items()}
        self.children: list[_DOMNode] = []
        self.text_parts: list[str] = []
        self.parent = parent

    def add_child(self, node: _DOMNode) -> None:
        self.children.append(node)

    def add_text(self, data: str) -> None:
        if data:
            self.text_parts.append(data)

    def iter_descendants(self, tags: set[str] | None = None) -> Iterable[_DOMNode]:
        stack = list(self.children)
        while stack:
            node = stack.pop()
            if tags is None or node.tag in tags:
                yield node
            stack.extend(node.children)

    def find_ancestor(self, tags: set[str]) -> _DOMNode | None:
        current = self.parent
        while current is not None:
            if current.tag in tags:
                return current
            current = current.parent
        return None

    def get_text(self) -> str:
        pieces: list[str] = []

        def _collect(node: _DOMNode) -> None:
            pieces.extend(node.text_parts)
            for child in node.children:
                _collect(child)

        _collect(self)
        combined = " ".join(pieces)
        normalized = " ".join(combined.split())
        return normalized.strip()


class _DOMTreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.root = _DOMNode("_root", {})
        self._stack: list[_DOMNode] = [self.root]
        self._by_tag: dict[str, list[_DOMNode]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k: (v or "") for k, v in attrs}
        parent = self._stack[-1]
        node = _DOMNode(tag, attr_map, parent=parent)
        parent.add_child(node)
        self._stack.append(node)
        self._by_tag.setdefault(node.tag, []).append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if len(self._stack) > 1:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        self._stack[-1].add_text(data)

    def iter_tag(self, tag: str) -> Iterable[_DOMNode]:
        return list(self._by_tag.get(tag.lower(), []))


def _build_dom_tree(html: str) -> _DOMTreeBuilder | None:
    builder = _DOMTreeBuilder()
    try:
        builder.feed(html)
    except Exception:
        return None
    return builder


def _render_listing_with_playwright(url: str, timeout: float) -> tuple[str | None, str | None]:
    wait_ms = max(int(timeout * 1000), 1000)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = None
            try:
                context = browser.new_context(
                    user_agent=DEFAULT_HEADERS["user-agent"],
                    locale="en-US",
                    extra_http_headers={
                        "accept": DEFAULT_HEADERS["accept"],
                        "accept-language": DEFAULT_HEADERS["accept-language"],
                    },
                )
                page = context.new_page()
                try:
                    page.goto(url, wait_until="networkidle", timeout=wait_ms)
                except PlaywrightTimeoutError:
                    with contextlib.suppress(PlaywrightTimeoutError):
                        page.wait_for_load_state("domcontentloaded", timeout=wait_ms)
                html = page.content()
                final_url = page.url
                return html, final_url
            finally:
                if context is not None:
                    with contextlib.suppress(Exception):
                        context.close()
                with contextlib.suppress(Exception):
                    browser.close()
    except (PlaywrightError, PlaywrightTimeoutError):
        return None, None
    except Exception:
        return None, None


def _iter_dom_descendants(node: _DOMNode) -> Iterable[_DOMNode]:
    for child in node.children:
        yield child
        yield from _iter_dom_descendants(child)


def _extract_dom_features(builder: _DOMTreeBuilder | None) -> list[dict[str, Any]]:
    if builder is None:
        return []
    feature_sections: list[dict[str, Any]] = []
    seen: set[str] = set()
    for heading_node in builder.iter_tag("h2"):
        heading_text = heading_node.get_text()
        if not heading_text or "feature" not in heading_text.lower():
            continue
        container = heading_node.find_ancestor({"section", "article", "div"}) or heading_node
        list_items: list[str] = []
        for li_node in container.iter_descendants({"li"}):
            text = li_node.get_text()
            if text and text not in list_items:
                list_items.append(text)
                if len(list_items) >= 12:
                    break
        paragraphs: list[str] = []
        for p_node in container.iter_descendants({"p"}):
            text = p_node.get_text()
            if text and text not in paragraphs:
                paragraphs.append(text)
                if len(paragraphs) >= 6:
                    break
        fingerprint = "|".join(list_items) + "::" + "|".join(paragraphs)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        feature_sections.append(
            {
                "heading": heading_text.strip(),
                "items": list_items,
                "paragraphs": paragraphs,
            }
        )
    return feature_sections[:4]


def _extract_dom_faqs(builder: _DOMTreeBuilder | None) -> list[dict[str, Any]]:
    if builder is None:
        return []
    faq_sections: list[dict[str, Any]] = []
    seen: set[str] = set()
    for heading_node in builder.iter_tag("h2"):
        heading_text = heading_node.get_text()
        if not heading_text or "faq" not in heading_text.lower():
            continue
        container = heading_node.find_ancestor({"section", "article", "div"}) or heading_node
        question_text_pool: set[str] = set()
        for question_node in container.iter_descendants(FAQ_QUESTION_TAGS):
            text = question_node.get_text()
            if text:
                question_text_pool.add(text.strip())
        entries: list[dict[str, str | None]] = []
        section_fingerprints: set[tuple[str, str]] = set()
        current_question_text: str | None = None
        current_answers: list[str] = []
        current_answer_seen: set[str] = set()

        def _flush_entry() -> None:
            nonlocal current_question_text, current_answers, current_answer_seen
            if not current_question_text:
                current_answers = []
                current_answer_seen = set()
                return
            answer_text = "\n\n".join(current_answers).strip()
            fingerprint = (current_question_text, answer_text)
            if fingerprint not in section_fingerprints:
                section_fingerprints.add(fingerprint)
                entries.append(
                    {
                        "question": current_question_text,
                        "answer": answer_text or None,
                    }
                )
            current_question_text = None
            current_answers = []
            current_answer_seen = set()

        for node in _iter_dom_descendants(container):
            if len(entries) >= FAQ_ENTRY_LIMIT:
                break
            if node.tag in FAQ_QUESTION_TAGS:
                if current_question_text:
                    _flush_entry()
                    if len(entries) >= FAQ_ENTRY_LIMIT:
                        break
                question_text = node.get_text()
                if not question_text:
                    continue
                current_question_text = question_text.strip()
                current_answers = []
                current_answer_seen = set()
                continue
            if not current_question_text:
                continue
            answer_text = node.get_text()
            if not answer_text:
                continue
            answer_text = answer_text.strip()
            if not answer_text:
                continue
            if current_question_text and answer_text == current_question_text:
                continue
            if answer_text in question_text_pool:
                continue
            preferred_tag = node.tag in FAQ_ANSWER_TAGS
            if not preferred_tag and len(answer_text) < FAQ_ANSWER_FALLBACK_MIN_LEN:
                continue
            if current_question_text:
                lowered_question = current_question_text.lower()
                lowered_answer = answer_text.lower()
                if lowered_answer.startswith(lowered_question):
                    trimmed = answer_text[len(current_question_text) :].lstrip(" :.-\n\t")
                    if trimmed:
                        answer_text = trimmed
            if not answer_text or answer_text in current_answer_seen:
                continue
            current_answers.append(answer_text)
            current_answer_seen.add(answer_text)

        if current_question_text and len(entries) < FAQ_ENTRY_LIMIT:
            _flush_entry()

        fingerprint = "|".join(
            f"{entry['question']}::{entry.get('answer') or ''}" for entry in entries if entry.get("question")
        )
        if not entries or not fingerprint or fingerprint in seen:
            continue
        seen.add(fingerprint)
        faq_sections.append({"heading": heading_text.strip(), "entries": entries})
    return faq_sections[:3]


def _normalize_question(text: str) -> str:
    return " ".join(text.split()).strip().lower()


def _skip_js_string(source: str, start: int) -> int:
    i = start + 1
    length = len(source)
    while i < length:
        char = source[i]
        if char == "\\":
            i += 2
            continue
        if char == '"':
            return i
        i += 1
    return length - 1


def _iter_next_flight_payloads(html: str) -> Iterable[str]:
    if NEXT_FLIGHT_NEEDLE not in html:
        return []
    pos = 0
    length = len(html)
    needle_len = len(NEXT_FLIGHT_NEEDLE)
    while True:
        start = html.find(NEXT_FLIGHT_NEEDLE, pos)
        if start == -1:
            break
        i = start + needle_len
        depth = 1
        while i < length and depth > 0:
            char = html[i]
            if char == '"':
                i = _skip_js_string(html, i)
            elif char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth != 0:
            break
        block_start = start + len("self.__next_f.push(")
        block_end = i
        j = block_start
        while j <= block_end:
            if html[j] == '"':
                end = _skip_js_string(html, j)
                literal = html[j : end + 1]
                try:
                    decoded = json.loads(literal)
                except json.JSONDecodeError:
                    decoded = None
                if isinstance(decoded, str):
                    yield decoded
                j = end
            j += 1
        pos = i + 1


def _extract_json_arrays_from_text(payload: str, key: str) -> Iterable[str]:
    idx = 0
    length = len(payload)
    key_len = len(key)
    while True:
        idx = payload.find(key, idx)
        if idx == -1:
            break
        start = payload.find("[", idx + key_len)
        if start == -1:
            break
        depth = 0
        in_string = False
        i = start
        while i < length:
            char = payload[i]
            if char == '"':
                escaped = False
                back = i - 1
                while back >= 0 and payload[back] == "\\":
                    escaped = not escaped
                    back -= 1
                if not escaped:
                    in_string = not in_string
            elif not in_string:
                if char == "[":
                    depth += 1
                elif char == "]":
                    depth -= 1
                    if depth == 0:
                        yield payload[start : i + 1]
                        break
            i += 1
        idx = i + 1


def _extract_flight_faq_entries(html: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for payload in _iter_next_flight_payloads(html):
        if '"faq":' not in payload:
            continue
        for array_text in _extract_json_arrays_from_text(payload, '"faq":'):
            try:
                data = json.loads(array_text)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, list):
                continue
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                question = entry.get("question")
                answer = entry.get("answer")
                if not isinstance(question, str) or not isinstance(answer, str):
                    continue
                normalized = _normalize_question(question)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    entries.append(
                        {
                            "question": question.strip(),
                            "answer": answer.strip(),
                        }
                    )
    return entries


def _merge_faq_sections(
    dom_sections: list[dict[str, Any]],
    fallback_entries: list[dict[str, str]],
) -> list[dict[str, Any]]:
    if not fallback_entries:
        return dom_sections
    fallback_map = {
        _normalize_question(entry.get("question", "")): entry.get("answer", "")
        for entry in fallback_entries
        if isinstance(entry, dict)
        and isinstance(entry.get("question"), str)
        and isinstance(entry.get("answer"), str)
    }
    fallback_map = {key: value for key, value in fallback_map.items() if key and value}
    if not dom_sections:
        new_entries: list[dict[str, str]] = []
        for entry in fallback_entries[:FAQ_ENTRY_LIMIT]:
            question = entry.get("question")
            answer = entry.get("answer")
            if isinstance(question, str) and isinstance(answer, str):
                new_entries.append({"question": question.strip(), "answer": answer.strip()})
        if new_entries:
            return [{"heading": "FAQs", "entries": new_entries}]
        return dom_sections
    for section in dom_sections:
        entries = section.get("entries")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if entry.get("answer"):
                continue
            question = entry.get("question")
            if not isinstance(question, str):
                continue
            normalized = _normalize_question(question)
            fallback_answer = fallback_map.get(normalized)
            if fallback_answer:
                entry["answer"] = fallback_answer
    return dom_sections


def _extract_profile_product_paths(builder: _DOMTreeBuilder | None, final_url: str) -> list[str]:
    if builder is None:
        return []
    parsed = urlparse(final_url)
    path_parts = [segment for segment in parsed.path.strip("/").split("/") if segment]
    if not path_parts:
        return []
    username = path_parts[0].lower()
    base = f"{parsed.scheme}://{parsed.netloc}"
    results: list[str] = []
    seen: set[str] = set()
    for anchor in builder.iter_tag("a"):
        href = anchor.attrs.get("href", "")
        if not href:
            continue
        absolute = urljoin(base + "/", href)
        parsed_href = urlparse(absolute)
        if parsed_href.netloc and parsed_href.netloc.lower() != parsed.netloc.lower():
            continue
        path_segments = [segment for segment in parsed_href.path.strip("/").split("/") if segment]
        if len(path_segments) < 2:
            continue
        if path_segments[0].lower() != username:
            continue
        product_path = "/".join(path_segments[:2])
        normalized = f"{parsed.scheme}://{parsed.netloc}/{product_path}"
        if normalized not in seen:
            seen.add(normalized)
            results.append(normalized)
    return results


def _safe_json_loads(payload: str) -> Any | None:
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def _type_matches(value: Any, expected: str) -> bool:
    if isinstance(value, str):
        return value.lower() == expected.lower()
    if isinstance(value, list):
        return any(_type_matches(item, expected) for item in value)
    return False


def _iter_nodes(obj: Any) -> Iterable[dict[str, Any]]:
    stack: list[Any] = [obj]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _collect_by_type(obj: Any, typename: str) -> list[dict[str, Any]]:
    if obj is None:
        return []
    results: list[dict[str, Any]] = []
    for node in _iter_nodes(obj):
        if _type_matches(node.get("@type"), typename):
            results.append(node)
    return results


def _ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _as_strings(value: Any) -> list[str]:
    entries = _ensure_list(value)
    results: list[str] = []
    for entry in entries:
        if isinstance(entry, (str, int, float)):
            results.append(str(entry))
    return results


def _extract_name(value: Any) -> str | None:
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str):
            return name
    elif isinstance(value, str):
        return value
    return None


def _summarize_offer(node: dict[str, Any]) -> dict[str, Any]:
    price_spec = node.get("priceSpecification")
    billing_interval = None
    billing_duration = None
    if isinstance(price_spec, dict):
        billing_interval = price_spec.get("billingInterval") or price_spec.get("billingPeriod")
        billing_duration = price_spec.get("billingDuration") or price_spec.get("billingFrequency")
    return {
        "name": node.get("name"),
        "price": node.get("price"),
        "currency": node.get("priceCurrency"),
        "availability": node.get("availability"),
        "url": node.get("url"),
        "billing_interval": billing_interval,
        "billing_duration": billing_duration,
        "price_valid_until": node.get("priceValidUntil"),
    }


def _summarize_additional_properties(value: Any) -> list[dict[str, Any]]:
    props: list[dict[str, Any]] = []
    for entry in _ensure_list(value):
        if isinstance(entry, dict):
            props.append(
                {
                    "name": entry.get("name"),
                    "value": entry.get("value"),
                    "description": entry.get("description"),
                }
            )
    return props


def _summarize_product(node: dict[str, Any]) -> dict[str, Any]:
    offers = []
    for offer_node in _collect_by_type(node.get("offers"), "Offer"):
        offers.append(_summarize_offer(offer_node))
    return {
        "name": node.get("name"),
        "description": node.get("description"),
        "category": node.get("category"),
        "brand": _extract_name(node.get("brand")),
        "seller": _extract_name(node.get("seller")),
        "sku": node.get("sku"),
        "url": node.get("url"),
        "includes": _summarize_additional_properties(node.get("additionalProperty")),
        "offers": offers,
    }


def _summarize_org(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": node.get("name"),
        "url": node.get("url"),
        "sameAs": _as_strings(node.get("sameAs")),
        "contactPoint": node.get("contactPoint"),
    }


def _as_text(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("text", "label", "title", "name", "value"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                normalized = candidate.strip()
                if normalized:
                    return normalized
    return None


def _first_text(node: dict[str, Any], keys: Iterable[str]) -> str | None:
    if not isinstance(node, dict):
        return None
    for key in keys:
        value = node.get(key)
        text = _as_text(value)
        if text:
            return text
    return None


def _extract_cta(node: dict[str, Any]) -> dict[str, Any] | None:
    for key in CTA_KEYS:
        value = node.get(key)
        if isinstance(value, dict):
            text = _as_text(value)
            href = value.get("href") or value.get("url") or value.get("link")
            if text or href:
                return {"text": text, "href": href}
        elif isinstance(value, str):
            text = value.strip()
            if text:
                return {"text": text, "href": None}
    return None


def _extract_hero_from_payload(payload: Any) -> dict[str, Any] | None:
    for node in _iter_nodes(payload):
        if not isinstance(node, dict):
            continue
        title = _first_text(node, TITLE_KEYS)
        subtitle = _first_text(node, DESC_KEYS)
        cta = _extract_cta(node)
        badge = _as_text(node.get("badge") or node.get("tag"))
        rating_value = node.get("rating") or node.get("ratingValue") or node.get("ratingText")
        rating = _as_text(rating_value) if rating_value is not None else None
        if title and (subtitle or cta):
            return {
                "title": title,
                "subtitle": subtitle,
                "badge": badge,
                "rating": rating,
                "cta": cta,
            }
    return None


def _collect_feature_sections(payload: Any) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    seen: set[tuple[str | None, tuple[tuple[str | None, str | None], ...]]] = set()
    for node in _iter_nodes(payload):
        if not isinstance(node, dict):
            continue
        for key, value in list(node.items()):
            if not isinstance(key, str) or key.lower() not in FEATURE_KEYS:
                continue
            if not isinstance(value, list):
                continue
            items: list[dict[str, Any]] = []
            for entry in value:
                if isinstance(entry, dict):
                    title = _first_text(entry, TITLE_KEYS)
                    description = _first_text(entry, DESC_KEYS)
                elif isinstance(entry, str):
                    title = entry.strip()
                    description = None
                else:
                    continue
                if not title and not description:
                    continue
                items.append({"title": title, "description": description})
            if not items:
                continue
            heading = _first_text(node, TITLE_KEYS) or key
            fingerprint = (heading, tuple((item["title"], item["description"]) for item in items))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            sections.append({"heading": heading, "items": items[:10]})
    return sections[:6]


_NUMBER_PATTERN = re.compile(r"(\d[\d,]*(?:\.\d+)?)")
_WIDTH_PATTERN = re.compile(r"width:\s*([0-9]+(?:\.[0-9]+)?)%")


def _parse_numeric(text: str, *, as_int: bool) -> int | float | None:
    match = _NUMBER_PATTERN.search(text)
    if not match:
        return None
    raw = match.group(1).replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    if as_int:
        return int(round(value))
    return value


def _extract_width_percentage(node: _DOMNode | None) -> float | None:
    if node is None:
        return None
    style = node.attrs.get("style", "")
    match = _WIDTH_PATTERN.search(style)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    for child in node.children:
        percentage = _extract_width_percentage(child)
        if percentage is not None:
            return percentage
    return None


def _extract_dom_reviews(builder: _DOMTreeBuilder | None) -> dict[str, Any] | None:
    if builder is None:
        return None
    for heading_node in builder.iter_tag("h2"):
        heading_text = heading_node.get_text()
        if not heading_text or "review" not in heading_text.lower():
            continue
        allowed = {"section", "article", "div"}
        best_container: _DOMNode | None = None
        current = heading_node.parent
        while current is not None and current.tag in allowed:
            attrs_blob = " ".join(
                filter(
                    None,
                    (current.attrs.get("id", ""), current.attrs.get("class", "")),
                )
            ).lower()
            if current.tag == "section" or "review" in attrs_blob:
                best_container = current
            current = current.parent
        container = best_container or heading_node.find_ancestor(allowed) or heading_node
        average_rating: float | None = None
        rating_scale: float | None = None
        total_reviews: int | None = None

        for node in container.iter_descendants({"span", "div", "p"}):
            text = node.get_text()
            if not text:
                continue
            lower = text.lower()
            if total_reviews is None and "total review" in lower:
                match_total = re.search(r"(\d[\d,]*)\s+total reviews?", text, re.I)
                if match_total:
                    try:
                        total_reviews = int(match_total.group(1).replace(",", ""))
                    except ValueError:
                        total_reviews = None
                else:
                    value = _parse_numeric(text, as_int=True)
                    if isinstance(value, int):
                        total_reviews = value
            if average_rating is None and "out of" in lower:
                match = re.search(r"(\d[\d,]*(?:\.\d+)?)\s+out of\s+(\d[\d,]*(?:\.\d+)?)", text, re.I)
                if match:
                    avg = match.group(1).replace(",", "")
                    scale = match.group(2).replace(",", "")
                    try:
                        average_rating = float(avg)
                        rating_scale = float(scale)
                    except ValueError:
                        pass

        distribution: dict[int, dict[str, float | int | None]] = {}
        for span_node in container.iter_descendants({"span"}):
            label = span_node.get_text()
            if not label:
                continue
            match = re.match(r"([1-5])\s+star", label.strip(), re.I)
            if not match:
                continue
            rating_index = int(match.group(1))
            parent = span_node.parent
            percentage = None
            if parent:
                siblings = parent.children
                for sibling in siblings:
                    if sibling is span_node:
                        continue
                    percentage = _extract_width_percentage(sibling)
                    if percentage is not None:
                        break
                if percentage is None:
                    percentage = _extract_width_percentage(parent)
            count = None
            if percentage is not None and total_reviews is not None:
                count = int(round(total_reviews * (percentage / 100)))
            distribution[rating_index] = {
                "percent": percentage,
                "count": count,
            }

        if not (total_reviews or average_rating or distribution):
            continue

        ordered_distribution = []
        for rating_value in range(5, 0, -1):
            entry = distribution.get(rating_value)
            if entry is None:
                ordered_distribution.append(
                    {"stars": rating_value, "percent": None, "count": None}
                )
            else:
                ordered_distribution.append(
                    {"stars": rating_value, "percent": entry["percent"], "count": entry["count"]}
                )

        return {
            "heading": heading_text.strip(),
            "average_rating": average_rating,
            "rating_scale": rating_scale,
            "total_reviews": total_reviews,
            "distribution": ordered_distribution,
        }
    return None


def _flatten_feature_sections(*section_groups: Any) -> list[str]:
    flattened: list[str] = []
    seen: set[str] = set()

    def _append(text: str) -> None:
        normalized = " ".join(text.split()).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            flattened.append(normalized)

    for group in section_groups:
        if not isinstance(group, list):
            continue
        for section in group:
            if isinstance(section, str):
                _append(section)
                continue
            if not isinstance(section, dict):
                continue
            items = section.get("items")
            if not isinstance(items, list):
                continue
            for item in items:
                text: str | None = None
                if isinstance(item, str):
                    text = item
                elif isinstance(item, dict):
                    text = _first_text(item, TITLE_KEYS) or _first_text(item, DESC_KEYS) or _as_text(item)
                elif isinstance(item, (int, float, bool)):
                    text = str(item)
                if text:
                    _append(text)
    return flattened


def _extract_pricing_options(builder: _DOMTreeBuilder | None) -> list[str]:
    if builder is None:
        return []
    options: list[str] = []
    seen: set[str] = set()
    for node in builder.iter_tag("div"):
        role = node.attrs.get("role", "").lower()
        if role != "radio":
            continue
        text = node.get_text()
        if not text:
            continue
        normalized = " ".join(text.split())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        options.append(normalized)
    return options


def _surface_sections_by_heading(target: dict[str, Any], sections: list[dict[str, Any]]) -> None:
    """
    Promote DOM sections (features, FAQs, etc.) so their heading becomes a direct key
    in the content summary (e.g. "FAQs": {"entries": [...]})
    """
    if not isinstance(target, dict) or not isinstance(sections, list):
        return
    for idx, section in enumerate(sections):
        if not isinstance(section, dict):
            continue
        heading = section.get("heading")
        if isinstance(heading, str):
            heading_key = heading.strip()
        else:
            heading_key = ""
        if not heading_key:
            heading_key = f"dom_section_{idx + 1}"
        payload = {key: value for key, value in section.items() if key != "heading"}
        if not payload:
            continue
        key_candidate = heading_key
        suffix = 2
        while key_candidate in target:
            key_candidate = f"{heading_key} ({suffix})"
            suffix += 1
        value: Any = payload
        if len(payload) == 1 and "entries" in payload:
            value = payload["entries"]
        target[key_candidate] = value


def _extract_flat_faqs(
    content_summary: dict[str, Any] | None, dom_sections: list[dict[str, Any]] | None
) -> list[dict[str, str | None]]:
    sources: list[list[Any]] = []
    if isinstance(content_summary, dict):
        content_faqs = content_summary.get("FAQs")
        if isinstance(content_faqs, list):
            sources.append(content_faqs)
        elif isinstance(content_faqs, dict):
            entries = content_faqs.get("entries")
            if isinstance(entries, list):
                sources.append(entries)
    if isinstance(dom_sections, list):
        for section in dom_sections:
            if not isinstance(section, dict):
                continue
            entries = section.get("entries")
            if isinstance(entries, list):
                sources.append(entries)

    flattened: list[dict[str, str | None]] = []
    seen_questions: set[str] = set()
    for entry_list in sources:
        for entry in entry_list:
            if not isinstance(entry, dict):
                continue
            question = entry.get("question")
            if not isinstance(question, str):
                continue
            normalized_question = " ".join(question.split()).strip()
            if not normalized_question:
                continue
            answer_value = entry.get("answer")
            if isinstance(answer_value, str):
                normalized_answer = answer_value.strip() or None
            else:
                normalized_answer = None
            fingerprint = normalized_question.lower()
            if fingerprint in seen_questions:
                continue
            seen_questions.add(fingerprint)
            flattened.append(
                {
                    "question": normalized_question,
                    "answer": normalized_answer,
                }
            )
    return flattened


def _build_flat_snapshot(
    final_url: str,
    products: list[dict[str, Any]],
    features: list[str] | None,
    faqs: list[dict[str, str | None]] | None,
    pricing_options: list[str] | None,
) -> dict[str, Any]:
    primary_product = products[0] if products else None

    def _clean_product_field(key: str) -> str | None:
        if not isinstance(primary_product, dict):
            return None
        value = primary_product.get(key)
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return _as_text(value)

    return {
        "final_url": final_url,
        "name": _clean_product_field("name"),
        "description": _clean_product_field("description"),
        "brand": _clean_product_field("brand"),
        "sku": _clean_product_field("sku"),
        "features": features or [],
        "faqs": faqs or [],
        "pricing": pricing_options or [],
    }


def _collect_text_chunks(payload: Any, limit: int = 8) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in _iter_nodes(payload):
        if not isinstance(node, dict):
            continue
        for key in ("description", "body", "summary", "text", "copy", "details", "content"):
            value = node.get(key)
            if not isinstance(value, str):
                continue
            normalized = " ".join(value.split())
            if len(normalized) < 40:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            chunks.append({"source": key, "text": normalized})
            if len(chunks) >= limit:
                return chunks
    return chunks


def _summarize_page_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, (dict, list)):
        return {}
    hero = _extract_hero_from_payload(payload)
    feature_sections = _collect_feature_sections(payload)
    descriptions = _collect_text_chunks(payload)
    return {
        "hero": hero,
        "feature_sections": feature_sections,
        "descriptions": descriptions,
    }


def _build_next_data_url(final_url: str, build_id: str) -> str | None:
    parsed = urlparse(final_url)
    if not parsed.scheme or not parsed.netloc:
        return None
    path = parsed.path or "/"
    if path.endswith("/") and path != "/":
        path = path[:-1]
    suffix = "index" if path in {"", "/"} else path.lstrip("/")
    base = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    return f"{base}/_next/data/{build_id}/{suffix}.json"


def fetch_listing_snapshot(
    target: str, *, timeout: float = 30.0, _client: httpx.Client | None = None, _allow_profile_hop: bool = True
) -> dict[str, Any]:
    url, _slug = _normalize_target(target)
    rendered_html, rendered_final_url = _render_listing_with_playwright(url, timeout)
    response_status: int | None = None
    final_url = rendered_final_url or url
    dom_feature_sections: list[dict[str, Any]] = []
    dom_faq_sections: list[dict[str, Any]] = []
    dom_reviews: dict[str, Any] | None = None
    page_payload: Any | None = None

    products: list[dict[str, Any]] = []
    organizations: list[dict[str, Any]] = []
    pricing_options: list[str] = []

    own_client = False
    client = _client
    if client is None:
        client = httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=timeout)
        own_client = True

    try:
        response = client.get(url)
        response_status = response.status_code
        final_url = rendered_final_url or str(response.url)
        parsed_final = urlparse(final_url)
        path_segments = [segment for segment in parsed_final.path.strip("/").split("/") if segment]
        html_text = response.text

        parser = _HTMLSnapshotParser()
        parser.feed(html_text)
        dom_builder = _build_dom_tree(html_text)
        rendered_dom_builder = _build_dom_tree(rendered_html) if rendered_html else None

        if _allow_profile_hop and len(path_segments) <= 1:
            product_links = _extract_profile_product_paths(dom_builder, final_url)
            if product_links:
                snapshots = []
                for product_url in product_links[:8]:
                    snapshots.append(
                        fetch_listing_snapshot(
                            product_url,
                            timeout=timeout,
                            _client=client,
                            _allow_profile_hop=False,
                        )
                    )
                return {
                    "requested_url": url,
                    "profile_url": final_url,
                    "profile_username": path_segments[0] if path_segments else None,
                    "product_count": len(product_links),
                    "product_urls": product_links,
                    "products": snapshots,
                }

        flight_faq_entries = _extract_flight_faq_entries(html_text)
        dom_feature_sections = _extract_dom_features(dom_builder)
        dom_faq_sections = _extract_dom_faqs(dom_builder)
        dom_faq_sections = _merge_faq_sections(dom_faq_sections, flight_faq_entries)
        dom_reviews = _extract_dom_reviews(dom_builder)
        pricing_builder = rendered_dom_builder or dom_builder
        pricing_options = _extract_pricing_options(pricing_builder)

        for blob in parser.jsonld_scripts:
            parsed = _safe_json_loads(blob)
            if parsed is None:
                continue
            for product in _collect_by_type(parsed, "Product"):
                products.append(_summarize_product(product))
            for org in _collect_by_type(parsed, "Organization"):
                organizations.append(_summarize_org(org))

        next_data = _safe_json_loads(parser.next_data_script) if parser.next_data_script else None
        build_id = next_data.get("buildId") if isinstance(next_data, dict) else None
        if isinstance(build_id, str):
            next_data_url = _build_next_data_url(final_url, build_id)
            if next_data_url:
                try:
                    params = dict(response.url.params)
                except AttributeError:
                    params = {}
                try:
                    data_resp = client.get(next_data_url, params=params)
                    if data_resp.status_code == 200:
                        payload_candidate = data_resp.json()
                        if isinstance(payload_candidate, dict):
                            page_payload = payload_candidate.get("pageProps") or payload_candidate
                except httpx.HTTPError:
                    pass
    finally:
        if own_client:
            client.close()

    content_summary = _summarize_page_payload(page_payload)
    if not isinstance(content_summary, dict):
        content_summary = {}

    structured_features = content_summary.get("feature_sections")
    if not isinstance(structured_features, list):
        structured_features = None

    if dom_feature_sections:
        _surface_sections_by_heading(content_summary, dom_feature_sections)
    if dom_faq_sections:
        _surface_sections_by_heading(content_summary, dom_faq_sections)

    features = _flatten_feature_sections(structured_features, dom_feature_sections)
    faqs = _extract_flat_faqs(content_summary, dom_faq_sections)

    return _build_flat_snapshot(
        final_url=final_url,
        products=products,
        features=features,
        faqs=faqs,
        pricing_options=pricing_options,
    )


def _json_default(obj: Any) -> str:
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except UnicodeDecodeError:
            return obj.hex()
    raise TypeError(f"Object of type {type(obj)!r} is not JSON serializable")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract listing metadata, JSON-LD, and Next.js payloads from a whop.com page."
    )
    parser.add_argument(
        "target",
        help="Listing URL or slug (e.g. 'iris-out-5c' or 'https://whop.com/iris-out-5c').",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--features-only",
        action="store_true",
        help="Print only the extracted feature sections inside a simplified `features` object.",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        snapshot = fetch_listing_snapshot(args.target, timeout=args.timeout)
        if args.features_only:
            raw_features = snapshot.get("features")
            features: list[str] | None = None
            if isinstance(raw_features, list) and all(isinstance(item, str) for item in raw_features):
                features = raw_features
            else:
                structured_sections: Any = raw_features if isinstance(raw_features, list) else None
                dom_sections: Any = snapshot.get("dom_feature_sections")
                if not isinstance(dom_sections, list):
                    dom_sections = None
                content = snapshot.get("content")
                if isinstance(content, dict):
                    if structured_sections is None:
                        structured_candidate = content.get("feature_sections")
                        if isinstance(structured_candidate, list):
                            structured_sections = structured_candidate
                features = _flatten_feature_sections(structured_sections, dom_sections)
            simplified = {
                "final_url": snapshot.get("final_url"),
                "name": snapshot.get("name"),
                "description": snapshot.get("description"),
                "brand": snapshot.get("brand"),
                "sku": snapshot.get("sku"),
                "features": features or [],
                "pricing": snapshot.get("pricing") or [],
            }
            print(json.dumps(simplified, indent=2, default=_json_default))
            return
        print(json.dumps(snapshot, indent=2, default=_json_default))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
