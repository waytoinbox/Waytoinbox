"""
Reset last_cloudwatch_sync for sent Mailgun campaigns that have 0 CampaignEvents,
so the next Celery Beat tick re-scans from sent_at and picks up real event data.

Usage:
    python manage.py reset_mailgun_sync
    python manage.py reset_mailgun_sync --dry-run
"""

from django.core.management.base import BaseCommand
from django.db.models import Count


class Command(BaseCommand):
    help = 'Reset last_cloudwatch_sync for sent campaigns with no event data (Mailgun fix)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show which campaigns would be reset without actually changing anything.',
        )

    def handle(self, *args, **options):
        from Email_validate_app.models import Campaign

        # Find sent campaigns where CampaignEvent count is 0
        campaigns = (
            Campaign.objects
            .filter(status='sent', deleted_at__isnull=True)
            .annotate(event_count=Count('events'))
            .filter(event_count=0)
            .only('id', 'campaign_name', 'sent_at', 'last_cloudwatch_sync')
        )

        total = campaigns.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS('No campaigns need resetting.'))
            return

        self.stdout.write(f'Found {total} sent campaign(s) with 0 events:')
        for c in campaigns:
            self.stdout.write(
                f'  [{c.id}] {c.campaign_name!r}  '
                f'sent_at={c.sent_at}  last_sync={c.last_cloudwatch_sync}'
            )

        if options['dry_run']:
            self.stdout.write(self.style.WARNING('Dry-run — no changes made.'))
            return

        updated = campaigns.update(last_cloudwatch_sync=None)
        self.stdout.write(
            self.style.SUCCESS(
                f'Reset last_cloudwatch_sync=None for {updated} campaign(s). '
                'The next Celery Beat tick will re-sync them from sent_at.'
            )
        )
