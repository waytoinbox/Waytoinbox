from django.contrib import admin
from .models import TemplateLibrary, GuestActivity


@admin.register(TemplateLibrary)
class TemplateLibraryAdmin(admin.ModelAdmin):
    list_display    = ('name', 'category', 'is_active', 'has_design_json', 'created_at')
    list_filter     = ('category', 'is_active')
    search_fields   = ('name',)
    readonly_fields = ('created_at', 'updated_at')
    fields = (
        'name', 'category', 'subject', 'is_active',
        'html_content', 'design_json',
        'thumbnail', 'created_at', 'updated_at',
    )

    @admin.display(boolean=True, description='Has design JSON')
    def has_design_json(self, obj):
        return obj.design_json is not None


@admin.register(GuestActivity)
class GuestActivityAdmin(admin.ModelAdmin):
    list_display   = ('created_at', 'activity_type', 'ip_address', 'input_value', 'result')
    list_filter    = ('activity_type', 'result')
    search_fields  = ('ip_address', 'input_value')
    readonly_fields = ('ip_address', 'activity_type', 'input_value', 'result', 'created_at')
    ordering       = ('-created_at',)
    date_hierarchy = 'created_at'
