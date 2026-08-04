from django.urls import path
from Email_validate_app import views

urlpatterns = [
    path("Email_Campaigns/list-segment/", views.list_segment, name="list_segment"),
    path("Email_Campaigns/list-segment/delete/", views.delete_campaign_list, name="delete_campaign_list"),
    path("Email_Campaigns/list/<int:list_id>/check/", views.campaign_list_check, name="campaign_list_check"),
    path("Email_Campaigns/list/<int:list_id>/contacts/", views.campaign_list_contacts, name="campaign_list_contacts"),
    path("Email_Campaigns/list/<int:list_id>/add-contact/", views.add_campaign_contact, name="add_campaign_contact"),
    path("Email_Campaigns/list/<int:list_id>/upload-contacts/", views.upload_campaign_contacts, name="upload_campaign_contacts"),
    path("Email_Campaigns/list/<int:list_id>/contacts/page/", views.campaign_contacts_page, name="campaign_contacts_page"),
    path("Email_Campaigns/list/<int:list_id>/parse-file/", views.parse_upload_file, name="parse_upload_file"),
    path("Email_Campaigns/list/<int:list_id>/import-contacts/", views.import_contacts, name="import_contacts"),
    path("Email_Campaigns/contacts/", views.all_contacts, name="all_contacts"),
    path("Email_Campaigns/contacts/page/", views.all_contacts_page, name="all_contacts_page"),
    path("Email_Campaigns/list/<int:list_id>/rename/",        views.list_rename,         name="list_rename"),
    path("Email_Campaigns/list/<int:list_id>/duplicate/",     views.list_duplicate,      name="list_duplicate"),
    path("Email_Campaigns/list/<int:list_id>/download/",      views.list_download,       name="list_download"),
    path("Email_Campaigns/list/<int:list_id>/toggle-status/", views.list_toggle_status,  name="list_toggle_status"),
]
