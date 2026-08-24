from django.urls import path
from Email_validate_app.views import (
    warmup_dashboard,
    warmup_sender_action,
)

# One combined Warmup page — no separate Senders/Receivers pages. Sender
# enrollment/control lives on the existing Email Accounts page instead
# (views/so_email_accounts.py), which calls warmup_sender_action directly.
# Receivers are a fixed, admin-managed shared pool — see
# views/admin/warmup.py and urls/admin.py (wti-admin/warmup-receivers/...)
# instead of an end-user-facing route.
urlpatterns = [
    path('Warmup/',                 warmup_dashboard,     name='warmup_dashboard'),
    path('Warmup/senders/action/',  warmup_sender_action, name='warmup_sender_action'),
]
