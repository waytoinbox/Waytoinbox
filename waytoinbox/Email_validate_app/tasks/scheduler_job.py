
import logging
from celery import shared_task
from Email_validate_app.tasks.base import LoggedTask
from django.db import IntegrityError, DatabaseError
from Email_validate_app.models import BlocklistMonitor, BlacklistStatus, BlacklistListed,DomainBlocklist,DomainBlacklistStatus,DomainBlacklistListed,UserTable,SubsPayment
from Email_validate_app.models import ServiceTrial, TrialUsageLog
from Email_validate_app.services.trial_manager import SERVICE_LABELS as TRIAL_SERVICE_LABELS
from Email_validate_app.services.monitor import get_blacklist_notifications, send_mail_notification
from Email_validate_app.services.monitor import ip_blacklists, domain_blacklists
from Email_validate_app.services.mailer import send_subscription_expiry_email, send_job_failure_alert, send_trial_expired_email
from Email_validate_app.services.credit_manager import expire_subscription_credits
from datetime import datetime
from django.utils.timezone import make_aware, now


logger = logging.getLogger(__name__)

@shared_task(name="Email_validate_app.tasks.scheduler_job.scheduler_job", base=LoggedTask)
def scheduler_job():
    logger.info("Scheduler job1 triggered")
    try:
        data = list(BlocklistMonitor.objects.all())  # Force evaluation

        for entry in data:
            ip = entry.ips
            ip_id = entry.ip_id

            try:
                status = ip_blacklists(ip)
                # print(f"Blacklist status for IP {ip}: {status}")  # Debugging line
                if not isinstance(status, dict):
                    logger.warning(f"Expected dict from check_blacklists, got {type(status)}. IP: {ip}")
                    status = {}
            except Exception as e:
                logger.error(f"Error checking blacklists for IP {ip}: {e}")
                status = {}

            for blacklist_name, blacklist_status in status.items():
                try:
                    BlacklistStatus.objects.create(
                        ip_id=ip_id,
                        ips=ip,
                        blacklists_name=blacklist_name,
                        status=blacklist_status
                    )
                    if blacklist_status == 'Listed':
                        BlacklistListed.objects.create(
                            ip_id=ip_id,
                            ips=ip,
                            blacklists_name=blacklist_name,
                            status=blacklist_status
                        )
                except IntegrityError as e:
                    logger.error(f"IntegrityError while inserting status for IP {ip}: {e}")
                except DatabaseError as e:
                    logger.error(f"DatabaseError while inserting status for IP {ip}: {e}")
                except Exception as e:
                    logger.error(f"Unexpected error while inserting status for IP {ip}: {e}")

                try:
                    data1 = list(BlocklistMonitor.objects.all()) 
                    
                    # Define today's start and end as timezone-aware datetimes
                    today = now().date()
                    today_start = make_aware(datetime.combine(today, datetime.min.time()))
                    today_end = make_aware(datetime.combine(today, datetime.max.time()))

                    for i in data1:
                        try:
                            listed_entries = BlacklistListed.objects.filter(
                                ip_id=i.ip_id,
                                created_date__range=(today_start, today_end),
                                status='Listed'
                            ).values_list('created_date', flat=True)

                            listed_count = len(listed_entries)
                            created_dates = list(listed_entries)
                            logger.debug("listed_entries: %s", listed_entries)
                            
                            # If no listed entries found, use today's date as default
                            if not listed_count:
                                listed_count = 0
                                created_dates = [now()]  # Set to current time

                            i.listed_count = listed_count
                            i.created_date = created_dates

                            logger.debug("IP %s - listed: %d - dates: %s", i.ip_id, listed_count, created_dates)

                            # Update DB using efficient .update()
                            BlocklistMonitor.objects.filter(ip_id=i.ip_id).update(
                                last_monitor_date=max(created_dates) if created_dates else None,
                                listed_count=listed_count
                            )

                        except Exception as inner_e:
                            logger.error("Inner loop error for ip_id %s: %s", i.ip_id, inner_e)

                except Exception as e:
                    logger.error("Error fetching blacklist data: %s", e)
    
                

        logger.info("Scheduler job1 executed successfully.")

    except Exception as e:
        logger.error(f"An error occurred in the scheduler job: {e}")
        send_job_failure_alert("IP Blacklist Check (scheduler_job)", e)




