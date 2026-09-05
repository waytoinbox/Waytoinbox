"""
so_analytics.py
----------------
V2.4.9 — Advanced Analytics / Optimization: a read-only aggregation layer
over the existing SOEvent data.

Nothing in this module writes to SOEvent, SOCampaign, SOCampaignContact, or
any other send-path model, and nothing in send_next_step / so_send_campaign_task
/ so_imap.py's reply-bounce-complaint detection / pick_sender_account /
pick_variant_label imports this module. This is purely a reader built on top
of what V1-V2.4.8 already record — see the inspection notes below for exactly
what that data does and does not support.

## Event semantics this module relies on (traced from the actual code, not
## assumed)

- 'sent' / 'delivered' — created together, exactly once per successful
  step-send, by services/so_drip.py::_record_success. metadata['step'] is
  the exact 0-based SOSequenceStep.order that was just sent. A given
  contact only ever sends a given step number once (current_step only moves
  forward), so at this granularity "total events" and "unique contacts" are
  the same number by construction — no dedup ambiguity here.
- 'opened' / 'clicked' — views/so_tracking.py creates ONE new row per
  pixel-load / per link-click, with NO de-dup guard. A single physical email
  opened 5 times produces 5 'opened' rows. This module always computes both
  the raw event total and the distinct-contact ("unique") count for these
  two types, and — per explicit product decision — uses the unique count
  for every rate calculation (open_rate, click_rate, and anywhere else
  these feed a percentage). See compute_overview.

  IMPORTANT — 'opened' is "the tracking pixel was fetched," not a proven
  human recipient action, and this module's open-rate numbers inherit that.
  Investigated limitation: a sender viewing their own Gmail/Outlook/Yahoo
  Sent-folder copy of a sent campaign email (auto-saved by the provider
  with the same embedded pixel URL) produces an indistinguishable 'opened'
  event/row — there is no reliable IP/User-Agent/session signal available
  at the pixel endpoint to separate that from a genuine recipient open
  (see views/so_tracking.py and services/so_smtp.py::inject_tracking for
  the forensic-only metadata this module does not read or rely on). This
  is a known, architecture-level limitation of pixel-based open tracking,
  not something this analytics layer can correct after the fact — do not
  add IP/UA-based filtering here as a "fix" for it.
- 'replied' / 'bounced' / 'complained' — deduplicated at write time: at most
  one row ever, per (campaign, email, event_type) — see
  services/so_imap.py::_record_once. total == unique for these by
  construction; this module still computes both defensively rather than
  assuming the guarantee holds forever.
- 'unsubscribed' — created once per unsubscribe action
  (services/so_inbox.py::unsubscribe_contact); not guarded against a
  double-submit of the confirmation form, so this module conservatively
  uses the unique-contact count here too.
- 'failed' is NOT an SOEvent type at all — a permanently failed send is
  SOCampaignContact.status == 'failed' (services/so_drip.py::_record_failure,
  after MAX_ATTEMPTS). Computed from SOCampaignContact, not SOEvent.

## Step / variant / day-hour attribution for engagement events

'sent'/'delivered' carry an exact step number (metadata['step']), written
once per real send by services/so_drip.py::_record_success — never
touched again afterward, including by condition branching (V3.1-V3.7),
so this attribution is exact and permanent regardless of how a contact's
sequence position later changes.

'opened'/'clicked'/'replied' carry a precise, per-event `step_order` field
(SOTrackedLink/SOOpenPixel's own step_order for opened/clicked — V3.2/
V3.6 — and V3.7's ref_ids-matched value for replied), populated once, at
the moment the engagement is recorded (views/so_tracking.py,
services/so_imap.py::_record_once), and preferred over the alternative
below whenever it's present (V3.8 — see _resolve_engagement_step). Before
V3.8, this module ignored step_order entirely and joined every engagement
event's `message_id` back to the 'sent' event sharing that message_id —
which is `cc.message_id`, i.e. whichever step was MOST RECENTLY sent to
that contact at event time, not necessarily the step actually engaged
with. A late click/open/reply on an older step's email, arriving after
later steps have already gone out, was misattributed to the newer step;
step_order fixes this because it's captured once, per event, and never
changes as the contact's sequence position moves on.

'bounced'/'complained'/'unsubscribed' still use the message_id join
exclusively — so_imap.py's step_order for bounced/complained is still
only the same current_step-1 heuristic the join already approximates
(the V3.7 precise-attribution refinement is scoped to 'replied' only),
so there is nothing more accurate to prefer there. The same join is also
the fallback for opened/clicked/replied whenever step_order is NULL
(legacy events predating V3.2/V3.6, or a 'replied' event recorded before
any step had been sent). Events attributable by neither method (blank/
legacy message_id with no step_order either) are counted in an explicit
"unattributed" bucket, never guessed into a step.

## Subsequence contamination

SOCampaignContact.current_step is reset to 0 when a contact branches into a
subsequence (services/so_subsequence.py::branch_contact), and metadata['step']
does not record whether a given send was on the main sequence or a
subsequence — so step-number 1 on one contact's row can mean something
different than step-number 1 on another's. Per-step/variant analytics here
is scoped to the MAIN sequence only (mirrors V2.2's own "main sequence only"
precedent) and excludes every event belonging to a contact that currently
has active_subsequence_id set, rather than guessing which of that contact's
past sends were pre- or post-branch. This is conservative — it can
under-count a branched contact's real pre-branch main-sequence sends — but
it never misattributes data. The excluded-contact count is always reported
alongside the step/variant tables.

## Timezone

Day/hour bucketing and the Today/7d/30d date-filter presets use
campaign.schedule_timezone (via campaign_timezone()) for single-campaign
views — the exact timezone concept services/so_drip.py's own send-window
logic (_campaign_tz) already established for "what day/hour is it for this
campaign's sends" — rather than inventing a second timezone concept for the
same campaign. Cross-campaign views (comparison, sender-account rollups)
use the requesting user's UserTable.timezone, falling back to UTC. All
storage remains UTC (SOEvent.created_at, auto_now_add, USE_TZ=True) — only
the bucket boundaries are computed in local time.
"""

