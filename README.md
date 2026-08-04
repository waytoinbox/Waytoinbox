# Innovicloud

A Django-based email deliverability and campaign management platform. It combines email validation tooling with a full bulk email sending system backed by AWS SES and Celery.

---

## Features

### Email Validation
- **Single Email Verify** — real-time MX record lookup, SMTP check, and deliverability scoring with per-record history
- **Bulk Email Verify** — upload a CSV/list and validate thousands of addresses asynchronously; results downloadable as a report
- **Email Header Analysis** — paste raw email headers to inspect SPF/DKIM/DMARC authentication results, routing hops, and delivery delays

### Domain & IP Security
- **DMARC Domain Checker** — checks DMARC, SPF, and DKIM records for any domain; DKIM lookup supports manual selector input and intelligent ESP auto-discovery (Google Workspace, Microsoft 365, Amazon SES, Mailchimp, SendGrid, and more)
- **Domain Reputation Analysis** — queries public reputation databases and blocklists for a domain
- **IP Blocklist Monitor** — monitors one or more IP addresses against major DNSBLs and alerts on listing changes
- **Domain Blocklist Monitor** — same monitoring for domains

### Email Campaigns
- **Campaign Builder** — drag-and-drop template editor (stored as HTML + design JSON); supports multiple sender identities verified via AWS SES
- **Contact Lists** — import and manage subscriber lists with subscription status tracking (subscribed / unsubscribed / never-subscribed)
- **Send Immediately or Schedule** — campaigns can be sent on demand or scheduled at a future date/time with a per-campaign IANA timezone selector
- **Async delivery** — campaign emails are dispatched via Celery workers (not in the HTTP request); Redis locks prevent duplicate sends across Beat ticks
- **AWS SES integration** — uses `send_raw_email` to attach `List-Unsubscribe` and `List-Unsubscribe-Post` MIME headers
- **Unsubscribe handling** — signed tokens, one-click unsubscribe, automatic list status update
- **Test sends** — send a draft to up to 5 addresses before launching
- **CloudWatch sync** — campaign delivery metrics pulled from AWS CloudWatch after send

### Platform
- **Credits system** — per-user credit balance for paid operations; subscription plans via Razorpay
- **Sender Verify** — confirm ownership of From addresses through SES token email flow
- **REST API** — API key authentication for external callers (email validation, domain checks, blocklist queries)
- **Dashboard** — usage summary, recent activity, credit balance
- **User profiles** — timezone preference, company details, notification settings

---

## Tech Stack

| Layer | Technology |
|---|---|
| Web framework | Django 6.0.3 |
| Database | MySQL (mysqlclient) |
| Task queue | Celery + Redis (broker DB 0, results DB 1, cache/locks DB 2) |
| Email delivery | AWS SES (`send_raw_email`) |
| DNS lookups | dnspython 2.8.0 |
| Scheduler | Celery Beat with `DatabaseScheduler` |
| Crypto / key parsing | cryptography 46.0.5 |
| PDF export | xhtml2pdf, ReportLab |
| Payments | Razorpay |
| Frontend | Vanilla JS + custom CSS (no frontend build step) |

---

## Project Structure

```
Innovicloud/
├── Innovicloud/                  # Django project settings, URLs, Celery app
│   └── logs/                     # Rotating log files (gitignored except .gitkeep)
│       ├── app.log               # All requests and INFO+ events
│       ├── errors.log            # ERROR+ events across all modules
│       └── tasks.log             # Celery task activity
├── Email_validate_app/
│   ├── models.py                 # All models (User, Campaign, CampaignList, etc.)
│   ├── middleware.py             # RequestID, RequestLogging, UnhandledException, SessionExpiry
│   ├── utils.py                  # Shared helpers
│   ├── views/
│   │   ├── errors.py             # handler400/403/404/500 + csrf_failure
│   │   ├── auth.py               # Login, register, logout
│   │   ├── dashboard.py          # Home dashboard
│   │   ├── email_validation.py   # Single & bulk validation views
│   │   ├── campaigns.py          # Campaign CRUD and send
│   │   ├── contacts.py           # Contact list management
│   │   ├── templates.py          # Email template CRUD
│   │   ├── dmarc.py              # DMARC/SPF/DKIM checker
│   │   ├── blocklist.py          # Domain & IP blocklist monitor
│   │   ├── reputation.py         # Domain reputation analysis
│   │   ├── sender_verify.py      # SES sender identity verification
│   │   ├── subscription.py       # Plans and credit purchases
│   │   ├── billing.py            # Billing history
│   │   ├── profile.py            # User settings
│   │   └── api.py                # Public REST API (API key auth)
│   ├── services/
│   │   ├── errors.py             # error_response() / success_response() helpers
│   │   ├── mailer.py             # All transactional email sending
│   │   ├── email_validation.py   # Core validation logic
│   │   └── campaign_sender.py    # AWS SES send_raw_email wrapper
│   ├── tasks/
│   │   ├── base.py               # LoggedTask — on_failure/on_retry hooks for all tasks
│   │   ├── verify_emails.py      # Bulk validation task chain
│   │   ├── send_scheduled_campaigns.py  # Campaign dispatch
│   │   ├── scheduler_job.py      # Beat-scheduled jobs
│   │   ├── update_reputations.py # Scheduled reputation refresh
│   │   └── sync_campaigns_cloudwatch.py # CloudWatch metrics sync
│   ├── templates/
│   │   ├── errors/               # 400, 403, 404, 500 error pages
│   │   ├── partials/             # Reusable template fragments
│   │   └── i_*.html              # Page templates
│   └── static/
│       └── Email_validate_app/
│           └── js/
│               ├── wti_toast.js  # WTI unified toast + fetch helper
│               └── wti_builder.js
└── requirements.txt
```

