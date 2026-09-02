from collections import defaultdict
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import dns.resolver
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from Email_validate_app.models import (
    Blacklists, BlocklistMonitor,
    DomainBlacklists, DomainBlocklist, UserTable,
    DomainBlacklistStatus, DomainBlacklistListed,
)


class AlreadyMonitored(Exception):
    """Raised inside a monitor-add transaction when a concurrent request
    created the same monitor first. Rolling back leaves nothing created and
    nothing charged; the caller then reports its usual "already monitored"
    result.

    Deliberately NOT a ValueError: the add paths treat ValueError (which
    InsufficientCredits subclasses) as "No Analysis Credits left", so making
    this one would report a duplicate as a billing failure.

    Lives here rather than in views/blocklist.py because both the web and API
    add paths raise it, and views importing from one another is the kind of
    coupling that makes a view module hard to move.
    """


def get_blacklist_notifications():
    last_24_hours = timezone.now() - timedelta(hours=96)

    user_ips_qs = BlocklistMonitor.objects.filter(
        last_monitor_date__gte=last_24_hours
    ).values('user_id', 'ips', 'last_monitor_date', 'listed_count')

    user_domains_qs = DomainBlocklist.objects.filter(
        last_monitor_date__gte=last_24_hours
    ).values('user_id', 'domain', 'last_monitor_date', 'listed_count')

    all_user_ids = set(
        [e['user_id'] for e in user_ips_qs] +
        [e['user_id'] for e in user_domains_qs]
    )

    email_map = dict(
        UserTable.objects.filter(id__in=all_user_ids).values_list('id', 'user_email')
    )

    grouped = defaultdict(lambda: {
        "ips": [],
        "domains": [],
        "ip_listed_count": 0,
        "domain_listed_count": 0,
    })

    for entry in user_ips_qs:
        uid = entry['user_id']
        grouped[uid]["user_id"] = uid
        grouped[uid]["user_email"] = email_map.get(uid)
        grouped[uid]["ip_listed_count"] += int(entry['listed_count'])
        if int(entry['listed_count']) > 0:
            grouped[uid]["ips"].append(entry['ips'])

    for entry in user_domains_qs:
        uid = entry['user_id']
        grouped[uid]["user_id"] = uid
        grouped[uid]["user_email"] = email_map.get(uid)
        grouped[uid]["domain_listed_count"] += int(entry['listed_count'])
        if int(entry['listed_count']) > 0:
            grouped[uid]["domains"].append(entry['domain'])

    return [
        u for u in grouped.values()
        if u["ip_listed_count"] > 0 or u["domain_listed_count"] > 0
    ]


