import logging
from django.utils import timezone
from django.core.paginator import Paginator
from Email_validate_app.models import Coupon

logger = logging.getLogger('Email_validate_app.services')


def get_coupon_list(request_get):
    qs = Coupon.objects.select_related('created_by').order_by('-created_at')

    q = request_get.get('q', '').strip()
    if q:
        qs = qs.filter(code__icontains=q)

    status = request_get.get('status', '')
    now = timezone.now()
    if status == 'active':
        qs = qs.filter(is_active=True)
    elif status == 'inactive':
        qs = qs.filter(is_active=False)
    elif status == 'expired':
        qs = qs.filter(valid_until__lt=now)

    dtype = request_get.get('dtype', '')
    if dtype:
        qs = qs.filter(discount_type=dtype)

    page_num = request_get.get('page', 1)
    page_obj = Paginator(qs, 25).get_page(page_num)
    params = {'q': q, 'status': status, 'dtype': dtype}
    return page_obj, params, qs.count()


def create_coupon(data, created_by):
    coupon = Coupon(
        code=data['code'].strip().upper(),
        discount_type=data['discount_type'],
        discount_value=data['discount_value'],
        max_uses=data.get('max_uses') or None,
        valid_from=data['valid_from'],
        valid_until=data.get('valid_until') or None,
        is_active=data.get('is_active', True),
        description=data.get('description', '').strip(),
        created_by=created_by,
        per_user_limit=data.get('per_user_limit') or None,
        min_order_amount=data.get('min_order_amount') or 0,
        applicable_services=data.get('applicable_services', ''),
    )
    coupon.full_clean()
    coupon.save()
    return coupon


def edit_coupon(coid, data):
    coupon = Coupon.objects.get(pk=coid)
    coupon.code = data['code'].strip().upper()
    coupon.discount_type = data['discount_type']
    coupon.discount_value = data['discount_value']
    coupon.max_uses = data.get('max_uses') or None
    coupon.valid_from = data['valid_from']
    coupon.valid_until = data.get('valid_until') or None
    coupon.is_active = data.get('is_active', True)
    coupon.description = data.get('description', '').strip()
    # Absent keys leave the stored value alone, so a form that does not post
    # these fields cannot silently reset an existing coupon's limits.
    if 'per_user_limit' in data:
        coupon.per_user_limit = data.get('per_user_limit') or None
    if 'min_order_amount' in data:
        coupon.min_order_amount = data.get('min_order_amount') or 0
    if 'applicable_services' in data:
        coupon.applicable_services = data.get('applicable_services', '')
    coupon.full_clean()
    coupon.save()
    return coupon


def toggle_coupon(coid):
    coupon = Coupon.objects.get(pk=coid)
    coupon.is_active = not coupon.is_active
    coupon.save(update_fields=['is_active'])
    return coupon


def delete_coupon(coid):
    coupon = Coupon.objects.get(pk=coid)
    code = coupon.code
    coupon.delete()
    return code
