from django.urls import path
from Email_validate_app.views.api import (
    validate_email_view,
    domain_validate_api,
    ip_blocklist_check_api,
    domain_blocklist_check_api,
    header_analysis_api,
    api_bulk_validate,
    api_validate_status,
    api_validate_results,
)
from Email_validate_app import views

urlpatterns = [
    path("api/validate/email/", validate_email_view, name="validate_email"),
    path("api/domain-validate/", domain_validate_api, name="domain_validate_api"),
    path("api/validate/bulk/", api_bulk_validate, name="api_bulk_validate"),
    path("api/validate/status/<job_id>/", api_validate_status, name="api_validate_status"),
    path("api/validate/results/<job_id>/", api_validate_results, name="api_validate_results"),
    path("api/blocklist/ip/", ip_blocklist_check_api, name="ip_blocklist_check_api"),
    path("api/blocklist/domain/", domain_blocklist_check_api, name="domain_blocklist_check_api"),
    path("api/header-analysis/", header_analysis_api, name="header_analysis_api"),
    path("api/add-to-monitors/", views.add_to_monitors, name="add_to_monitors"),
]