import re
from collections import defaultdict
from datetime import datetime, timedelta, time as dt_time

from django.db.models import Count, Q
from django.utils.timezone import now

MIN_SAMPLE_SIZE = 100  # delivered emails, per the spec's own recommended threshold

EVENT_TYPES = ('sent', 'delivered', 'opened', 'clicked', 'replied', 'bounced', 'complained', 'unsubscribed')

WEEKDAY_NAMES = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')


# ── Timezone / date-range resolution ────────────────────────────────────────

def resolve_timezone(tz_name):
    from zoneinfo import ZoneInfo, available_timezones
    if tz_name and tz_name in available_timezones():
        return ZoneInfo(tz_name)
    return ZoneInfo('UTC')


def campaign_timezone(campaign):
    return resolve_timezone(getattr(campaign, 'schedule_timezone', None))


def user_timezone(user):
    return resolve_timezone(getattr(user, 'timezone', None))


def resolve_date_range(preset, tz, date_from=None, date_to=None):
    """(start, end) as tz-aware datetimes, end EXCLUSIVE, or (None, None) for
    'all' (no filter at all). date_from/date_to are `date` objects, used only
    when preset == 'custom'."""
    today_local = now().astimezone(tz).date()

    def _day_start(d):
        return datetime.combine(d, dt_time.min, tzinfo=tz)

    if preset == 'today':
        start = _day_start(today_local)
        end = start + timedelta(days=1)
    elif preset == '7d':
        start = _day_start(today_local - timedelta(days=6))
        end = _day_start(today_local) + timedelta(days=1)
    elif preset == '30d':
        start = _day_start(today_local - timedelta(days=29))
        end = _day_start(today_local) + timedelta(days=1)
    elif preset == 'custom' and date_from and date_to:
        lo, hi = (date_from, date_to) if date_from <= date_to else (date_to, date_from)
        start = _day_start(lo)
        end = _day_start(hi) + timedelta(days=1)
    else:
        return None, None
    return start, end


def _apply_date(qs, start, end):
    if start is not None:
        qs = qs.filter(created_at__gte=start)
    if end is not None:
        qs = qs.filter(created_at__lt=end)
    return qs


# ── Safe math ────────────────────────────────────────────────────────────────

def _pct(numerator, denominator):
    """None (not 0) when the denominator is unavailable, so callers render
    'Insufficient data' / '—' instead of a misleading 0%."""
    if not denominator:
        return None
    v = round(numerator / denominator * 100, 1)
    return int(v) if v == int(v) else v


# ── Core event aggregation ──────────────────────────────────────────────────

def event_totals(events_qs):
    """One aggregate query -> {event_type: {'total': N, 'unique': N}}."""
    agg_kwargs = {}
    for et in EVENT_TYPES:
        agg_kwargs[f'{et}__total'] = Count('id', filter=Q(event_type=et))
        agg_kwargs[f'{et}__unique'] = Count('email', filter=Q(event_type=et), distinct=True)
    row = events_qs.aggregate(**agg_kwargs)
    return {
        et: {'total': row[f'{et}__total'] or 0, 'unique': row[f'{et}__unique'] or 0}
        for et in EVENT_TYPES
    }


def _branched_emails(campaign):
    """Emails currently on a subsequence — excluded from step/variant
    analytics. See module docstring, 'Subsequence contamination'."""
    from Email_validate_app.models import SOCampaignContact
    return set(
        SOCampaignContact.objects.filter(campaign=campaign, active_subsequence_id__isnull=False)
        .values_list('email', flat=True)
    )


def _resolve_engagement_step(row, msg_index):
    """V3.8 — step attribution for a single opened/clicked/replied/bounced
    SOEvent row. `row` must include 'event_type', 'message_id', 'step_order'.

    opened/clicked/replied carry a precise, per-event step_order —
    SOTrackedLink/SOOpenPixel's own step_order for opened/clicked (V3.2/
    V3.6), V3.7's ref_ids-matched value for replied — populated exactly
    once, on the event row itself, at the moment the engagement was
    recorded (views/so_tracking.py, services/so_imap.py::_record_once).
    Preferring it over the message_id join (previously the ONLY
    attribution path) fixes late engagement: a click/open/reply on an
    OLDER step's email, arriving after later steps have already been
    sent, previously resolved via message_id to whichever step was most
    recently sent at event time — not the step actually engaged with.

    bounced is deliberately excluded from this preference: so_imap.py's
    step_order for bounced is still only the same current_step-1 heuristic
    the message_id join already approximates (see _record_once's V3.7
    docstring — the precise-attribution refinement there is scoped to
    'replied' only), never a precise per-event value, so there is nothing
    more accurate to prefer there — it keeps using the message_id join,
    completely unchanged from before this function existed.

    Falls back to the message_id join whenever step_order is NULL (legacy
    events predating V3.2/V3.6, or a 'replied' event recorded before any
    step had been sent — see _record_once) — never removed, only ever
    preferred over.
    """
    if row['event_type'] in ('opened', 'clicked', 'replied') and row['step_order'] is not None:
        return row['step_order']
    return msg_index.get(row['message_id']) if row['message_id'] else None


def _sent_message_index(campaign, exclude_emails=frozenset()):
    """message_id -> step order, for EVERY 'sent' event in this campaign
    (deliberately not date-filtered — a reply that arrives after the window
    end must still resolve to the step, sent before that window closed, that
    it belongs to)."""
    from Email_validate_app.models import SOEvent

    qs = SOEvent.objects.filter(campaign=campaign, event_type='sent').exclude(email__in=exclude_emails)
    return {
        row['message_id']: (row['metadata'] or {}).get('step')
        for row in qs.values('message_id', 'metadata') if row['message_id']
    }


