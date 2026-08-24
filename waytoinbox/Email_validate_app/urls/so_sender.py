from django.urls import path
from Email_validate_app.views import so_sender as so_views

urlpatterns = [
    path('Sales-Outreach/sender/',                        so_views.so_campaigns,          name='so_campaigns'),
    path('Sales-Outreach/sender/create/',                 so_views.so_campaign_create,    name='so_campaign_create'),
    path('Sales-Outreach/sender/save/',                   so_views.so_campaign_save,      name='so_campaign_save'),
    path('Sales-Outreach/sender/sequence-autosave/',      so_views.so_sequence_autosave,  name='so_sequence_autosave'),
    path('Sales-Outreach/sender/estimate-recipients/',    so_views.so_estimate_recipients, name='so_estimate_recipients'),
    path('Sales-Outreach/sender/content-score/',          so_views.so_content_score,      name='so_content_score'),
    path('Sales-Outreach/sender/test-send/',              so_views.so_test_send,          name='so_test_send'),
    path('Sales-Outreach/sender/<int:cid>/',              so_views.so_campaign_detail,    name='so_campaign_detail'),
    path('Sales-Outreach/sender/<int:cid>/edit/',         so_views.so_campaign_edit,      name='so_campaign_edit'),
    path('Sales-Outreach/sender/<int:cid>/action/',       so_views.so_campaign_action,    name='so_campaign_action'),
]
