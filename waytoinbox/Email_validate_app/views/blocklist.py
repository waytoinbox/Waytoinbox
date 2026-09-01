import re
import ipaddress
import logging
from datetime import datetime
from urllib.parse import urlparse

import pytz

from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.utils import timezone
from django.db import transaction, IntegrityError, DatabaseError

from Email_validate_app.models import (
    UserTable, CurrentCredits, UsedCredits, SubsPayment, ServiceCredit,
    BlocklistMonitor, Blacklists, BlacklistStatus, BlacklistListed,
    DomainBlocklist, DomainBlacklistListed, DomainBlacklistStatus, DomainBlacklists,
)
from Email_validate_app.utils import get_user_id
from Email_validate_app.services.monitor import ip_blacklists, domain_blacklists
from Email_validate_app.services.credit_manager import (
    get_effective_balance, deduct_service_credits, InsufficientCredits,
)

from .billing import get_current_credit


class _AlreadyMonitored(Exception):
    """Raised inside the add transaction when a concurrent request created
    the same monitor first. Rolls the transaction back so nothing is
    created and nothing is charged; the caller reports the usual
    'already monitored' result.

    Deliberately not a ValueError: the surrounding handler treats those as
    'No Analysis Credits left'.
    """

logger = logging.getLogger(__name__)


def _get_ac_subscription_context(user_id):
    """Return (ac_current, plan_total, ac_used, plan_name, plan_valid_till)."""
    ac_current = ac_used = 0
    try:
        cobj = CurrentCredits.objects.filter(user_id=user_id).first()
        if cobj:
            ac_current = cobj.ac_current_credits or 0
            ac_used    = cobj.ac_used_credits    or 0
    except Exception:
        pass

    plan_total = 0
    plan_name = plan_valid_till = None
    try:
        sub = SubsPayment.objects.filter(
            user_id=user_id, plan_status='Active'
        ).order_by('-payment_time').first()
        if sub:
            plan_name       = sub.subs_plan
            plan_valid_till = sub.valid_time
            plan_total      = int(sub.ac_credits) if sub.ac_credits else 0
    except Exception:
        pass

    if not plan_total:
        try:
            cobj = CurrentCredits.objects.filter(user_id=user_id).first()
            plan_total = (cobj.ac_total_credits or 0) if cobj else 0
        except Exception:
            plan_total = 0

    return ac_current, plan_total, ac_used, plan_name, plan_valid_till


def Blocklist_Monitor(request):
    user_id = get_user_id(request)
    if not user_id:
        return redirect('login')

    from Email_validate_app.services.filter_status import BLOCKLIST_STATUSES
    ac_current, plan_total, ac_used, plan_name, plan_valid_till = _get_ac_subscription_context(user_id)
    # Phase 6 commit 10: show this service's own wallet plus the shared legacy
    # AC pool behind it, not the raw AC column. The two blocklist pages share
    # _get_ac_subscription_context() but need different service balances, so the
    # figure is overridden here rather than inside the helper — the helper still
    # supplies the legacy plan totals below.
    ip_balance = get_effective_balance(user_id, 'ip_blocklist')

    return render(request, "i_ip_blocklist.html", {
        "credits":            ip_balance,
        "ac_current_credits": ip_balance,
        "ac_total_credits":   plan_total,
        "ac_used_credits":    ac_used,
        "plan_name":          plan_name,
        "plan_valid_till":    plan_valid_till,
        "pf_statuses":        BLOCKLIST_STATUSES,
    })


def Domain_Blacklist(request):
    user_id = get_user_id(request)
    if not user_id:
        return redirect('login')

    from Email_validate_app.services.filter_status import BLOCKLIST_STATUSES
    ac_current, plan_total, ac_used, plan_name, plan_valid_till = _get_ac_subscription_context(user_id)
    domain_balance = get_effective_balance(user_id, 'domain_blocklist')

    return render(request, "i_domain_blocklist.html", {
        "credits":            domain_balance,
        "ac_current_credits": domain_balance,
        "ac_total_credits":   plan_total,
        "ac_used_credits":    ac_used,
        "plan_name":          plan_name,
        "plan_valid_till":    plan_valid_till,
        "pf_statuses":        BLOCKLIST_STATUSES,
    })


