import json
import logging

from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST

from Email_validate_app.models import TemplateLibrary, UserTemplate
from Email_validate_app.views.admin._base import admin_required, handle_admin_errors, audit

logger = logging.getLogger('Email_validate_app.views')

CATEGORY_CHOICES = [
    ('newsletter', 'Newsletter'),
    ('promotion',  'Promotion'),
    ('welcome',    'Welcome'),
    ('announcement', 'Announcement'),
    ('event',      'Event'),
    ('custom',     'Custom'),
]


@admin_required
@handle_admin_errors
def admin_templates(request):
    library_qs = TemplateLibrary.objects.filter(deleted_at__isnull=True).order_by('-created_at')
    user_qs    = UserTemplate.objects.select_related('user').filter(
        deleted_at__isnull=True
    ).order_by('-created_at')

    q = request.GET.get('q', '').strip()
    if q:
        library_qs = library_qs.filter(Q(name__icontains=q) | Q(category__icontains=q))
        user_qs    = user_qs.filter(Q(name__icontains=q) | Q(user__user_email__icontains=q))

    try:
        page_num = int(request.GET.get('page', 1))
    except (ValueError, TypeError):
        page_num = 1

    return render(request, 'admin/email_templates/list.html', {
        'page':          'email_templates',
        'library_page':  Paginator(library_qs, 20).get_page(page_num),
        'user_page':     Paginator(user_qs, 20).get_page(page_num),
        'q':             q,
        'library_total': library_qs.count(),
        'user_total':    user_qs.count(),
    })


@admin_required
@handle_admin_errors
def admin_library_template_create(request):
    """GET only — renders the builder-based template creation page."""
    return render(request, 'admin/email_templates/create.html', {
        'page':       'email_templates',
        'categories': CATEGORY_CHOICES,
    })


@admin_required
@handle_admin_errors
def admin_library_template_upload(request):
    """GET → upload form.  POST → create new TemplateLibrary record."""
    if request.method == 'POST':
        name        = request.POST.get('name', '').strip()
        category    = request.POST.get('category', 'custom').strip()
        subject     = request.POST.get('subject', '').strip()
        is_active   = request.POST.get('is_active', '1') == '1'
        html_content = request.POST.get('html_content', '').strip()
        design_raw  = request.POST.get('design_json', '').strip()

        html_file = request.FILES.get('html_file')
        if html_file:
            html_content = html_file.read().decode('utf-8', errors='replace').strip()

        errors = []
        if not name:
            errors.append('Template name is required.')
        if not html_content:
            errors.append('HTML content is required (paste HTML or upload a file).')
        if errors:
            return JsonResponse({'status': 'error', 'message': ' '.join(errors)}, status=400)

        design_json = None
        if design_raw:
            try:
                design_json = json.loads(design_raw)
            except (json.JSONDecodeError, ValueError):
                return JsonResponse({'status': 'error', 'message': 'Design JSON is not valid JSON.'}, status=400)

        tmpl = TemplateLibrary.objects.create(
            name=name, category=category, subject=subject,
            is_active=is_active, html_content=html_content, design_json=design_json,
        )
        audit(request, 'library_template.create', 'email_templates',
              target_type='TemplateLibrary', target_id=str(tmpl.id),
              target_repr=tmpl.name, new_value={'name': name, 'category': category})

        return JsonResponse({'status': 'ok', 'id': tmpl.id, 'name': tmpl.name})

    return render(request, 'admin/email_templates/upload.html', {
        'page':       'email_templates',
        'categories': CATEGORY_CHOICES,
    })


@admin_required
@handle_admin_errors
def admin_library_template_edit(request, tid):
    """GET → edit form pre-filled.  POST → update record."""
    tmpl = get_object_or_404(TemplateLibrary, id=tid, deleted_at__isnull=True)

    if request.method == 'POST':
        name         = request.POST.get('name', '').strip()
        category     = request.POST.get('category', tmpl.category).strip()
        subject      = request.POST.get('subject', '').strip()
        is_active    = request.POST.get('is_active', '1') == '1'
        html_content = request.POST.get('html_content', '').strip()
        design_raw   = request.POST.get('design_json', '').strip()

        html_file = request.FILES.get('html_file')
        if html_file:
            html_content = html_file.read().decode('utf-8', errors='replace').strip()

        errors = []
        if not name:
            errors.append('Template name is required.')
        if not html_content:
            errors.append('HTML content is required.')
        if errors:
            return JsonResponse({'status': 'error', 'message': ' '.join(errors)}, status=400)

        # Keep existing design_json if user left the field blank
        design_json = tmpl.design_json
        if design_raw:
            try:
                design_json = json.loads(design_raw)
            except (json.JSONDecodeError, ValueError):
                return JsonResponse({'status': 'error', 'message': 'Design JSON is not valid JSON.'}, status=400)

        old_name = tmpl.name
        tmpl.name        = name
        tmpl.category    = category
        tmpl.subject     = subject
        tmpl.is_active   = is_active
        tmpl.html_content = html_content
        tmpl.design_json = design_json
        tmpl.save()

        audit(request, 'library_template.update', 'email_templates',
              target_type='TemplateLibrary', target_id=str(tmpl.id),
              target_repr=tmpl.name,
              old_value={'name': old_name}, new_value={'name': name, 'category': category})

        return JsonResponse({'status': 'ok'})

    return render(request, 'admin/email_templates/upload.html', {
        'page':           'email_templates',
        'tmpl':           tmpl,
        'categories':     CATEGORY_CHOICES,
        'design_json_str': json.dumps(tmpl.design_json, indent=2) if tmpl.design_json else '',
    })


@admin_required
@handle_admin_errors
@require_POST
def admin_library_template_toggle(request, tid):
    tmpl = get_object_or_404(TemplateLibrary, id=tid, deleted_at__isnull=True)
    tmpl.is_active = not tmpl.is_active
    tmpl.save(update_fields=['is_active'])

    audit(request, 'library_template.toggle', 'email_templates',
          target_type='TemplateLibrary', target_id=str(tmpl.id),
          target_repr=tmpl.name, new_value={'is_active': tmpl.is_active})

    badge = (
        '<span class="badge badge-success"><span class="badge-dot"></span>Active</span>'
        if tmpl.is_active else
        '<span class="badge badge-grey">Inactive</span>'
    )
    return JsonResponse({'status': 'ok', 'is_active': tmpl.is_active, 'badge_html': badge})


@admin_required
@handle_admin_errors
@require_POST
def admin_library_template_delete(request, tid):
    tmpl = get_object_or_404(TemplateLibrary, id=tid, deleted_at__isnull=True)
    name = tmpl.name
    tmpl.deleted_at = timezone.now()
    tmpl.save(update_fields=['deleted_at'])

    audit(request, 'library_template.delete', 'email_templates',
          target_type='TemplateLibrary', target_id=str(tid), target_repr=name)

    return JsonResponse({'status': 'ok'})
