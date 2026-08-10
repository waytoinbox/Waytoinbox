"""
filter_status.py v1.0
Canonical status option lists for filter dropdowns.
Each entry is a (db_value, display_label) tuple.
Import the relevant list in your view and pass it to the template context as `pf_statuses`.
"""

CAMPAIGN_STATUSES = [
    ('draft',     'Draft'),
    ('scheduled', 'Scheduled'),
    ('sending',   'Sending'),
    ('sent',      'Sent'),
    ('failed',    'Failed'),
    ('cancelled', 'Cancelled'),
]

EMAIL_VALIDATE_STATUSES = [
    ('Valid',   'Valid'),
    ('Invalid', 'Invalid'),
    ('Risky',   'Risky'),
    ('Others',  'Others'),
]

CONTACT_STATUSES = [
    ('subscribed',       'Subscribed'),
    ('unsubscribed',     'Unsubscribed'),
    ('never_subscribed', 'Never Subscribed'),
]

SENDER_DOMAIN_STATUSES = [
    ('verified',  'Verified'),
    ('pending',   'Pending'),
    ('migrating', 'Migrating'),
]

SENDER_EMAIL_STATUSES = [
    ('verified', 'Verified'),
    ('pending',  'Pending'),
]

REPUTATION_STATUSES = [
    ('verified',   'Verified'),
    ('unverified', 'Unverified'),
]

BLOCKLIST_STATUSES = [
    ('clean',  'Clean'),
    ('listed', 'Listed'),
]

CAMPAIGN_EVENT_STATUSES = [
    ('send',        'Sent'),
    ('delivery',    'Delivered'),
    ('open',        'Opened'),
    ('click',       'Clicked'),
    ('bounce',      'Bounced'),
    ('complaint',   'Complaint'),
    ('unsubscribe', 'Unsubscribed'),
    ('reject',      'Rejected'),
]
