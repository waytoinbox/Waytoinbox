from Email_validate_app.tasks.update_reputations import update_all_reputations        # noqa: F401
from Email_validate_app.tasks.send_scheduled_campaigns import send_scheduled_campaigns  # noqa: F401
from Email_validate_app.tasks.send_scheduled_campaigns import send_campaign_emails_task # noqa: F401
from Email_validate_app.tasks.sync_campaigns_cloudwatch import sync_pending_campaigns  # noqa: F401
from Email_validate_app.tasks.scheduler_job import (                                   # noqa: F401
    scheduler_job,
    my_second_job,
    subscription_expiry_job,
    bl_notification_job,
)

# Sales Outreach — Celery autodiscovery only imports this package's __init__, so
# any task module missing from here is never registered and its queued messages
# are rejected as "unregistered task".
from Email_validate_app.tasks.so_send_campaign import (                                # noqa: F401
    so_send_campaign_task,
    so_dispatch_scheduled_campaigns,
    so_dispatch_due_sequence_steps,
    so_recover_stuck_campaigns,
)
from Email_validate_app.tasks.so_inbox_sync import so_sync_all_inboxes, so_sync_one_inbox  # noqa: F401
from Email_validate_app.tasks.so_subsequence import so_dispatch_subsequence_branches   # noqa: F401

# Warmup — same "must be imported here or autodiscovery never finds it" rule.
from Email_validate_app.tasks.warmup import (                                          # noqa: F401
    warmup_dispatch_sends,
    warmup_send_one,
    warmup_dispatch_checks,
    warmup_check_one,
    warmup_recover_stuck,
)

# Credit/entitlement expiry sweeps (Phase 4) and session cleanup — same
# "must be imported here or autodiscovery never finds it" rule. These are
# CELERY_BEAT_SCHEDULE entries with no other importer anywhere in the
# codebase, so without this import beat still dispatches them on schedule
# but the worker rejects every occurrence as an unregistered task.
from Email_validate_app.tasks.credit_expiry import (                                   # noqa: F401
    expire_credit_lots,
    expire_trial_entitlements,
)
from Email_validate_app.tasks.clearsessions import clearsessions_task                  # noqa: F401
