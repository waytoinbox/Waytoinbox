from django.urls import path
from Email_validate_app.views import (
    so_email_accounts, so_add_email_account, so_email_account_action, so_edit_email_account,
)

urlpatterns = [
    path('Sales-Outreach/so-accounts/',             so_email_accounts,       name='so_email_accounts'),
    path('Sales-Outreach/so-accounts/add/',         so_add_email_account,    name='so_add_email_account'),
    path('Sales-Outreach/so-accounts/action/',      so_email_account_action, name='so_email_account_action'),
    path('Sales-Outreach/so-accounts/<int:id>/edit/', so_edit_email_account, name='so_edit_email_account'),
]
