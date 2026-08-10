import logging

from celery import Task

from Email_validate_app.services.mailer import send_job_failure_alert

logger = logging.getLogger('Email_validate_app.tasks')


class LoggedTask(Task):
    abstract = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        logger.error(
            'Task FAILED | task=%s | id=%s | exc=%s',
            self.name,
            task_id,
            exc,
            exc_info=True,
        )
        try:
            send_job_failure_alert(self.name, exc)
        except Exception:
            logger.warning('send_job_failure_alert itself failed for task=%s', self.name)
        super().on_failure(exc, task_id, args, kwargs, einfo)

    def on_retry(self, exc, task_id, args, kwargs, einfo):
        logger.warning(
            'Task RETRY | task=%s | id=%s | exc=%s',
            self.name,
            task_id,
            exc,
        )
        super().on_retry(exc, task_id, args, kwargs, einfo)