@shared_task(name="Email_validate_app.tasks.scheduler_job.my_second_job", base=LoggedTask)
def my_second_job():
    logger.info("Scheduler job2 triggered")
    try:
        data = list(DomainBlocklist.objects.all())  # Force evaluation
        # print(f"Domain blacklist data: {data}")

        for entry in data:
            domain = entry.domain
            domain_id = entry.domain_id

            try:
                status = domain_blacklists(domain)  # Assuming this function can also check domains
                # print(f"Blacklist status for domain {domain}: {status}")  # Debugging line
                if not isinstance(status, dict):
                    logger.warning(f"Expected dict from check_blacklists, got {type(status)}. Domain: {domain}")
                    status = {}
            except Exception as e:
                logger.error(f"Error checking blacklists for domain {domain}: {e}")
                status = {}

            for blacklist_name, blacklist_status in status.items():
                try:
                    DomainBlacklistStatus.objects.create(
                        domain=domain_id,
                        domains=domain,
                        blacklists_name=blacklist_name,
                        status=blacklist_status
                    )
                    if blacklist_status == 'Listed':
                        DomainBlacklistListed.objects.create(
                            domain=domain_id,
                            domains=domain,
                            blacklists_name=blacklist_name,
                            status=blacklist_status
                        )
                except IntegrityError as e:
                    logger.error(f"IntegrityError while inserting status for domain {domain}: {e}")
                except DatabaseError as e:
                    logger.error(f"DatabaseError while inserting status for domain {domain}: {e}")
                except Exception as e:
                    logger.error(f"Unexpected error while inserting status for domain {domain}: {e}")

                try:
                    data1 = list(DomainBlocklist.objects.all()) 
                    
                    # Define today's start and end as timezone-aware datetimes
                    today = now().date()
                    today_start = make_aware(datetime.combine(today, datetime.min.time()))
                    today_end = make_aware(datetime.combine(today, datetime.max.time()))

                    for i in data1:
                        try:
                            listed_entries = DomainBlacklistListed.objects.filter(
                                domain=i.domain_id,
                                created_date__range=(today_start, today_end),
                                status='Listed'
                            ).values_list('created_date', flat=True)

                            listed_count = len(listed_entries)
                            created_dates = list(listed_entries)
                            
                            # If no listed entries found, use today's date as default
                            if not listed_count:
                                listed_count = 0
                                created_dates = [now()]  # Set to current time

                            i.listed_count = listed_count
                            i.created_date = created_dates

                            logger.debug("Domain %s - listed: %d - dates: %s", i.domain_id, listed_count, created_dates)

                            # Update DB using efficient .update()
                            DomainBlocklist.objects.filter(domain_id=i.domain_id).update(
                                last_monitor_date=max(created_dates) if created_dates else None,
                                listed_count=listed_count
                            )

                        except Exception as inner_e:
                            logger.error("Inner loop error for domain_id %s: %s", i.domain_id, inner_e)

                except Exception as e:
                    logger.error("Error fetching blacklist data: %s", e)
    
                        

        logger.info("Scheduler job2 executed successfully.")

    except Exception as e:
        logger.error(f"An error occurred in the scheduler job: {e}")
        send_job_failure_alert("Domain Blacklist Check (my_second_job)", e)







