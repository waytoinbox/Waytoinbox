from django.contrib import admin
from .models import TemplateLibrary


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
