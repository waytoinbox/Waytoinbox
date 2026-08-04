from django.conf import settings


def get_provider():
    """Return the configured EmailProvider instance (SES or Mailgun)."""
    provider = getattr(settings, 'EMAIL_PROVIDER', 'ses').lower()
    if provider == 'mailgun':
        from .mailgun_provider import MailgunProvider
        return MailgunProvider()
    from .ses_provider import SESProvider
    return SESProvider()