def check_ip_blacklists(request):
    if request.method == 'POST':
        ip_s = request.POST.get('Ip_s')

        if not ip_s:
            messages.error(request, 'IP is required.')
            return redirect('Blocklist_Monitor')

        # Validate IP format
        try:
            ipaddress.ip_address(ip_s)
        except ValueError:
            messages.error(request, 'Enter a valid IP address.')
            return redirect('Blocklist_Monitor')

        user_id = get_user_id(request)
        try:
            user = UserTable.objects.get(id=user_id)
        except UserTable.DoesNotExist:
            messages.error(request, 'User not found.')
            return redirect('Blocklist_Monitor')

        # Phase 6 commit 6: the balance is the ip_blocklist service wallet plus
        # the legacy AC pool behind it, rather than the raw AC column. That
        # legacy half is still ONE pool shared with Reputation, Header Analyzer
        # and Domain Blocklist — spent in place, never copied here.
        if get_effective_balance(user_id, 'ip_blocklist') <= 0:
            messages.warning(
                request,
                'You have already reached your limit for IP checks. Please extend your limit or update your Plan to continue.'
            )
            return redirect('Blocklist_Monitor')

        try:
            current_datetime = datetime.utcnow().replace(tzinfo=pytz.UTC)
        except Exception as e:
            messages.error(request, f"Invalid timezone: {str(e)}")
            return redirect('Blocklist_Monitor')

        # Check for duplicates (only active records)
        if BlocklistMonitor.objects.filter(user=user, ips=ip_s, is_hidden=False).exists():
            messages.warning(request, f"IP '{ip_s}' is already being monitored.")
        else:
            # Despite this view's name, this branch ADDS a monitor — the charge
            # is for the add, not for the recurring re-check (scheduler_job
            # re-scans every monitored IP nightly and costs nothing).
            #
            # Creation and the charge share one transaction so a failure on
            # either side cannot leave a monitor that was never paid for, or a
            # credit spent with no monitor. The lock is the same row, in the
            # same order, deduct_service_credits() takes moments later, so two
            # simultaneous adds of one IP serialise and the duplicate re-check
            # below is reliable.
            try:
                with transaction.atomic():
                    ServiceCredit.objects.select_for_update().filter(
                        user_id=user_id, service='ip_blocklist',
                    ).first()

                    if BlocklistMonitor.objects.filter(
                        user=user, ips=ip_s, is_hidden=False,
                    ).exists():
                        messages.warning(request, f"IP '{ip_s}' is already being monitored.")
                        return redirect('Blocklist_Monitor')

                    new_entry = BlocklistMonitor.objects.create(
                        user=user,
                        ips=ip_s,
                        created_date=current_datetime
                    )
                    deduct_service_credits(
                        user_id, 'ip_blocklist', 1,
                        ref_type='ip_check', ref_id=ip_s,
                        description='IP Blocklist Check',
                    )
            except InsufficientCredits:
                # Lost a race against another add since the gate above.
                messages.warning(
                    request,
                    'You have already reached your limit for IP checks. Please extend your limit or update your Plan to continue.'
                )
                return redirect('Blocklist_Monitor')

            logger.info("Inserted IP monitor ID: %s", new_entry.ip_id)
            try:
                status = ip_blacklists(ip_s)
                if not isinstance(status, dict):
                    logger.warning(f"Expected dict from check_blacklists, got {type(status)}. IP: {ip_s}")
                    status = {}
            except Exception as e:
                logger.error(f"Error checking blacklists for IP {ip_s}: {e}")
                status = {}

            for blacklist_name, blacklist_status in status.items():
                try:
                    BlacklistStatus.objects.create(
                        ip_id=new_entry.ip_id,
                        ips=ip_s,
                        blacklists_name=blacklist_name,
                        status=blacklist_status
                    )
                    if blacklist_status == 'Listed':
                        BlacklistListed.objects.create(
                            ip_id=new_entry.ip_id,
                            ips=ip_s,
                            blacklists_name=blacklist_name,
                            status=blacklist_status
                        )
                except IntegrityError as e:
                    logger.error(f"IntegrityError while inserting status for IP {ip_s}: {e}")
                except DatabaseError as e:
                    logger.error(f"DatabaseError while inserting status for IP {ip_s}: {e}")
                except Exception as e:
                    logger.error(f"Unexpected error while inserting status for IP {ip_s}: {e}")

            listed_blacklists = [name for name, result in status.items() if result == 'Listed']
            listed_count = len(listed_blacklists)

            logger.info("IP %s listed on %d blacklist(s): %s", ip_s, listed_count, listed_blacklists)

            BlocklistMonitor.objects.filter(ip_id=new_entry.ip_id).update(
                ips=ip_s,
                last_monitor_date=current_datetime,
                listed_count=str(listed_count)
            )

            # (The charge already happened, atomically with the monitor row.)
            messages.success(request, f"IP '{ip_s}' has been successfully added to the monitor.")

        return redirect('Blocklist_Monitor')

    messages.error(request, 'Invalid request method.')
    return redirect('Blocklist_Monitor')


