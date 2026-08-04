"""
segment_builder.py
------------------
Translates a Segment's JSON rules into a Django ORM queryset against
CampaignEmail. All filtering happens inside one SQL query; no Python-side
contact iteration.

Rule JSON structure stored in Segment.rules:
{
  "conditions": [
    {"field": "subscribed",  "operator": "equals",      "value": "subscribed"},
    {"field": "email",       "operator": "contains",    "value": "@gmail.com"},
    {"field": "date_added",  "operator": "in_last_days","value": "30"},
    {"field": "list_membership", "operator": "in_list", "value": "42"},
    {"field": "campaign_activity", "operator": "opened_campaign", "value": "7"},
    ...
  ]
}

Segment.match_type = 'all' → AND all conditions
Segment.match_type = 'any' → OR all conditions
"""

import logging
from datetime import date, timedelta

from django.db.models import Q, Subquery

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Field → operator → ORM lookup mapping
# ---------------------------------------------------------------------------

# Text fields that support string operators
_STRING_FIELDS = {
    'email':      'email',
    'first_name': 'first_name',
    'last_name':  'last_name',
    'company':    'extra_data__company',
    'country':    'extra_data__country',
    'state':      'extra_data__state',
    'city':       'extra_data__city',
}

_STRING_OPERATORS = {
    'equals':       '__iexact',
    'not_equals':   '__iexact',
    'contains':     '__icontains',
    'not_contains': '__icontains',
    'starts_with':  '__istartswith',
    'ends_with':    '__iendswith',
    'is_empty':     '__in',
    'is_not_empty': '__in',
}

_SUBSCRIPTION_VALUES = {
    'subscribed':       'subscribed',
    'unsubscribed':     'unsubscribed',
    'never_subscribed': 'never_subscribed',
}

_CAMPAIGN_ACTIVITY_MAP = {
    'opened_any':          ('open', None, True),
    'not_opened_any':      ('open', None, False),
    'clicked_any':         ('click', None, True),
    'not_clicked_any':     ('click', None, False),
    'opened_campaign':     ('open', 'value', True),
    'not_opened_campaign': ('open', 'value', False),
    'clicked_campaign':    ('click', 'value', True),
    'not_clicked_campaign':('click', 'value', False),
}

_DELIVERY_STATUS_MAP = {
    'delivered':  'delivery',
    'bounced':    'bounce',
    'complained': 'complaint',
    'rejected':   'reject',
}


def _build_condition_q(condition, user_id):
    """
    Convert one condition dict into a Q object (or None to skip).
    Negation is handled by the caller (negate=True conditions use ~Q).
    Returns (Q, negate:bool).
    """
    from Email_validate_app.models import CampaignEvent, CampaignEmail

    field    = condition.get('field', '')
    operator = condition.get('operator', '')
    value    = condition.get('value', '')
    negate   = False

    # ── String / text fields ─────────────────────────────────────────────────
    if field in _STRING_FIELDS:
        orm_field = _STRING_FIELDS[field]
        if operator in ('is_empty', 'is_not_empty'):
            # 'is_empty' → value in ('', None)
            q = Q(**{f'{orm_field}__in': ['', None]}) | Q(**{f'{orm_field}__isnull': True})
            if operator == 'is_not_empty':
                q = ~q
            return q, False

        if not value:
            return None, False

        lookup = _STRING_OPERATORS.get(operator, '__icontains')
        q = Q(**{f'{orm_field}{lookup}': value})
        if operator in ('not_equals', 'not_contains'):
            negate = True
        return q, negate

    # ── Subscription status ──────────────────────────────────────────────────
    if field == 'subscribed':
        if operator == 'equals' and value in _SUBSCRIPTION_VALUES:
            return Q(subscribed=value), False
        if operator == 'not_equals' and value in _SUBSCRIPTION_VALUES:
            return Q(subscribed=value), True
        # Multi-select: "in" operator with comma-separated values
        if operator == 'in':
            vals = [v.strip() for v in value.split(',') if v.strip() in _SUBSCRIPTION_VALUES]
            if vals:
                return Q(subscribed__in=vals), False
        return None, False

    # ── Email domain ─────────────────────────────────────────────────────────
    if field == 'email_domain':
        if not value:
            return None, False
        domain = value.lstrip('@').lower()
        q = Q(email__iendswith=f'@{domain}')
        if operator == 'not_equals':
            negate = True
        return q, negate

    # ── Date added ───────────────────────────────────────────────────────────
    if field == 'date_added':
        today = date.today()
        if operator == 'today':
            return Q(created_at__date=today), False
        if operator == 'yesterday':
            return Q(created_at__date=today - timedelta(days=1)), False
        if operator == 'in_last_days':
            try:
                n = int(value)
            except (ValueError, TypeError):
                return None, False
            since = today - timedelta(days=n)
            return Q(created_at__date__gte=since), False
        if operator == 'before' and value:
            try:
                d = date.fromisoformat(value)
            except ValueError:
                return None, False
            return Q(created_at__date__lt=d), False
        if operator == 'after' and value:
            try:
                d = date.fromisoformat(value)
            except ValueError:
                return None, False
            return Q(created_at__date__gt=d), False
        if operator == 'between' and value:
            parts = value.split(',')
            if len(parts) == 2:
                try:
                    d1 = date.fromisoformat(parts[0].strip())
                    d2 = date.fromisoformat(parts[1].strip())
                    return Q(created_at__date__gte=d1, created_at__date__lte=d2), False
                except ValueError:
                    return None, False
        return None, False

    # ── List membership ──────────────────────────────────────────────────────
    if field == 'list_membership':
        try:
            list_id = int(value)
        except (ValueError, TypeError):
            return None, False

        if operator == 'in_list':
            return Q(list_id=list_id), False

        if operator == 'not_in_list':
            # Emails that appear in the given list; exclude those from result
            emails_in_list = (
                CampaignEmail.objects
                .filter(user_id=user_id, list_id=list_id, deleted_at__isnull=True)
                .values('email')
            )
            return Q(email__in=Subquery(emails_in_list)), True

        return None, False

    # ── Campaign activity ────────────────────────────────────────────────────
    if field == 'campaign_activity':
        mapping = _CAMPAIGN_ACTIVITY_MAP.get(operator)
        if not mapping:
            return None, False
        event_type, value_source, include = mapping

        filters = {'user_id': user_id, 'event_type': event_type}
        if value_source == 'value':
            try:
                filters['campaign_id'] = int(value)
            except (ValueError, TypeError):
                return None, False

        matched_emails = CampaignEvent.objects.filter(**filters).values('email')
        q = Q(email__in=Subquery(matched_emails))
        if not include:
            negate = True
        return q, negate

    # ── Delivery status ──────────────────────────────────────────────────────
    if field == 'delivery_status':
        event_type = _DELIVERY_STATUS_MAP.get(operator)
        if not event_type:
            return None, False
        matched = CampaignEvent.objects.filter(user_id=user_id, event_type=event_type).values('email')
        return Q(email__in=Subquery(matched)), False

    return None, False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _build_group_q(conditions, match_type, user_id):
    """Build a Q object for one condition group."""
    combined = None
    for condition in conditions:
        try:
            q, negate = _build_condition_q(condition, user_id)
        except Exception as exc:
            logger.warning('segment_builder: skipping bad condition %s: %s', condition, exc)
            continue
        if q is None:
            continue
        effective = ~q if negate else q
        if combined is None:
            combined = effective
        elif match_type == 'all':
            combined &= effective
        else:
            combined |= effective
    return combined


