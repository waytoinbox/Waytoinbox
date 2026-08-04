"""
Admin Console — shared decorators and helpers.

Every admin view must be decorated with @admin_required.
Every mutating view should also use @log_admin_action or call audit() directly.
"""
import logging
import functools

from django.http import JsonResponse
from django.shortcuts import redirect, render

from Email_validate_app.models import AdminActivity, UserTable

logger = logging.getLogger('Email_validate_app.views')


# ── Permission guard ──────────────────────────────────────────────────────────

def admin_required(view_fn):
    """Verify session + DB admin status on every request. Sets request._admin_user."""
    @functools.wraps(view_fn)
    def wrapper(request, *args, **kwargs):
        if not request.session.get('logged_in'):
            return redirect('login')
        if not request.session.get('is_admin'):
            return redirect('home')
        try:
            user = UserTable.objects.get(user_email=request.session['logged_in'])
        except UserTable.DoesNotExist:
            request.session.flush()
            return redirect('login')
        if not user.is_admin or not user.is_active:
            return redirect('home')
        request._admin_user = user
        return view_fn(request, *args, **kwargs)
    return wrapper


# ── Audit helper ──────────────────────────────────────────────────────────────

def audit(request, action, module, target_type='', target_id='', target_repr='',
          old_value=None, new_value=None, status='success', notes=''):
    """Create an AdminActivity record. Call from any view after a mutating operation."""
    try:
        AdminActivity.objects.create(
            admin=getattr(request, '_admin_user', None),
            action=action,
            module=module,
            target_type=target_type,
            target_id=str(target_id),
            target_repr=target_repr,
            old_value=old_value,
            new_value=new_value,
            ip_address=request.META.get('REMOTE_ADDR'),
            user_agent=request.META.get('HTTP_USER_AGENT', ''),
            status=status,
            notes=notes,
        )
    except Exception as e:
        logger.error('Failed to write audit log: %s', e)


# ── log_admin_action decorator ────────────────────────────────────────────────

def log_admin_action(action, module):
    """
    Decorator for POST views.
    Automatically audits the action on success; logs error + audits on exception.
    Usage:
        @admin_required
        @log_admin_action('user.toggle', 'users')
        def my_view(request, uid):
            ...
            # set request._audit_target to a (target_type, target_id, target_repr) tuple
    """
    def decorator(view_fn):
        @functools.wraps(view_fn)
        def wrapper(request, *args, **kwargs):
            try:
                response = view_fn(request, *args, **kwargs)
                target = getattr(request, '_audit_target', ('', '', ''))
                audit(
                    request, action=action, module=module,
                    target_type=target[0], target_id=target[1], target_repr=target[2],
                    old_value=getattr(request, '_audit_old', None),
                    new_value=getattr(request, '_audit_new', None),
                    status='success',
                )
                return response
            except Exception as exc:
                logger.error('Admin action failed | %s | %s | %s', action, module, exc, exc_info=True)
                target = getattr(request, '_audit_target', ('', '', ''))
                audit(
                    request, action=action, module=module,
                    target_type=target[0], target_id=target[1], target_repr=target[2],
                    status='error', notes=str(exc),
                )
                raise
        return wrapper
    return decorator


# ── handle_admin_errors decorator ────────────────────────────────────────────

def handle_admin_errors(view_fn):
    """
    Wraps a view to catch unexpected exceptions.
    Returns JSON 500 for AJAX requests; renders errors/500.html for browser requests.
    Never exposes stack traces or internal paths.
    """
    @functools.wraps(view_fn)
    def wrapper(request, *args, **kwargs):
        try:
            return view_fn(request, *args, **kwargs)
        except Exception as exc:
            logger.error(
                'Admin view error | %s | %s | %s',
                request.method, request.path, exc, exc_info=True,
            )
            wants_json = (
                request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                or 'application/json' in request.headers.get('Accept', '')
            )
            if wants_json:
                return JsonResponse(
                    {'status': 'error', 'message': 'An unexpected error occurred. Please try again.'},
                    status=500,
                )
            return render(request, 'errors/500.html', status=500)
    return wrapper


# ── JSON response helpers ─────────────────────────────────────────────────────

def json_ok(data=None, message=None):
    payload = {'status': 'ok'}
    if message:
        payload['message'] = message
    if data is not None:
        payload['data'] = data
    return JsonResponse(payload)


def json_error(message, status=400):
    return JsonResponse({'status': 'error', 'message': message}, status=status)
