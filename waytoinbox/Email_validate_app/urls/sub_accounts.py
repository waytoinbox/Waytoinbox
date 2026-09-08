from django.urls import path
from Email_validate_app import views

urlpatterns = [
    path("sub-accounts/", views.sub_accounts_overview, name="sub_accounts_overview"),
    path("sub-accounts/create/", views.create_sub_account, name="create_sub_account"),
    path("account/switch/", views.switch_account, name="switch_account"),
    path("account/return/", views.return_to_main, name="return_to_main"),
]
