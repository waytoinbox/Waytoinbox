from django.urls import path
from Email_validate_app import views

urlpatterns = [
    path("services/upload/", views.service_validate_emails, name="upload"),
    path("verify_emails/", views.verify_emails, name="verify_emails"),
    path("Analyze/", views.Analyze, name="analyze"),
    path("services/download_results/", views.download_results, name="download_results"),
    path("services/delete_query/", views.delete_query, name="delete_query"),
    path("services/single_service/", views.single_service, name="single_service"),
    path("services/hide_email_history/", views.hide_email_history, name="hide_email_history"),
    path("run_email_validation/", views.run_email_validation, name="run_email_validation"),
]
