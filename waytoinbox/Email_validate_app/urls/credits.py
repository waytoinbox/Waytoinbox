from django.urls import path
from Email_validate_app import views

urlpatterns = [
    path("subscription/quote/",  views.subscription_quote,  name="subscription_quote"),
    path("subscription/order/",  views.subscription_order,  name="subscription_order"),
    path("subscription/verify/", views.subscription_verify, name="subscription_verify"),
]