# ── Campaign overview ────────────────────────────────────────────────────────

def compute_overview(campaign, start=None, end=None):
    """Campaign-level totals + rates for [start, end). Handles
    campaign.tracking_enabled (V2.3.4): when tracking is off, opened/clicked
    are reported as None (not 0) so callers can render 'Tracking disabled'
    instead of a misleading 0-engagement campaign."""
    from Email_validate_app.models import SOEvent, SOCampaignContact

    events = _apply_date(SOEvent.objects.filter(campaign=campaign), start, end)
    counts = event_totals(events)

    # SOCampaignContact records no timestamp at all for when a send
    # permanently failed (_record_failure sets status/attempts/error/
    # next_action_at but nothing datetime-shaped) -- there is no real field
    # to date-filter this by, and fabricating one (e.g. campaign creation
    # date) is explicitly disallowed. So "failed" is always the all-time
    # count for this campaign, regardless of the selected date preset; the
    # UI labels it accordingly rather than implying it obeys the filter.
    failed = SOCampaignContact.objects.filter(campaign=campaign, status='failed').count()

    sent_total = counts['sent']['total']
    delivered_total = counts['delivered']['total']
    delivered_unique = counts['delivered']['unique']
    tracking_off = not campaign.tracking_enabled

    opened_unique = None if tracking_off else counts['opened']['unique']
    opened_total = None if tracking_off else counts['opened']['total']
    clicked_unique = None if tracking_off else counts['clicked']['unique']
    clicked_total = None if tracking_off else counts['clicked']['total']
    replied_unique = counts['replied']['unique']
    bounced_unique = counts['bounced']['unique']
    complained_unique = counts['complained']['unique']
    unsubscribed_unique = counts['unsubscribed']['unique']

    return {
        'tracking_enabled': campaign.tracking_enabled,
        'totals': {
            'sent': sent_total, 'delivered': delivered_total,
            'opened': opened_unique, 'opened_total_events': opened_total,
            'clicked': clicked_unique, 'clicked_total_events': clicked_total,
            'replied': replied_unique, 'bounced': bounced_unique,
            'complained': complained_unique, 'unsubscribed': unsubscribed_unique,
            'failed': failed,
        },
        'rates': {
            'delivery_rate': _pct(delivered_total, sent_total),
            'open_rate': None if tracking_off else _pct(opened_unique, delivered_unique),
            'click_rate': None if tracking_off else _pct(clicked_unique, delivered_unique),
            'reply_rate': _pct(replied_unique, delivered_unique),
            'bounce_rate': _pct(bounced_unique, sent_total),
            'complaint_rate': _pct(complained_unique, delivered_unique),
            'unsubscribe_rate': _pct(unsubscribed_unique, delivered_unique),
        },
    }


def compute_funnel(campaign, start=None, end=None):
    """Sent -> Delivered -> Opened -> Clicked -> Replied, contact-level
    (unique emails) at every stage — a funnel is inherently a per-recipient
    journey, so this deliberately differs from the 'Sent' KPI number (which
    counts total emails including every sequence step)."""
    from Email_validate_app.models import SOEvent

    events = _apply_date(SOEvent.objects.filter(campaign=campaign), start, end)
    counts = event_totals(events)
    tracking_off = not campaign.tracking_enabled
    stages = [
        ('sent', counts['sent']['unique']),
        ('delivered', counts['delivered']['unique']),
        ('opened', None if tracking_off else counts['opened']['unique']),
        ('clicked', None if tracking_off else counts['clicked']['unique']),
        ('replied', counts['replied']['unique']),
    ]
    first = stages[0][1] or 0
    return [
        {'stage': stage, 'value': value, 'pct_of_first': _pct(value, first) if value is not None else None}
        for stage, value in stages
    ]


def compute_trend(campaign, start=None, end=None):
    """Daily RAW event counts (campaign.schedule_timezone day buckets) for a
    sending-volume trend chart — a time series of activity, not a
    per-recipient metric, so total events (not unique contacts) is the right
    number here.

    Bucketed in Python (astimezone), not via Django's TruncDate(tzinfo=...):
    that lowers to MySQL's CONVERT_TZ(), which silently returns NULL unless
    the server's named-timezone tables are loaded (mysql.time_zone_name) —
    confirmed empty on this deployment. created_at itself is a normal
    UTC-aware datetime (USE_TZ=True) regardless, so Python-side conversion
    needs no MySQL timezone support at all."""
    from Email_validate_app.models import SOEvent

    tz = campaign_timezone(campaign)
    events = _apply_date(SOEvent.objects.filter(campaign=campaign), start, end).values('created_at', 'event_type')

    by_day = defaultdict(lambda: {et: 0 for et in EVENT_TYPES})
    for r in events:
        day = r['created_at'].astimezone(tz).date()
        by_day[day][r['event_type']] += 1
    days = sorted(by_day.keys())
    return {
        'labels': [d.isoformat() for d in days],
        'series': {et: [by_day[d][et] for d in days] for et in EVENT_TYPES},
    }


# ── Sequence step / A-B variant analytics ───────────────────────────────────

