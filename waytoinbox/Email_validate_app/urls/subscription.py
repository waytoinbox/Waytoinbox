from django.urls import path
from Email_validate_app import views

urlpatterns = [
    path("subscription/", views.subscription, name="subscription"),
    path("subscription-success/", views.subscription_success, name="subscription_success"),
    path("subscription_cancel/", views.subscription_cancel, name="subscription_cancel"),
    path("create_subscription/", views.create_subscription, name="create_subscription"),
    path("subs_payment/", views.subs_payment, name="subs_payment"),
]
