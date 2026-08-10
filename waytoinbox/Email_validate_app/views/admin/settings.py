import logging
from collections import defaultdict

from django.shortcuts import render
from django.views.decorators.http import require_POST

from Email_validate_app.models import AppSetting
from Email_validate_app.views.admin._base import (
    admin_required, handle_admin_errors, audit, json_ok, json_error,
)

logger = logging.getLogger('Email_validate_app.views')


@admin_required
@handle_admin_errors
def admin_settings(request):
    settings_qs = AppSetting.objects.all().order_by('group', 'key')
    grouped = defaultdict(list)
    for s in settings_qs:
        grouped[s.group].append(s)
    group_labels = dict(AppSetting.GROUPS)
    groups = [
        {'key': gkey, 'label': group_labels.get(gkey, gkey.title()), 'settings': gsettings}
        for gkey, gsettings in sorted(grouped.items())
    ]
    return render(request, 'admin/settings/index.html', {
        'page': 'settings',
        'groups': groups,
        'total': settings_qs.count(),
    })


@admin_required
@handle_admin_errors
@require_POST
def admin_settings_update(request):
    key = request.POST.get('key', '').strip()
    value = request.POST.get('value', '').strip()

    if not key:
        return json_error('Key is required.')

    try:
        setting = AppSetting.objects.get(key=key)
    except AppSetting.DoesNotExist:
        return json_error(f'Setting "{key}" not found.')

    old_value = setting.value
    setting.value = value
    setting.updated_by = request._admin_user
    setting.save(update_fields=['value', 'updated_by', 'updated_at'])

    audit(request, 'settings.update', 'settings',
          target_type='setting', target_id=setting.pk, target_repr=key,
          old_value={'value': old_value}, new_value={'value': value})
    return json_ok(message='Setting updated.')
