import json
from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def safe_json(value):
    """Serialize value to JSON and mark it safe for embedding in <script> tags."""
    return mark_safe(json.dumps(value, default=str))
