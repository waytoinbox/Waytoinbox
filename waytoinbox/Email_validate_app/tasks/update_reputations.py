"""
Celery task: update_all_reputations
Runs every 24 hours (2 AM UTC via Beat schedule).

For every Reputation row with status='verified':
  - Call fetch_domain_traffic_stats(domain)
  - For each returned stat, create a ReputationResults row
    only if (reputation_id, domain, date) does not already exist.
  - Never update/overwrite existing dated rows.
  - If the API returns no data, skip that domain silently.
"""

import logging
from datetime import datetime

from celery import shared_task

from Email_validate_app.models import Reputation, ReputationResults
from Email_validate_app.services.mailer import send_job_failure_alert
from Email_validate_app.tasks.base import LoggedTask

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name="Email_validate_app.tasks.update_reputations.update_all_reputations",
    max_retries=2,
    default_retry_delay=300,
    base=LoggedTask,
)
def update_all_reputations(self):
    """
    Refresh ReputationResults for all verified domains.
    Returns a summary dict: {domains_checked, domains_updated, rows_created, errors}.
    """
    # Import here so a missing google package doesn't break the whole app on startup
    from Email_validate_app.services.postmaster import fetch_domain_traffic_stats
    
    verified_reps = (
        Reputation.objects
        .filter(status="verified", is_hidden=False)
        .select_related("user")
    )

    total      = verified_reps.count()
    updated    = 0
    created    = 0
    errors     = 0

    logger.info("update_all_reputations: starting — %d verified domains", total)

    for rep in verified_reps:
        try:
            stats = fetch_domain_traffic_stats(rep.domain)
        except Exception as exc:
            logger.error(
                "update_all_reputations: API error for domain '%s' (rep_id=%s): %s",
                rep.domain, rep.id, exc,
            )
            errors += 1
            continue

        if not stats:
            logger.debug(
                "update_all_reputations: no data for '%s' (rep_id=%s) — skipping",
                rep.domain, rep.id,
            )
            continue

        domain_rows_created = 0

        for item in stats:
            # ── Parse date ──────────────────────────────────────────────
            try:
                date_obj = datetime.strptime(item["date"], "%Y%m%d").date()
            except (ValueError, KeyError):
                logger.warning(
                    "update_all_reputations: bad date '%s' for domain '%s' — skipping row",
                    item.get("date"), rep.domain,
                )
                continue

            _, was_created = ReputationResults.objects.update_or_create(
                domain=rep.domain,
                date=date_obj,
                defaults={
                    "spam_rate":         item.get("spam_rate"),
                    "domain_reputation": item.get("domain_reputation"),
                    "ip_reputation":     item.get("ip_reputation", []),
                    "delivery_errors":   item.get("delivery_errors", []),
                },
            )
            domain_rows_created += 1

        if domain_rows_created:
            updated += 1
            created += domain_rows_created
            logger.info(
                "update_all_reputations: '%s' (rep_id=%s) — %d row(s) upserted",
                rep.domain, rep.id, domain_rows_created,
            )

    summary = {
        "domains_checked": total,
        "domains_updated": updated,
        "rows_created":    created,
        "errors":          errors,
    }
    logger.info("update_all_reputations: done — %s", summary)

    if errors:
        send_job_failure_alert(
            "Domain Reputation Update (update_all_reputations)",
            Exception(f"{errors} domain(s) failed during reputation sync"),
            context={"Domains Checked": total, "Errors": errors, "Updated": updated},
        )

    return summary
