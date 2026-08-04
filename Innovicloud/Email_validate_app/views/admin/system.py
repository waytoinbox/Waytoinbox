import logging
import os
import time

from django.http import JsonResponse
from django.shortcuts import render
from django.db import connection
from django.utils import timezone

from Email_validate_app.views.admin._base import admin_required, handle_admin_errors

logger = logging.getLogger('Email_validate_app.views')


@admin_required
@handle_admin_errors
def admin_system(request):
    checks = _run_health_checks()
    return render(request, 'admin/system/index.html', {
        'page': 'system',
        'checks': checks,
        'now': timezone.now(),
    })


@admin_required
@handle_admin_errors
def admin_system_logs(request):
    n = min(int(request.GET.get('n', 100)), 500)
    log_file = _find_log_file()
    lines = []
    if log_file and os.path.isfile(log_file):
        try:
            with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
                all_lines = f.readlines()
            lines = [l.rstrip('\n') for l in all_lines[-n:]]
        except Exception as exc:
            lines = [f'Error reading log: {exc}']
    else:
        lines = ['Log file not configured or not found.']
    return JsonResponse({'lines': lines})


def _run_health_checks():
    checks = []

    # Database ping
    start = time.monotonic()
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        db_ms = round((time.monotonic() - start) * 1000, 1)
        checks.append({'name': 'Database', 'status': 'ok', 'detail': f'{db_ms} ms', 'icon': 'fa-database'})
    except Exception as exc:
        checks.append({'name': 'Database', 'status': 'error', 'detail': str(exc), 'icon': 'fa-database'})

    # Redis ping (optional)
    try:
        import redis
        from django.conf import settings
        redis_url = getattr(settings, 'CELERY_BROKER_URL', None) or getattr(settings, 'REDIS_URL', None)
        if redis_url and redis_url.startswith('redis'):
            start = time.monotonic()
            r = redis.from_url(redis_url)
            r.ping()
            redis_ms = round((time.monotonic() - start) * 1000, 1)
            checks.append({'name': 'Redis', 'status': 'ok', 'detail': f'{redis_ms} ms', 'icon': 'fa-server'})
        else:
            checks.append({'name': 'Redis', 'status': 'skip', 'detail': 'Not configured', 'icon': 'fa-server'})
    except ImportError:
        checks.append({'name': 'Redis', 'status': 'skip', 'detail': 'redis-py not installed', 'icon': 'fa-server'})
    except Exception as exc:
        checks.append({'name': 'Redis', 'status': 'error', 'detail': str(exc), 'icon': 'fa-server'})

    # Celery (optional)
    try:
        from Email_validate_app.celery import app as celery_app
        inspect = celery_app.control.inspect(timeout=1)
        active = inspect.active()
        if active is None:
            checks.append({'name': 'Celery', 'status': 'warn', 'detail': 'No workers responded', 'icon': 'fa-tasks'})
        else:
            worker_count = len(active)
            task_count = sum(len(v) for v in active.values())
            checks.append({
                'name': 'Celery',
                'status': 'ok',
                'detail': f'{worker_count} worker{"s" if worker_count != 1 else ""}, {task_count} active task{"s" if task_count != 1 else ""}',
                'icon': 'fa-tasks',
            })
    except Exception:
        checks.append({'name': 'Celery', 'status': 'skip', 'detail': 'Not available', 'icon': 'fa-tasks'})

    # Disk space
    try:
        import shutil
        total, used, free = shutil.disk_usage('/')
        free_gb = round(free / (1024 ** 3), 1)
        pct = round(used / total * 100)
        status = 'warn' if pct > 85 else 'ok'
        checks.append({'name': 'Disk', 'status': status, 'detail': f'{free_gb} GB free ({pct}% used)', 'icon': 'fa-hdd'})
    except Exception:
        checks.append({'name': 'Disk', 'status': 'skip', 'detail': 'N/A', 'icon': 'fa-hdd'})

    return checks


def _find_log_file():
    from django.conf import settings
    for handler in logging.root.handlers:
        base = getattr(handler, 'baseFilename', None)
        if base:
            return base
    log_dir = getattr(settings, 'LOG_DIR', None) or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'logs'
    )
    candidate = os.path.join(log_dir, 'django.log')
    if os.path.isfile(candidate):
        return candidate
    return None
