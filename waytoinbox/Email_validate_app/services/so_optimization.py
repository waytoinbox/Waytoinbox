"""
so_optimization.py
-------------------
V2.4.9 — deterministic, advisory-only optimization recommendations built on
top of services/so_analytics.py's metrics. No AI/LLM calls, no machine
learning, no automatic weight/schedule changes — every recommendation is a
plain comparison against services.so_analytics.MIN_SAMPLE_SIZE, surfaced for
a human to act on (or not) manually via the existing V2.2/V2.4.8 UI. This
module never writes to SOSequenceVariant.weight, SOEmailAccountRotation.weight,
or SOCampaign.schedule_at/send_hour_*/send_weekdays — see V2.4.9's explicit
scope limit.

Each recommendation is a plain dict:
    {category, message, metrics: {...}, sample_size, sufficient_data}
`sufficient_data=False` recommendations still describe what's known, but the
`message` is phrased as "Insufficient data" / "Not enough delivered emails"
rather than declaring a winner — never present weak evidence as a definitive
result.
"""

from Email_validate_app.services.so_analytics import (
    MIN_SAMPLE_SIZE, compute_step_analytics, compute_variant_analytics,
    compute_sender_account_analytics, compute_day_hour_analytics,
    compute_campaign_comparison,
)


def _sufficiency_note(n, kind='delivered emails'):
    return f'Insufficient data ({n} {kind}, need at least {MIN_SAMPLE_SIZE}).'


