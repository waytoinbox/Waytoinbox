"""
campaign_sender.py
------------------
Shared email-sending logic for bulk campaigns.
Used by both views.py (immediate/test sends) and the Celery task (async sends).
"""

import logging
import time

from django.conf import settings

logger = logging.getLogger(__name__)


def build_unsubscribe_link(email, campaign_id, test=False):
    from django.core import signing
    payload = {'e': email, 'c': campaign_id}
    if test:
        payload['test'] = True
    token = signing.dumps(payload, salt='unsub')
    return f"{settings.SITE_URL}/unsubscribe/{token}/"


def inject_unsubscribe_link(html, link):
    footer = (
        f'<div style="text-align:center;padding:16px 0 8px;font-size:12px;color:#888;">'
        f'If you no longer wish to receive these emails, '
        f'<a href="{link}" style="color:#888;">unsubscribe here</a>.'
        f'</div>'
    )
    if '</body>' in html:
        return html.replace('</body>', footer + '</body>', 1)
    return html + footer


def send_campaign_emails(campaign):
    """Send campaign emails via the configured provider. Returns (sent_count, errors)."""
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.header import Header
    from Email_validate_app.models import CampaignEmail
    from Email_validate_app.services.providers import get_provider

    from Email_validate_app.services.segment_builder import get_segment_emails

    email_set = set()

    # Collect from M2M lists
    multi_lists = list(campaign.campaign_lists.all())
    if multi_lists:
        list_ids = [l.id for l in multi_lists]
        qs = CampaignEmail.objects.filter(
            list_id__in=list_ids, deleted_at__isnull=True, subscribed='subscribed',
        ).values_list('email', flat=True)
        email_set.update(qs)
    elif campaign.campaign_list_id:
        qs = CampaignEmail.objects.filter(
            list_id=campaign.campaign_list_id, deleted_at__isnull=True, subscribed='subscribed',
        ).values_list('email', flat=True)
        email_set.update(qs)

    # Collect from M2M segments
    multi_segments = list(campaign.campaign_segments.all())
    if not multi_segments and campaign.campaign_segment_id:
        multi_segments = [campaign.campaign_segment]
    for seg in multi_segments:
        seg_emails = get_segment_emails(seg, campaign.user_id)
        valid = CampaignEmail.objects.filter(
            user_id=campaign.user_id, email__in=seg_emails,
            subscribed='subscribed', deleted_at__isnull=True,
        ).values_list('email', flat=True)
        email_set.update(valid)

    # Subtract excluded lists/segments
    exclude_set = set()
    excl_lists = list(campaign.exclude_lists.all())
    if excl_lists:
        excl_ids = [l.id for l in excl_lists]
        exclude_set.update(
            CampaignEmail.objects.filter(list_id__in=excl_ids, deleted_at__isnull=True)
            .values_list('email', flat=True)
        )
    for seg in campaign.exclude_segments.all():
        exclude_set.update(get_segment_emails(seg, campaign.user_id))
    email_set -= exclude_set

    recipients = list(email_set)
    if not recipients:
        return 0, ['No subscribed recipients found for this campaign.']

    if campaign.template is None:
        return 0, ['No email template is linked to this campaign.']

    provider  = get_provider()
    source    = f"{campaign.sender_name} <{campaign.from_email}>"
    subject   = campaign.template.subject or campaign.campaign_name
    base_html = campaign.template.html_content

    batch_size  = getattr(settings, 'EMAIL_BATCH_SIZE',        500)
    batch_delay = getattr(settings, 'EMAIL_BATCH_DELAY',       60)
    retry_count = getattr(settings, 'EMAIL_BATCH_RETRY_COUNT', 3)
    retry_delay = getattr(settings, 'EMAIL_BATCH_RETRY_DELAY', 30)

    batches     = [recipients[i:i + batch_size] for i in range(0, len(recipients), batch_size)]
    num_batches = len(batches)
    sent, errors = 0, []
    campaign_start = time.monotonic()

    logger.info(
        'Campaign %s: %d recipient(s), %d batch(es) of %d '
        '(retry=%d×%ds, inter-batch delay=%ds)',
        campaign.id, len(recipients), num_batches, batch_size,
        retry_count, retry_delay, batch_delay,
    )

    for batch_num, batch in enumerate(batches, start=1):
        batch_start  = time.monotonic()
        batch_sent   = 0
        batch_errors = 0

        logger.info('Campaign %s: Starting batch %d/%d (%d emails)',
                    campaign.id, batch_num, num_batches, len(batch))

        for addr in batch:
            for attempt in range(retry_count + 1):
                if attempt > 0:
                    logger.warning(
                        'Campaign %s: Batch %d — retry %d/%d for %s',
                        campaign.id, batch_num, attempt, retry_count, addr,
                    )
                    time.sleep(retry_delay)
                try:
                    link = build_unsubscribe_link(addr, campaign.id)
                    html = inject_unsubscribe_link(base_html, link)

                    msg = MIMEMultipart('alternative')
                    msg['From']                  = source
                    msg['To']                    = addr
                    msg['Subject']               = str(Header(subject, 'utf-8'))
                    msg['Reply-To']              = campaign.reply_email
                    msg['List-Unsubscribe']      = f'<{link}>'
                    msg['List-Unsubscribe-Post'] = 'List-Unsubscribe=One-Click'
                    msg.attach(MIMEText(html, 'html', 'utf-8'))

                    result = provider.send_raw(
                        source, addr, msg.as_bytes(),
                        tags={'campaign_id': str(campaign.id)},
                    )
                    if result.success:
                        sent       += 1
                        batch_sent += 1
                        break                        # success — stop retrying
                    elif attempt < retry_count:
                        continue                     # provider error — will retry
                    else:
                        errors.append(f'{addr}: {result.error}')
                        batch_errors += 1

                except Exception as exc:
                    if attempt < retry_count:
                        continue                     # transient error — will retry
                    errors.append(f'{addr}: {exc}')
                    batch_errors += 1

        batch_duration = time.monotonic() - batch_start
        logger.info(
            'Campaign %s: Batch %d/%d complete — sent=%d failed=%d duration=%.2fs',
            campaign.id, batch_num, num_batches, batch_sent, batch_errors, batch_duration,
        )

        if batch_num < num_batches:
            logger.info('Campaign %s: Sleeping %ds before batch %d/%d',
                        campaign.id, batch_delay, batch_num + 1, num_batches)
            time.sleep(batch_delay)

    total_duration = time.monotonic() - campaign_start
    logger.info(
        'Campaign %s: All %d batch(es) complete — total_sent=%d total_errors=%d duration=%.2fs',
        campaign.id, num_batches, sent, len(errors), total_duration,
    )
    return sent, errors


