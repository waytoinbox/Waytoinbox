"""
filter_utils.py v1.0
Reusable filter helpers for all view functions.

All helpers are pure functions that operate on ORM QuerySets.
Every helper is user-scope-safe — the caller is responsible for
providing a QuerySet already scoped to the current user.
"""
import logging
import time
from datetime import date as _date

from django.conf import settings
from django.db.models import Q

logger = logging.getLogger(__name__)

_SLOW_MS = getattr(settings, 'FILTER_SLOW_QUERY_MS', 500)


# ── Date helpers ───────────────────────────────────────────────────────────────

def normalize_date(value: str):
    """Parse 'YYYY-MM-DD' string; return a date object or None."""
    if not value:
        return None
    try:
        return _date.fromisoformat(value.strip())
    except ValueError:
        return None


def safe_date_range(date_from_str: str, date_to_str: str):
    """Return (date_from, date_to) as date objects; swap if inverted; either may be None."""
    d_from = normalize_date(date_from_str)
    d_to   = normalize_date(date_to_str)
    if d_from and d_to and d_from > d_to:
        d_from, d_to = d_to, d_from
    return d_from, d_to


# ── QuerySet filter helpers ────────────────────────────────────────────────────

def apply_search(qs, value: str, *fields):
    """Case-insensitive OR search across the named model fields."""
    if not value:
        return qs
    q = Q()
    for field in fields:
        q |= Q(**{f'{field}__icontains': value})
    return qs.filter(q)


def apply_status(qs, value: str, field: str = 'status'):
    """Case-insensitive exact match on a status field."""
    if not value:
        return qs
    return qs.filter(**{f'{field}__iexact': value})


def apply_date_range(qs, d_from, d_to, field: str = 'created_at'):
    """Apply date range; d_from and d_to are date objects or None."""
    if d_from:
        qs = qs.filter(**{f'{field}__date__gte': d_from})
    if d_to:
        qs = qs.filter(**{f'{field}__date__lte': d_to})
    return qs


# ── Performance helper ─────────────────────────────────────────────────────────

def timed_count(qs, label: str, user_id=None) -> int:
    """Execute qs.count() and log a warning when it exceeds FILTER_SLOW_QUERY_MS."""
    t0 = time.monotonic()
    n  = qs.count()
    ms = (time.monotonic() - t0) * 1000
    if ms > _SLOW_MS:
        logger.warning(
            'filter_utils: SLOW QUERY [%s] user=%s elapsed=%.0fms count=%d',
            label, user_id, ms, n,
        )
    else:
        logger.debug(
            'filter_utils: [%s] user=%s elapsed=%.0fms count=%d',
            label, user_id, ms, n,
        )
    return n


# ── Request helper ─────────────────────────────────────────────────────────────

def extract_filter_params(request) -> dict:
    """Extract the 4 standard filter GET params from a Django request.

    Returns:
        {
            'search':    str,
            'status':    str,
            'date_from': date | None,
            'date_to':   date | None,
        }
    """
    search = request.GET.get('search', '').strip()
    status = request.GET.get('status', '').strip()
    d_from, d_to = safe_date_range(
        request.GET.get('date_from', ''),
        request.GET.get('date_to',   ''),
    )
    logger.debug(
        'filter_utils: extract search=%r status=%r date_from=%s date_to=%s path=%s',
        search, status, d_from, d_to, request.path,
    )
    return {'search': search, 'status': status, 'date_from': d_from, 'date_to': d_to}
