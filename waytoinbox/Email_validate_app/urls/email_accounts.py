from django.urls import path
from Email_validate_app import views

urlpatterns = [
    path('Sales-Outreach/email-accounts/',        views.email_accounts,        name='email_accounts'),
    path('Sales-Outreach/email-accounts/add/',    views.add_email_account,     name='add_email_account'),
    path('Sales-Outreach/email-accounts/action/', views.email_accounts_action, name='email_accounts_action'),
]