@shared_task(name="Email_validate_app.tasks.scheduler_job.subscription_expiry_job", base=LoggedTask)
def subscription_expiry_job():
    logger.info("Subscription expiry check triggered")
    try:
        expired = SubsPayment.objects.filter(
            plan_status="Active",
            valid_time__lt=now()
        ).select_related('user')

        for sub in expired:
            SubsPayment.objects.filter(pk=sub.pk).update(plan_status="Inactive")
            try:
                # Does NOT clear balances - credits never expire. The plan
                # status flip above is what actually ends the subscription.
                expire_subscription_credits(sub.user.id, sub)
            except Exception as e:
                logger.error(f"Credit expiry record failed for {sub.user.user_email}: {e}")
            try:
                # Always save in-app notification
                try:
                    from Email_validate_app.utils import create_notification
                    create_notification(sub.user.id, 'expiry',
                        f"Your {sub.subs_plan} plan has expired",
                        url='/subscription/')
                except Exception as e:
                    logger.error(f"Expiry notification failed for {sub.user.user_email}: {e}")

                # Send email only if toggle is on
                if getattr(sub.user, 'notify_expiry', True):
                    try:
                        send_subscription_expiry_email(
                            user_name=sub.user.user_name,
                            user_email=sub.user.user_email,
                            plan=sub.subs_plan,
                            expired_on=sub.valid_time,
                        )
                    except Exception as e:
                        logger.error(f"Expiry email failed for {sub.user.user_email}: {e}")
            except Exception as e:
                logger.error(f"Expiry job failed for user: {e}")

        logger.info(f"Subscription expiry job: {expired.count()} expired plans processed.")
    except Exception as e:
        logger.error(f"Subscription expiry job error: {e}")
        send_job_failure_alert("Subscription Expiry Check (subscription_expiry_job)", e)


@shared_task(name="Email_validate_app.tasks.scheduler_job.trial_expiry_notification_job", base=LoggedTask)
def trial_expiry_notification_job():
    """One-time side effects when a 7-day free trial's window elapses.

    Deliberately separate from subscription_expiry_job/expire_subscription_credits
    above -- different query (UserTable.trial_* vs SubsPayment.plan_status), no
    shared code path, so the existing paid-subscription expiry flow is untouched.

    No stored "expired" flag drives access anywhere -- trial_manager's
    get_trial_remaining()/credit_manager's deduct_service_credits() already
    self-expire correctly by comparing trial_ends_at to now() live, with or
    without this job ever running. This job's only job is the one-time
    notification + audit trail, guarded by trial_expiry_notified_at so a
    user is never notified twice.
    """
    logger.info("Trial expiry notification check triggered")
    try:
        expired_users = UserTable.objects.filter(
            trial_started_at__isnull=False,
            trial_ends_at__lt=now(),
            trial_expiry_notified_at__isnull=True,
        )

        notified = 0
        for user in expired_users:
            try:
                UserTable.objects.filter(pk=user.pk).update(trial_expiry_notified_at=now())

                # Audit: log whatever unused allowance was forfeited, per service.
                for row in ServiceTrial.objects.filter(user_id=user.id):
                    remaining = row.limit - row.used
                    if remaining > 0:
                        TrialUsageLog.objects.create(
                            user_id=user.id, service=row.service, entry_type='expired',
                            amount=-remaining, balance_before=remaining, balance_after=0,
                            ref_type='trial_expiry',
                            description=(f"Trial expired: {remaining} unused "
                                        f"{TRIAL_SERVICE_LABELS.get(row.service, row.service)} "
                                        f"credits forfeited"),
                        )

                try:
                    from Email_validate_app.utils import create_notification
                    create_notification(user.id, 'expiry',
                        "Your 7-day free trial has ended", url='/pricing/')
                except Exception as e:
                    logger.error(f"Trial expiry notification failed for {user.user_email}: {e}")

                if getattr(user, 'notify_expiry', True):
                    try:
                        send_trial_expired_email(user.user_name, user.user_email, user.trial_ends_at)
                    except Exception as e:
                        logger.error(f"Trial expiry email failed for {user.user_email}: {e}")

                notified += 1
            except Exception as e:
                logger.error(f"Trial expiry job failed for user {user.user_email}: {e}")

        logger.info(f"Trial expiry job: {notified} users notified.")
    except Exception as e:
        logger.error(f"Trial expiry job error: {e}")
        send_job_failure_alert("Trial Expiry Notification (trial_expiry_notification_job)", e)


@shared_task(name="Email_validate_app.tasks.scheduler_job.bl_notification_job", base=LoggedTask)
def bl_notification_job():
    logger.info("Blacklist alert triggered")
    try:
        data = get_blacklist_notifications()
        send_mail_notification(data)
        logger.info("Blacklist notification job executed successfully.")

    except Exception as e:
        logger.error(f"An error occurred in the blacklist notification job: {e}")
        send_job_failure_alert("Blacklist Alert Notification (bl_notification_job)", e)
    