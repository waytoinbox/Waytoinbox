from django.urls import path
from Email_validate_app import views

urlpatterns = [
    path("dmarc_check/", views.DMARC_check, name="dmarc_check"),
    path("Header_Analysis/", views.Header_Analysis, name="Header_Analysis"),
]