def compute_step_analytics(campaign, start=None, end=None):
    """Per-main-sequence-step Sent/Delivered (exact) + Opened/Clicked/
    Replied (precise step_order, V3.8 — see _resolve_engagement_step) /
    Bounced (message_id-attributed, see module docstring) + rates.
    Excludes subsequence-branched contacts entirely (see _branched_emails)."""
    from Email_validate_app.models import SOEvent, SOSequenceStep

    branched = _branched_emails(campaign)
    steps = list(SOSequenceStep.objects.filter(campaign=campaign).prefetch_related('variants').order_by('order'))
    step_orders = {s.order for s in steps}

    sent_delivered_qs = _apply_date(
        SOEvent.objects.filter(campaign=campaign, event_type__in=('sent', 'delivered')).exclude(email__in=branched),
        start, end,
    ).values('event_type', 'metadata', 'email')

    per_step_total = defaultdict(lambda: defaultdict(int))
    for row in sent_delivered_qs:
        step = (row['metadata'] or {}).get('step')
        if step in step_orders:
            per_step_total[step][row['event_type']] += 1

    engage_types = ('opened', 'clicked', 'replied', 'bounced')
    engage_qs = _apply_date(
        SOEvent.objects.filter(campaign=campaign, event_type__in=engage_types).exclude(email__in=branched),
        start, end,
    ).values('event_type', 'email', 'message_id', 'step_order')

    msg_index = _sent_message_index(campaign, exclude_emails=branched)
    per_step_unique = defaultdict(lambda: defaultdict(set))
    unattributed = defaultdict(set)
    for row in engage_qs:
        step = _resolve_engagement_step(row, msg_index)
        if step is None or step not in step_orders:
            unattributed[row['event_type']].add(row['email'])
        else:
            per_step_unique[step][row['event_type']].add(row['email'])

    tracking_off = not campaign.tracking_enabled
    out = []
    for s in steps:
        sent_n = per_step_total[s.order].get('sent', 0)
        delivered_n = per_step_total[s.order].get('delivered', 0)
        opened_n = None if tracking_off else len(per_step_unique[s.order]['opened'])
        clicked_n = None if tracking_off else len(per_step_unique[s.order]['clicked'])
        replied_n = len(per_step_unique[s.order]['replied'])
        bounced_n = len(per_step_unique[s.order]['bounced'])
        out.append({
            'order': s.order,
            'label': s.name or f'Step {s.order + 1}',
            'variants': [{'label': v.label, 'subject': v.subject} for v in s.variants.all().order_by('label')],
            'sent': sent_n, 'delivered': delivered_n,
            'opened': opened_n, 'clicked': clicked_n, 'replied': replied_n, 'bounced': bounced_n,
            'open_rate': None if tracking_off else _pct(opened_n, delivered_n),
            'click_rate': None if tracking_off else _pct(clicked_n, delivered_n),
            'reply_rate': _pct(replied_n, delivered_n),
            'bounce_rate': _pct(bounced_n, sent_n),
        })
    return {
        'steps': out,
        'excluded_branched_contacts': len(branched),
        'unattributed': {et: len(v) for et, v in unattributed.items()},
    }


def _variant_label_index(campaign, exclude_emails=frozenset()):
    from Email_validate_app.models import SOCampaignContact
    return dict(
        SOCampaignContact.objects.filter(campaign=campaign).exclude(email__in=exclude_emails)
        .values_list('email', 'variant_label')
    )


def _resolve_analytics_variant_label(campaign_id, email, step, raw_label, cache):
    """V4.x variation-weight fix — mirrors services/so_drip.py::
    _resolve_step_and_variant's own legacy-vs-new split exactly, so
    analytics attribution matches what was actually sent. A non-empty
    stored variant_label (every contact enrolled before this fix, including
    any that were merely scheduled and never yet sent — see the model
    field's own role as a permanent enrollment-time sentinel) means one
    sticky label for the whole sequence, returned as-is. An empty
    variant_label (contacts enrolled after this fix) has no stored
    per-step record at all — which variant they got for a given step is
    reconstructed here via the EXACT SAME pick_variant_label() the send
    path itself calls, never a separate reimplementation of the
    hashing/weighting, since selection is a pure function of
    (campaign_id, email, step.id). `cache` is a plain dict local to one
    compute_variant_analytics() call, keyed by (email, step.id) — the
    stable step identity, not step.order — so the same (email, step) pair
    recurring across sent/delivered/opened/clicked/replied/bounced rows for
    one contact is only hashed once. An email with no matching
    SOCampaignContact row at all (raw_label is None, an orphaned-event
    edge case) is treated the same as a new-style contact rather than a
    hardcoded guess, for the same reason: it's the most accurate
    reconstruction available from what's actually stored."""
    if raw_label:
        return raw_label
    key = (email, step.id)
    if key not in cache:
        from Email_validate_app.services.so_drip import pick_variant_label
        cache[key] = pick_variant_label(campaign_id, email, step.id, list(step.variants.all()))
    return cache[key]


