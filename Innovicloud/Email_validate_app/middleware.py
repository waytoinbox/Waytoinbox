import time
import uuid
import logging
import threading
import traceback

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render

logger = logging.getLogger('Email_validate_app')

_local = threading.local()


def get_request_id():
    return getattr(_local, 'request_id', '-')


def get_user_id_ctx():
    return getattr(_local, 'user_id', '-')


class RequestIDMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = str(uuid.uuid4())
        request.request_id = request_id
        _local.request_id = request_id

        session = getattr(request, 'session', {})
        user_email = session.get('logged_in', '-')
        _local.user_id = user_email

        response = self.get_response(request)
        response['X-Request-ID'] = request_id
        return response


class RequestLoggingMiddleware:
    SKIP_PREFIXES = ('/static/', '/media/', '/favicon.ico')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if any(request.path.startswith(p) for p in self.SKIP_PREFIXES):
            return self.get_response(request)

        start = time.monotonic()
        logger.info(
            '%s %s',
            request.method,
            request.path,
            extra={'request_id': get_request_id(), 'user_id': get_user_id_ctx()},
        )

        response = self.get_response(request)

        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            '%s %s -> %s (%dms)',
            request.method,
            request.path,
            response.status_code,
            duration_ms,
            extra={'request_id': get_request_id(), 'user_id': get_user_id_ctx()},
        )
        return response


class UnhandledExceptionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        except Exception as exc:
            return self._handle(request, exc)

    def _handle(self, request, exc):
        if settings.DEBUG:
            raise exc

        logger.critical(
            'Unhandled exception | %s %s | %s',
            request.method,
            request.path,
            exc,
            exc_info=True,
            extra={'request_id': get_request_id(), 'user_id': get_user_id_ctx()},
        )

        wants_json = (
            request.path.startswith('/api/')
            or 'application/json' in request.headers.get('Accept', '')
            or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        )

        if wants_json:
            payload = {
                'status': 'error',
                'code': 'server_error',
                'message': 'An unexpected error occurred. Please try again.',
                'request_id': getattr(request, 'request_id', '-'),
            }
            return JsonResponse(payload, status=500)

        try:
            return render(request, 'errors/500.html', status=500)
        except Exception:
            return JsonResponse({'status': 'error', 'message': 'Server error'}, status=500)


class ContentSecurityPolicyMiddleware:
    """INF-01: Add CSP and Permissions-Policy headers to every response."""

    # 'unsafe-inline' is required until templates are migrated to nonce-based CSP.
    _CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' "
            "https://cdn.jsdelivr.net "
            "https://cdnjs.cloudflare.com "
            "https://checkout.razorpay.com; "
        "style-src 'self' 'unsafe-inline' "
            "https://cdn.jsdelivr.net "
            "https://cdnjs.cloudflare.com "
            "https://fonts.googleapis.com; "
        "font-src 'self' "
            "https://fonts.gstatic.com "
            "https://cdnjs.cloudflare.com "
            "https://cdn.jsdelivr.net; "
        "img-src 'self' data: blob: https:; "
        "connect-src 'self' "
            "https://api.razorpay.com "
            "https://checkout.razorpay.com; "
        "frame-src https://api.razorpay.com; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self';"
    )
    _PERMISSIONS = "geolocation=(), microphone=(), camera=()"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not response.has_header("Content-Security-Policy"):
            response["Content-Security-Policy"] = self._CSP
        response["Permissions-Policy"] = self._PERMISSIONS
        return response


class SessionExpiryMiddleware:
    """
    Convert login redirects (302 → /login/) to JSON 401 for AJAX/API requests.
    Without this, fetch() callers silently receive the login-page HTML as a
    successful 200 response after following the redirect.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        if response.status_code != 302:
            return response

        login_url = getattr(settings, 'LOGIN_URL', '/login/')
        location = response.get('Location', '')
        if not location.startswith(login_url):
            return response

        wants_json = (
            request.path.startswith('/api/')
            or 'application/json' in request.headers.get('Accept', '')
            or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or request.headers.get('X-Fetch') == '1'
        )
        if not wants_json:
            return response

        return JsonResponse(
            {
                'status': 'error',
                'code': 'unauthorized',
                'message': 'Your session has expired. Please log in again.',
                'redirect': login_url,
            },
            status=401,
        )
