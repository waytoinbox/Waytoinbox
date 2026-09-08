"""Centralized free/public email-domain detection for self-service signup.

Used only by Email_validate_app.views.auth.signup() to gate NEW account
creation (see that function for the full flow). Deliberately kept separate
from the disposable/role-based/blacklisted domain checks in
Email_validate_app.tasks.verify_emails, which power the unrelated bulk
email-list verification product — Gmail/Outlook/etc. are perfectly valid
results there and must not be affected by this policy.
"""

FREE_EMAIL_DOMAINS = frozenset({
    'gmail.com', 'googlemail.com',
    'outlook.com', 'hotmail.com', 'live.com', 'msn.com',
    'yahoo.com', 'yahoo.co.in', 'ymail.com',
    'aol.com',
    'icloud.com', 'me.com',
    'protonmail.com', 'proton.me',
    'zoho.com',
    'gmx.com',
    'mail.com',
})


def is_free_email_domain(email: str) -> bool:
    """True if `email`'s domain is a known free/public provider.

    Exact match only (no substring matching) against a lowercased,
    whitespace-stripped domain, so a business domain that merely contains
    one of these names is never a false positive.
    """
    if not email or '@' not in email:
        return False
    domain = email.rsplit('@', 1)[-1].strip().lower()
    return domain in FREE_EMAIL_DOMAINS
