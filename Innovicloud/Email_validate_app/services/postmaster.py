"""
Google Postmaster Tools API wrapper.
Fetches traffic stats for a single domain and returns a list of dicts.

Credentials are loaded from .env variables:
  GOOGLE_TOKEN_JSON       — full contents of token.json  (JSON string)
  GOOGLE_CREDENTIALS_JSON — full contents of credentials.json (JSON string)

Each returned dict contains:
  {
      "date":               "20240601",   # YYYYMMDD string
      "spam_rate":          0.001,        # float or None
      "domain_reputation":  "HIGH",       # str or None
      "ip_reputation":      [...],        # list (raw API value)
      "delivery_errors":    [...],        # list (raw API value)
  }

Returns an empty list if the domain is not registered, has no stats,
or any auth/API error occurs (errors are logged, not raised).
"""

import json
import logging
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/postmaster.readonly']


def _get_service():
    """
    Build an authenticated Postmaster Tools API service.
    Reads token from GOOGLE_TOKEN_JSON env var.
    Auto-refreshes the access token when expired using the stored refresh_token.
    """
    token_json = os.environ.get('GOOGLE_TOKEN_JSON', '').strip()
    if not token_json:
        raise EnvironmentError(
            "GOOGLE_TOKEN_JSON is not set in .env. "
            "Paste the full contents of your token.json there."
        )

    token_data = json.loads(token_json)
    creds = Credentials(
        token=token_data.get('token'),
        refresh_token=token_data.get('refresh_token'),
        token_uri=token_data.get('token_uri', 'https://oauth2.googleapis.com/token'),
        client_id=token_data.get('client_id'),
        client_secret=token_data.get('client_secret'),
        scopes=token_data.get('scopes', SCOPES),
    )

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            logger.info("Google Postmaster token refreshed successfully.")
        else:
            raise RuntimeError(
                "Google credentials are invalid and cannot be refreshed. "
                "Re-run the OAuth flow and update GOOGLE_TOKEN_JSON in .env."
            )

    return build('gmailpostmastertools', 'v1beta1', credentials=creds)


def fetch_domain_traffic_stats(domain: str) -> list:
    """
    Fetch all available traffic stats for *domain* from Postmaster Tools.

    Returns a list of stat dicts (may be empty).
    Never raises — errors are logged and an empty list is returned.
    """
    try:
        service = _get_service()
        parent  = f'domains/{domain}'

        domains_resp = service.domains().list().execute()
        registered   = {d['name'] for d in domains_resp.get('domains', [])}
        if parent not in registered:
            logger.info("Domain '%s' not found in Postmaster Tools.", domain)
            return []

        rows    = []
        request = service.domains().trafficStats().list(parent=parent)

        while request is not None:
            response = request.execute()

            for item in response.get('trafficStats', []):
                date_str = item.get('name', '').split('/')[-1]

                rows.append({
                    'date':              date_str,
                    'spam_rate':         item.get('userReportedSpamRatio'),
                    'domain_reputation': item.get('domainReputation'),
                    'ip_reputation':     item.get('ipReputations', []),
                    'delivery_errors':   item.get('deliveryErrors', []),
                })

            request = service.domains().trafficStats().list_next(
                previous_request=request,
                previous_response=response,
            )

        return rows

    except HttpError as exc:
        logger.error("Postmaster Tools HttpError for domain '%s': %s", domain, exc)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error fetching Postmaster stats for '%s': %s", domain, exc)
        return []