def compute_variant_analytics(campaign, start=None, end=None):
    """Per (step, variant_label): Sent/Delivered/Opened/Clicked/Replied/
    Bounced + rates, plus an advisory best_variant_label per step (reply-rate
    winner among variants meeting MIN_SAMPLE_SIZE on delivered).

    V4.x variation-weight fix — variant_label is no longer one stable label
    for the whole sequence for every contact (see
    _resolve_analytics_variant_label above for the legacy/new split this
    now requires per step, not just per contact). The step dimension uses
    the same step_order-first, message_id-fallback attribution as
    compute_step_analytics (V3.8 — see _resolve_engagement_step)."""
    from Email_validate_app.models import SOEvent, SOSequenceStep

    branched = _branched_emails(campaign)
    steps = list(SOSequenceStep.objects.filter(campaign=campaign).prefetch_related('variants').order_by('order'))
    step_orders = {s.order for s in steps}
    steps_by_order = {s.order: s for s in steps}
    variant_of = _variant_label_index(campaign, exclude_emails=branched)
    label_cache = {}

    bucket = defaultdict(lambda: defaultdict(set))  # (step, label) -> event_type -> set(email)

    sent_delivered_qs = _apply_date(
        SOEvent.objects.filter(campaign=campaign, event_type__in=('sent', 'delivered')).exclude(email__in=branched),
        start, end,
    ).values('event_type', 'metadata', 'email')
    for row in sent_delivered_qs:
        step_order = (row['metadata'] or {}).get('step')
        if step_order not in step_orders:
            continue
        label = _resolve_analytics_variant_label(
            campaign.id, row['email'], steps_by_order[step_order], variant_of.get(row['email']), label_cache,
        )
        bucket[(step_order, label)][row['event_type']].add(row['email'])

    engage_types = ('opened', 'clicked', 'replied', 'bounced')
    engage_qs = _apply_date(
        SOEvent.objects.filter(campaign=campaign, event_type__in=engage_types).exclude(email__in=branched),
        start, end,
    ).values('event_type', 'email', 'message_id', 'step_order')
    msg_index = _sent_message_index(campaign, exclude_emails=branched)
    for row in engage_qs:
        step_order = _resolve_engagement_step(row, msg_index)
        if step_order not in step_orders:
            continue
        label = _resolve_analytics_variant_label(
            campaign.id, row['email'], steps_by_order[step_order], variant_of.get(row['email']), label_cache,
        )
        bucket[(step_order, label)][row['event_type']].add(row['email'])

    tracking_off = not campaign.tracking_enabled
    out = []
    for s in steps:
        variant_rows = []
        for v in s.variants.all().order_by('label'):
            b = bucket.get((s.order, v.label), {})
            sent_n = len(b.get('sent', ()))
            delivered_n = len(b.get('delivered', ()))
            opened_n = None if tracking_off else len(b.get('opened', ()))
            clicked_n = None if tracking_off else len(b.get('clicked', ()))
            replied_n = len(b.get('replied', ()))
            bounced_n = len(b.get('bounced', ()))
            variant_rows.append({
                'label': v.label, 'subject': v.subject, 'weight': v.weight, 'is_active': v.is_active,
                'sent': sent_n, 'delivered': delivered_n, 'opened': opened_n, 'clicked': clicked_n,
                'replied': replied_n, 'bounced': bounced_n,
                'open_rate': None if tracking_off else _pct(opened_n, delivered_n),
                'click_rate': None if tracking_off else _pct(clicked_n, delivered_n),
                'reply_rate': _pct(replied_n, delivered_n),
                'sufficient_sample': delivered_n >= MIN_SAMPLE_SIZE,
            })
        eligible = [r for r in variant_rows if r['sufficient_sample'] and r['reply_rate'] is not None]
        best = max(eligible, key=lambda r: r['reply_rate']) if eligible else None
        out.append({
            'step_order': s.order,
            'step_label': s.name or f'Step {s.order + 1}',
            'variants': variant_rows,
            'best_variant_label': best['label'] if best else None,
        })
    return out


# ── Sender-account analytics ────────────────────────────────────────────────

def compute_sender_account_analytics(user_id, start=None, end=None, campaign=None, account_id=None):
    """Per-sender-account performance, using SOEvent.account (typed FK,
    V2.3.1) — never inferred from the email address. Events with account_id
    NULL (legacy/unattributed, pre-V2.3.5 or unmatched by the backfill) are
    excluded from the per-account rows and reported separately, never
    guessed into an account.

    account_id (optional, V4.4 — Account Analytics tabs) narrows the result
    to a single account's own row, reusing the exact same aggregation this
    function already does for every account rather than computing anything
    new — callers just get a 0-or-1-row 'accounts' list back."""
    from Email_validate_app.models import SOEvent, SOEmailAccount

    events = SOEvent.objects.filter(campaign__user_id=user_id)
    if campaign is not None:
        events = events.filter(campaign=campaign)
    if account_id is not None:
        events = events.filter(account_id=account_id)
    events = _apply_date(events, start, end)

    unattributed = event_totals(events.filter(account_id__isnull=True))

    agg_kwargs = {}
    for et in EVENT_TYPES:
        agg_kwargs[f'{et}__total'] = Count('id', filter=Q(event_type=et))
        agg_kwargs[f'{et}__unique'] = Count('email', filter=Q(event_type=et), distinct=True)
    rows = events.filter(account_id__isnull=False).values('account_id').annotate(**agg_kwargs)

    accounts = {a.id: a for a in SOEmailAccount.objects.filter(id__in=[r['account_id'] for r in rows])}

    out = []
    for r in rows:
        acc = accounts.get(r['account_id'])
        sent_total = r['sent__total']
        delivered_total = r['delivered__total']
        delivered_unique = r['delivered__unique']
        opened_unique = r['opened__unique']
        clicked_unique = r['clicked__unique']
        replied_unique = r['replied__unique']
        bounced_unique = r['bounced__unique']
        complained_unique = r['complained__unique']
        unsub_unique = r['unsubscribed__unique']
        out.append({
            'account_id': r['account_id'],
            'email': acc.email if acc else '(deleted account)',
            'status': acc.status if acc else None,
            'sent': sent_total, 'delivered': delivered_total,
            'opened': opened_unique, 'clicked': clicked_unique, 'replied': replied_unique,
            'bounced': bounced_unique, 'complained': complained_unique, 'unsubscribed': unsub_unique,
            'delivery_rate': _pct(delivered_total, sent_total),
            'open_rate': _pct(opened_unique, delivered_unique),
            'click_rate': _pct(clicked_unique, delivered_unique),
            'reply_rate': _pct(replied_unique, delivered_unique),
            'bounce_rate': _pct(bounced_unique, sent_total),
            'complaint_rate': _pct(complained_unique, delivered_unique),
            'unsubscribe_rate': _pct(unsub_unique, delivered_unique),
            'sufficient_sample': delivered_total >= MIN_SAMPLE_SIZE,
        })
    out.sort(key=lambda r: r['sent'], reverse=True)
    return {'accounts': out, 'unattributed': unattributed}


# ── Day / hour send-window analytics ────────────────────────────────────────

