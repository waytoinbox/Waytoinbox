"""
SES provider — wraps boto3 SES v1 (send), SESv2 (identity management),
and CloudWatch Logs (event tracking).
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from django.conf import settings

from .base import EmailProvider, SendResult, DomainStatus

logger = logging.getLogger(__name__)

_LOG_GROUP = '/aws/events/SES-Tracking-Logs'
_PRE_SEND_BUFFER_MINUTES = 5

_EVENT_TYPE_MAP = {
    'send': 'send',
    'delivery': 'delivery',
    'open': 'open',
    'click': 'click',
    'bounce': 'bounce',
    'complaint': 'complaint',
    'reject': 'reject',
    'unsubscribe': 'unsubscribe',
}


class SESProvider(EmailProvider):

    def _ses_v1(self):
        import boto3
        return boto3.client(
            'ses',
            aws_access_key_id=settings.AWS_SES_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SES_SECRET_ACCESS_KEY,
            region_name=settings.AWS_SES_REGION,
        )

    def _ses_v2(self):
        import boto3
        return boto3.client(
            'sesv2',
            aws_access_key_id=settings.AWS_SES_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SES_SECRET_ACCESS_KEY,
            region_name=settings.AWS_SES_REGION,
        )

    def _logs(self):
        import boto3
        return boto3.client(
            'logs',
            aws_access_key_id=settings.AWS_SES_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SES_SECRET_ACCESS_KEY,
            region_name=settings.AWS_SES_REGION,
        )

    # ── Send ─────────────────────────────────────────────────────────────────

    def send_raw(self, source, destination, mime_bytes, tags=None):
        ses_tags = [{'Name': k, 'Value': str(v)} for k, v in (tags or {}).items()]
        try:
            resp = self._ses_v1().send_raw_email(
                Source=source,
                Destinations=[destination],
                RawMessage={'Data': mime_bytes},
                ConfigurationSetName=settings.AWS_SES_CONFIGURATION_SET,
                Tags=ses_tags,
            )
            return SendResult(success=True, message_id=resp.get('MessageId'))
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    # ── Domain management ────────────────────────────────────────────────────

    def create_domain(self, domain):
        from botocore.exceptions import ClientError
        try:
            resp   = self._ses_v2().create_email_identity(EmailIdentity=domain)
            tokens = resp.get('DkimAttributes', {}).get('Tokens', [])
        except ClientError as exc:
            if exc.response['Error']['Code'] != 'AlreadyExistsException':
                raise
            detail = self._ses_v2().get_email_identity(EmailIdentity=domain)
            tokens = detail.get('DkimAttributes', {}).get('Tokens', [])

        dns_records = self._tokens_to_dns_records(tokens, domain)
        return {'dns_records': dns_records, 'status': 'pending'}

    def get_domain_status(self, domain):
        from botocore.exceptions import ClientError
        try:
            detail   = self._ses_v2().get_email_identity(EmailIdentity=domain)
            verified = detail.get('VerifiedForSendingStatus', False)
            dkim     = detail.get('DkimAttributes', {})
            raw_status = dkim.get('Status', 'NOT_STARTED')
            # Map SES status strings to our canonical set
            dkim_status = 'SUCCESS' if raw_status == 'SUCCESS' else (
                'FAILED' if raw_status in ('FAILED', 'TEMPORARY_FAILURE') else 'PENDING'
            )
            tokens = dkim.get('Tokens', [])
            dns_records = self._tokens_to_dns_records(tokens, domain)
            return DomainStatus(verified=verified, dkim_status=dkim_status, dns_records=dns_records)
        except ClientError:
            return DomainStatus(verified=False, dkim_status='PENDING', dns_records=[])

    def create_email_identity(self, email):
        from botocore.exceptions import ClientError
        try:
            self._ses_v2().create_email_identity(EmailIdentity=email)
        except ClientError as exc:
            if exc.response['Error']['Code'] != 'AlreadyExistsException':
                pass  # non-fatal

    # ── Event tracking ───────────────────────────────────────────────────────

    def fetch_campaign_events(self, campaign_id, start_dt, end_dt):
        from botocore.exceptions import ClientError

        if not (settings.AWS_SES_ACCESS_KEY_ID and settings.AWS_SES_SECRET_ACCESS_KEY):
            return []

        start_ms = int(start_dt.timestamp() * 1000)
        end_ms   = int(end_dt.timestamp() * 1000)
        filter_pattern = f'{{$.detail.mail.tags.campaign_id[0] = "{campaign_id}"}}'

        client = self._logs()
        kwargs = {
            'logGroupName': _LOG_GROUP,
            'startTime': start_ms,
            'endTime': end_ms,
            'filterPattern': filter_pattern,
        }

        log_events = []
        while True:
            try:
                resp = client.filter_log_events(**kwargs)
            except ClientError as exc:
                code = exc.response.get('Error', {}).get('Code', '')
                if code == 'ResourceNotFoundException':
                    logger.warning("CloudWatch log group '%s' not found.", _LOG_GROUP)
                else:
                    logger.error("CloudWatch filter_log_events error: %s", exc)
                break
            log_events.extend(resp.get('events', []))
            token = resp.get('nextToken')
            if not token:
                break
            kwargs['nextToken'] = token

        events = []
        for log_event in log_events:
            parsed = self._parse_ses_event(log_event.get('message', ''))
            if parsed is None or parsed['campaign_id'] != campaign_id:
                continue
            for email in parsed['emails']:
                events.append({
                    'event_type': parsed['event_type'],
                    'email':      email,
                    'message_id': parsed['message_id'],
                    'timestamp':  parsed['event_time'],
                    'raw':        parsed['raw'],
                })
        return events

    # ── Private helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _tokens_to_dns_records(tokens, domain):
        return [
            {
                'record_type': 'CNAME',
                'name':  f'{tok}._domainkey.{domain}',
                'value': f'{tok}.dkim.amazonses.com',
            }
            for tok in tokens
        ]

    @staticmethod
    def _parse_ses_event(raw_message):
        try:
            event = json.loads(raw_message)
        except (json.JSONDecodeError, TypeError):
            return None

        if event.get('source') == 'aws.ses' and isinstance(event.get('detail'), dict):
            event = event['detail']

        mail = event.get('mail') or {}
        message_id = mail.get('messageId')
        if not message_id:
            return None

        tags = mail.get('tags') or {}
        campaign_id_values = tags.get('campaign_id') or []
        try:
            campaign_id = int(campaign_id_values[0])
        except (ValueError, IndexError):
            return None

        emails = [a for a in (mail.get('destination') or []) if a]
        if not emails:
            return None

        raw_type   = (event.get('eventType') or '').lower()
        event_type = _EVENT_TYPE_MAP.get(raw_type)
        if not event_type:
            return None

        sub    = event.get(raw_type)
        ts_str = (sub.get('timestamp') if isinstance(sub, dict) else None) or mail.get('timestamp')
        if ts_str:
            try:
                event_time = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                event_time = datetime.now(timezone.utc)
        else:
            event_time = datetime.now(timezone.utc)

        return {
            'campaign_id': campaign_id,
            'message_id':  message_id,
            'emails':      emails,
            'event_type':  event_type,
            'event_time':  event_time,
            'raw':         event,
        }