def get_blocklist_data(request):
    user_id = get_user_id(request)

    if not user_id:
        return JsonResponse({'error': 'User not logged in'}, status=401)

    try:
        current_credits = get_current_credit(user_id)
    except Exception as e:
        logger.error("Error fetching credits: %s", e)
        current_credits = 0

    monitors = BlocklistMonitor.objects.filter(user_id=user_id, is_hidden=False).order_by('-created_date')

    data = []
    for monitor in monitors:
        data.append({
            'ip_id': monitor.ip_id,
            'ips': monitor.ips,
            'created_date': monitor.created_date.isoformat() if monitor.created_date else '',
            'deleted_date': monitor.deleted_date.isoformat() if monitor.deleted_date else '',
            'last_monitor_date': monitor.last_monitor_date.isoformat() if monitor.last_monitor_date else '',
            'listed_count': monitor.listed_count if monitor.listed_count is not None else 0
        })

    return JsonResponse({
        'data': data,
        'current_credits': current_credits
    })


def blocklist_names(request):
    ip_id = request.GET.get("ip_id")

    results = (
        BlacklistListed.objects
        .filter(ip_id=ip_id)
        .order_by("-created_date")
        .values("blacklists_name", "created_date")
    )

    data = [
        {
            "blacklists_name": r["blacklists_name"],
            "created_date": r["created_date"].isoformat() if r["created_date"] else None
        }
        for r in results
    ]

    return JsonResponse({"blacklists": data})


def get_domain_blocklist_data(request):
    user_id = get_user_id(request)

    if not user_id:
        return JsonResponse({'error': 'User not logged in'}, status=401)

    try:
        current_credits = get_current_credit(user_id)
    except Exception as e:
        logger.error("Error fetching credits: %s", e)
        current_credits = 0

    monitors = DomainBlocklist.objects.filter(user_id=user_id, is_hidden=False).order_by('-created_date')

    data = []
    for monitor in monitors:
        data.append({
            'domain_id': monitor.domain_id,
            'domain': monitor.domain,
            'created_date': monitor.created_date.isoformat() if monitor.created_date else '',
            'deleted_date': monitor.deleted_date.isoformat() if monitor.deleted_date else '',
            'last_monitor_date': monitor.last_monitor_date.isoformat() if monitor.last_monitor_date else '',
            'listed_count': monitor.listed_count if monitor.listed_count is not None else 0
        })

    return JsonResponse({
        'data': data,
        'current_credits': current_credits
    })


@require_POST
def hide_blocklist_row(request):
    user_id = get_user_id(request)
    if not user_id:
        return JsonResponse({"status": "error", "message": "Login required"}, status=401)
    record_id   = request.POST.get("record_id")
    record_type = request.POST.get("record_type")
    if not record_id or record_type not in ("ip", "domain"):
        return JsonResponse({"status": "error", "message": "Invalid parameters"}, status=400)

    current_datetime = timezone.now()
    if record_type == "ip":
        updated = BlocklistMonitor.objects.filter(ip_id=record_id, user_id=user_id).update(is_hidden=True, deleted_date=current_datetime)
    else:
        updated = DomainBlocklist.objects.filter(domain_id=record_id, user_id=user_id).update(is_hidden=True, deleted_date=current_datetime)
    if updated:
        return JsonResponse({"status": "ok"})
    return JsonResponse({"status": "error", "message": "Record not found"}, status=404)