def compute_day_hour_analytics(campaign, start=None, end=None):
    """Advisory-only: for 'delivered' events bucketed by weekday/hour of
    their paired 'sent' event (campaign.schedule_timezone), what fraction
    eventually got a reply? A 'replied' event is scored under the bucket of
    the ORIGINAL send it replied to, not the bucket the reply itself arrived
    in — resolved via the same precise (email, step_order) attribution as
    compute_step_analytics/compute_variant_analytics (V3.8 — see
    _resolve_engagement_step), falling back to the message_id join
    whenever step_order is NULL (legacy data)."""
    from Email_validate_app.models import SOEvent

    tz = campaign_timezone(campaign)
    sent_qs = _apply_date(
        SOEvent.objects.filter(campaign=campaign, event_type='sent'), start, end,
    ).values('message_id', 'created_at', 'email', 'metadata')

    msg_bucket = {}
    # V3.8 — secondary index keyed by (email, step), so a precise step_order
    # (see _resolve_engagement_step) can resolve straight to the exact
    # send's own day/hour bucket without needing that send's message_id at
    # all. Populated only alongside msg_bucket (i.e. only when message_id is
    # present) — msg_bucket's own contents/behavior are unchanged.
    step_bucket = {}
    for row in sent_qs:
        if not row['message_id']:
            continue
        local = row['created_at'].astimezone(tz)
        bucket = (local.weekday(), local.hour)
        msg_bucket[row['message_id']] = bucket
        step = (row['metadata'] or {}).get('step')
        if step is not None:
            step_bucket[(row['email'], step)] = bucket

    delivered_qs = _apply_date(
        SOEvent.objects.filter(campaign=campaign, event_type='delivered'), start, end,
    ).values('message_id')
    weekday_totals, hour_totals = defaultdict(int), defaultdict(int)
    for row in delivered_qs:
        bucket = msg_bucket.get(row['message_id']) if row['message_id'] else None
        if bucket is None:
            continue
        wd, hr = bucket
        weekday_totals[wd] += 1
        hour_totals[hr] += 1

    replied_qs = _apply_date(
        SOEvent.objects.filter(campaign=campaign, event_type='replied'), start, end,
    ).values('message_id', 'email', 'step_order')
    weekday_replies, hour_replies = defaultdict(int), defaultdict(int)
    unattributed_replies = 0
    for row in replied_qs:
        # V3.8 — prefer the precise (email, step_order) bucket over the
        # message_id join, same rationale as _resolve_engagement_step;
        # falls back to the message_id join whenever step_order is NULL or
        # doesn't resolve (legacy data) — never removed, only preferred over.
        bucket = step_bucket.get((row['email'], row['step_order'])) if row['step_order'] is not None else None
        if bucket is None:
            bucket = msg_bucket.get(row['message_id']) if row['message_id'] else None
        if bucket is None:
            unattributed_replies += 1
            continue
        wd, hr = bucket
        weekday_replies[wd] += 1
        hour_replies[hr] += 1

    by_weekday = []
    for wd in range(7):
        sent_n = weekday_totals.get(wd, 0)
        replied_n = weekday_replies.get(wd, 0)
        by_weekday.append({
            'weekday': WEEKDAY_NAMES[wd], 'delivered': sent_n, 'replied': replied_n,
            'reply_rate': _pct(replied_n, sent_n),
            'sufficient_sample': sent_n >= MIN_SAMPLE_SIZE,
        })
    by_hour = []
    for hr in range(24):
        sent_n = hour_totals.get(hr, 0)
        replied_n = hour_replies.get(hr, 0)
        by_hour.append({
            'hour': hr, 'label': f'{hr:02d}:00–{(hr + 1) % 24:02d}:00',
            'delivered': sent_n, 'replied': replied_n,
            'reply_rate': _pct(replied_n, sent_n),
            'sufficient_sample': sent_n >= MIN_SAMPLE_SIZE,
        })
    return {
        'timezone': str(tz), 'by_weekday': by_weekday, 'by_hour': by_hour,
        'unattributed_replies': unattributed_replies,
    }


# ── Cross-campaign comparison ───────────────────────────────────────────────

def compute_campaign_comparison(user_id, start=None, end=None, account_id=None):
    """One row per non-deleted campaign owned by user_id, computed via ONE
    grouped aggregate query across all their campaigns' events (never a
    per-campaign loop) to avoid N+1. Tenant isolation: every campaign and
    every event is scoped to user_id.

    account_id (optional, V4.4 — Account Analytics) restricts the events
    aggregated to just that sender account, and — unlike the unscoped path,
    which zero-fills every one of the user's campaigns so none go missing —
    only lists campaigns this account actually has events in, so campaigns
    it was never used to send from don't clutter the breakdown with rows of
    zeros."""
    from Email_validate_app.models import SOCampaign, SOEvent

    campaigns = {c.id: c for c in SOCampaign.objects.filter(user_id=user_id, deleted_at__isnull=True)}
    if not campaigns:
        return []

    events = _apply_date(SOEvent.objects.filter(campaign__user_id=user_id), start, end)
    if account_id is not None:
        events = events.filter(account_id=account_id)
    agg_kwargs = {}
    for et in EVENT_TYPES:
        agg_kwargs[f'{et}__total'] = Count('id', filter=Q(event_type=et))
        agg_kwargs[f'{et}__unique'] = Count('email', filter=Q(event_type=et), distinct=True)
    by_campaign = {r['campaign_id']: r for r in events.values('campaign_id').annotate(**agg_kwargs)}

    cids = campaigns.keys() if account_id is None else [cid for cid in by_campaign if cid in campaigns]

    out = []
    for cid in cids:
        camp = campaigns[cid]
        r = by_campaign.get(cid)
        z = {f'{et}__total': 0 for et in EVENT_TYPES}
        z.update({f'{et}__unique': 0 for et in EVENT_TYPES})
        r = r or z
        sent_total = r['sent__total']
        delivered_total = r['delivered__total']
        delivered_unique = r['delivered__unique']
        tracking_off = not camp.tracking_enabled
        out.append({
            'campaign_id': cid, 'name': camp.name, 'status': camp.status,
            'tracking_enabled': camp.tracking_enabled,
            'sent': sent_total, 'delivered': delivered_total,
            'delivery_rate': _pct(delivered_total, sent_total),
            'open_rate': None if tracking_off else _pct(r['opened__unique'], delivered_unique),
            'click_rate': None if tracking_off else _pct(r['clicked__unique'], delivered_unique),
            'reply_rate': _pct(r['replied__unique'], delivered_unique),
            'bounce_rate': _pct(r['bounced__unique'], sent_total),
            'unsubscribe_rate': _pct(r['unsubscribed__unique'], delivered_unique),
        })
    out.sort(key=lambda r: r['sent'], reverse=True)
    return out


