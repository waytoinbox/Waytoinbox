from django.urls import path
from Email_validate_app.views import (
    so_prospects, so_prospects_action, so_prospects_import, so_prospects_parse_file,
)

urlpatterns = [
    path('Sales-Outreach/prospects/',             so_prospects,             name='so_prospects'),
    path('Sales-Outreach/prospects/action/',      so_prospects_action,      name='so_prospects_action'),
    path('Sales-Outreach/prospects/parse-file/',  so_prospects_parse_file,  name='so_prospects_parse_file'),
    path('Sales-Outreach/prospects/import/',      so_prospects_import,      name='so_prospects_import'),
]
