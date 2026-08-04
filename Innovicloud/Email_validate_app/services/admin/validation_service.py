"""Admin Validation Jobs Service."""
import logging
import re

from django.core.paginator import Paginator
from django.db import models

from Email_validate_app.models import ListFiles

logger = logging.getLogger('Email_validate_app.services')

_WIN_TABLE_RE = re.compile(r'^WIN_\d+_\d{4}_\d{2}_\d{2}$')


def _drop_win_table(table_name: str) -> None:
    if not table_name or not _WIN_TABLE_RE.fullmatch(table_name):
        return
    from django.db import connection
    with connection.cursor() as cur:
        cur.execute('DROP TABLE IF EXISTS `%s`' % table_name)  # nosec: pattern-validated above

_PER_PAGE = 25


def get_validation_list(request_get):
    """Return (page_obj, params, total)."""
    qs = ListFiles.objects.select_related('user').order_by('-insert_date')

    q = request_get.get('q', '').strip()
    if q:
        qs = qs.filter(
            models.Q(file_name__icontains=q)
            | models.Q(user__user_email__icontains=q)
        )

    status_filter = request_get.get('status', '')
    if status_filter:
        qs = qs.filter(job_status=status_filter)

    total = qs.count()
    try:
        page_num = int(request_get.get('page', 1))
    except (ValueError, TypeError):
        page_num = 1

    paginator = Paginator(qs, _PER_PAGE)
    page_obj = paginator.get_page(page_num)
    params = {'q': q, 'status': status_filter}
    return page_obj, params, total


def get_validation_detail(fid):
    """Return dict with job."""
    job = ListFiles.objects.select_related('user').get(pk=fid)
    return {'job': job}


def delete_validation(fid):
    """Soft-delete: mark job_status as 'Deleted' and drop orphaned WIN_* table (DB-11). Returns (job, old_status)."""
    job = ListFiles.objects.get(pk=fid)
    if job.job_status == 'Deleted':
        raise ValueError('Job is already deleted.')
    old_status = job.job_status
    job.job_status = 'Deleted'
    job.save(update_fields=['job_status'])
    _drop_win_table(job.table_name)
    return job, old_status