def compute_campaign_recommendations(campaign, start=None, end=None, step_data=None,
                                      variant_data=None, day_hour=None, overview=None):
    """Advisory recommendations scoped to ONE campaign: best sequence step,
    best A/B variant per step, best sending day/hour, and flags for this
    campaign's own high-bounce / high-unsubscribe / low-reply condition.

    Callers that already computed step_data/variant_data/day_hour/overview
    (e.g. the campaign-analytics view, which needs all of these anyway for
    its own tables) should pass them in to avoid re-running the same
    aggregation queries a second time. Each is computed on demand when not
    supplied, so this function remains independently callable."""
    from Email_validate_app.services.so_analytics import compute_overview as _compute_overview

    out = []

    if step_data is None:
        step_data = compute_step_analytics(campaign, start, end)
    if variant_data is None:
        variant_data = compute_variant_analytics(campaign, start, end)
    if day_hour is None:
        day_hour = compute_day_hour_analytics(campaign, start, end)
    if overview is None:
        overview = _compute_overview(campaign, start, end)

    # ── Best sequence step (by reply rate, delivered-gated) ────────────────
    eligible_steps = [s for s in step_data['steps'] if s['delivered'] >= MIN_SAMPLE_SIZE and s['reply_rate'] is not None]
    if eligible_steps:
        best = max(eligible_steps, key=lambda s: s['reply_rate'])
        out.append({
            'category': 'best_step',
            'message': f"{best['label']} has the highest reply rate ({best['reply_rate']}%) among steps with enough data.",
            'metrics': {'step': best['label'], 'reply_rate': best['reply_rate'], 'delivered': best['delivered']},
            'sample_size': best['delivered'], 'sufficient_data': True,
        })
    elif step_data['steps']:
        biggest = max(step_data['steps'], key=lambda s: s['delivered'])
        out.append({
            'category': 'best_step', 'message': _sufficiency_note(biggest['delivered']),
            'metrics': {}, 'sample_size': biggest['delivered'], 'sufficient_data': False,
        })

    # ── Best / underperforming A/B variant per step ─────────────────────────
    for step in variant_data:
        eligible = [v for v in step['variants'] if v['sufficient_sample'] and v['reply_rate'] is not None]
        if len(eligible) >= 2:
            best = max(eligible, key=lambda v: v['reply_rate'])
            worst = min(eligible, key=lambda v: v['reply_rate'])
            if best['label'] != worst['label']:
                out.append({
                    'category': 'best_variant',
                    'message': (
                        f"{step['step_label']}: Variant {best['label']} outperforms Variant {worst['label']} "
                        f"on reply rate ({best['reply_rate']}% vs {worst['reply_rate']}%)."
                    ),
                    'metrics': {
                        'step': step['step_label'], 'best_variant': best['label'], 'best_reply_rate': best['reply_rate'],
                        'best_n': best['delivered'], 'worst_variant': worst['label'],
                        'worst_reply_rate': worst['reply_rate'], 'worst_n': worst['delivered'],
                        'difference_pp': round(best['reply_rate'] - worst['reply_rate'], 1),
                    },
                    'sample_size': min(best['delivered'], worst['delivered']), 'sufficient_data': True,
                })
                out.append({
                    'category': 'underperforming_variant',
                    'message': f"{step['step_label']}: Variant {worst['label']} is underperforming at {worst['reply_rate']}% reply rate (n={worst['delivered']}).",
                    'metrics': {'step': step['step_label'], 'variant': worst['label'], 'reply_rate': worst['reply_rate'], 'delivered': worst['delivered']},
                    'sample_size': worst['delivered'], 'sufficient_data': True,
                })
        elif step['variants'] and len(step['variants']) > 1:
            biggest = max(step['variants'], key=lambda v: v['delivered'])
            out.append({
                'category': 'best_variant', 'message': f"{step['step_label']}: {_sufficiency_note(biggest['delivered'])}",
                'metrics': {'step': step['step_label']}, 'sample_size': biggest['delivered'], 'sufficient_data': False,
            })

    # ── Best sending day / time window ──────────────────────────────────────
    eligible_days = [d for d in day_hour['by_weekday'] if d['sufficient_sample'] and d['reply_rate'] is not None]
    if eligible_days:
        best_day = max(eligible_days, key=lambda d: d['reply_rate'])
        out.append({
            'category': 'best_day',
            'message': f"{best_day['weekday']} has the highest reply rate ({best_day['reply_rate']}%) among days with enough data.",
            'metrics': {'weekday': best_day['weekday'], 'reply_rate': best_day['reply_rate'], 'delivered': best_day['delivered']},
            'sample_size': best_day['delivered'], 'sufficient_data': True,
        })
    else:
        biggest = max(day_hour['by_weekday'], key=lambda d: d['delivered'])
        out.append({
            'category': 'best_day', 'message': _sufficiency_note(biggest['delivered']),
            'metrics': {}, 'sample_size': biggest['delivered'], 'sufficient_data': False,
        })

    eligible_hours = [h for h in day_hour['by_hour'] if h['sufficient_sample'] and h['reply_rate'] is not None]
    if eligible_hours:
        best_hour = max(eligible_hours, key=lambda h: h['reply_rate'])
        out.append({
            'category': 'best_time_window',
            'message': f"{best_hour['label']} has the highest reply rate ({best_hour['reply_rate']}%) among time windows with enough data.",
            'metrics': {'window': best_hour['label'], 'reply_rate': best_hour['reply_rate'], 'delivered': best_hour['delivered']},
            'sample_size': best_hour['delivered'], 'sufficient_data': True,
        })
    else:
        biggest = max(day_hour['by_hour'], key=lambda h: h['delivered'])
        out.append({
            'category': 'best_time_window', 'message': _sufficiency_note(biggest['delivered']),
            'metrics': {}, 'sample_size': biggest['delivered'], 'sufficient_data': False,
        })

    # ── This campaign's own high-bounce / high-unsubscribe / low-reply flags ─
    ov = overview
    delivered = ov['totals']['delivered']
    if delivered >= MIN_SAMPLE_SIZE:
        if ov['rates']['bounce_rate'] is not None and ov['rates']['bounce_rate'] >= 5:
            out.append({
                'category': 'high_bounce_campaign',
                'message': f"This campaign has a {ov['rates']['bounce_rate']}% bounce rate over {ov['totals']['sent']} sent.",
                'metrics': {'bounce_rate': ov['rates']['bounce_rate'], 'sent': ov['totals']['sent']},
                'sample_size': ov['totals']['sent'], 'sufficient_data': True,
            })
        if ov['rates']['unsubscribe_rate'] is not None and ov['rates']['unsubscribe_rate'] >= 2:
            out.append({
                'category': 'high_unsubscribe_campaign',
                'message': f"This campaign has a {ov['rates']['unsubscribe_rate']}% unsubscribe rate over {delivered} delivered.",
                'metrics': {'unsubscribe_rate': ov['rates']['unsubscribe_rate'], 'delivered': delivered},
                'sample_size': delivered, 'sufficient_data': True,
            })
        if ov['rates']['reply_rate'] is not None and ov['rates']['reply_rate'] < 1:
            out.append({
                'category': 'low_reply_campaign',
                'message': f"This campaign has a low reply rate ({ov['rates']['reply_rate']}%) over {delivered} delivered.",
                'metrics': {'reply_rate': ov['rates']['reply_rate'], 'delivered': delivered},
                'sample_size': delivered, 'sufficient_data': True,
            })
    else:
        out.append({
            'category': 'campaign_health', 'message': _sufficiency_note(delivered),
            'metrics': {}, 'sample_size': delivered, 'sufficient_data': False,
        })

    return out


