"""
so_content_score.py
-------------------
Heuristic quality/spam score for a Sales Outreach draft, powering the
"Overall score: Good / See Details" pill in the sequence editor.

Deliberately separate from `services/email_analyzer.py`, which scores *received*
raw headers and needs SPF/DKIM/DMARC results that a draft simply does not have.
Only the threshold shape is borrowed: low score = good.

    score_email(subject, html) -> {
        'score': int, 'label': 'Good'|'Fair'|'Poor',
        'reasons': [{'severity': 'warn'|'info', 'text': str}, ...],
        'stats': {...},
    }
"""

import re

from Email_validate_app.services.so_html import strip_to_text

# Thresholds mirror email_analyzer.spam_score: <=3 clean, <=7 middling, else bad.
_GOOD_MAX = 3
_FAIR_MAX = 7

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

_MERGE_TAG_RE = re.compile(r'\{\{\s*\w+\s*\}\}')
_LINK_RE      = re.compile(r'<a\b[^>]*\bhref=', re.IGNORECASE)
_IMG_RE       = re.compile(r'<img\b', re.IGNORECASE)


def score_email(subject, html):
    subject = (subject or '').strip()
    html    = html or ''
    text    = strip_to_text(html).strip()

    score   = 0
    reasons = []

    def hit(points, severity, text_):
        nonlocal score
        score += points
        reasons.append({'severity': severity, 'text': text_})

    # ── Subject ──────────────────────────────────────────────────────────────
    if not subject:
        hit(3, 'warn', 'Subject line is empty.')
    else:
        if len(subject) > 70:
            hit(1, 'warn', f'Subject is {len(subject)} characters — aim for under 70 so it is not truncated.')
        elif len(subject) < 15:
            hit(1, 'info', f'Subject is only {len(subject)} characters — short subjects can look terse.')

        letters = [c for c in subject if c.isalpha()]
        if len(letters) >= 6 and sum(1 for c in letters if c.isupper()) / len(letters) > 0.6:
            hit(2, 'warn', 'Subject is mostly capital letters, a common spam signal.')

        if subject.count('!') >= 2:
            hit(1, 'warn', 'Subject has multiple exclamation marks.')

        if re.search(r'(?:^|\s)(?:re|fwd):', subject, re.IGNORECASE):
            hit(2, 'warn', 'Subject fakes a reply/forward prefix — this damages trust and deliverability.')

    # ── Body presence ────────────────────────────────────────────────────────
    word_count = len(text.split())
    if not text:
        hit(3, 'warn', 'Email body is empty.')
    elif word_count < 20:
        hit(1, 'info', f'Body is only {word_count} words — very short emails can read as low-effort.')
    elif word_count > 400:
        hit(1, 'info', f'Body is {word_count} words — long cold emails tend to get skimmed.')

    # ── Spam vocabulary ──────────────────────────────────────────────────────
    haystack = f'{subject}\n{text}'.lower()
    found = sorted({w for w in _SPAM_WORDS if w in haystack})
    if found:
        shown = ', '.join(f'"{w}"' for w in found[:5])
        more  = f' (+{len(found) - 5} more)' if len(found) > 5 else ''
        hit(min(len(found), 3), 'warn', f'Contains spam trigger wording: {shown}{more}.')

    if haystack.count('!') >= 4:
        hit(1, 'info', 'Heavy use of exclamation marks throughout the email.')

    # ── Links and images ─────────────────────────────────────────────────────
    link_count = len(_LINK_RE.findall(html))
    img_count  = len(_IMG_RE.findall(html))

    if link_count > 8:
        hit(2, 'warn', f'{link_count} links — cold outreach with many links is filtered more aggressively.')
    elif link_count > 4:
        hit(1, 'info', f'{link_count} links — consider trimming to one clear call to action.')

    if img_count and word_count < 40:
        hit(2, 'warn', 'Mostly images with little text — a classic spam pattern.')
    elif img_count > 4:
        hit(1, 'info', f'{img_count} images — image-heavy mail often loads blocked by default.')

    # ── Personalization & compliance ─────────────────────────────────────────
    if not _MERGE_TAG_RE.search(f'{subject} {html}'):
        hit(1, 'info', 'No personalization tag used — try {{first_name}} or {{company}}.')

    if 'unsubscribe' not in html.lower():
        hit(2, 'warn', 'No unsubscribe link — add {{unsubscribe_url}}. Required for compliant outreach.')

    label = 'Good' if score <= _GOOD_MAX else ('Fair' if score <= _FAIR_MAX else 'Poor')
    if not reasons:
        reasons.append({'severity': 'info', 'text': 'No issues detected. This email looks good to send.'})

    return {
        'score':   score,
        'label':   label,
        'reasons': reasons,
        'stats': {
            'subject_length': len(subject),
            'word_count':     word_count,
            'link_count':     link_count,
            'image_count':    img_count,
        },
    }
