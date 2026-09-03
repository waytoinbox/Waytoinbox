import logging

from django.core.mail import send_mail, BadHeaderError
from django.conf import settings

logger = logging.getLogger(__name__)

ADMIN_EMAIL = "support@waytoinbox.com"


def send_job_failure_alert(job_name: str, error: Exception, context: dict = None):
    from django.utils.timezone import now as tz_now
    import traceback

    failure_time = tz_now().strftime('%d %b %Y, %I:%M %p UTC')
    tb = traceback.format_exc()

    lines = [
        "A scheduled job has failed or stopped unexpectedly.",
        "",
        "-" * 44,
        "JOB FAILURE DETAILS",
        "-" * 44,
        f"Job Name     : {job_name}",
        f"Failure Time : {failure_time}",
        f"Error        : {error}",
    ]

    if context:
        lines.append("")
        lines.append("Additional Context:")
        for k, v in context.items():
            lines.append(f"  {k}: {v}")

    lines += [
        "",
        "Traceback:",
        tb,
        "-" * 44,
        "Please investigate and take corrective action.",
        "",
        "— Waytoinbox Alert System",
        "support@waytoinbox.com",
    ]

    subject = f"[ALERT] Job Failed: {job_name} | Waytoinbox"
    message = "\n".join(lines)

    try:
        send_mail(subject, message, settings.EMAIL_HOST_USER, [ADMIN_EMAIL])
    except Exception as mail_err:
        import logging
        logging.getLogger(__name__).error(
            f"send_job_failure_alert: could not send alert for '{job_name}': {mail_err}"
        )


def send_verification_email(user_email, user_name, verification_link):
    subject = "Verify Your Email — Waytoinbox"
    message = f"""Hi {user_name},

Welcome to Waytoinbox! We're glad to have you.

Please verify your email address by clicking the link below:
{verification_link}

This link will expire after one use. If you did not create an account, you can safely ignore this email.

— The Waytoinbox Team
support@waytoinbox.com
"""
    try:
        send_mail(subject, message, settings.EMAIL_HOST_USER, [user_email])
        logger.info("Verification email sent to %s", user_email)
    except BadHeaderError:
        logger.error("Verification email: BadHeaderError for %s", user_email)
    except Exception as e:
        logger.error("Verification email error for %s: %s", user_email, e)


def send_welcome_email(user_name, user_email):
    subject = "Welcome to Waytoinbox — You're Verified!"
    message = f"""Hi {user_name},

Your email has been verified successfully. Welcome aboard!

You can now log in and start verifying emails, monitoring your sender reputation, and keeping your lists clean.

👉 https://waytoinbox.com/login/

If you have any questions, reply to this email — we're happy to help.

— The Waytoinbox Team
support@waytoinbox.com
"""
    try:
        send_mail(subject, message, settings.EMAIL_HOST_USER, [user_email])
        logger.info("Welcome email sent to %s", user_email)
    except Exception as e:
        logger.error("Welcome email error for %s: %s", user_email, e)


def send_subscription_expiry_email(user_name, user_email, plan, expired_on):
    subject = "Your Waytoinbox Subscription Has Expired — Renew to Continue"
    expired_str = expired_on.strftime('%d %b %Y') if expired_on else 'N/A'
    message = (
        f"Hi {user_name},\n\n"
        f"Your {plan} subscription expired on {expired_str}.\n\n"
        f"Your account is now on the free tier. To continue using premium features "
        f"and your full credit balance, please renew your subscription.\n\n"
        f"👉 Renew now: https://waytoinbox.com/subscription/\n\n"
        f"If you have any questions, reply to this email — we're happy to help.\n\n"
        f"— The Waytoinbox Team\n"
        f"support@waytoinbox.com\n"
    )
    try:
        send_mail(subject, message, settings.EMAIL_HOST_USER, [user_email])
        logger.info("Expiry email sent to %s", user_email)
    except Exception as e:
        logger.error("Expiry email error for %s: %s", user_email, e)


