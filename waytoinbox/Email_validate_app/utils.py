import logging

from .models import UserTable

logger = logging.getLogger(__name__)


def get_true_user(request):
    """The actually-authenticated UserTable row, from session['logged_in']
    alone -- ignores any acting_as_email context. This is the only identity
    allowed to initiate a Main<->Sub Account switch/return (see
    get_active_user below); never resolves to an impersonated account."""
    if 'logged_in' not in request.session:
        return None
    login_id = request.session['logged_in']
    return UserTable.objects.filter(user_email=login_id).first()


def get_active_user(request):
    """The UserTable row whose data/services/credits this request should
    operate against: an authorized acting_as_email Sub Account if one is
    set, otherwise the true authenticated user.

    acting_as_email is never trusted blindly -- every call re-verifies that
    (1) the true authenticated user exists, (2) it is itself a Main Account
    (parent_account_id is NULL -- a Sub Account can never act as anyone),
    and (3) the target account's parent_account_id actually equals the true
    user's id. Any failure clears the stale/invalid session key and falls
    back to the true user, so a tampered or dangling acting_as_email can
    never expose another account's data -- it just safely reverts.
    """
    true_user = get_true_user(request)
    if not true_user:
        return None

    acting_email = request.session.get('acting_as_email')
    if not acting_email:
        return true_user

    acting_user = None
    if true_user.parent_account_id is None:
        candidate = UserTable.objects.filter(user_email=acting_email).first()
        if candidate and candidate.parent_account_id == true_user.id:
            acting_user = candidate

    if acting_user is None:
        request.session.pop('acting_as_email', None)
        return true_user

    return acting_user


def get_user_id(request):
    try:
        user = get_active_user(request)
        if not user:
            if 'logged_in' in request.session:
                logger.warning(
                    "get_user_id: no user found for login_id=%s",
                    request.session.get('logged_in'),
                )
            return None
        return user.id
    except Exception as e:
        logger.error("Error in get_user_id: %s", e)
        return None


def create_notification(user_id, notif_type, message, url=''):
    try:
        from Email_validate_app.models import UserNotification
        UserNotification.objects.create(user_id=user_id, type=notif_type, message=message, url=url)
    except Exception as e:
        logger.error("In-app notification failed: user=%s type=%s error=%s", user_id, notif_type, e)
