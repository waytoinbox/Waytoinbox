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

    nav_trial_active/nav_trial_days_left cost nothing extra -- `user` is
    already loaded below, so these just read fields already on it.

    Display only: no balance is written, no service is charged.
    """
    if not request.session.get('logged_in'):
        return {}
    try:
        from Email_validate_app.models import UserTable
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
        }
    except Exception:
        return {}
