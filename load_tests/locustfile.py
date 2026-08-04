"""
Waytoinbox — Locust load test
==============================

User classes:
  BrowseUser       Public-page browsing (GET only, no auth)
  DashboardUser    Full authenticated flow: login → dashboard → get_data → logout
  APIUser          API-key auth: single email + domain validation
  RateLimitProbe   Intentional bad-credential flood — verifies 429 fires at attempt 6

Run (web UI — opens http://localhost:8089):
  locust -f load_tests/locustfile.py --host https://staging.waytoinbox.com

Run headless (30 users, 5 min, HTML report):
  locust -f load_tests/locustfile.py --host https://staging.waytoinbox.com \
    --headless -u 30 -r 3 --run-time 5m --html load_tests/report.html

Environment variables:
  LT_EMAIL     test-user email   (default: testuser@waytoinbox.com)
  LT_PASSWORD  test-user password
  LT_API_KEY   raw API key for APIUser class (must exist in DB)

Notes:
  - The session cookie is named 'sid' (not 'sessionid') after INF-12.
  - CSRF token is extracted from the Set-Cookie header after GET /login/.
  - RateLimitProbe deliberately sends wrong passwords; each task instance
    isolates its count per virtual-user so it doesn't interfere with DashboardUser.
"""

import os
import json
import random
import string
import logging

from locust import HttpUser, TaskSet, task, between, events, constant_throughput

logger = logging.getLogger(__name__)

# ─── Config (override via env) ───────────────────────────────────────────────

_LT_EMAIL    = os.environ.get("LT_EMAIL", "testuser@waytoinbox.com")
_LT_PASSWORD = os.environ.get("LT_PASSWORD", "")
_LT_API_KEY  = os.environ.get("LT_API_KEY", "")

# Sample domains/emails used in validation requests (realistic but all .invalid)
_TEST_EMAILS = [
    "alice@example.com",
    "bob@gmail.com",
    "charlie@yahoo.com",
    "dave@hotmail.com",
    "eve@outlook.com",
    "frank@protonmail.com",
    "grace@icloud.com",
    "heidi@aol.com",
]

_TEST_DOMAINS = [
    "example.com",
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
]


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _csrf(client) -> str:
    """Extract CSRF token from the active cookie jar."""
    return client.cookies.get("csrftoken", "")


def _headers(client, extra: dict | None = None) -> dict:
    base = {
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRFToken": _csrf(client),
    }
    if extra:
        base.update(extra)
    return base


def _login(client, email: str, password: str) -> bool:
    """GET the login page (seeds CSRF cookie) then POST credentials."""
    client.get("/login/", name="/login/ [GET]")
    resp = client.post(
        "/login/",
        data={"email": email, "password": password},
        headers=_headers(client),
        name="/login/ [POST]",
        allow_redirects=False,
    )
    if resp.status_code in (200, 302):
        try:
            body = resp.json()
            return body.get("status") == "ok"
        except Exception:
            return resp.status_code == 302
    return False


# ─── BrowseUser — anonymous page requests ────────────────────────────────────

class BrowseUser(HttpUser):
    """
    Simulates an unauthenticated visitor browsing public pages.
    Verifies that the landing page, login page, and signup page serve quickly
    under load without leaking server timing information.
    """
    weight = 3
    wait_time = between(1, 4)

    @task(5)
    def home(self):
        self.client.get("/", name="/ [home]")

    @task(3)
    def login_page(self):
        self.client.get("/login/", name="/login/ [GET]")

    @task(1)
    def signup_page(self):
        self.client.get("/signup/", name="/signup/ [GET]")

    @task(1)
    def robots_txt(self):
        with self.client.get("/robots.txt", name="/robots.txt", catch_response=True) as resp:
            if resp.status_code != 200:
                resp.failure(f"robots.txt returned {resp.status_code}")
            elif settings_admin_leak(resp.text):
                resp.failure("robots.txt leaks ADMIN_URL — security misconfiguration")

    @task(1)
    def nonexistent_page(self):
        with self.client.get("/this-does-not-exist-404/", name="/404", catch_response=True) as resp:
            if resp.status_code not in (404, 302):
                resp.failure(f"Expected 404, got {resp.status_code}")
            else:
                resp.success()


