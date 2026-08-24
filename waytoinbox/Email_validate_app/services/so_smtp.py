import re
import smtplib
import ssl
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header
from html import escape

from django.conf import settings
from django.core import signing


SITE_URL = getattr(settings, 'SITE_URL', 'https://waytoinbox.com').rstrip('/')

_TAG_MAP = {
    '{{first_name}}':     lambda p: p.get('first_name', ''),
    '{{last_name}}':      lambda p: p.get('last_name', ''),
    '{{full_name}}':      lambda p: (p.get('first_name', '') + ' ' + p.get('last_name', '')).strip(),
    '{{email}}':          lambda p: p.get('email', ''),
    '{{company}}':        lambda p: p.get('company', ''),
    '{{phone}}':          lambda p: p.get('phone', ''),
    '{{unsubscribe_url}}': lambda p: p.get('unsubscribe_url', ''),
}

_HREF_RE = re.compile(r'href="(https?://[^"]+)"', re.IGNORECASE)

TRANSPARENT_GIF = bytes([
    0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0x01, 0x00,
    0x01, 0x00, 0x80, 0x00, 0x00, 0xff, 0xff, 0xff,
    0x00, 0x00, 0x00, 0x21, 0xf9, 0x04, 0x01, 0x00,
    0x00, 0x00, 0x00, 0x2c, 0x00, 0x00, 0x00, 0x00,
    0x01, 0x00, 0x01, 0x00, 0x00, 0x02, 0x02, 0x44,
    0x01, 0x00, 0x3b,
])


def decrypt_password(account):
    raw = signing.loads(account.password, salt='so-ea-pwd')
    return raw.replace(' ', '')


def substitute_tags(html: str, prospect_data: dict) -> str:
    for tag, fn in _TAG_MAP.items():
        html = html.replace(tag, escape(fn(prospect_data)))
    return html


def inject_tracking(html: str, cc, site_url: str = SITE_URL, enable_tracking: bool = True):
    """
    Substitutes merge tags, then — when enable_tracking is True — injects the
    open-pixel and wraps links for click tracking.

    enable_tracking should be False wherever SITE_URL doesn't actually resolve
    to a deployment that has the /so/track/ and /so/unsubscribe/ routes live
    (see settings.ENABLE_EMAIL_TRACKING, derived from ENVIRONMENT). Rewriting
    links against a URL nothing is listening on doesn't just fail to track —
    it breaks the links themselves for whoever opens the email, so callers
    must skip it rather than send broken links.

    Returns (modified_html, list_of_SOTrackedLink_instances_to_bulk_create).
    The SOTrackedLink instances are unsaved — caller must bulk_create them.
    """
    from Email_validate_app.models import SOTrackedLink

    unsub_url  = f'{site_url}/so/unsubscribe/{cc.tracking_token}/'
    open_url   = f'{site_url}/so/track/open/{cc.tracking_token}/'
    track_prefix = f'{site_url}/so/track/'

    prospect_data = {
        'first_name': getattr(cc.prospect, 'first_name', '') if cc.prospect else '',
        'last_name':  getattr(cc.prospect, 'last_name',  '') if cc.prospect else '',
        'email':      cc.email,
        'company':    getattr(cc.prospect, 'company', '') if cc.prospect else '',
        'phone':      getattr(cc.prospect, 'phone',   '') if cc.prospect else '',
        'unsubscribe_url': unsub_url,
    }

    html = substitute_tags(html, prospect_data)

    if not enable_tracking:
        return html, []

    tracked_links = []

    def _replace_href(match):
        original = match.group(1)
        if original.startswith(track_prefix) or '/so/unsubscribe/' in original:
            return match.group(0)
        link = SOTrackedLink(campaign_contact=cc, destination_url=original)
        tracked_links.append(link)
        return f'href="{site_url}/so/track/click/{link.token}/"'

    html = _HREF_RE.sub(_replace_href, html)

    footer = (
        f'<div style="text-align:center;padding:16px 0 8px;font-size:12px;color:#999;">'
        f'If you no longer wish to receive these emails, '
        f'<a href="{unsub_url}" style="color:#999;">unsubscribe here</a>.'
        f'</div>'
    )
    pixel = f'<img src="{open_url}" width="1" height="1" alt="" style="display:none;border:0;" />'

    if '</body>' in html:
        html = html.replace('</body>', footer + pixel + '</body>', 1)
    else:
        html += footer + pixel

    return html, tracked_links


def build_message(from_name: str, from_email: str, to_email: str,
                  subject: str, html: str, unsub_url: str, msg_id: str,
                  in_reply_to: str = None, cc_email: str = '') -> MIMEMultipart:
    msg = MIMEMultipart('alternative')
    msg['Message-ID']       = msg_id
    msg['From']             = f'{from_name} <{from_email}>' if from_name else from_email
    msg['To']               = to_email
    # Bcc is deliberately never set as a header — the caller still needs to
    # add bcc addresses to the SMTP envelope recipient list separately.
    if cc_email:
        msg['Cc'] = cc_email
    msg['Subject']          = str(Header(subject, 'utf-8'))
    # Test sends have no unsubscribe URL; emitting `<>` would be a malformed header.
    if unsub_url:
        msg['List-Unsubscribe']      = f'<{unsub_url}>'
        msg['List-Unsubscribe-Post'] = 'List-Unsubscribe=One-Click'
    # Threads a reply under the prospect's message in their mail client.
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
        msg['References']  = in_reply_to
    msg.attach(MIMEText(html, 'html', 'utf-8'))
    return msg


def open_smtp(account) -> smtplib.SMTP:
    plain_pwd = decrypt_password(account)
    ctx = ssl.create_default_context()
    server = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=15)
    server.ehlo()
    server.starttls(context=ctx)
    server.ehlo()
    server.login(account.username, plain_pwd)
    return server
