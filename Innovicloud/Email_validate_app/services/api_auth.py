import hashlib
import logging
from functools import wraps
from django.http import JsonResponse
from Email_validate_app.models import APIKey

logger = logging.getLogger(__name__)


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def api_key_required(f):
    @wraps(f)
    def decorated(request, *args, **kwargs):
        # SEC-11: Accept key only via header — query params appear in logs and browser history.
        key = request.headers.get("X-API-Key")

        if not key:
            return JsonResponse(
                {"error": "API key required. Send it via the X-API-Key header."},
                status=401,
            )

        # DB-10: compare against stored SHA-256 hash, never the raw key
        key_hash = _hash_key(key)
        try:
            api_key = APIKey.objects.get(key_hash=key_hash, is_active=True)
        except APIKey.DoesNotExist:
            return JsonResponse({"error": "Invalid or inactive API key"}, status=403)

        request.api_key = api_key
        return f(request, *args, **kwargs)

    return decorated
