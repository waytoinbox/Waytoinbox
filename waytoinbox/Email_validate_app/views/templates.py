from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.contrib import messages
from django.urls import reverse
from django.views.decorators.http import require_POST as _require_POST

from Email_validate_app.utils import get_user_id


def template_builder(request):
    """Standalone full-page GrapesJS editor for creating/editing a UserTemplate.
    Navigated to (not opened in a modal) from the Create Campaign page."""
    if not request.session.get('logged_in'):
        messages.warning(request, "You need to login first.")
        return redirect(reverse('login'))

    from Email_validate_app.models import UserTemplate
    user_id = get_user_id(request)

    editing_template = None
    template_id = request.GET.get('template_id', '').strip()
    if template_id:
        try:
            t = UserTemplate.objects.get(id=template_id, user_id=user_id, deleted_at__isnull=True)
        except UserTemplate.DoesNotExist:
            messages.warning(request, "Template not found.")
            return redirect(reverse('create_campaign'))
        editing_template = {
            'id': t.id, 'name': t.name, 'subject': t.subject,
            'html_content': t.html_content, 'design_json': t.design_json,
        }

    return render(request, 'i_Template_Builder.html', {
        'editing_template':    editing_template,
        'return_campaign_id':  request.GET.get('return_campaign_id', '').strip(),
        'from_page':           request.GET.get('from', '').strip(),
    })


@_require_POST
def use_library_template(request, template_id):
    """Copy a TemplateLibrary record into the user's own UserTemplate.
    The original library record is never modified."""
    if not request.session.get('logged_in'):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)

    from Email_validate_app.models import TemplateLibrary, UserTemplate
    user_id = get_user_id(request)

    try:
        lib = TemplateLibrary.objects.get(id=template_id, is_active=True, deleted_at__isnull=True)
    except TemplateLibrary.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Template not found'}, status=404)

    # TemplateLibrary has no GrapesJS project state, only raw HTML — the
    # builder falls back to importing html_content as components on first open.
    ut = UserTemplate.objects.create(
        user_id=user_id,
        library_template=lib,
        name=lib.name,
        subject=lib.subject,
        html_content=lib.html_content,
        design_json=lib.design_json,
        used_in_campaign=True,
    )

    return JsonResponse({'status': 'ok', 'template': {
        'id': ut.id, 'name': ut.name, 'subject': ut.subject,
        'html_content': ut.html_content, 'design_json': ut.design_json,
    }})


@_require_POST
def save_user_template(request):
    """Create or update a UserTemplate from the visual builder. Saving never
    writes back to TemplateLibrary, even if this template originated from one."""
    if not request.session.get('logged_in'):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)

    import json as _json
    from premailer import transform as _inline_css
    from django.conf import settings as _settings
    from Email_validate_app.models import UserTemplate
    user_id = get_user_id(request)

    template_id  = request.POST.get('template_id', '').strip()
    name         = request.POST.get('name', '').strip()
    subject      = request.POST.get('subject', '').strip()
    html_content = request.POST.get('html_content', '').strip()
    design_raw   = request.POST.get('design_json', '').strip()

    if not name:
        return JsonResponse({'status': 'error', 'message': 'Template name is required.'}, status=400)
    if not html_content:
        return JsonResponse({'status': 'error', 'message': 'Template content cannot be empty.'}, status=400)

    # html_content arrives as GrapesJS HTML + a <style> block. Inline the CSS
    # here so the stored copy is production-ready for SES/Gmail/Outlook.
    try:
        _site_url = getattr(_settings, 'SITE_URL', '').rstrip('/')
        html_content = _inline_css(
            html_content,
            base_url=_site_url or None,
            remove_classes=False,
            keep_style_tags=False,
        )
    except Exception:
        pass  # fall back to the un-inlined HTML rather than failing the save

    # design_json is the raw GrapesJS project state — opaque to the backend.
    design_json = None
    if design_raw:
        try:
            design_json = _json.loads(design_raw)
        except ValueError:
            design_json = None

    if template_id:
        try:
            ut = UserTemplate.objects.get(id=template_id, user_id=user_id, deleted_at__isnull=True)
        except UserTemplate.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': 'Template not found'}, status=404)
        ut.name = name
        ut.subject = subject
        ut.html_content = html_content
        ut.design_json = design_json
        ut.save()
    else:
        ut = UserTemplate.objects.create(
            user_id=user_id, name=name, subject=subject,
            html_content=html_content, design_json=design_json,
        )

    return JsonResponse({'status': 'ok', 'template': {
        'id': ut.id, 'name': ut.name, 'subject': ut.subject,
        'html_content': ut.html_content, 'design_json': ut.design_json,
    }})