# ── Branch analytics (V4.2) ─────────────────────────────────────────────────
#
# Read-only consumption of SOCampaignContact.branch_path — never writes
# anything, never touches current_step/status/active_subsequence_id/
# last_condition_id, and does not change how branch_path itself is written
# (see services/so_subsequence.py::branch_via_condition/_append_branch_path,
# both untouched by this module).
#
# branch_path is bounded to 500 chars (_BRANCH_PATH_MAX) and drops whole
# entries from the FRONT once exceeded — it is "available/recent branch
# history" for a contact, never assumed to be a complete audit log. This
# module never claims otherwise; a campaign whose contacts have very long
# branch histories will under-report early hops, by design of the field
# itself, not a bug introduced here.

_BRANCH_HOP_RE = re.compile(r'main:(\d+)>cond:(\d+):(yes|no)>main:(\d+)')


def _parse_branch_path(branch_path):
    """Yield (source_step_order, condition_id, direction, target_step_order)
    for every well-formed hop found in a contact's branch_path.

    Each hop is written as 'main:<order>>cond:<id>:<yes|no>>main:<order>'
    (services/so_subsequence.py::branch_via_condition) — note the two
    different reference schemes inside one string: the 'main:' segments are
    SOSequenceStep ORDER values (0-based sequence position), never a
    database id, while 'cond:' is the real SOSequenceCondition PRIMARY KEY.
    Mixing these up is the single easiest mistake when reading this field.

    Regex-scanning for the exact hop pattern (rather than splitting on '>'
    into fixed-size chunks) is what makes this safe against a truncated
    leading fragment: _append_branch_path can cut the stored string at an
    arbitrary byte offset, which can leave a partial, malformed fragment at
    the very start (e.g. a lone 'yes>main:2' with no preceding 'main:X>
    cond:Y:' of its own). A regex match only ever succeeds against a
    complete, well-formed hop, so any such fragment — or any other garbage,
    including empty/None input — is silently skipped rather than raising or
    miscounting. Every tuple this yields is a genuine, complete hop.
    """
    if not branch_path:
        return
    for m in _BRANCH_HOP_RE.finditer(branch_path):
        source_order, condition_id, direction, target_order = m.groups()
        yield int(source_order), int(condition_id), direction, int(target_order)


# V4.0 — group hops. A SEPARATE regex/pattern ('grp:', never 'cond:') written
# by services/so_subsequence.py::branch_via_group, so an existing 'cond:' hop
# is never matched here and a new 'grp:' hop is never matched by
# _BRANCH_HOP_RE above — the two are parsed independently, over the same
# branch_path string, and never interfere with each other. This keeps
# _parse_branch_path/_BRANCH_HOP_RE and everything that already consumes
# their output completely untouched by this addition.
_GROUP_HOP_RE = re.compile(r'main:(\d+)>grp:(\d+):(yes|no)>main:(\d+)')


def _parse_group_path(branch_path):
    """Yield (source_step_order, group_id, direction, target_step_order) for
    every well-formed group hop found in a contact's branch_path. Same
    truncation-safety reasoning as _parse_branch_path above — a regex match
    only ever succeeds against a complete, well-formed 'grp:' hop, so a
    truncated/malformed fragment (or a 'cond:' hop, which this pattern
    structurally cannot match) is silently skipped rather than miscounted."""
    if not branch_path:
        return
    for m in _GROUP_HOP_RE.finditer(branch_path):
        source_order, group_id, direction, target_order = m.groups()
        yield int(source_order), int(group_id), direction, int(target_order)