def compute_account_recommendations(user_id, start=None, end=None, accounts_data=None, campaigns_data=None):
    """Advisory recommendations scoped ACROSS a user's campaigns: best/worst
    sender account, high-bounce sender, low-reply-rate campaigns, and
    high-unsubscribe campaigns.

    Callers that already computed the sender-account rollup and/or campaign
    comparison (e.g. the Analytics overview view, which needs both anyway)
    should pass them in as accounts_data/campaigns_data to avoid re-running
    those aggregations a second time."""
    out = []

    if accounts_data is None:
        accounts_data = compute_sender_account_analytics(user_id, start, end)
    accounts = accounts_data['accounts']
    eligible = [a for a in accounts if a['sufficient_sample'] and a['reply_rate'] is not None]
    if len(eligible) >= 2:
        best = max(eligible, key=lambda a: a['reply_rate'])
        worst = min(eligible, key=lambda a: a['reply_rate'])
        if best['account_id'] != worst['account_id']:
            out.append({
                'category': 'best_sender',
                'message': (
                    f"{best['email']} has a {best['reply_rate']}% reply rate over {best['delivered']} delivered — "
                    f"currently outperforming {worst['email']} ({worst['reply_rate']}% over {worst['delivered']} delivered)."
                ),
                'metrics': {
                    'best_account': best['email'], 'best_reply_rate': best['reply_rate'], 'best_n': best['delivered'],
                    'worst_account': worst['email'], 'worst_reply_rate': worst['reply_rate'], 'worst_n': worst['delivered'],
                    'difference_pp': round(best['reply_rate'] - worst['reply_rate'], 1),
                },
                'sample_size': min(best['delivered'], worst['delivered']), 'sufficient_data': True,
            })
    elif accounts:
        biggest = max(accounts, key=lambda a: a['delivered'])
        out.append({
            'category': 'best_sender', 'message': _sufficiency_note(biggest['delivered']),
            'metrics': {}, 'sample_size': biggest['delivered'], 'sufficient_data': False,
        })

    for a in accounts:
        if a['sufficient_sample'] and a['bounce_rate'] is not None and a['bounce_rate'] >= 5:
            out.append({
                'category': 'high_bounce_sender',
                'message': f"{a['email']} has a {a['bounce_rate']}% bounce rate over {a['sent']} sent.",
                'metrics': {'account': a['email'], 'bounce_rate': a['bounce_rate'], 'sent': a['sent']},
                'sample_size': a['sent'], 'sufficient_data': True,
            })

    if campaigns_data is None:
        campaigns_data = compute_campaign_comparison(user_id, start, end)
    for c in campaigns_data:
        if c['delivered'] < MIN_SAMPLE_SIZE:
            continue
        if c['reply_rate'] is not None and c['reply_rate'] < 1:
            out.append({
                'category': 'low_reply_campaign',
                'message': f"\"{c['name']}\" has a low reply rate ({c['reply_rate']}%) over {c['delivered']} delivered.",
                'metrics': {'campaign': c['name'], 'reply_rate': c['reply_rate'], 'delivered': c['delivered']},
                'sample_size': c['delivered'], 'sufficient_data': True,
            })
        if c['unsubscribe_rate'] is not None and c['unsubscribe_rate'] >= 2:
            out.append({
                'category': 'high_unsubscribe_campaign',
                'message': f"\"{c['name']}\" has a {c['unsubscribe_rate']}% unsubscribe rate over {c['delivered']} delivered.",
                'metrics': {'campaign': c['name'], 'unsubscribe_rate': c['unsubscribe_rate'], 'delivered': c['delivered']},
                'sample_size': c['delivered'], 'sufficient_data': True,
            })

    return out