def domain_blocklist_names(request):
    domain_id = request.GET.get("ip_id")  # keeping same param for frontend compatibility

    if not domain_id:
        return JsonResponse({"error": "ip_id is required"}, status=400)

    try:
        results = (
            DomainBlacklistListed.objects
            .filter(domain_id=domain_id, status="Listed")
            .values("blacklists_name", "created_date")
            .order_by("-created_date")
        )

        data = [
            {
                "blacklists_name": r["blacklists_name"],
                "created_date": r["created_date"].isoformat() if r["created_date"] else None
            }
            for r in results
        ]
        logger.debug("Domain blacklist names for domain_id %s: %s", domain_id, data)

        return JsonResponse({"blacklists": data})

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


def check_domain_blocklist(request):
    if request.method != 'POST':
        messages.error(request, 'Invalid request method.')
        return redirect('Domain_Blacklist')

    domain_s = request.POST.get('domain_s')
    logger.debug("Received domain: %s", domain_s)

    if not domain_s:
        messages.error(request, 'Domain is required.')
        return redirect('Domain_Blacklist')

    # Normalize
    domain_s = domain_s.strip().lower()

    # Remove protocol
    parsed = urlparse(domain_s)
    if parsed.netloc:
        domain_s = parsed.netloc

    # Validate domain
    def is_valid_domain(domain):
        regex = r"^(?!-)(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,}$"
        return re.match(regex, domain)

    if not is_valid_domain(domain_s):
        messages.error(request, 'Enter a valid domain.')
        return redirect('Domain_Blacklist')

    # Get user
    user_id = get_user_id(request)
    try:
        user = UserTable.objects.get(id=user_id)
    except UserTable.DoesNotExist:
        messages.error(request, 'User not found.')
        return redirect('Domain_Blacklist')

    current_datetime = timezone.now()

    # Prevent duplicate (only active records)
    if DomainBlocklist.objects.filter(user=user, domain=domain_s, is_hidden=False).exists():
        messages.warning(request, f"Domain '{domain_s}' already exists.")
        return redirect('Domain_Blacklist')

    # Phase 6 commit 7: despite this view's name it ADDS a monitor — the charge
    # is for the add, not for the recurring re-check (my_second_job re-scans
    # every monitored domain and costs nothing).
    #
    # Creation and the charge now share one transaction; the old order deducted
    # first and created second, so a failed insert burned a credit. The lock is
    # the same row, in the same order (ServiceCredit -> CurrentCredits), that
    # deduct_service_credits() takes, so two simultaneous adds of one domain
    # serialise and the duplicate re-check below is reliable.
    #
    # Both existing error contracts are preserved: InsufficientCredits
    # subclasses ValueError, so an empty balance still produces "No Analysis
    # Credits left.", and any other failure still produces "Error: ...".
    try:
        with transaction.atomic():
            ServiceCredit.objects.select_for_update().filter(
                user_id=user_id, service='domain_blocklist',
            ).first()

            if DomainBlocklist.objects.filter(
                user=user, domain=domain_s, is_hidden=False,
            ).exists():
                raise _AlreadyMonitored(domain_s)

            new_entry = DomainBlocklist.objects.create(
                user=user,
                domain=domain_s,
                created_date=current_datetime,
                last_monitor_date=current_datetime,
                listed_count=0
            )
            deduct_service_credits(
                user_id, 'domain_blocklist', 1,
                ref_type='ip_check', ref_id=domain_s,
                description='Domain Blocklist Check',
            )
    except _AlreadyMonitored:
        # A concurrent request created it first — same result the pre-check
        # above produces.
        messages.warning(request, f"Domain '{domain_s}' already exists.")
        return redirect('Domain_Blacklist')
    except ValueError:
        messages.warning(request, "No Analysis Credits left.")
        return redirect('Domain_Blacklist')
    except Exception as e:
        messages.error(request, f"Error: {str(e)}")
        return redirect('Domain_Blacklist')

    # Blacklist check
    try:
        logger.debug("Checking domain: %s", domain_s)
        status = domain_blacklists(domain_s)
        logger.debug("Domain blacklist result: %s", status)
    except Exception as e:
        logger.error("Domain blacklist check error: %s", e)
        status = {}

    # Bulk insert
    status_objs = []
    listed_objs = []

    for name, result in status.items():
        status_objs.append(
            DomainBlacklistStatus(
                domain=new_entry,
                domains=domain_s,
                blacklists_name=name,
                status=result
            )
        )

        if result == 'Listed':
            listed_objs.append(
                DomainBlacklistListed(
                    domain=new_entry,
                    domains=domain_s,
                    blacklists_name=name,
                    status=result
                )
            )

    if status_objs:
        DomainBlacklistStatus.objects.bulk_create(status_objs)

    if listed_objs:
        DomainBlacklistListed.objects.bulk_create(listed_objs)

    # Count listed
    listed_count = sum(1 for v in status.values() if v == 'Listed')

    DomainBlocklist.objects.filter(domain_id=new_entry.domain_id).update(
        listed_count=listed_count,
        last_monitor_date=current_datetime
    )

    messages.success(request, f"Domain '{domain_s}' added successfully.")

    return redirect('Domain_Blacklist')


