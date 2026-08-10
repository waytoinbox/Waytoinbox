from celery import shared_task
from django.core import management


@shared_task(name='Email_validate_app.tasks.clearsessions.clearsessions_task')
def clearsessions_task():
    """INF-13: purge expired rows from django_session to prevent unbounded table growth."""
    management.call_command('clearsessions')
