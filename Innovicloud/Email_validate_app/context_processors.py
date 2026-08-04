from Email_validate_app.services.credit_manager import (
    get_vc_current_credit,
    get_ac_current_credit,
    get_cc_current_credit,
)


def nav_credits(request):
    """Inject VC / AC / CC current-credit counts into every template context."""
    if not request.session.get('logged_in'):
        return {}
    try:
        from Email_validate_app.models import UserTable
        email = request.session['logged_in']
        user = UserTable.objects.filter(user_email=email).first()
        if not user:
            return {}
        uid = user.id
        return {
            'nav_vc_credits': get_vc_current_credit(uid),
            'nav_ac_credits': get_ac_current_credit(uid),
            'nav_cc_credits': get_cc_current_credit(uid),
        }
    except Exception:
        return {}
