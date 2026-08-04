from django.shortcuts import render
from django.http import JsonResponse


def _wants_json(request):
    return (
        request.path.startswith('/api/')
        or 'application/json' in request.headers.get('Accept', '')
        or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    )


def _json(code, message, status):
    payload = {'status': 'error', 'code': code, 'message': message}
    if hasattr(request := None, 'request_id'):
        payload['request_id'] = request.request_id
    return JsonResponse(payload, status=status)


def handler400(request, exception=None):
    if _wants_json(request):
        return JsonResponse({'status': 'error', 'code': 'bad_request', 'message': 'Bad request.'}, status=400)
    return render(request, 'errors/400.html', status=400)


def handler403(request, exception=None):
    if _wants_json(request):
        return JsonResponse({'status': 'error', 'code': 'forbidden', 'message': 'You do not have permission to access this resource.'}, status=403)
    return render(request, 'errors/403.html', status=403)


def handler404(request, exception=None):
    if _wants_json(request):
        return JsonResponse({'status': 'error', 'code': 'not_found', 'message': 'The requested resource was not found.'}, status=404)
    return render(request, 'errors/404.html', status=404)


def csrf_failure(request, reason=''):
    """CSRF_FAILURE_VIEW — must accept (request, reason='')."""
    if _wants_json(request):
        return JsonResponse(
            {'status': 'error', 'code': 'forbidden', 'message': 'CSRF verification failed. Refresh the page and try again.'},
            status=403,
        )
    return render(request, 'errors/403.html', status=403)


def handler500(request):
    if _wants_json(request):
        payload = {
            'status': 'error',
            'code': 'server_error',
            'message': 'An unexpected error occurred. Please try again.',
        }
        if hasattr(request, 'request_id'):
            payload['request_id'] = request.request_id
        return JsonResponse(payload, status=500)
    return render(request, 'errors/500.html', status=500)
