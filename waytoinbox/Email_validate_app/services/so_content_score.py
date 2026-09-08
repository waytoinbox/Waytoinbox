"""
so_content_score.py
-------------------
Heuristic quality/spam score for a Sales Outreach draft, powering the
"Overall score: Good / See Details" pill in the sequence editor.

Deliberately separate from `services/email_analyzer.py`, which scores *received*
raw headers and needs SPF/DKIM/DMARC results that a draft simply does not have
(SPF/DKIM/DMARC/blacklist checks must never be added here — that's what keeps
the two services separate). Only the threshold shape is borrowed: low score
is good.

    score_email(subject, html, preheader='') -> {
        'score': int, 'label': 'Good'|'Fair'|'Poor',
        'reasons': [{'category': str, 'severity': 'warn'|'info', 'text': str, 'points': int}, ...],
        'categories': {category_name: [reason, ...], ...},  # same reason dicts, grouped
        'stats': {...},
    }

Every finding belongs to exactly one of a fixed set of categories
(_CATEGORIES below) — this is what lets "See Details" group findings
instead of showing one flat list. 'reasons' keeps its original flat shape
(with 'category'/'points' now added alongside the original 'severity'/
'text') so nothing that already reads reasons[i].severity/.text breaks.
"""

import re

from Email_validate_app.services.so_html import strip_to_text
from Email_validate_app.services.so_smtp import _TAG_MAP as _SEND_TAG_MAP

# Thresholds mirror email_analyzer.spam_score: <=3 clean, <=7 middling, else bad.
_GOOD_MAX = 3
_FAIR_MAX = 7

# Fixed display order for the "See Details" modal (Phase 5/6) — every hit()
# call below must use one of these names.
CATEGORIES = ('Subject', 'Content', 'Links', 'Personalization', 'HTML', 'Compliance')

# Reuses so_smtp.py's own substitution table as the single source of truth
# for "what counts as a real personalization tag" — its keys are exactly
# the tags substitute_tags() will actually replace at send time, so a tag
# not in this set is exactly the case that would reach a recipient
# unsubstituted (e.g. a typo'd {{frist_name}}). Not the view-layer
# PERSONALIZATION_TAGS list (views/so_sender.py) — that's a UI display
# list for the tag-insertion dropdown, kept in sync with this same set by
# convention, not itself the substitution engine — importing from the
# service layer here avoids a service-importing-from-view dependency for
# no added benefit.
_VALID_TAG_NAMES = frozenset(t.strip('{} ') for t in _SEND_TAG_MAP)

_SPAM_WORDS = (
    'act now', 'apply now', 'buy now', 'call now', 'cash bonus', 'cheap',
    'click here', 'congratulations', 'credit card', 'dear friend', 'discount',
    'double your', 'earn money', 'extra income', 'free access', 'free gift',
    'free money', 'free trial', 'guarantee', 'income', 'increase sales',
    'limited time', 'lowest price', 'make money', 'no cost', 'no obligation',
    'offer expires', 'once in a lifetime', 'order now', 'risk free',
    'satisfaction guaranteed', 'special promotion', 'this is not spam',
    'urgent', 'while supplies last', 'winner', 'you have been selected',
)

_CTA_PHRASES = (
    'schedule a call', 'book a time', 'let me know', 'reply to this email',
    'click the link below', 'learn more', 'get started', 'set up a time',
    'grab a slot', 'worth a quick chat', 'happy to chat', 'book a demo',
)

