from django.urls import path
from Email_validate_app import views

urlpatterns = [
    path("Email_Campaigns/campaigns/", views.campaigns, name="campaigns"),
    path("Email_Campaigns/campaigns/<int:campaign_id>/", views.campaign_detail, name="campaign_detail"),
    path("Email_Campaigns/campaigns/<int:campaign_id>/stats/", views.campaign_stats_json, name="campaign_stats_json"),
    path("Email_Campaigns/campaign/save/", views.save_campaign, name="save_campaign"),
    path("Email_Campaigns/campaign/test-send/", views.send_test_email_create, name="send_test_email_create"),
    path("Email_Campaigns/campaign/<int:campaign_id>/test-send/", views.send_test_email, name="send_test_email"),
    path("unsubscribe/<str:token>/", views.campaign_unsubscribe, name="campaign_unsubscribe"),
    path("Email_Campaigns/create/", views.create_campaign, name="create_campaign"),
]
