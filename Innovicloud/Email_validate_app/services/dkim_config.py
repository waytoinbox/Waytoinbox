"""
Configurable DKIM selector lists and ESP fingerprint mappings.
Import COMMON_SELECTORS and ESP_SELECTOR_MAP wherever DKIM auto-discovery is needed.
"""

# Ordered list of common selectors tried during auto-discovery (after ESP-specific ones).
COMMON_SELECTORS = [
    'default',
    'google',
    'selector1',
    'selector2',
    'k1',
    'k2',
    'mail',
    'smtp',
    'email',
    'dkim',
    's1',
    's2',
    'mx',
    'pm',
    'zoho',
    'mailjet',
    'mta',
    'key1',
    'key2',
    'sig1',
    'krs',
    'krs1',
    'krs2',
]

# Maps substrings found in MX hostnames or SPF include domains → candidate selectors.
# Checked BEFORE COMMON_SELECTORS when a provider match is detected.
ESP_SELECTOR_MAP = {
    # Google Workspace
    'aspmx.l.google.com':          ['google'],
    'googlemail.com':               ['google'],
    'google.com':                   ['google'],

    # Microsoft 365 / Exchange Online
    'mail.protection.outlook.com': ['selector1', 'selector2'],
    'outlook.com':                  ['selector1', 'selector2'],

    # Amazon SES
    'amazonses.com':                ['amazon', 'ses', 'dkim'],
    'amazonaws.com':                ['amazon'],

    # Mailchimp / Mandrill
    'mcsv.net':                     ['k1', 'k2'],
    'mailchimpapp.net':             ['k1', 'k2'],
    'mandrillapp.com':              ['mandrill', 'k1', 'k2'],

    # SendGrid
    'sendgrid.net':                 ['s1', 's2', 'smtpapi'],

    # Brevo (Sendinblue)
    'sendinblue.com':               ['mail'],
    'brevo.com':                    ['mail'],

    # Zoho Mail
    'zoho.com':                     ['zoho'],
    'zohomx.com':                   ['zoho'],

    # Mailgun
    'mailgun.org':                  ['smtp', 'email', 'pic'],
    'mailgun.com':                  ['smtp', 'email'],

    # Postmark
    'mtasv.net':                    ['pm', '20210112'],

    # SparkPost / MessageSystems
    'sparkpostmail.com':            ['scph0316', 'scph1220', 'scph0818'],
    'messagesystems.com':           ['scph0316'],

    # Mailjet
    'mailjet.com':                  ['mailjet'],

    # ActiveCampaign
    'activecampaign.com':           ['k1', 'k2'],

    # HubSpot
    'hubspotemail.net':             ['hs1', 'hs2'],

    # Constant Contact
    'constantcontact.com':          ['ccsend'],

    # Klaviyo
    'klaviyomail.com':              ['klaviyo'],
}
