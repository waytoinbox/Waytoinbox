from django.utils import timezone


def _sync_domain_status(domain_obj):
    """Sync status from both SES and Mailgun independently, then update canonical status."""
    from Email_validate_app.services.providers.ses_provider import SESProvider
    from Email_validate_app.services.providers.mailgun_provider import MailgunProvider

    update_fields = []
    ses_just_verified = False
    mg_just_verified = False

    # ── SES ──────────────────────────────────────────────────────────────────
    try:
        ses = SESProvider().get_domain_status(domain_obj.domain)
        domain_obj.ses_status = 'verified' if ses.verified else 'pending'
        if ses.dns_records:
            domain_obj.ses_dkim_tokens = ses.dns_records
        if ses.verified and not domain_obj.ses_verified_at:
            domain_obj.ses_verified_at = timezone.now()
            ses_just_verified = True
        update_fields += ['ses_status', 'ses_dkim_tokens', 'ses_verified_at']
    except Exception:
        pass

    # ── Mailgun ───────────────────────────────────────────────────────────────
    try:
        mg = MailgunProvider().get_domain_status(domain_obj.domain)
        domain_obj.mailgun_status = 'verified' if mg.verified else 'pending'
        if mg.dns_records:
            domain_obj.mailgun_dkim_tokens = mg.dns_records
        if mg.verified and not domain_obj.mailgun_verified_at:
            domain_obj.mailgun_verified_at = timezone.now()
            mg_just_verified = True
        update_fields += ['mailgun_status', 'mailgun_dkim_tokens', 'mailgun_verified_at']
    except Exception:
        pass

    # ── Canonical status — verified only when BOTH providers verified ─────────
    both_verified = domain_obj.ses_status == 'verified' and domain_obj.mailgun_status == 'verified'
    domain_obj.status      = 'verified' if both_verified else 'pending'
    domain_obj.dkim_status = 'SUCCESS'  if both_verified else 'PENDING'
    domain_obj.dkim_tokens = domain_obj.ses_dkim_tokens or domain_obj.mailgun_dkim_tokens
    # Fire exactly once: when both providers are verified and verified_at hasn't been stamped yet.
    # Using both_verified (not ses/mg individually) handles the case where the two providers
    # verify on different sync calls — the notification still fires on the call that tips both to verified.
    just_verified = both_verified and not domain_obj.verified_at

    if both_verified and not domain_obj.verified_at:
        domain_obj.verified_at = timezone.now()
        update_fields.append('verified_at')

    update_fields += ['status', 'dkim_status', 'dkim_tokens']

    if update_fields:
        domain_obj.save(update_fields=list(set(update_fields)))

    if just_verified:
        try:
            from Email_validate_app.utils import create_notification
            create_notification(domain_obj.user_id, 'sender_verify',
                f'Domain "{domain_obj.domain}" has been verified successfully',
                url='/Email_Campaigns/sender-verify/')
        except Exception:
            pass
        try:
            from Email_validate_app.models import UserTable as _UT
            _u = _UT.objects.filter(pk=domain_obj.user_id).only(
                'user_name', 'user_email', 'notify_sender_verify'
            ).first()
            if _u and getattr(_u, 'notify_sender_verify', True):
                from Email_validate_app.services.mailer import send_sender_domain_verified_email
                send_sender_domain_verified_email(_u.user_name, _u.user_email, domain_obj.domain)
        except Exception:
            pass
