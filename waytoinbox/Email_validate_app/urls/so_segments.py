from django.urls import path
from Email_validate_app.views import so_segments as so_seg_views

urlpatterns = [
    path("Sales-Outreach/segments/",                         so_seg_views.so_segments_list,          name="so_segments"),
    path("Sales-Outreach/segments/api/",                     so_seg_views.so_segment_api,            name="so_segment_api"),
    path("Sales-Outreach/segments/preview/",                 so_seg_views.so_segment_preview,        name="so_segment_preview"),
    path("Sales-Outreach/segments/builder/",                 so_seg_views.so_segment_builder,        name="so_segment_builder"),
    path("Sales-Outreach/segments/<int:seg_id>/",            so_seg_views.so_segment_detail_api,     name="so_segment_detail_api"),
    path("Sales-Outreach/segments/<int:seg_id>/edit/",       so_seg_views.so_segment_builder_edit,   name="so_segment_builder_edit"),
    path("Sales-Outreach/segments/<int:seg_id>/duplicate/",  so_seg_views.so_segment_duplicate,      name="so_segment_duplicate"),
    path("Sales-Outreach/segments/<int:seg_id>/prospects/",  so_seg_views.so_segment_prospects,      name="so_segment_prospects"),
    path("Sales-Outreach/segments/<int:seg_id>/prospects/page/", so_seg_views.so_segment_prospects_page, name="so_segment_prospects_page"),
    path("Sales-Outreach/segments/<int:seg_id>/download/",   so_seg_views.so_segment_download,       name="so_segment_download"),
]