def _build_grouped_q(groups, user_id):
    """
    Per-connector evaluation with AND-before-OR precedence (SQL-style).
    Each group after the first carries a 'connector' field: 'and' | 'or'.
    AND chains are evaluated first, then OR-ed together.
    e.g. G1 AND G2 OR G3  →  (G1 AND G2) OR G3
    """
    if not groups:
        return None

    # Split into OR-separated chains; each chain is AND-connected groups
    or_terms   = []
    and_chain  = []
    for i, group in enumerate(groups):
        connector = group.get('connector', 'and') if i > 0 else 'start'
        if i == 0 or connector == 'and':
            and_chain.append(group)
        else:                       # 'or' — flush current chain, start new
            or_terms.append(and_chain)
            and_chain = [group]
    if and_chain:
        or_terms.append(and_chain)

    # Build Q per OR-term (AND within term), then OR the terms
    combined_q = None
    for chain in or_terms:
        term_q = None
        for group in chain:
            gq = _build_group_q(
                group.get('conditions', []),
                group.get('match_type', 'all'),
                user_id,
            )
            if gq is None:
                continue
            term_q = gq if term_q is None else term_q & gq
        if term_q is None:
            continue
        combined_q = term_q if combined_q is None else combined_q | term_q

    return combined_q


def build_segment_queryset(segment, user_id):
    """
    Return a CampaignEmail queryset matching the segment rules.
    Supports per-connector grouped format { groups: [...] } and legacy flat format { conditions: [...] }.
    """
    from Email_validate_app.models import CampaignEmail

    qs    = CampaignEmail.objects.filter(user_id=user_id, deleted_at__isnull=True)
    rules = segment.rules or {}

    # ── Per-connector grouped format ──────────────────────────────────────────
    if 'groups' in rules:
        groups = rules['groups']
        if not groups:
            return qs.none()
        combined_q = _build_grouped_q(groups, user_id)
        if combined_q is None:
            return qs.none()
        return qs.filter(combined_q)

    # ── Legacy flat format ────────────────────────────────────────────────────
    conditions = rules.get('conditions', [])
    if not conditions:
        return qs.none()
    match_type = getattr(segment, 'match_type', 'all')
    combined_q = _build_group_q(conditions, match_type, user_id)
    if combined_q is None:
        return qs.none()
    return qs.filter(combined_q)


def count_segment_contacts(segment, user_id):
    """Return the number of unique emails matching the segment."""
    qs = build_segment_queryset(segment, user_id)
    return qs.values('email').distinct().count()


def get_segment_emails(segment, user_id):
    """Return a deduplicated list of email strings matching the segment."""
    qs = build_segment_queryset(segment, user_id)
    return list(qs.values_list('email', flat=True).distinct())


def get_segment_preview(segment_or_rules, user_id, match_type='all', limit=15):
    """
    Return a preview dict: {count, contacts}.
    segment_or_rules can be a Segment instance or a raw rules dict.
    """
    from Email_validate_app.models import CampaignEmail

    if isinstance(segment_or_rules, dict):
        # Build a lightweight proxy object
        class _FakeSegment:
            pass
        seg = _FakeSegment()
        seg.rules      = segment_or_rules
        seg.match_type = match_type
    else:
        seg = segment_or_rules

    qs    = build_segment_queryset(seg, user_id)
    count = qs.values('email').distinct().count()

    rows = list(
        qs.values('email', 'first_name', 'last_name', 'subscribed')
        .distinct()
        .order_by('email')[:limit]
    )
    return {'count': count, 'contacts': rows}