def send_trial_expired_email(user_name, user_email, expired_on):
    subject = "Your Waytoinbox Free Trial Has Ended"
    expired_str = expired_on.strftime('%d %b %Y') if expired_on else 'N/A'
    message = (
        f"Hi {user_name},\n\n"
        f"Your 7-day free trial ended on {expired_str}.\n\n"
        f"To keep using Email Validation, Email Marketing, Sales Outreach, "
        f"Reputation Analysis, Email Header Analyzer, IP Blocklist Monitor and "
        f"Domain Blocklist Monitor, please choose a paid plan or buy credits.\n\n"
        f"👉 See plans: https://waytoinbox.com/pricing/\n\n"
        f"If you have any questions, reply to this email — we're happy to help.\n\n"
        f"— The Waytoinbox Team\n"
        f"support@waytoinbox.com\n"
    )
    try:
        send_mail(subject, message, settings.EMAIL_HOST_USER, [user_email])
        logger.info("Trial expiry email sent to %s", user_email)
    except Exception as e:
        logger.error("Trial expiry email error for %s: %s", user_email, e)


def send_payment_success_email(user_name, user_email, amount, currency, order_id, payment_time, extra):
    """
    extra = {'type': 'payg', 'credits': 500}
         or {'type': 'subscription', 'plan': 'Classic', 'vc_credits': 1050, 'ac_credits': 5, 'valid_till': '...'}
         or {'type': 'service_credits', 'cart': {'email_validation': 25000, 'sales_outreach': 10}}
    """
    paid_str = f"${amount} {currency}"
    time_str = payment_time.strftime('%d %b %Y, %I:%M %p UTC') if payment_time else 'N/A'

    if extra.get('type') == 'service_credits':
        # A service purchase has no single credit number — it is a basket, so
        # itemise it rather than printing a meaningless total.
        from Email_validate_app.services.pricing import SERVICE_LABELS
        cart  = extra.get('cart') or {}
        width = max((len(SERVICE_LABELS.get(s, s)) for s in cart), default=0)
        detail = "".join(
            f"  {SERVICE_LABELS.get(svc, svc):<{width}} : {int(qty):,}\n"
            for svc, qty in cart.items()
        ) or "  Credits Added : 0\n"
        subject = "Payment Successful — Credits Added | Waytoinbox"
    elif extra.get('type') == 'subscription':
        plan   = extra.get('plan', '')
        vc_c   = extra.get('vc_credits', 0)
        ac_c   = extra.get('ac_credits', 0)
        valid  = extra.get('valid_till', 'N/A')
        detail = (
            f"  Plan               : {plan}\n"
            f"  Validation Credits : {vc_c:,}\n"
            f"  Analysis Credits   : {ac_c}\n"
            f"  Valid Till         : {valid}\n"
        )
        subject = f"Subscription Activated — {plan} Plan | Waytoinbox"
    else:
        credits = extra.get('credits', 0)
        detail  = f"  Credits Added : {credits:,}\n"
        subject = f"Payment Successful — {credits:,} Credits Added | Waytoinbox"

    message = (
        f"Hi {user_name},\n\n"
        f"Your payment was successful. Here are your details:\n\n"
        f"  Order ID      : {order_id}\n"
        f"  Amount Paid   : {paid_str}\n"
        f"  Date & Time   : {time_str}\n"
        f"{detail}\n"
        f"Your account has been updated. Log in to start using your credits.\n\n"
        f"If you have any questions, reply to this email.\n\n"
        f"— The Waytoinbox Team\n"
        f"support@waytoinbox.com\n"
    )
    try:
        send_mail(subject, message, settings.EMAIL_HOST_USER, [user_email])
        logger.info("Payment success email sent to %s", user_email)
    except Exception as e:
        logger.error("Payment success email error for %s: %s", user_email, e)


def send_admin_signup_notification(user_name, user_email):
    subject = "New Verified User — Waytoinbox"
    message = f"""A new user has verified their account on Waytoinbox.

Name  : {user_name}
Email : {user_email}

Log in to the admin panel to review.
"""
    try:
        send_mail(subject, message, settings.EMAIL_HOST_USER, [ADMIN_EMAIL])
        logger.info("Admin notified of verified signup: %s", user_email)
    except Exception as e:
        logger.error("Admin signup notification error for %s: %s", user_email, e)


