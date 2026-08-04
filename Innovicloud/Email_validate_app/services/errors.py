from django.http import JsonResponse

ERR_BAD_REQUEST  = 'bad_request'
ERR_UNAUTHORIZED = 'unauthorized'
ERR_FORBIDDEN    = 'forbidden'
ERR_NOT_FOUND    = 'not_found'
ERR_RATE_LIMITED = 'rate_limited'
ERR_SERVER       = 'server_error'
ERR_VALIDATION   = 'validation_error'
ERR_CONFLICT     = 'conflict'


def error_response(code, message, http_status=400, request=None):
    payload = {'status': 'error', 'code': code, 'message': message}
    if request and hasattr(request, 'request_id'):
        payload['request_id'] = request.request_id
    return JsonResponse(payload, status=http_status)


def success_response(data=None, message=None, http_status=200):
    payload = {'status': 'ok'}
    if message:
        payload['message'] = message
    if data is not None:
        payload['data'] = data
    return JsonResponse(payload, status=http_status)
