from django.urls import path
from Email_validate_app import views

urlpatterns = [
    path("Email_Campaigns/sender-verify/", views.sender_verify, name="sender_verify"),
    path("Email_Campaigns/sender-verify/action/", views.sender_verify_action, name="sender_verify_action"),
    path("Email_Campaigns/sender-verify/confirm/<str:token>/", views.sender_verify_confirm, name="sender_verify_confirm"),
    path("Email_Campaigns/sender-verify/dns/", views.sender_verify_dns_page, name="sender_verify_dns"),
]
