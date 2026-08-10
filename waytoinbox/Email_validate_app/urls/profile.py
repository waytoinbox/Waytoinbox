from django.urls import path
from Email_validate_app import views

urlpatterns = [
    path("profile/", views.profile, name="profile"),
    path("profile/activity/", views.profile_activity_json, name="profile_activity_json"),
    path("profile/update/", views.profile_update_ajax, name="profile_update_ajax"),
    path("profile/change-password/", views.change_password_ajax, name="change_password_ajax"),
    path("profile/delete-request/", views.delete_account_request, name="delete_account_request"),
    path("profile/notifications/", views.notification_update_ajax, name="notification_update_ajax"),
    path("profile/notifications-list/", views.notifications_json, name="notifications_json"),
    path("profile/notifications-count/", views.notifications_count_json, name="notifications_count_json"),
]