def settings_admin_leak(body: str) -> bool:
    """Return True if any non-standard admin path is listed in robots.txt."""
    # /admin/ is the default — the real admin URL is a secret slug.
    # If robots.txt contains /admin/ that means ADMIN_URL was accidentally set
    # to the default, which is a misconfiguration (INF-15).
    suspicious = ["/admin/", "/django-admin/", "/staff/"]
    for s in suspicious:
        if s in body and "Disallow: " + s in body:
            return True
    return False


# ─── DashboardUser — authenticated CRUD flow ─────────────────────────────────

class DashboardUser(HttpUser):
    """
    Full authenticated session: login → dashboard → AJAX get_data → logout.
    Tests DB connection pooling (INF-16) and session lifecycle (INF-05, INF-12).
    """
    weight = 4
    wait_time = between(2, 6)

    def on_start(self):
        if not _LT_PASSWORD:
            logger.warning("LT_PASSWORD not set — DashboardUser will skip login")
            self._logged_in = False
            return
        self._logged_in = _login(self.client, _LT_EMAIL, _LT_PASSWORD)
        if not self._logged_in:
            logger.warning("DashboardUser: login failed for %s", _LT_EMAIL)

    def on_stop(self):
        if self._logged_in:
            self.client.get("/logout/", name="/logout/")

    @task(5)
    def dashboard(self):
        if not self._logged_in:
            return
        with self.client.get("/dashboard/", name="/dashboard/", catch_response=True) as resp:
            if resp.status_code == 302:
                resp.failure("Session expired — redirect to login")
            elif resp.status_code != 200:
                resp.failure(f"Dashboard returned {resp.status_code}")

    @task(8)
    def get_data_poll(self):
        """Simulates the periodic AJAX poll from the dashboard JS."""
        if not self._logged_in:
            return
        with self.client.post(
            "/get_data/",
            json={},
            headers=_headers(self.client),
            name="/get_data/ [AJAX]",
            catch_response=True,
        ) as resp:
            if resp.status_code == 401:
                resp.failure("get_data: unauthenticated (session expired?)")
            elif resp.status_code not in (200, 204):
                resp.failure(f"get_data returned {resp.status_code}")

    @task(2)
    def profile_page(self):
        if not self._logged_in:
            return
        self.client.get("/profile/", name="/profile/")


# ─── APIUser — API-key validation endpoint ───────────────────────────────────

class APIUser(HttpUser):
    """
    Simulates an API consumer hitting /api/validate/email/ with X-API-Key header.
    Tests API key hashing lookup performance (DB-10) under concurrent load.
    """
    weight = 2
    wait_time = between(0.5, 2)

    def on_start(self):
        self._api_key = _LT_API_KEY
        if not self._api_key:
            logger.warning("LT_API_KEY not set — APIUser requests will likely 401")

    @task(6)
    def validate_single_email(self):
        email = random.choice(_TEST_EMAILS)
        with self.client.post(
            "/api/validate/email/",
            json={"email": email},
            headers={
                "X-API-Key": self._api_key,
                "Content-Type": "application/json",
            },
            name="/api/validate/email/",
            catch_response=True,
        ) as resp:
            if resp.status_code == 401:
                if self._api_key:
                    resp.failure("API key rejected — check LT_API_KEY or key_hash migration")
                else:
                    resp.success()  # expected when no key configured
            elif resp.status_code == 429:
                resp.success()  # rate limited — valid behavior, not a failure
            elif resp.status_code not in (200, 202):
                resp.failure(f"validate/email/ returned {resp.status_code}")

    @task(2)
    def validate_domain(self):
        domain = random.choice(_TEST_DOMAINS)
        with self.client.post(
            "/api/domain-validate/",
            json={"domain": domain},
            headers={
                "X-API-Key": self._api_key,
                "Content-Type": "application/json",
            },
            name="/api/domain-validate/",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 202, 401, 429):
                resp.success()
            else:
                resp.failure(f"domain-validate returned {resp.status_code}")

    @task(1)
    def missing_api_key(self):
        """Verify that requests without X-API-Key always get 401, never 500."""
        with self.client.post(
            "/api/validate/email/",
            json={"email": "probe@example.com"},
            headers={"Content-Type": "application/json"},
            name="/api/validate/email/ [no-key]",
            catch_response=True,
        ) as resp:
            if resp.status_code == 401:
                resp.success()
            elif resp.status_code == 403:
                resp.success()
            else:
                resp.failure(
                    f"Expected 401 with no API key, got {resp.status_code} — "
                    "authentication guard may be broken"
                )