---

## Getting Started

### Prerequisites
- Python 3.11+
- MySQL
- Redis

### Setup

```bash
# Clone and create virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp Innovicloud/.env.example Innovicloud/.env
# Edit .env — set DB credentials, Redis URL, AWS credentials, SECRET_KEY, SITE_URL

# Apply migrations
cd Innovicloud
python manage.py migrate

# Verify no issues
python manage.py check

# Start development server
python manage.py runserver
```

### Running Background Workers

```bash
# Celery worker (email sending, validation tasks)
celery -A Innovicloud worker -l info

# Celery Beat (scheduled campaigns, blocklist monitoring)
celery -A Innovicloud beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler
```

---

## Environment Variables

| Variable | Description |
|---|---|
| `SECRET_KEY` | Django secret key |
| `DEBUG` | `True` / `False` |
| `DATABASE_URL` | MySQL connection string |
| `REDIS_URL` | Redis connection (default: `redis://localhost:6379`) |
| `AWS_SES_ACCESS_KEY_ID` | AWS access key for SES |
| `AWS_SES_SECRET_ACCESS_KEY` | AWS secret key for SES |
| `AWS_SES_REGION` | SES region (e.g. `us-east-1`) |
| `AWS_SES_CONFIGURATION_SET` | SES configuration set name |
| `SITE_URL` | Public base URL (used in unsubscribe links) |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | Payment gateway credentials |

---

## Error Handling & Logging

### Middleware stack (in order)

| Middleware | What it does |
|---|---|
| `RequestIDMiddleware` | Assigns a UUID to every request (`request.request_id`); adds `X-Request-ID` response header |
| `RequestLoggingMiddleware` | Logs method, path, status, and duration for every non-static request |
| `UnhandledExceptionMiddleware` | Catches any exception that escapes a view; returns JSON for API paths, custom 500 page for browsers |
| `SessionExpiryMiddleware` | Converts login redirects (302 → `/login/`) on AJAX requests to JSON `401` so fetch callers don't silently receive login-page HTML |

### Log files

All logs are written to `Innovicloud/logs/` with 10 MB rotation and 5 backups.

| File | Contents |
|---|---|
| `app.log` | Every request + INFO/DEBUG events from views and services |
| `errors.log` | ERROR and above from all modules |
| `tasks.log` | Celery task start, retry, failure events |

Tail logs live during development:

```powershell
# Windows
Get-Content Innovicloud\logs\errors.log -Tail 50 -Wait

# macOS / Linux
tail -f Innovicloud/logs/errors.log
```

Each log line includes `rid=<uuid>` — use it to trace a single request across all three files.

### Custom error pages

| Code | Template | Trigger |
|---|---|---|
| 400 | `errors/400.html` | Bad request |
| 403 | `errors/403.html` | Forbidden; also used for CSRF failures |
| 404 | `errors/404.html` | Page not found |
| 500 | `errors/500.html` | Unhandled server error |

All handlers return JSON automatically when the request sets `Accept: application/json`, `X-Requested-With: XMLHttpRequest`, or targets a `/api/` path.

### Celery task errors

Every task uses `base=LoggedTask` (defined in `tasks/base.py`). On permanent failure (after all retries), `on_failure` logs to `tasks.log` at ERROR level and sends an admin failure alert email.

### Frontend toasts (`WTI`)

All user-facing error messages are shown via the `WTI` namespace loaded from `wti_toast.js`:

```javascript
WTI.toast('Something went wrong', 'error');          // manual
WTI.toast('Saved!', 'success');

// CSRF-aware fetch with automatic error toasts
const { ok, data } = await WTI.apiFetch('/api/validate/', {
  method: 'POST',
  body: JSON.stringify({ email }),
});
```

Session expiry is handled automatically: `apiFetch` detects a `401 { code: "unauthorized" }` response, shows a warning toast, and redirects to `/login/` after 1.2 seconds.
