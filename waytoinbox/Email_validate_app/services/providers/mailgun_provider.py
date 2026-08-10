"""
Mailgun provider — sends via Mailgun REST API (MIME endpoint), manages
domains via /v4/domains, and polls the Mailgun Events API for campaign stats.
"""

import logging
from datetime import datetime, timezone

import requests
from django.conf import settings

from .base import EmailProvider, SendResult, DomainStatus

logger = logging.getLogger(__name__)


class MailgunPermanentError(RuntimeError):
    """Domain or resource does not exist in Mailgun — no point retrying."""


_EVENT_TYPE_MAP = {
    'accepted':     'send',
    'delivered':    'delivery',
    'opened':       'open',
    'clicked':      'click',
    'failed':       'bounce',
    'unsubscribed': 'unsubscribe',
    'complained':   'complaint',
}


class MailgunProvider(EmailProvider):

    @property
    def _api_key(self):
        return settings.MAILGUN_API_KEY

    @property
    def _base(self):
        return getattr(settings, 'MAILGUN_BASE_URL', 'https://api.mailgun.net').rstrip('/')

    def _auth(self):
        return ('api', self._api_key)

    @staticmethod
    def _extract_domain(source: str) -> str:
        """Extract sending domain from 'Name <email@domain.com>' or 'email@domain.com'."""
        import re
        m = re.search(r'<([^>]+)>', source)
        email = m.group(1) if m else source.strip()
        return email.split('@')[-1]

    # ── Send ─────────────────────────────────────────────────────────────────

    def send_raw(self, source, destination, mime_bytes, tags=None):
        domain = self._extract_domain(source)
        url = f'{self._base}/v3/{domain}/messages.mime'
        data = {'to': destination}

        if tags:
            # Mailgun accepts multiple o:tag values; build list
            data['o:tag'] = [f'{k}_{v}' for k, v in tags.items()]

        data.update({
            'o:tracking':        'yes',
            'o:tracking-clicks': 'yes',
            'o:tracking-opens':  'yes',
        })

        try:
            resp = requests.post(
                url,
                auth=self._auth(),
                data=data,
                files={'message': ('message.mime', mime_bytes, 'message/rfc2822')},
                timeout=30,
            )
            if resp.status_code in (200, 202):
                body = resp.json()
                return SendResult(success=True, message_id=body.get('id'))
            return SendResult(success=False, error=f'HTTP {resp.status_code}: {resp.text}')
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    # ── Domain management ────────────────────────────────────────────────────

    def create_domain(self, domain):
        create_url = f'{self._base}/v4/domains'
        try:
            resp = requests.post(
                create_url,
                auth=self._auth(),
                data={'name': domain},
                timeout=30,
            )
        except Exception as exc:
            raise RuntimeError(f'Mailgun create_domain failed: {exc}') from exc

        if resp.status_code not in (200, 201, 409):
            raise RuntimeError(f'Mailgun create_domain HTTP {resp.status_code}: {resp.text}')

        return {'dns_records': self._get_dns_records(domain), 'status': 'pending'}

    def get_domain_status(self, domain):
        url = f'{self._base}/v4/domains/{domain}'
        try:
            resp = requests.get(url, auth=self._auth(), timeout=30)
        except Exception:
            return DomainStatus(verified=False, dkim_status='PENDING', dns_records=[])

        if resp.status_code != 200:
            return DomainStatus(verified=False, dkim_status='PENDING', dns_records=[])

        body   = resp.json()
        domain_data = body.get('domain') or {}
        state  = domain_data.get('state', 'unverified')
        verified = state == 'active'

        sending_raw = body.get('sending_dns_records', [])
        sending     = self._parse_dns_records(sending_raw)
        receiving   = self._parse_mx_records(domain, body.get('receiving_dns_records', []))
        dns_records = sending + receiving
        dkim_status = 'SUCCESS' if all(
            r.get('valid') == 'valid' for r in sending_raw
        ) else 'PENDING'

        return DomainStatus(verified=verified, dkim_status=dkim_status, dns_records=dns_records)

    def create_email_identity(self, email):
        pass  # Mailgun verifies domains; per-address registration is not needed

    # ── Event tracking ───────────────────────────────────────────────────────

    def fetch_campaign_events(self, campaign_id, start_dt, end_dt):
        # Resolve sending domain from the campaign's from_email
        domain = None
        try:
            from Email_validate_app.models import Campaign
            campaign = Campaign.objects.get(id=campaign_id)
            domain = self._extract_domain(campaign.from_email)
        except Exception:
            pass

        events = []
        errors = []
        for mg_event in _EVENT_TYPE_MAP:
            try:
                events.extend(
                    self._fetch_events_by_type(campaign_id, mg_event, start_dt, end_dt, domain)
                )
            except MailgunPermanentError:
                raise  # bubble up immediately — no point trying other event types
            except RuntimeError as exc:
                logger.error('Mailgun event type %s failed: %s', mg_event, exc)
                errors.append(str(exc))

        if errors:
            raise RuntimeError(
                f'Mailgun fetch failed for {len(errors)} event type(s): {errors[0]}'
            )
        return events

    def _fetch_events_by_type(self, campaign_id, mg_event_type, start_dt, end_dt, domain=None):
        url = f'{self._base}/v3/{domain}/events'
        params = {
            'event': mg_event_type,
            'tags':  f'campaign_id_{campaign_id}',
            'begin': int(start_dt.timestamp()),
            'end':   int(end_dt.timestamp()),
            'limit': 300,
        }

        events = []
        while url:
            try:
                resp = requests.get(url, auth=self._auth(), params=params, timeout=30)
            except Exception as exc:
                raise RuntimeError(f'Mailgun events fetch error: {exc}') from exc

            if resp.status_code != 200:
                try:
                    msg = resp.json().get('message', '')
                except Exception:
                    msg = resp.text
                if resp.status_code == 404 and 'domain not found' in msg.lower():
                    raise MailgunPermanentError(
                        f"Domain '{domain}' not found in Mailgun — "
                        "campaign was likely sent via a different provider."
                    )
                raise RuntimeError(
                    f'Mailgun events HTTP {resp.status_code}: {resp.text}'
                )

            body  = resp.json()
            items = body.get('items', [])

            if not items:
                break  # Mailgun always returns a next URL — empty items means no more data

            for item in items:
                event_type = _EVENT_TYPE_MAP.get(item.get('event', '').lower())
                if not event_type:
                    continue
                recipient  = item.get('recipient', '')
                message_id = (item.get('message') or {}).get('headers', {}).get('message-id', '')
                ts         = item.get('timestamp')
                try:
                    event_time = datetime.fromtimestamp(float(ts), tz=timezone.utc) if ts else datetime.now(timezone.utc)
                except (ValueError, TypeError):
                    event_time = datetime.now(timezone.utc)

                events.append({
                    'event_type': event_type,
                    'email':      recipient,
                    'message_id': message_id,
                    'timestamp':  event_time,
                    'raw':        item,
                })

            paging = body.get('paging') or {}
            next_url = paging.get('next')
            url = next_url if next_url else None
            params = {}  # pagination URL already has params baked in

        return events

    # ── Private helpers ──────────────────────────────────────────────────────

    def _get_dns_records(self, domain):
        url = f'{self._base}/v4/domains/{domain}'
        try:
            resp = requests.get(url, auth=self._auth(), timeout=30)
            if resp.status_code == 200:
                body = resp.json()
                sending   = self._parse_dns_records(body.get('sending_dns_records', []))
                receiving = self._parse_mx_records(domain, body.get('receiving_dns_records', []))
                return sending + receiving
        except Exception:
            pass
        return []

    @staticmethod
    def _parse_dns_records(raw_records):
        result = []
        for r in raw_records or []:
            record_type = (r.get('record_type') or 'TXT').upper()
            name  = r.get('name', '')
            value = r.get('value', '')
            if name and value:
                result.append({'record_type': record_type, 'name': name, 'value': value})
        return result

    @staticmethod
    def _parse_mx_records(domain, raw_records):
        result = []
        for r in raw_records or []:
            value    = r.get('value', '')
            priority = r.get('priority', '10')
            if value:
                result.append({
                    'record_type': 'MX',
                    'name':  domain,
                    'value': value,
                    'priority': str(priority),
                })
        return result