def send_test_campaign_emails(campaign, recipients):
    """Send test emails for a campaign. Returns (sent_count, errors)."""
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.header import Header
    from Email_validate_app.services.providers import get_provider

    provider  = get_provider()
    source    = f"{campaign.sender_name} <{campaign.from_email}>"
    subject   = f"[TEST] {campaign.template.subject or campaign.campaign_name}"
    base_html = campaign.template.html_content

    sent, errors = 0, []
    for addr in recipients:
        try:
            link = build_unsubscribe_link(addr, campaign.id, test=True)
            html = inject_unsubscribe_link(base_html, link)

            msg = MIMEMultipart('alternative')
            msg['From']                  = source
            msg['To']                    = addr
            msg['Subject']               = str(Header(subject, 'utf-8'))
            msg['Reply-To']              = campaign.reply_email
            msg['List-Unsubscribe']      = f'<{link}>'
            msg['List-Unsubscribe-Post'] = 'List-Unsubscribe=One-Click'
            msg.attach(MIMEText(html, 'html', 'utf-8'))

            result = provider.send_raw(
                source, addr, msg.as_bytes(),
                tags={'campaign_id': str(campaign.id), 'test': '1'},
            )
            if result.success:
                sent += 1
            else:
                errors.append(f'{addr}: {result.error}')
        except Exception as exc:
            errors.append(f'{addr}: {exc}')
    return sent, errors