def templates_page(request):
    if not request.session.get('logged_in'):
        messages.warning(request, "You need to login first.")
        return redirect(reverse('login'))

    import json as _json
    from Email_validate_app.models import UserTemplate, TemplateLibrary, Campaign
    user_id = get_user_id(request)

    user_templates    = UserTemplate.objects.filter(user_id=user_id, deleted_at__isnull=True).order_by('-updated_at')
    library_templates = TemplateLibrary.objects.filter(is_active=True, deleted_at__isnull=True).order_by('category', 'name')

    # Templates linked to active campaigns (editing these affects the campaign)
    active_tpl_ids = set(
        Campaign.objects
        .filter(user_id=user_id, status__in=('draft', 'scheduled', 'sending'),
                template__isnull=False, deleted_at__isnull=True)
        .values_list('template_id', flat=True)
    )

    # Templates used in sent campaigns — distinct, with last campaign name
    sent_campaign_qs = (
        Campaign.objects
        .filter(user_id=user_id, status='sent', template__isnull=False, deleted_at__isnull=True)
        .select_related('template')
        .order_by('-sent_at')
    )
    seen_tpl_ids = set()
    used_templates = []
    used_html_map_raw = {}
    for c in sent_campaign_qs:
        t = c.template
        if t and t.id not in seen_tpl_ids and t.deleted_at is None:
            seen_tpl_ids.add(t.id)
            used_templates.append({
                'id': t.id, 'name': t.name, 'updated_at': t.updated_at,
                'campaign_name': c.campaign_name, 'sent_at': c.sent_at,
            })
            used_html_map_raw[str(t.id)] = t.html_content or ''

    user_tpl_list = [{'id': t.id, 'name': t.name, 'updated_at': t.updated_at,
                      'used_in_campaign': t.used_in_campaign,
                      'in_active_campaign': t.id in active_tpl_ids} for t in user_templates]
    lib_tpl_list  = [{'id': t.id, 'name': t.name, 'category': t.category,
                      'get_category_display': t.get_category_display()} for t in library_templates]

    user_html_map = _json.dumps({str(t.id): t.html_content or '' for t in user_templates})
    lib_html_map  = _json.dumps({str(t.id): t.html_content or '' for t in library_templates})
    used_html_map = _json.dumps(used_html_map_raw)

    return render(request, 'i_Templates.html', {
        'user_templates':    user_tpl_list,
        'library_templates': lib_tpl_list,
        'used_templates':    used_templates,
        'user_html_map':     user_html_map,
        'lib_html_map':      lib_html_map,
        'used_html_map':     used_html_map,
    })


@_require_POST
def duplicate_user_template(request, template_id):
    if not request.session.get('logged_in'):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)

    from Email_validate_app.models import UserTemplate
    user_id = get_user_id(request)

    try:
        t = UserTemplate.objects.get(id=template_id, user_id=user_id, deleted_at__isnull=True)
    except UserTemplate.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Template not found'}, status=404)

    copy = UserTemplate.objects.create(
        user_id=user_id,
        library_template=t.library_template,
        name=t.name,
        subject=t.subject,
        html_content=t.html_content,
        design_json=t.design_json,
        used_in_campaign=True,
    )
    if not t.used_in_campaign:
        t.used_in_campaign = True
        t.save(update_fields=['used_in_campaign'])
    return JsonResponse({'status': 'ok', 'template_id': copy.id})


@_require_POST
def upload_template_image(request):
    if not request.session.get('logged_in'):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)

    user_id = get_user_id(request)
    file = request.FILES.get('image')

    if not file:
        return JsonResponse({'status': 'error', 'message': 'No image provided'})

    if file.size > 5 * 1024 * 1024:
        return JsonResponse({'status': 'error', 'message': 'Image too large (max 5 MB)'})

    # SEC-13: verify actual file content — client-supplied Content-Type can be spoofed
    try:
        from PIL import Image
        import io
        img_data = file.read()
        Image.open(io.BytesIO(img_data)).verify()
        file.seek(0)  # reset so Django can save the file normally
    except Exception:
        return JsonResponse({'status': 'error', 'message': 'Invalid image file.'})

    from Email_validate_app.models import TemplateImage
    img = TemplateImage(user_id=user_id, original_name=file.name)
    img.file.save(file.name, file, save=True)

    # Return a relative URL so the builder preview works on any host.
    # premailer's base_url in save_user_template converts it to absolute
    # before the HTML is stored in the DB, so email clients get a full URL.
    return JsonResponse({'status': 'ok', 'url': img.file.url})


@_require_POST
def delete_user_template(request, template_id):
    if not request.session.get('logged_in'):
        return JsonResponse({'status': 'error', 'message': 'Not authenticated'}, status=403)

    from Email_validate_app.models import UserTemplate
    from django.utils import timezone as _tz
    user_id = get_user_id(request)

    try:
        t = UserTemplate.objects.get(id=template_id, user_id=user_id, deleted_at__isnull=True)
    except UserTemplate.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Template not found'}, status=404)

    t.deleted_at = _tz.now()
    t.save(update_fields=['deleted_at'])
    return JsonResponse({'status': 'ok'})
