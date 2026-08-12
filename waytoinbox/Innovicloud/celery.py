import os
from celery import Celery
from celery.signals import task_prerun, task_postrun, worker_process_init

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "Innovicloud.settings")

app = Celery("Innovicloud")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@worker_process_init.connect
def _disable_db_conn_pooling(sender=None, **kwargs):
    # gevent workers share DB connections across greenlets, which causes
    # "MySQL server has gone away" errors on long-running tasks.
    # Disabling pooling (CONN_MAX_AGE=0) gives each task a fresh connection.
    from django.conf import settings
    for db in settings.DATABASES.values():
        db['CONN_MAX_AGE'] = 0


@task_prerun.connect
def _db_close_old_before_task(sender=None, **kwargs):
    from django.db import close_old_connections
    close_old_connections()


@task_postrun.connect
def _db_close_old_after_task(sender=None, **kwargs):
    from django.db import close_old_connections
    close_old_connections()
