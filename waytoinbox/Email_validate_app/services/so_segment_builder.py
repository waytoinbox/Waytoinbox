"""
so_segment_builder.py
---------------------
Sales Outreach counterpart of segment_builder.py.

Translates an SOSegment's JSON rules into a Django ORM queryset against
SOProspect. All filtering happens inside one SQL query; no Python-side
prospect iteration.

Rule JSON structure stored in SOSegment.rules — identical to Segment.rules:
{
  "groups": [
    {"match_type": "all", "conditions": [
        {"field": "subscribed", "operator": "equals", "value": "subscribed"},
        {"field": "company",    "operator": "contains", "value": "Acme"}
    ]},
    {"connector": "or", "match_type": "any", "conditions": [...]}
  ]
}

The legacy flat format {"conditions": [...]} is also accepted.
"""

import logging
from datetime import date, timedelta

from django.db.models import Q, Subquery

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Field → operator → ORM lookup mapping
# ---------------------------------------------------------------------------

# company and phone are real columns on SOProspect; the rest live in extra_data.
_STRING_FIELDS = {
    'email':      'email',
    'first_name': 'first_name',
    'last_name':  'last_name',
    'company':    'company',
    'phone':      'phone',
    'job_title':  'extra_data__job_title',
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

# operator -> (SOEvent.event_type, value_source, include)
_CAMPAIGN_ACTIVITY_MAP = {
    'opened_any':           ('opened',  None,    True),
    'not_opened_any':       ('opened',  None,    False),
    'clicked_any':          ('clicked', None,    True),
    'not_clicked_any':      ('clicked', None,    False),
    'replied_any':          ('replied', None,    True),
    'not_replied_any':      ('replied', None,    False),
    'opened_campaign':      ('opened',  'value', True),
    'not_opened_campaign':  ('opened',  'value', False),
    'clicked_campaign':     ('clicked', 'value', True),
    'not_clicked_campaign': ('clicked', 'value', False),
}

_DELIVERY_STATUS_MAP = {
    'delivered':  'delivered',
    'bounced':    'bounced',
    'complained': 'complained',
    'sent':       'sent',
}


def _build_condition_q(condition, user_id):
    """
    Convert one condition dict into a Q object (or None to skip).
    Returns (Q, negate:bool) — negation is applied by the caller.
    """
    from Email_validate_app.models import SOEvent, SOListProspect

    field    = condition.get('field', '')
    operator = condition.get('operator', '')
    value    = condition.get('value', '')
    negate   = False

    # ── String / text fields ─────────────────────────────────────────────────
    if field in _STRING_FIELDS:
        orm_field = _STRING_FIELDS[field]
        if operator in ('is_empty', 'is_not_empty'):
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

    # ── Subscription status (SOProspect.status) ──────────────────────────────
    if field == 'subscribed':
        if operator == 'equals' and value in _SUBSCRIPTION_VALUES:
            return Q(status=value), False
        if operator == 'not_equals' and value in _SUBSCRIPTION_VALUES:
            return Q(status=value), True
        if operator == 'in':
            vals = [v.strip() for v in value.split(',') if v.strip() in _SUBSCRIPTION_VALUES]
            if vals:
                return Q(status__in=vals), False
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
            return Q(created_at__date__gte=today - timedelta(days=n)), False
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

        member_ids = (
            SOListProspect.objects
            .filter(so_list_id=list_id, so_list__user_id=user_id, so_list__deleted_at__isnull=True)
            .values('prospect_id')
        )
        q = Q(id__in=Subquery(member_ids))
        if operator == 'not_in_list':
            return q, True
        if operator == 'in_list':
            return q, False
        return None, False

    # ── Campaign activity ────────────────────────────────────────────────────
    if field == 'campaign_activity':
        mapping = _CAMPAIGN_ACTIVITY_MAP.get(operator)
        if not mapping:
            return None, False
        event_type, value_source, include = mapping

        filters = {'campaign__user_id': user_id, 'event_type': event_type}
        if value_source == 'value':
            try:
                filters['campaign_id'] = int(value)
            except (ValueError, TypeError):
                return None, False

        matched = SOEvent.objects.filter(**filters).values('email')
        return Q(email__in=Subquery(matched)), (not include)

    # ── Delivery status ──────────────────────────────────────────────────────
    if field == 'delivery_status':
        event_type = _DELIVERY_STATUS_MAP.get(operator)
        if not event_type:
            return None, False
        matched = SOEvent.objects.filter(
            campaign__user_id=user_id, event_type=event_type,
        ).values('email')
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
            logger.warning('so_segment_builder: skipping bad condition %s: %s', condition, exc)
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
    e.g. G1 AND G2 OR G3  ->  (G1 AND G2) OR G3
    """
    if not groups:
        return None

    or_terms  = []
    and_chain = []
    for i, group in enumerate(groups):
        connector = group.get('connector', 'and') if i > 0 else 'start'
        if i == 0 or connector == 'and':
            and_chain.append(group)
        else:
            or_terms.append(and_chain)
            and_chain = [group]
    if and_chain:
        or_terms.append(and_chain)

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


def build_so_segment_queryset(segment, user_id):
    """Return an SOProspect queryset matching the segment rules."""
    from Email_validate_app.models import SOProspect

    qs    = SOProspect.objects.filter(user_id=user_id, deleted_at__isnull=True)
    rules = segment.rules or {}

    if 'groups' in rules:
        groups = rules['groups']
        if not groups:
            return qs.none()
        combined_q = _build_grouped_q(groups, user_id)
        if combined_q is None:
            return qs.none()
        return qs.filter(combined_q)

    conditions = rules.get('conditions', [])
    if not conditions:
        return qs.none()
    match_type = getattr(segment, 'match_type', 'all')
    combined_q = _build_group_q(conditions, match_type, user_id)
    if combined_q is None:
        return qs.none()
    return qs.filter(combined_q)


def count_so_segment_prospects(segment, user_id):
    """Return the number of unique emails matching the segment."""
    return build_so_segment_queryset(segment, user_id).values('email').distinct().count()


def get_so_segment_emails(segment, user_id):
    """Return a deduplicated list of email strings matching the segment."""
    qs = build_so_segment_queryset(segment, user_id)
    return list(qs.values_list('email', flat=True).distinct())


def get_so_segment_preview(segment_or_rules, user_id, match_type='all', limit=15):
    """Return {count, prospects} for a saved segment or a raw rules dict."""
    if isinstance(segment_or_rules, dict):
        class _FakeSegment:
            pass
        seg = _FakeSegment()
        seg.rules      = segment_or_rules
        seg.match_type = match_type
    else:
        seg = segment_or_rules

    qs    = build_so_segment_queryset(seg, user_id)
    count = qs.values('email').distinct().count()
    rows  = list(
        qs.values('email', 'first_name', 'last_name', 'company', 'status')
        .distinct()
        .order_by('email')[:limit]
    )
    return {'count': count, 'prospects': rows}
