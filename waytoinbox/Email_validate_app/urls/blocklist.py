from django.urls import path
from Email_validate_app import views

urlpatterns = [
    path("Blocklist_Monitor/", views.Blocklist_Monitor, name="Blocklist_Monitor"),
    path("check_ip_blacklists/", views.check_ip_blacklists, name="check_ip_blacklists"),
    path("Domain_Blacklist/", views.Domain_Blacklist, name="Domain_Blacklist"),
    path("check_domain_blocklist/", views.check_domain_blocklist, name="check_domain_blocklist"),
    path("get-blocklist-data/", views.get_blocklist_data, name="get_blocklist_data"),
    path("blocklist_names/", views.blocklist_names, name="blocklist_names"),
    path("get-domain-blocklist-data/", views.get_domain_blocklist_data, name="get_domain_blocklist_data"),
    path("domain_blocklist_names/", views.domain_blocklist_names, name="domain_blocklist_names"),
    path("blocklist/hide_row/", views.hide_blocklist_row, name="hide_blocklist_row"),
]