def add_to_monitors(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)
    user_id = get_user_id(request)
    if not user_id:
        return JsonResponse({'status': 'error', 'message': 'Login required'}, status=401)
    try:
        user = UserTable.objects.get(id=user_id)
    except UserTable.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'User not found'}, status=404)
    domain_s = (request.POST.get('domain') or '').strip().lower()
    ip_s     = (request.POST.get('ip') or '').strip()
    if not domain_s and not ip_s:
        return JsonResponse({'status': 'error', 'message': 'No domain or IP provided'}, status=400)
    current_datetime = timezone.now()
    domain_result = None
    ip_result     = None
    credits_used  = 0
    if domain_s:
        parsed = urlparse(domain_s)
        if parsed.netloc:
            domain_s = parsed.netloc
        domain_regex = r"^(?!-)(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,}$"
        if not re.match(domain_regex, domain_s):
            domain_result = {'status': 'error', 'message': "'" + domain_s + "' is not a valid domain"}
        elif DomainBlocklist.objects.filter(user=user, domain=domain_s, is_hidden=False).exists():
            domain_result = {'status': 'duplicate', 'message': "'" + domain_s + "' is already monitored"}
        else:
            try:
                # Create and charge together. The old order deducted first and
                # created second, so a failed insert burned the credit.
                # InsufficientCredits subclasses ValueError, so the existing
                # `except ValueError` below still yields 'No Analysis Credits
                # left'.
                with transaction.atomic():
                    ServiceCredit.objects.select_for_update().filter(
                        user_id=user_id, service='domain_blocklist',
                    ).first()

                    if DomainBlocklist.objects.filter(
                        user=user, domain=domain_s, is_hidden=False,
                    ).exists():
                        raise _AlreadyMonitored(domain_s)

                    entry = DomainBlocklist.objects.create(
                        user=user, domain=domain_s,
                        created_date=current_datetime,
                        last_monitor_date=current_datetime,
                        listed_count=0
                    )
                    deduct_service_credits(
                        user_id, 'domain_blocklist', 1,
                        ref_type='ip_check', ref_id=domain_s,
                        description='Domain Monitor Add',
                    )
                try:
                    bl_status = domain_blacklists(domain_s)
                except Exception:
                    bl_status = {}
                status_objs = []
                listed_objs = []
                listed_count = 0
                for name, res in (bl_status.items() if isinstance(bl_status, dict) else []):
                    status_objs.append(DomainBlacklistStatus(domain=entry, domains=domain_s, blacklists_name=name, status=res))
                    if res == 'Listed':
                        listed_objs.append(DomainBlacklistListed(domain=entry, domains=domain_s, blacklists_name=name, status=res))
                        listed_count += 1
                if status_objs: DomainBlacklistStatus.objects.bulk_create(status_objs)
                if listed_objs: DomainBlacklistListed.objects.bulk_create(listed_objs)
                DomainBlocklist.objects.filter(domain_id=entry.domain_id).update(listed_count=listed_count, last_monitor_date=current_datetime)
                credits_used += 1
                domain_result = {'status': 'added', 'message': "'" + domain_s + "' added to Domain Monitor", 'listed_count': listed_count}
            except _AlreadyMonitored:
                # A concurrent request won the race and created it first.
                domain_result = {'status': 'duplicate', 'message': "'" + domain_s + "' is already monitored"}
            except ValueError:
                domain_result = {'status': 'error', 'message': 'No Analysis Credits left'}
            except Exception as e:
                domain_result = {'status': 'error', 'message': str(e)}
    if ip_s and ip_s != 'Not Found':
        try:
            import ipaddress as _ipa
            _ipa.ip_address(ip_s)
        except ValueError:
            ip_result = {'status': 'error', 'message': "'" + ip_s + "' is not a valid IP address"}
        else:
            if BlocklistMonitor.objects.filter(user=user, ips=ip_s, is_hidden=False).exists():
                ip_result = {'status': 'duplicate', 'message': "'" + ip_s + "' is already monitored"}
            else:
                try:
                    # Create and charge together. The old order deducted first
                    # and created second, so a failed insert burned the credit.
                    # InsufficientCredits subclasses ValueError, so the existing
                    # `except ValueError` below still produces the same
                    # 'No Analysis Credits left' result.
                    with transaction.atomic():
                        ServiceCredit.objects.select_for_update().filter(
                            user_id=user_id, service='ip_blocklist',
                        ).first()

                        if BlocklistMonitor.objects.filter(
                            user=user, ips=ip_s, is_hidden=False,
                        ).exists():
                            raise _AlreadyMonitored(ip_s)

                        entry = BlocklistMonitor.objects.create(
                            user=user, ips=ip_s,
                            created_date=current_datetime
                        )
                        deduct_service_credits(
                            user_id, 'ip_blocklist', 1,
                            ref_type='ip_check', ref_id=ip_s,
                            description='IP Monitor Add',
                        )
                    try:
                        bl_status = ip_blacklists(ip_s)
                    except Exception:
                        bl_status = {}
                    listed_count = 0
                    for bname, bstatus in (bl_status.items() if isinstance(bl_status, dict) else []):
                        try:
                            BlacklistStatus.objects.create(ip_id=entry.ip_id, ips=ip_s, blacklists_name=bname, status=bstatus)
                            if bstatus == 'Listed':
                                BlacklistListed.objects.create(ip_id=entry.ip_id, ips=ip_s, blacklists_name=bname, status=bstatus)
                                listed_count += 1
                        except Exception:
                            pass
                    BlocklistMonitor.objects.filter(ip_id=entry.ip_id).update(listed_count=str(listed_count), last_monitor_date=current_datetime)
                    credits_used += 1
                    ip_result = {'status': 'added', 'message': "'" + ip_s + "' added to IP Monitor", 'listed_count': listed_count}
                except _AlreadyMonitored:
                    # A concurrent request won the race and created it first.
                    # Same result the pre-check above would have produced.
                    ip_result = {'status': 'duplicate', 'message': "'" + ip_s + "' is already monitored"}
                except ValueError:
                    ip_result = {'status': 'error', 'message': 'No Analysis Credits left'}
                except Exception as e:
                    ip_result = {'status': 'error', 'message': str(e)}
    # Phase 6 commit 10: this endpoint is called only from the Header Analyzer
    # page (i_header_analysis.html), whose credit bar is rendered from
    # header_analysis's effective balance and then refreshed from this field.
    # It therefore has to report the same metric, or the bar would change
    # meaning between page load and refresh.
    #
    # A request can add an IP and a domain at once, so no single per-monitor
    # figure would be correct anyway; what the bar tracks is the shared pool
    # this page spends from, which an added monitor also draws on.
    try:
        remaining = get_effective_balance(user_id, 'header_analysis')
    except Exception:
        remaining = None
    return JsonResponse({
        'status': 'ok',
        'domain': domain_result,
        'ip': ip_result,
        'credits_used': credits_used,
        'ac_current_credits': remaining,
    })
