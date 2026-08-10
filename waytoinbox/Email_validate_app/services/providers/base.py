from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, List


@dataclass
class SendResult:
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None


@dataclass
class DomainStatus:
    verified: bool
    dkim_status: str       # 'PENDING' | 'SUCCESS' | 'FAILED'
    dns_records: List[dict]  # [{'record_type', 'name', 'value'}]


class EmailProvider(ABC):

    @abstractmethod
    def send_raw(self, source: str, destination: str, mime_bytes: bytes,
                 tags: dict = None) -> SendResult:
        """Send one pre-built MIME email.
        tags = {'campaign_id': '42', 'test': '1', ...}
        Returns SendResult with success flag, optional message_id, optional error string.
        """

    @abstractmethod
    def create_domain(self, domain: str) -> dict:
        """Register a domain for sending.
        Returns {'dns_records': [{'record_type', 'name', 'value'}], 'status': str}
        """

    @abstractmethod
    def get_domain_status(self, domain: str) -> DomainStatus:
        """Check verification status of a previously registered domain."""

    def create_email_identity(self, email: str) -> None:
        """Register an individual email address. Default no-op — providers that
        verify domains (e.g. Mailgun) do not need per-address registration."""

    @abstractmethod
    def fetch_campaign_events(self, campaign_id: int, start_dt, end_dt) -> list:
        """Return events for a campaign in the given time window.
        Each item is a dict: {event_type, email, message_id, timestamp}
        event_type values: send | delivery | open | click | bounce | complaint | unsubscribe
        """
