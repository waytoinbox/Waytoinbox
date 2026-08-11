import os
from celery import Celery
from celery.signals import task_prerun, task_postrun

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "Innovicloud.settings")

app = Celery("Innovicloud")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@task_prerun.connect
def _db_close_old_before_task(sender=None, **kwargs):
    from django.db import close_old_connections
    close_old_connections()


@task_postrun.connect
def _db_close_old_after_task(sender=None, **kwargs):
    from django.db import close_old_connections
    close_old_connections()
