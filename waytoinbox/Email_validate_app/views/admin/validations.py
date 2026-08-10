import logging

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from Email_validate_app.models import ListFiles
from Email_validate_app.views.admin._base import (
    admin_required, audit, handle_admin_errors, json_error, json_ok,
)
from Email_validate_app.services.admin import validation_service

logger = logging.getLogger('Email_validate_app.views')


@admin_required
@handle_admin_errors
def admin_validations(request):
    page_obj, params, total = validation_service.get_validation_list(request.GET)
    return render(request, 'admin/validations/list.html', {
        'page': 'validations',
        'page_obj': page_obj,
        'params': params,
        'total': total,
    })


@admin_required
@handle_admin_errors
def admin_validation_detail(request, fid):
    ctx = validation_service.get_validation_detail(fid)
    ctx['page'] = 'validations'
    return render(request, 'admin/validations/detail.html', ctx)


@admin_required
@handle_admin_errors
@require_POST
def admin_validation_cancel(request, fid):
    try:
        job = ListFiles.objects.get(pk=fid)
    except ListFiles.DoesNotExist:
        return json_error('Job not found.', status=404)

    if job.job_status not in ('Processing', 'Pending'):
        return json_error(f'Cannot cancel a job with status "{job.job_status}".')

    old_status = job.job_status
    job.job_status = 'Cancelled'
    job.save(update_fields=['job_status'])

    audit(
        request, action='validation.cancel', module='validations',
        target_type='validation', target_id=fid,
        target_repr=job.file_name or str(fid),
        old_value={'job_status': old_status}, new_value={'job_status': 'Cancelled'},
    )
    return json_ok(message='Job cancelled.')


@admin_required
@handle_admin_errors
@require_POST
def admin_validation_delete(request, fid):
    try:
        job, old_status = validation_service.delete_validation(fid)
    except ListFiles.DoesNotExist:
        return json_error('Job not found.', status=404)
    except ValueError as exc:
        return json_error(str(exc))

    audit(
        request, action='validation.delete', module='validations',
        target_type='validation', target_id=fid,
        target_repr=job.file_name or str(fid),
        old_value={'job_status': old_status}, new_value={'job_status': 'Deleted'},
    )
    return json_ok(
        data={'redirect': '/wti-admin/validations/'},
        message='Validation job deleted.',
    )


@admin_required
@handle_admin_errors
def admin_validation_download(request, fid):
    try:
        job = ListFiles.objects.get(pk=fid)
    except ListFiles.DoesNotExist:
        return HttpResponse('Job not found.', status=404)

    if job.job_status != 'Complete':
        return HttpResponse('Job is not complete yet.', status=400)

    return HttpResponse(
        f'Download not yet configured for table: {job.table_name}',
        content_type='text/plain',
    )