def send_job_completed_email(user_name, user_email, job_id, file_name, created_at, total):
    subject = "Job Completed — Waytoinbox Email Validation"
    separator = "-" * 40
    message = (
        "Dear " + str(user_name) + ",\n\n"
        "Your email validation job has been completed successfully.\n"
        "The results are ready for download from your dashboard.\n\n"
        + separator + "\n"
        "JOB DETAILS\n"
        + separator + "\n"
        "Job ID       : #" + str(job_id) + "\n"
        "File Name    : " + str(file_name) + "\n"
        "Created At   : " + str(created_at) + "\n"
        "Total Emails : " + str(total) + "\n"
        + separator + "\n\n"
        "To download your results, please visit:\n"
        "https://waytoinbox.com/services/\n\n"
        "If you have any questions or concerns, feel free to contact us at\n"
        "support@waytoinbox.com — we are happy to help.\n\n"
        "Best regards,\n"
        "The Waytoinbox Team\n"
        "https://waytoinbox.com"
    )
    try:
        send_mail(subject, message, settings.EMAIL_HOST_USER, [user_email])
        logger.info("Job completed email sent to %s", user_email)
    except Exception as e:
        logger.error("Job completed email error for %s: %s", user_email, e)


def send_admin_domain_added_notification(user_email, domain, provider):
    from django.utils.timezone import now
    separator = "-" * 44
    message = (
        "A user has added a new sender domain on Waytoinbox.\n\n"
        + separator + "\n"
        "DOMAIN DETAILS\n"
        + separator + "\n"
        f"User Email : {user_email}\n"
        f"Domain     : {domain}\n"
        f"Provider   : {provider.upper()}\n"
        f"Added At   : {now().strftime('%d %b %Y, %I:%M %p UTC')}\n"
        + separator + "\n\n"
        "The domain is pending DNS verification.\n"
    )
    try:
        send_mail(
            subject=f"New Sender Domain Added — {domain} | Waytoinbox",
            message=message,
            from_email=settings.EMAIL_HOST_USER,
            recipient_list=[ADMIN_EMAIL],
        )
        logger.info("Admin notified of new sender domain: %s by %s", domain, user_email)
    except Exception as e:
        logger.error("Admin domain added notification error for %s: %s", domain, e)


def send_admin_reputation_added_notification(user_email, domain, rep_status):
    from django.utils.timezone import now
    separator = "-" * 44
    message = (
        "A user has added a domain for Reputation Analysis on Waytoinbox.\n\n"
        + separator + "\n"
        "REPUTATION ANALYSIS DETAILS\n"
        + separator + "\n"
        f"User Email  : {user_email}\n"
        f"Domain      : {domain}\n"
        f"Status      : {rep_status.capitalize()}\n"
        f"Added At    : {now().strftime('%d %b %Y, %I:%M %p UTC')}\n"
        + separator + "\n\n"
        "Log in to the admin panel to review.\n"
    )
    try:
        send_mail(
            subject=f"Reputation Analysis — {domain} Added | Waytoinbox",
            message=message,
            from_email=settings.EMAIL_HOST_USER,
            recipient_list=[ADMIN_EMAIL],
        )
        logger.info("Admin notified of reputation analysis add: %s by %s", domain, user_email)
    except Exception as e:
        logger.error("Admin reputation notification error for %s: %s", domain, e)


def send_campaign_result_email(user_name, user_email, campaign_name, status, send_count=0):
    separator = "-" * 40
    if status == 'sent':
        subject = f'Campaign Sent — {campaign_name} | Waytoinbox'
        detail  = f'Total Sent   : {send_count:,} recipient(s)'
        summary = 'Your campaign has been sent successfully.'
    else:
        subject = f'Campaign Failed — {campaign_name} | Waytoinbox'
        detail  = 'Status       : Failed'
        summary = 'Your campaign could not be sent. Please check your credits and contact list.'
    message = (
        f'Dear {user_name},\n\n'
        f'{summary}\n\n'
        + separator + '\n'
        'CAMPAIGN DETAILS\n'
        + separator + '\n'
        f'Campaign     : {campaign_name}\n'
        f'{detail}\n'
        + separator + '\n\n'
        'Visit your dashboard to review the campaign:\n'
        'https://waytoinbox.com/Email_Campaigns/campaigns/\n\n'
        'For help, contact us at support@waytoinbox.com\n\n'
        'Best regards,\n'
        'The Waytoinbox Team\n'
        'https://waytoinbox.com'
    )
    try:
        send_mail(subject, message, settings.EMAIL_HOST_USER, [user_email])
        logger.info('Campaign result email sent to %s — %s', user_email, status)
    except Exception as e:
        logger.error('Campaign result email error for %s: %s', user_email, e)


