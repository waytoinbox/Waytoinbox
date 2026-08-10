import logging
from django.shortcuts import render, get_object_or_404
from django.views.decorators.http import require_POST
from django.utils.dateparse import parse_datetime

from Email_validate_app.models import Coupon
from Email_validate_app.views.admin._base import (
    admin_required, handle_admin_errors, audit, json_ok, json_error,
)
from Email_validate_app.services.admin import coupon_service

logger = logging.getLogger('Email_validate_app.views')


@admin_required
@handle_admin_errors
def admin_coupons(request):
    page_obj, params, total = coupon_service.get_coupon_list(request.GET)
    return render(request, 'admin/coupons/list.html', {
        'page': 'coupons',
        'page_obj': page_obj,
        'total': total,
        **params,
    })


@admin_required
@handle_admin_errors
def admin_coupon_create(request):
    if request.method == 'POST':
        try:
            data = _parse_form(request.POST)
            coupon = coupon_service.create_coupon(data, request._admin_user)
            audit(request, 'coupon.create', 'coupons',
                  target_type='coupon', target_id=coupon.pk, target_repr=coupon.code)
            return json_ok(data={'redirect': '/wti-admin/coupons/'}, message='Coupon created.')
        except Exception as exc:
            return json_error(str(exc))
    return render(request, 'admin/coupons/form.html', {
        'page': 'coupons',
        'coupon': None,
        'form_action': '/wti-admin/coupons/create/',
        'title': 'Create Coupon',
    })


@admin_required
@handle_admin_errors
def admin_coupon_edit(request, coid):
    coupon = get_object_or_404(Coupon, pk=coid)
    if request.method == 'POST':
        try:
            data = _parse_form(request.POST)
            old_code = coupon.code
            coupon = coupon_service.edit_coupon(coid, data)
            audit(request, 'coupon.edit', 'coupons',
                  target_type='coupon', target_id=coid, target_repr=old_code,
                  old_value={'code': old_code},
                  new_value={'code': coupon.code})
            return json_ok(data={'redirect': '/wti-admin/coupons/'}, message='Coupon updated.')
        except Exception as exc:
            return json_error(str(exc))
    return render(request, 'admin/coupons/form.html', {
        'page': 'coupons',
        'coupon': coupon,
        'form_action': f'/wti-admin/coupons/{coid}/edit/',
        'title': 'Edit Coupon',
    })


@admin_required
@handle_admin_errors
@require_POST
def admin_coupon_toggle(request, coid):
    coupon = coupon_service.toggle_coupon(coid)
    audit(request, 'coupon.toggle', 'coupons',
          target_type='coupon', target_id=coid, target_repr=coupon.code,
          new_value={'is_active': coupon.is_active})
    label = 'Active' if coupon.is_active else 'Inactive'
    badge_cls = 'badge-success' if coupon.is_active else 'badge-grey'
    badge_html = f'<span class="badge {badge_cls}"><span class="badge-dot"></span>{label}</span>'
    return json_ok(data={'badge_html': badge_html, 'is_active': coupon.is_active})


@admin_required
@handle_admin_errors
@require_POST
def admin_coupon_delete(request, coid):
    code = coupon_service.delete_coupon(coid)
    audit(request, 'coupon.delete', 'coupons',
          target_type='coupon', target_id=coid, target_repr=code)
    return json_ok(data={'redirect': '/wti-admin/coupons/'}, message=f'Coupon {code} deleted.')


def _parse_form(post):
    raw_from = post.get('valid_from', '').strip()
    raw_until = post.get('valid_until', '').strip()
    max_uses_raw = post.get('max_uses', '').strip()

    valid_from = parse_datetime(raw_from)
    if not valid_from:
        raise ValueError('Invalid valid_from date.')

    valid_until = parse_datetime(raw_until) if raw_until else None

    try:
        discount_value = float(post.get('discount_value', '0'))
    except ValueError:
        raise ValueError('Invalid discount value.')

    return {
        'code': post.get('code', ''),
        'discount_type': post.get('discount_type', ''),
        'discount_value': discount_value,
        'max_uses': int(max_uses_raw) if max_uses_raw else None,
        'valid_from': valid_from,
        'valid_until': valid_until,
        'is_active': post.get('is_active') == '1',
        'description': post.get('description', ''),
    }
