from django.utils.timezone import now

from Email_validate_app.services.credit_manager import get_all_service_balances

# Maps a service to the request-path prefixes that belong to it -- used by
# _current_service() below to decide which single service's balance the
# topbar credit badge shows. Built from the actual urls/*.py modules: most
# services (email_marketing, sales_outreach, reputation, header_analysis,
# ip_blocklist, domain_blocklist) each use one consistent path prefix across
# every one of their views.
SERVICE_PATH_PREFIXES = {
    'email_marketing':  ('/Email_Campaigns/',),
    'sales_outreach':   ('/Sales-Outreach/',),
    'reputation':       ('/Reputation_Analysis/', '/reputation/', '/get-reputation-data/'),
    'header_analysis':  ('/Header_Analysis/', '/dmarc_check/'),
    'ip_blocklist':     ('/Blocklist_Monitor/', '/check_ip_blacklists/', '/get-blocklist-data/',
                          '/blocklist_names/'),
    'domain_blocklist': ('/Domain_Blacklist/', '/check_domain_blocklist/', '/get-domain-blocklist-data/',
                          '/domain_blocklist_names/'),
}
# Email Validation's own URLs (urls/email_validation.py) never adopted one
# shared prefix, so it's matched by exact path instead of startswith().
EMAIL_VALIDATION_PATHS = {
    '/services/upload/', '/verify_emails/', '/Analyze/',
    '/services/download_results/', '/services/delete_query/',
    '/services/single_service/', '/services/hide_email_history/',
    '/run_email_validation/',
}

# Same icon each service already uses in the sidebar (i_index.html), for
# visual consistency between the sidebar entry and the topbar badge.
SERVICE_ICONS = {
    'email_validation': 'fa-envelope',
    'email_marketing':  'fa-paper-plane',
    'sales_outreach':   'fa-bullseye',
    'reputation':       'fa-award',
    'header_analysis':  'fa-gavel',
    'ip_blocklist':     'fa-server',
    'domain_blocklist': 'fa-globe',
}


def _current_service(path):
    """Which of the 7 services this request's page belongs to, if any.
    Drives the single dynamic credit badge in the topbar (see nav_credits()
    below) -- None for a page that isn't tied to one particular service
    (dashboard, profile, pricing, etc.), which hides the badge there rather
    than guessing which balance to show.
    """
    if path in EMAIL_VALIDATION_PATHS:
        return 'email_validation'
    for service, prefixes in SERVICE_PATH_PREFIXES.items():
        if any(path.startswith(prefix) for prefix in prefixes):
            return service
    return None


def nav_credits(request):
    """Inject the topbar's per-page credit badge (plus trial status) into
    every template context.

    The topbar used to show three fixed VC/AC/CC badges, then just linked
    to the Billing tab once each service got its own wallet (see the
    now-removed nav_validation_credits/nav_shared_analysis_credits/
    nav_marketing_credits -- Phase 6 commit 12's own comment on this
    change). This replaces that entirely: exactly one badge, for whichever
    single service the CURRENT PAGE belongs to (via _current_service()
    above), never all 7 services shown together. Navigating to a different
    service's page is a fresh request, so this recomputes on every load --
    nothing needs to change client-side for the badge to track the page.

    nav_trial_active/nav_trial_days_left/nav_is_verified/nav_trial_eligible
    cost nothing extra -- `user` is already loaded below, so these just read
    fields already on it. nav_trial_eligible/nav_is_verified drive the
    trial-activation popup on i_pricing.html/i_subscription.html: eligible
    (never started a trial) + verified -> "Activate Free Trial"; eligible +
    unverified -> "verify your email first"; not eligible (active or
    already used) -> no popup at all.

    Works for a signed-up-but-unverified user too, not just fully logged-in
    verified ones -- signup() now starts a session immediately (see
    views/auth.py), so 'logged_in' being in the session no longer implies
    is_verified.

    Display only: no balance is written, no service is charged.
    """
    if not request.session.get('logged_in'):
        return {}
    try:
        from Email_validate_app.models import SERVICE_CHOICES, UserTable
        from Email_validate_app.services.trial_manager import can_offer_trial

        email = request.session['logged_in']
        user = UserTable.objects.filter(user_email=email).first()
        if not user:
            return {}
        balances = get_all_service_balances(user.id)
        services = balances['services']
        trial_active = bool(user.trial_started_at and user.trial_ends_at
                            and user.trial_ends_at > now())

        current_service = _current_service(request.path)
        service_labels = dict(SERVICE_CHOICES)

        return {
            'nav_current_service':         current_service,
            'nav_current_service_balance': (
                services[current_service]['effective'] if current_service else None),
            'nav_current_service_label':   service_labels.get(current_service),
            'nav_current_service_icon':    SERVICE_ICONS.get(current_service),
            'nav_trial_active':    trial_active,
            'nav_trial_days_left': max(0, (user.trial_ends_at - now()).days) if trial_active else 0,
            'nav_is_verified':     user.is_verified,
            # can_offer_trial(), not is_trial_eligible() alone: also
            # retires the trial offer once the user has ever made a real
            # payment, even if they never activated a trial at all.
            'nav_trial_eligible':  can_offer_trial(user),
        }
    except Exception:
        return {}