def send_mail_notification(data):
    for user in data:
        user_email = user.get("user_email")
        user_obj = UserTable.objects.filter(user_email=user_email).only('id', 'notify_blocklist').first()
        ips = user.get("ips", [])
        domains = user.get("domains", [])
        ip_listed_count = user.get("ip_listed_count", 0)
        domain_listed_count = user.get("domain_listed_count", 0)

        ip_rows = "".join(f"""
            <tr>
                <td style="padding:10px 16px;border-bottom:1px solid #f0f0f0;font-size:14px;color:#333;">{ip}</td>
                <td style="padding:10px 16px;border-bottom:1px solid #f0f0f0;text-align:center;">
                    <span style="background:#fff0f0;color:#d93025;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600;">Blacklisted</span>
                </td>
            </tr>""" for ip in ips)

        ip_section = f"""
        <div style="margin-bottom:28px;">
            <h3 style="margin:0 0 12px;font-size:15px;color:#d93025;">🔴 Blacklisted IPs — Detected by {ip_listed_count} Providers</h3>
            <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;border:1px solid #f0f0f0;border-radius:8px;overflow:hidden;">
                <thead>
                    <tr style="background:#fff5f5;">
                        <th style="padding:10px 16px;text-align:left;font-size:13px;color:#888;font-weight:600;">IP Address</th>
                        <th style="padding:10px 16px;text-align:center;font-size:13px;color:#888;font-weight:600;">Status</th>
                    </tr>
                </thead>
                <tbody>{ip_rows}</tbody>
            </table>
        </div>""" if ips else ""

        domain_rows = "".join(f"""
            <tr>
                <td style="padding:10px 16px;border-bottom:1px solid #f0f0f0;font-size:14px;color:#333;">{domain}</td>
                <td style="padding:10px 16px;border-bottom:1px solid #f0f0f0;text-align:center;">
                    <span style="background:#fff0f0;color:#d93025;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600;">Blacklisted</span>
                </td>
            </tr>""" for domain in domains)

        domain_section = f"""
        <div style="margin-bottom:28px;">
            <h3 style="margin:0 0 12px;font-size:15px;color:#d93025;">🔴 Blacklisted Domains — Detected by {domain_listed_count} Providers</h3>
            <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;border:1px solid #f0f0f0;border-radius:8px;overflow:hidden;">
                <thead>
                    <tr style="background:#fff5f5;">
                        <th style="padding:10px 16px;text-align:left;font-size:13px;color:#888;font-weight:600;">Domain</th>
                        <th style="padding:10px 16px;text-align:center;font-size:13px;color:#888;font-weight:600;">Status</th>
                    </tr>
                </thead>
                <tbody>{domain_rows}</tbody>
            </table>
        </div>""" if domains else ""

        html_message = f"""
        <!DOCTYPE html>
        <html>
        <body style="margin:0;padding:0;background:#f4f6f9;font-family:Arial,sans-serif;">
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f9;padding:40px 0;">
                <tr>
                    <td align="center">
                        <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08);">
                            <tr>
                                <td style="background:linear-gradient(135deg,#d93025,#ff6b6b);padding:32px;text-align:center;">
                                    <h1 style="margin:0;color:#fff;font-size:22px;font-weight:700;">⚠️ Blacklist Alert Detected</h1>
                                    <p style="margin:8px 0 0;color:rgba(255,255,255,0.85);font-size:14px;">IP & Domain Blacklist Monitoring System</p>
                                </td>
                            </tr>
                            <tr>
                                <td style="padding:32px;">
                                    <p style="margin:0 0 24px;font-size:15px;color:#444;line-height:1.6;">
                                        Our monitoring system has detected that one or more of your <strong>IPs</strong> and/or <strong>Domains</strong>
                                        have been listed on blacklist providers. Immediate action is recommended.
                                    </p>
                                    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
                                        <tr>
                                            <td width="48%" style="background:#fff5f5;border:1px solid #ffd6d6;border-radius:8px;padding:16px;text-align:center;">
                                                <p style="margin:0;font-size:28px;font-weight:700;color:#d93025;">{ip_listed_count}</p>
                                                <p style="margin:4px 0 0;font-size:13px;color:#888;">Providers Blocking IPs</p>
                                            </td>
                                            <td width="4%"></td>
                                            <td width="48%" style="background:#fff5f5;border:1px solid #ffd6d6;border-radius:8px;padding:16px;text-align:center;">
                                                <p style="margin:0;font-size:28px;font-weight:700;color:#d93025;">{domain_listed_count}</p>
                                                <p style="margin:4px 0 0;font-size:13px;color:#888;">Providers Blocking Domains</p>
                                            </td>
                                        </tr>
                                    </table>
                                    {ip_section}
                                    {domain_section}
                                    <div style="background:#fffbea;border-left:4px solid #f5c518;padding:14px 16px;border-radius:4px;margin-bottom:24px;">
                                        <p style="margin:0;font-size:14px;color:#555;line-height:1.6;">
                                            <strong>Action Required:</strong> Please investigate and delist your IPs and domains to avoid
                                            disruption to your email delivery and network traffic.
                                        </p>
                                    </div>
                                    <p style="margin:0;font-size:14px;color:#666;">
                                        If you need assistance, please contact our support team.
                                    </p>
                                </td>
                            </tr>
                            <tr>
                                <td style="background:#f9f9f9;padding:20px 32px;text-align:center;border-top:1px solid #eee;">
                                    <p style="margin:0;font-size:12px;color:#aaa;">
                                        This is an automated alert from the <strong>Blacklist Monitoring System</strong>.<br/>
                                        Please do not reply to this email.
                                    </p>
                                </td>
                            </tr>
                        </table>
                    </td>
                </tr>
            </table>
        </body>
        </html>
        """

        subject = "⚠️ Blacklist Alert: Your IPs & Domains Have Been Detected"
        email = EmailMultiAlternatives(
            subject=subject,
            body="Your IPs and/or Domains have been blacklisted. Please view this email in an HTML-supported client.",
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[user_email],
        )

        try:
            from Email_validate_app.utils import create_notification
            if user_obj:
                parts = []
                if ips:     parts.append(f"{len(ips)} IP(s) listed")
                if domains: parts.append(f"{len(domains)} domain(s) listed")
                create_notification(
                    user_obj.id, 'blocklist',
                    "Blocklist alert — " + ", ".join(parts) if parts else "Blocklist alert detected",
                    url='/Blocklist_Monitor/'
                )
        except Exception:
            pass

        if not user_obj or user_obj.notify_blocklist:
            email.attach_alternative(html_message, "text/html")
            email.send(fail_silently=False)


def ip_blacklists(ip):
    if not ip or len(ip.split('.')) != 4:
        return f"Error: Invalid IP address '{ip}'"

    dnsbls = list(Blacklists.objects.values_list('Blacklists_name', 'dnsbl_domain'))
    reversed_ip = '.'.join(reversed(ip.split('.')))
    results = {}

    def check_single(name, dnsbl_domain):
        query = f"{reversed_ip}.{dnsbl_domain}"
        try:
            dns.resolver.resolve(query, 'A')
            return name, 'Listed'
        except dns.resolver.NXDOMAIN:
            return name, 'Unlisted'
        except Exception:
            return name, 'Unlisted'

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(check_single, name, d) for name, d in dnsbls]
        for future in as_completed(futures):
            name, status = future.result()
            results[name] = status

    return results


def domain_blacklists(domain):
    if not domain or "." not in domain:
        return f"Error: Invalid domain '{domain}'"

    if "@" in domain:
        domain = domain.split("@")[-1].lower().strip()
    else:
        domain = domain.lower().strip()

    dnsbls = list(DomainBlacklists.objects.values_list('Blacklists_name', 'dnsbl_domain'))
    results = {}

    def check_single(name, dnsbl_domain):
        query = f"{domain}.{dnsbl_domain}"
        try:
            dns.resolver.resolve(query, 'A')
            return name, 'Listed'
        except dns.resolver.NXDOMAIN:
            return name, 'Unlisted'
        except Exception:
            return name, 'Unlisted'

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(check_single, name, d) for name, d in dnsbls]
        for future in as_completed(futures):
            name, status = future.result()
            results[name] = status

    return results
