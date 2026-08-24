"""
so_html.py
----------
Server-side HTML sanitiser for Sales Outreach email bodies.

The sequence editor exposes a `<>` source view, so `html_body` is attacker-
controllable text that later gets rendered into a preview iframe and mailed out.
Everything that reaches the database goes through `sanitize_email_html()`.

Implemented on plain `lxml.html` (already a project dependency). `lxml.html.clean`
is deliberately NOT used — it was split out into the separate `lxml_html_clean`
distribution in lxml 5.2 and is not installed here.
"""

import logging
import re

from lxml import html as lxml_html
from lxml.etree import ParserError

logger = logging.getLogger(__name__)

# Elements dropped entirely, along with their subtree.
_FORBIDDEN_TAGS = frozenset({
    'script', 'iframe', 'object', 'embed', 'applet', 'frame', 'frameset',
    'base', 'form', 'input', 'textarea', 'select', 'option', 'button',
    'meta', 'link', 'noscript', 'svg', 'math',
})

# `<style>` is intentionally allowed — real email templates rely on it.

_URL_ATTRS = frozenset({'href', 'src', 'action', 'formaction', 'background', 'poster'})

# javascript:, vbscript:, data:text/html — tolerant of whitespace/entity padding
_DANGEROUS_URL = re.compile(
    r'^[\s\x00-\x20]*(?:javascript|vbscript|livescript|mocha)[\s\x00-\x20]*:',
    re.IGNORECASE,
)
_DANGEROUS_DATA_URL = re.compile(
    r'^[\s\x00-\x20]*data[\s\x00-\x20]*:[^,]*?(?:text/html|image/svg\+xml)',
    re.IGNORECASE,
)


# Whitespace and control characters, literal or percent-encoded. Browsers ignore
# these inside a URL scheme, so `java&#9;script:` and `java%09script:` both run.
_URL_NOISE = re.compile(r'(?:[\x00-\x20\s]|%0[09aAdD]|%1[0-9a-fA-F]|%20)+')


def _normalize_url(value):
    """Collapse a URL to the form a browser would actually resolve."""
    return _URL_NOISE.sub('', value or '')


def _attr_is_dangerous(name, value):
    lname = (name or '').lower()
    if lname.startswith('on'):                       # onclick, onerror, onload…
        return True
    if lname in ('srcdoc', 'xlink:href'):
        return True
    if lname in _URL_ATTRS and value:
        cleaned = _normalize_url(value)
        if _DANGEROUS_URL.match(cleaned) or _DANGEROUS_DATA_URL.match(cleaned):
            return True
    return False


def sanitize_email_html(raw, max_len=500_000):
    """Return `raw` with executable markup removed. Never raises."""
    if not raw:
        return ''
    if not isinstance(raw, str):
        raw = str(raw)
    if len(raw) > max_len:
        raw = raw[:max_len]

    try:
        fragment = lxml_html.fragment_fromstring(raw, create_parent='div')
    except (ParserError, ValueError, TypeError):
        try:
            fragment = lxml_html.fromstring(f'<div>{raw}</div>')
        except Exception:
            logger.warning('so_html: unparseable body, dropping to text')
            import html as _stdhtml
            return _stdhtml.escape(raw)

    # Drop forbidden elements (iterate over a materialised list — we mutate the tree)
    for el in list(fragment.iter()):
        tag = el.tag
        if not isinstance(tag, str):
            continue                                  # comments / PIs
        if tag.lower() in _FORBIDDEN_TAGS:
            parent = el.getparent()
            if parent is not None:
                tail = el.tail
                parent.remove(el)
                if tail:                              # keep trailing text
                    if len(parent):
                        prev = parent[-1]
                        prev.tail = (prev.tail or '') + tail
                    else:
                        parent.text = (parent.text or '') + tail

    # Strip dangerous attributes from whatever survived
    for el in fragment.iter():
        if not isinstance(el.tag, str):
            continue
        for name, value in list(el.attrib.items()):
            if _attr_is_dangerous(name, value):
                del el.attrib[name]

    inner = (fragment.text or '')
    for child in fragment:
        inner += lxml_html.tostring(child, encoding='unicode')
    return inner


def strip_to_text(raw):
    """Plain-text rendering of an HTML body — used by the content scorer."""
    if not raw:
        return ''
    try:
        return lxml_html.fromstring(raw).text_content()
    except Exception:
        return re.sub(r'<[^>]+>', ' ', raw)
