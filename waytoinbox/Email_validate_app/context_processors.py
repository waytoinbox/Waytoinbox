from django.utils.timezone import now

from Email_validate_app.services.credit_manager import get_all_service_balances


def nav_credits(request):
    """Inject the three nav-badge balances (plus trial status) into every
    template context.

    Phase 6 commit 12: this used to read the raw legacy VC/AC/CC columns
    directly, which was never migrated when Email Validation and Email
    Marketing moved onto their own service wallets in commits 1 and 2 — the
    nav badge kept showing the old column while the pages themselves showed
    the correct effective balance. It now reads the same
    get_all_service_balances() aggregate every other balance display uses:
    effective (wallet + legacy fallback + trial) for Email Validation and
    Email Marketing, and the shared Analysis Credits pool on its own — never
    multiplied across the four services that draw on it.

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
        from Email_validate_app.models import UserTable
        from Email_validate_app.services.trial_manager import is_trial_eligible

        email = request.session['logged_in']
        user = UserTable.objects.filter(user_email=email).first()
        if not user:
            return {}
        balances = get_all_service_balances(user.id)
        services = balances['services']
        trial_active = bool(user.trial_started_at and user.trial_ends_at
                            and user.trial_ends_at > now())
        return {
            'nav_validation_credits':      services['email_validation']['effective'],
            'nav_shared_analysis_credits': balances['legacy_shared'].get('ac', 0),
            'nav_marketing_credits':       services['email_marketing']['effective'],
            'nav_trial_active':    trial_active,
            'nav_trial_days_left': max(0, (user.trial_ends_at - now()).days) if trial_active else 0,
            'nav_is_verified':     user.is_verified,
            'nav_trial_eligible':  is_trial_eligible(user),
        }
    except Exception:
        return {}