def send_reputation_unverified_email(user_name, user_email, domain):
    separator = "-" * 40
    subject   = f'Reputation Check — {domain} is Unverified | Waytoinbox'
    message   = (
        f'Dear {user_name},\n\n'
        f'Your reputation analysis for "{domain}" has completed.\n\n'
        + separator + '\n'
        'RESULT\n'
        + separator + '\n'
        f'Domain       : {domain}\n'
        f'Status       : Unverified\n\n'
        'The domain could not be verified with Google Postmaster Tools. '
        'Make sure the domain is registered and DNS records are correct.\n\n'
        + separator + '\n\n'
        'View your results:\n'
        'https://waytoinbox.com/Email_Campaigns/reputation/\n\n'
        'For help, contact us at support@waytoinbox.com\n\n'
        'Best regards,\n'
        'The Waytoinbox Team\n'
        'https://waytoinbox.com'
    )
    try:
        send_mail(subject, message, settings.EMAIL_HOST_USER, [user_email])
        logger.info('Reputation unverified email sent to %s for domain %s', user_email, domain)
    except Exception as e:
        logger.error('Reputation unverified email error for %s: %s', user_email, e)


def send_sender_domain_verified_email(user_name, user_email, domain):
    separator = "-" * 40
    subject   = f'Domain Verified — {domain} | Waytoinbox'
    message   = (
        f'Dear {user_name},\n\n'
        f'Great news! Your sender domain "{domain}" has been successfully verified.\n\n'
        + separator + '\n'
        'VERIFICATION DETAILS\n'
        + separator + '\n'
        f'Domain       : {domain}\n'
        f'Status       : Verified\n\n'
        'You can now use this domain to send campaigns from Waytoinbox.\n\n'
        + separator + '\n\n'
        'Manage your senders:\n'
        'https://waytoinbox.com/sender-verify/\n\n'
        'For help, contact us at support@waytoinbox.com\n\n'
        'Best regards,\n'
        'The Waytoinbox Team\n'
        'https://waytoinbox.com'
    )
    try:
        send_mail(subject, message, settings.EMAIL_HOST_USER, [user_email])
        logger.info('Sender domain verified email sent to %s for domain %s', user_email, domain)
    except Exception as e:
        logger.error('Sender domain verified email error for %s: %s', user_email, e)


def send_delete_request_email(user_id, user_name, user_email, joined, active_plan, reason, extra_note):
    from django.utils.timezone import now
    separator = "-" * 44
    message = (
        "A user has submitted an account deletion request.\n\n"
        + separator + "\n"
        "USER DETAILS\n"
        + separator + "\n"
        f"User ID    : {user_id}\n"
        f"Name       : {user_name}\n"
        f"Email      : {user_email}\n"
        f"Joined     : {joined}\n"
        f"Active Plan: {active_plan or 'None'}\n\n"
        + separator + "\n"
        "DELETION REASON\n"
        + separator + "\n"
        f"Reason     : {reason}\n"
        f"Note       : {extra_note or '—'}\n\n"
        + separator + "\n"
        f"Requested  : {now().strftime('%d %b %Y, %I:%M %p UTC')}\n"
        + separator + "\n\n"
        "Please review and take appropriate action in the admin panel.\n"
    )
    try:
        send_mail(
            subject=f"Account Deletion Request — {user_email}",
            message=message,
            from_email=settings.EMAIL_HOST_USER,
            recipient_list=[ADMIN_EMAIL],
        )
        logger.info("Delete request email sent for %s", user_email)
    except Exception as e:
        logger.error("Delete request email error for %s: %s", user_email, e)