def compute_branch_analytics(campaign):
    """Read-only branch-execution statistics for one campaign, derived
    entirely from SOCampaignContact.branch_path (+ SOSequenceCondition/
    SOSequenceStep for labeling only). Bounded query cost regardless of
    campaign size: one query for branch_path strings, one for the
    conditions actually referenced, one for this campaign's steps — never
    N+1 per contact/condition/step.

    'evaluated' below means "enrolled in this campaign" (every contact the
    condition engine would ever consider on any given dispatcher tick), not
    a per-tick evaluation-attempt count — the engine does not persist a log
    of attempts that didn't result in a branch, only of ones that did (via
    branch_path), so that finer-grained number isn't available from
    existing data without inventing new state, which this phase explicitly
    must not do.
    """
    from Email_validate_app.models import SOCampaignContact, SOSequenceCondition, SOSequenceStep

    branch_paths = list(
        SOCampaignContact.objects.filter(campaign=campaign).values_list('branch_path', flat=True)
    )
    total_evaluated = len(branch_paths)
    total_with_branch = 0
    total_executions = 0
    yes_count = 0
    no_count = 0

    per_condition = defaultdict(lambda: {'yes': 0, 'no': 0})
    per_flow = defaultdict(int)   # (source_order, target_order) -> count

    for branch_path in branch_paths:
        hops = list(_parse_branch_path(branch_path))
        if hops:
            total_with_branch += 1
        for source_order, condition_id, direction, target_order in hops:
            total_executions += 1
            if direction == 'yes':
                yes_count += 1
            else:
                no_count += 1
            per_condition[condition_id][direction] += 1
            per_flow[(source_order, target_order)] += 1

    # Resolve condition_id -> SOSequenceCondition for labeling only — never
    # used to decide whether something "fired" (that's already fully
    # decided above, purely from parsed branch_path hops). A condition
    # since deleted/edited away is handled safely: it simply won't be found
    # here, and the row below reports it as such rather than guessing.
    conditions_by_id = {
        c.id: c for c in SOSequenceCondition.objects.filter(
            id__in=list(per_condition.keys())
        ).select_related('source_step')
    }
    steps_by_order = {s.order: s for s in SOSequenceStep.objects.filter(campaign=campaign)}

    def _step_label(order):
        step = steps_by_order.get(order)
        if step is None:
            return f'Step {order + 1} (deleted)'
        return step.name or f'Step {order + 1}'

    conditions_out = []
    for condition_id, counts in per_condition.items():
        yes_n, no_n = counts['yes'], counts['no']
        total_n = yes_n + no_n
        cond = conditions_by_id.get(condition_id)
        conditions_out.append({
            'condition_id': condition_id,
            'exists': cond is not None,
            'trigger_type': cond.trigger_type if cond else None,
            'is_active': cond.is_active if cond else None,
            'source_step_order': cond.source_step.order if (cond and cond.source_step_id) else None,
            'source_step_label': (
                _step_label(cond.source_step.order) if (cond and cond.source_step_id) else None
            ),
            'total': total_n, 'yes': yes_n, 'no': no_n,
            'yes_pct': _pct(yes_n, total_n), 'no_pct': _pct(no_n, total_n),
        })
    conditions_out.sort(key=lambda c: c['total'], reverse=True)

    flows_out = []
    for (source_order, target_order), count in per_flow.items():
        flows_out.append({
            'source_step_order': source_order, 'source_step_label': _step_label(source_order),
            'target_step_order': target_order, 'target_step_label': _step_label(target_order),
            'count': count, 'pct': _pct(count, total_executions),
        })
    flows_out.sort(key=lambda f: f['count'], reverse=True)

    # ── V4.0 — group execution aggregation, additive alongside the condition
    # aggregation above. A SEPARATE pass over the same already-fetched
    # branch_paths list (no new query) rather than folding into the loop
    # above, so the existing per-condition/per-flow computation is
    # provably untouched by this addition. ──────────────────────────────────
    from Email_validate_app.models import SOConditionGroup

    total_group_executions = 0
    group_yes_count = 0
    group_no_count = 0
    per_group = defaultdict(lambda: {'yes': 0, 'no': 0})
    per_group_flow = defaultdict(int)

    for branch_path in branch_paths:
        for source_order, group_id, direction, target_order in _parse_group_path(branch_path):
            total_group_executions += 1
            if direction == 'yes':
                group_yes_count += 1
            else:
                group_no_count += 1
            per_group[group_id][direction] += 1
            per_group_flow[(source_order, target_order)] += 1

    groups_by_id = {
        g.id: g for g in SOConditionGroup.objects.filter(
            id__in=list(per_group.keys())
        ).select_related('source_step').prefetch_related('conditions')
    }

    groups_out = []
    for group_id, counts in per_group.items():
        yes_n, no_n = counts['yes'], counts['no']
        total_n = yes_n + no_n
        grp = groups_by_id.get(group_id)
        groups_out.append({
            'group_id': group_id,
            'exists': grp is not None,
            'logic': grp.logic if grp else None,
            'is_active': grp.is_active if grp else None,
            'member_trigger_types': (
                sorted({c.trigger_type for c in grp.conditions.all()}) if grp else []
            ),
            'source_step_order': grp.source_step.order if (grp and grp.source_step_id) else None,
            'source_step_label': (
                _step_label(grp.source_step.order) if (grp and grp.source_step_id) else None
            ),
            'total': total_n, 'yes': yes_n, 'no': no_n,
            'yes_pct': _pct(yes_n, total_n), 'no_pct': _pct(no_n, total_n),
        })
    groups_out.sort(key=lambda g: g['total'], reverse=True)

    group_flows_out = []
    for (source_order, target_order), count in per_group_flow.items():
        group_flows_out.append({
            'source_step_order': source_order, 'source_step_label': _step_label(source_order),
            'target_step_order': target_order, 'target_step_label': _step_label(target_order),
            'count': count, 'pct': _pct(count, total_group_executions),
        })
    group_flows_out.sort(key=lambda f: f['count'], reverse=True)

    return {
        'total_contacts_evaluated': total_evaluated,
        'total_contacts_with_branch': total_with_branch,
        'total_branch_executions': total_executions,
        'yes_count': yes_count, 'no_count': no_count,
        'yes_pct': _pct(yes_count, total_executions), 'no_pct': _pct(no_count, total_executions),
        'conditions': conditions_out,
        'flows': flows_out,
        # V4.0 — has_activity now also reflects group-only activity (a
        # campaign that uses ONLY groups, zero standalone conditions, would
        # otherwise show a misleading "no branching activity" empty state
        # despite having real group executions). Never changes value for
        # any branch_path that contains no 'grp:' hops — total_group_executions
        # is always 0 there, so this OR is a no-op for every pre-V4.0
        # campaign/test fixture.
        'has_activity': total_executions > 0 or total_group_executions > 0,
        # V4.0 — group aggregation, parallel to (never replacing) the
        # condition/flow keys above.
        'total_group_executions': total_group_executions,
        'group_yes_count': group_yes_count, 'group_no_count': group_no_count,
        'group_yes_pct': _pct(group_yes_count, total_group_executions),
        'group_no_pct': _pct(group_no_count, total_group_executions),
        'groups': groups_out,
        'group_flows': group_flows_out,
        'has_group_activity': total_group_executions > 0,
    }