_MERGE_TAG_RE = re.compile(r'\{\{\s*(\w+)\s*\}\}')
_LINK_RE      = re.compile(r'<a\b[^>]*\bhref=', re.IGNORECASE)
_HREF_VAL_RE  = re.compile(r'<a\b[^>]*\bhref\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)
_IMG_RE       = re.compile(r'<img\b', re.IGNORECASE)
_IMG_TAG_RE   = re.compile(r'<img\b[^>]*>', re.IGNORECASE)
_IMG_ALT_RE   = re.compile(r'\balt\s*=\s*["\'][^"\']+["\']', re.IGNORECASE)
_FULL_DOC_RE  = re.compile(r'<(?:html|body|head)\b', re.IGNORECASE)
_EXCESS_PUNCT_RE = re.compile(r'[!?.]{3,}')
_REPEATED_WORD_RE = re.compile(r'\b(\w+)\s+\1\b', re.IGNORECASE)
_ALLCAPS_WORD_RE  = re.compile(r'\b[A-Z]{3,}\b')
# Common emoji blocks (Misc Symbols & Pictographs, Emoticons, Transport,
# Supplemental Symbols and Pictographs, Dingbats, Misc Symbols) — a
# deliberately simple range check, not a full Unicode emoji database.
_EMOJI_RE = re.compile(
    '[\U0001F300-\U0001FAFF\U00002600-\U000026FF\U00002700-\U000027BF]'
)


def score_email(subject, html, preheader=''):
    subject   = (subject or '').strip()
    html      = html or ''
    preheader = (preheader or '').strip()
    text      = strip_to_text(html).strip()

    score   = 0
    reasons = []

    def hit(category, points, severity, text_):
        nonlocal score
        score += points
        reasons.append({'category': category, 'severity': severity, 'text': text_, 'points': points})

    # ── Subject ──────────────────────────────────────────────────────────────
    if not subject:
        hit('Subject', 3, 'warn', 'Subject line is empty.')
    else:
        if len(subject) > 70:
            hit('Subject', 1, 'warn', f'Subject is {len(subject)} characters — aim for under 70 so it is not truncated.')
        elif len(subject) < 15:
            hit('Subject', 1, 'info', f'Subject is only {len(subject)} characters — short subjects can look terse.')

        letters = [c for c in subject if c.isalpha()]
        if len(letters) >= 6 and sum(1 for c in letters if c.isupper()) / len(letters) > 0.6:
            hit('Subject', 2, 'warn', 'Subject is mostly capital letters, a common spam signal.')

        if subject.count('!') >= 2:
            hit('Subject', 1, 'warn', 'Subject has multiple exclamation marks.')

        if re.search(r'(?:^|\s)(?:re|fwd):', subject, re.IGNORECASE):
            hit('Subject', 2, 'warn', 'Subject fakes a reply/forward prefix — this damages trust and deliverability.')

        if _EXCESS_PUNCT_RE.search(subject):
            hit('Subject', 1, 'warn', 'Subject has repeated punctuation (e.g. "??" or "...") — a common spam signal.')

        emoji_count = len(_EMOJI_RE.findall(subject))
        if emoji_count >= 3:
            hit('Subject', 1, 'warn', f'Subject uses {emoji_count} emoji — heavy emoji use can trigger spam filters.')

        if _REPEATED_WORD_RE.search(subject):
            hit('Subject', 1, 'info', 'Subject repeats the same word twice in a row — likely a typo.')

    # ── Body presence & length ───────────────────────────────────────────────
    word_count = len(text.split())
    if not text:
        hit('Content', 3, 'warn', 'Email body is empty.')
    elif word_count < 20:
        hit('Content', 1, 'info', f'Body is only {word_count} words — very short emails can read as low-effort.')
    elif word_count > 400:
        hit('Content', 1, 'info', f'Body is {word_count} words — long cold emails tend to get skimmed.')

    # ── Spam vocabulary ──────────────────────────────────────────────────────
    haystack = f'{subject}\n{text}'.lower()
    found = sorted({w for w in _SPAM_WORDS if w in haystack})
    if found:
        shown = ', '.join(f'"{w}"' for w in found[:5])
        more  = f' (+{len(found) - 5} more)' if len(found) > 5 else ''
        hit('Content', min(len(found), 3), 'warn', f'Contains spam trigger wording: {shown}{more}.')

    if haystack.count('!') >= 4:
        hit('Content', 1, 'info', 'Heavy use of exclamation marks throughout the email.')

    # ── Body formatting/readability ──────────────────────────────────────────
    allcaps_words = _ALLCAPS_WORD_RE.findall(text)
    if len(allcaps_words) > 3:
        hit('Content', 2, 'warn',
            f'Body has {len(allcaps_words)} ALL-CAPS words — reads as shouting and is a spam signal.')

    bold_tag_count = len(re.findall(r'<(?:b|strong)\b', html, re.IGNORECASE))
    if bold_tag_count > 10:
        hit('Content', 1, 'info', f'Body has {bold_tag_count} bold/strong tags — heavy formatting can hurt readability.')

    sentences = [s.strip() for s in re.split(r'[.!?]+', text) if s.strip()]
    if sentences:
        avg_sentence_len = sum(len(s.split()) for s in sentences) / len(sentences)
        if avg_sentence_len > 40:
            hit('Content', 1, 'info',
                f'Average sentence length is {avg_sentence_len:.0f} words — shorter sentences read better in cold outreach.')

    # ── Call to action ───────────────────────────────────────────────────────
    cta_hits = sum(haystack.count(p) for p in _CTA_PHRASES)
    link_count_preview = len(_LINK_RE.findall(html))  # used here only for the CTA-presence check below
    if cta_hits == 0 and link_count_preview == 0 and text:
        hit('Content', 1, 'info', 'No clear call to action detected — tell the reader exactly what to do next.')
    elif cta_hits >= 3:
        hit('Content', 1, 'info',
            f'The call to action is repeated {cta_hits} times — consider narrowing to one clear next step.')

    # ── Links ─────────────────────────────────────────────────────────────────
    link_count = len(_LINK_RE.findall(html))
    href_values = _HREF_VAL_RE.findall(html)

    if link_count > 8:
        hit('Links', 2, 'warn', f'{link_count} links — cold outreach with many links is filtered more aggressively.')
    elif link_count > 4:
        hit('Links', 1, 'info', f'{link_count} links — consider trimming to one clear call to action.')

    if href_values:
        link_domains = []
        for href in href_values:
            m = re.match(r'https?://([^/\s"\'<>]+)', href, re.IGNORECASE)
            if m:
                link_domains.append(m.group(1).lower())
        unique_domains = sorted(set(link_domains))
        if len(unique_domains) > 3:
            hit('Links', 1, 'info',
                f'Links point to {len(unique_domains)} different domains — many distinct destinations can look suspicious.')

        dup_counts = {h: href_values.count(h) for h in set(href_values)}
        duplicated = [h for h, n in dup_counts.items() if n > 1]
        if duplicated:
            hit('Links', 1, 'info', f'The same link is repeated {dup_counts[duplicated[0]]} times.')

        http_links = [h for h in href_values if h.lower().startswith('http://')]
        if http_links:
            hit('Links', 1, 'warn', f'{len(http_links)} link(s) use insecure http:// — prefer https://.')

        malformed = [
            h for h in href_values
            if h.lower().startswith(('http://', 'https://')) and (
                ' ' in h or '.' not in h.split('//', 1)[-1]
            )
        ]
        if malformed:
            hit('Links', 2, 'warn', f'{len(malformed)} link(s) look malformed and may not work correctly.')

    # ── Images / HTML structure ──────────────────────────────────────────────
    img_count = len(_IMG_RE.findall(html))

    if img_count and word_count < 40:
        hit('HTML', 2, 'warn', 'Mostly images with little text — a classic spam pattern.')
    elif img_count > 4:
        hit('HTML', 1, 'info', f'{img_count} images — image-heavy mail often loads blocked by default.')

    if img_count:
        img_tags = _IMG_TAG_RE.findall(html)
        no_alt = sum(1 for tag in img_tags if not _IMG_ALT_RE.search(tag))
        if no_alt:
            hit('HTML', 1, 'info', f'{no_alt} image(s) missing alt text — add it for accessibility and inbox rendering.')

    if _FULL_DOC_RE.search(html):
        hit('HTML', 1, 'info', 'Body contains full <html>/<head>/<body> tags — this is meant to be a fragment; wrapping tags may render oddly.')

    # ── Personalization ───────────────────────────────────────────────────────
    tag_names = _MERGE_TAG_RE.findall(f'{subject} {html}')
    if not tag_names:
        hit('Personalization', 1, 'info', 'No personalization tag used — try {{first_name}} or {{company}}.')
    else:
        unknown = sorted({t for t in tag_names if t not in _VALID_TAG_NAMES})
        if unknown:
            shown = ', '.join(f'{{{{{t}}}}}' for t in unknown)
            hit('Personalization', 1, 'warn',
                f'Unknown personalization tag(s): {shown} — check for typos; '
                f'unmatched tags are sent to recipients exactly as written.')

    # ── Preheader (optional — only scored when the caller actually sends one) ─
    if preheader:
        if len(preheader) > 150:
            hit('Content', 1, 'info',
                f'Preheader is {len(preheader)} characters — most inboxes only show the first ~100, so trim it.')

    # ── Compliance ────────────────────────────────────────────────────────────
    # 'unsubscribe_url' in tag_names is the precise signal: a real,
    # functional unsubscribe mechanism (send_next_step/inject_tracking
    # actually substitutes this at send time — see so_smtp.py), whether it
    # sits as bare text or inside an <a href="{{unsubscribe_url}}">
    # Unsubscribe</a> link (the merge-tag regex above doesn't care about
    # surrounding HTML). The literal-word check is kept as a fallback for
    # a manual, non-tag process (e.g. "reply STOP to unsubscribe") rather
    # than replaced, so this only adds a more precise positive match.
    if 'unsubscribe_url' not in tag_names and 'unsubscribe' not in html.lower():
        hit('Compliance', 2, 'warn', 'No unsubscribe link — add {{unsubscribe_url}}. Required for compliant outreach.')

    label = 'Good' if score <= _GOOD_MAX else ('Fair' if score <= _FAIR_MAX else 'Poor')

    categories = {cat: [r for r in reasons if r['category'] == cat] for cat in CATEGORIES}

    # The flat 'reasons' list keeps its own "no issues" fallback for any
    # caller that still just dumps it flatly (unchanged from before Phase
    # 4/5) — deliberately not injected into `categories` so a fully clean
    # email shows every category as genuinely empty, letting the caller
    # render its own "no issues in this category" line per category.
    flat_reasons = list(reasons)
    if not flat_reasons:
        flat_reasons.append({
            'category': None, 'severity': 'info',
            'text': 'No issues detected. This email looks good to send.', 'points': 0,
        })

    return {
        'score':      score,
        'label':      label,
        'reasons':    flat_reasons,
        'categories': categories,
        'stats': {
            'subject_length': len(subject),
            'word_count':     word_count,
            'link_count':     link_count,
            'image_count':    img_count,
        },
    }