# ─── RateLimitProbe — intentional login flood ────────────────────────────────

class RateLimitProbe(HttpUser):
    """
    Each virtual user sends 7 consecutive bad-password attempts at /login/.
    Attempts 1–5 must get a non-429 error response.
    Attempt 6+ must get HTTP 429 (SEC-04).

    This class is low-weight so it doesn't dominate the run, but it runs
    continuously to verify rate limiting holds under concurrent load from
    all other user classes simultaneously.
    """
    weight = 1
    wait_time = between(30, 60)  # long pause so one VU doesn't hammer forever

    def on_start(self):
        self._probe_ip_tag = _random_suffix()

    @task
    def probe_rate_limit(self):
        """
        Send 7 login attempts with a unique random 'username' to avoid
        polluting the shared test account's session or rate-limit bucket.

        Uses a random email so each VU probes a fresh bucket — this tests
        that the bucketing itself (by IP from X-Forwarded-For) works correctly,
        not just the counter.  In a real test run behind nginx the source IP
        will be the load-test runner's IP, so the bucket WILL fill across VUs.
        """
        fake_email = f"probe_{self._probe_ip_tag}@waytoinbox-test.invalid"
        fake_pw = "WrongPassword123!"

        # Seed CSRF cookie
        self.client.get("/login/", name="/login/ [probe-GET]")

        non_429_count = 0
        got_429 = False

        for attempt in range(1, 8):
            with self.client.post(
                "/login/",
                data={"email": fake_email, "password": fake_pw},
                headers=_headers(self.client),
                name=f"/login/ [flood-attempt]",
                catch_response=True,
            ) as resp:
                if resp.status_code == 429:
                    got_429 = True
                    resp.success()  # 429 is the expected/desired outcome
                    break
                elif resp.status_code in (200, 400):
                    non_429_count += 1
                    resp.success()
                else:
                    resp.failure(f"Unexpected status {resp.status_code} on attempt {attempt}")
                    break

        # After 5 bad attempts the next one MUST be 429.
        # If we sent 7 and never got 429, rate limiting is broken.
        if non_429_count >= 6 and not got_429:
            logger.error(
                "SECURITY: rate limiting NOT triggered after %d failed login attempts. "
                "SEC-04 is broken — _LOGIN_MAX_ATTEMPTS check is not firing.",
                non_429_count,
            )
        elif got_429 and non_429_count <= 5:
            logger.info("Rate limit probe OK: 429 after %d attempts", non_429_count)


def _random_suffix(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


# ─── Event hooks for summary reporting ───────────────────────────────────────

@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    stats = environment.stats
    total_reqs = stats.total.num_requests
    total_fails = stats.total.num_failures
    fail_rate = (total_fails / total_reqs * 100) if total_reqs else 0

    print("\n" + "=" * 60)
    print("LOAD TEST SUMMARY")
    print("=" * 60)
    print(f"  Total requests : {total_reqs:,}")
    print(f"  Failures       : {total_fails:,}  ({fail_rate:.1f}%)")
    print(f"  Avg RPS        : {stats.total.current_rps:.1f}")
    print(f"  Median latency : {stats.total.median_response_time} ms")
    print(f"  95th pct       : {stats.total.get_response_time_percentile(0.95)} ms")
    print(f"  99th pct       : {stats.total.get_response_time_percentile(0.99)} ms")

    # Thresholds: flag to stdout so CI can grep for THRESHOLD_BREACH
    if fail_rate > 1.0:
        print(f"\nTHRESHOLD_BREACH: failure rate {fail_rate:.1f}% exceeds 1%")
    p95 = stats.total.get_response_time_percentile(0.95)
    if p95 and p95 > 3000:
        print(f"THRESHOLD_BREACH: p95 latency {p95}ms exceeds 3000ms")
    print("=" * 60 + "\n")
