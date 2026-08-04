from django.urls import path
from Email_validate_app.views import segments as seg_views

urlpatterns = [
    path("Email_Campaigns/segments/",                        seg_views.segments_list,       name="segments"),
    path("Email_Campaigns/segments/api/",                    seg_views.segment_api,          name="segment_api"),
    path("Email_Campaigns/segments/preview/",                seg_views.segment_preview,      name="segment_preview"),
    path("Email_Campaigns/segments/builder/",                seg_views.segment_builder,      name="segment_builder"),
    path("Email_Campaigns/segments/<int:seg_id>/",           seg_views.segment_detail_api,   name="segment_detail_api"),
    path("Email_Campaigns/segments/<int:seg_id>/edit/",      seg_views.segment_builder_edit, name="segment_builder_edit"),
    path("Email_Campaigns/segments/<int:seg_id>/duplicate/",        seg_views.segment_duplicate,     name="segment_duplicate"),
    path("Email_Campaigns/segments/<int:seg_id>/contacts/",         seg_views.segment_contacts,      name="segment_contacts"),
    path("Email_Campaigns/segments/<int:seg_id>/contacts/page/",    seg_views.segment_contacts_page, name="segment_contacts_page"),
    path("Email_Campaigns/segments/<int:seg_id>/download/",         seg_views.segment_download,      name="segment_download"),
]
