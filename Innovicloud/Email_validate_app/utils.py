import logging

from .models import UserTable

logger = logging.getLogger(__name__)


def get_user_id(request):
    try:
        if 'logged_in' not in request.session:
            return None
        login_id = request.session['logged_in']
        user = UserTable.objects.filter(user_email=login_id).first()
        if not user:
            logger.warning("get_user_id: no user found for login_id=%s", login_id)
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
