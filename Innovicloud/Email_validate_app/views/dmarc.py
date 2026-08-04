import logging

from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.contrib import messages
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)

from Email_validate_app.models import CurrentCredits, SubsPayment, DMARCAnalysis
from Email_validate_app.utils import get_user_id
from Email_validate_app.services.credit_manager import get_ac_current_credit, deduct_ac_credits
from Email_validate_app.services.email_analyzer import ProfessionalEmailAnalyzer
from Email_validate_app.services.monitor import ip_blacklists, domain_blacklists
from Email_validate_app.services.dmarc_checker import (
    get_txt_record, check_spf, check_dmarc, check_dkim,
    check_dkim_auto, _detect_esp_selectors,
)


def domain_validate(request):
    """Handle domain validation form."""
    if request.method == "POST":
        domain = request.POST.get("domain_")
        dkim_selector = request.POST.get("dkim_selector", "").strip()

        spf_result   = check_spf(domain)
        dmarc_result = check_dmarc(domain)
        dkim_result  = check_dkim(domain, dkim_selector) if dkim_selector else check_dkim_auto(domain)

        context = {
            "domain": domain,
            "spf_result": spf_result,
            "dmarc_result": dmarc_result,
            "dkim_result": dkim_result,
            "active_tab": "dmarc",
        }
        return render(request, "i_home.html", context)

    return render(request, "i_home.html")


def DMARC_check(request):
    """Handle domain validation form. DMARC check is free — no credits deducted."""
    user_id = get_user_id(request)
    ip_current = get_ac_current_credit(user_id) if user_id else 0

    if request.method == "POST":
        domain = request.POST.get("_domain_")
        dkim_selector = request.POST.get("dkim_selector", "").strip()

        spf_result   = check_spf(domain)
        dmarc_result = check_dmarc(domain)
        dkim_result  = check_dkim(domain, dkim_selector) if dkim_selector else check_dkim_auto(domain)

        if user_id and domain:
            try:
                dmarc_record_str = dmarc_result.get('record', '') or ''
                dmarc_tags = {}
                for _tag in dmarc_record_str.split(';'):
                    _tag = _tag.strip()
                    if '=' in _tag:
                        k, v = _tag.split('=', 1)
                        dmarc_tags[k.strip()] = v.strip()

                DMARCAnalysis.objects.create(
                    user_id=user_id,
                    domain=domain,
                    # SPF
                    spf_status=spf_result.get('status', 'fail'),
                    spf_record=spf_result.get('record'),
                    spf_includes=spf_result.get('includes', []),
                    # DMARC
                    dmarc_status=dmarc_result.get('status', 'fail'),
                    dmarc_record=dmarc_record_str or None,
                    dmarc_policy=dmarc_result.get('policy'),
                    dmarc_subdomain_policy=dmarc_result.get('subdomain_policy'),
                    dmarc_alignment_spf=dmarc_tags.get('aspf'),
                    dmarc_alignment_dkim=dmarc_tags.get('adkim'),
                    dmarc_rua=(dmarc_result.get('reporting') or {}).get('rua'),
                    dmarc_ruf=(dmarc_result.get('reporting') or {}).get('ruf'),
                    # DKIM
                    dkim_status=(dkim_result or {}).get('status', 'fail'),
                    dkim_selector=(dkim_result or {}).get('selector'),
                    dkim_record=(dkim_result or {}).get('record'),
                    # Overall
                    analysis_result={
                        'spf':   spf_result.get('status') == 'pass',
                        'dmarc': dmarc_result.get('status') == 'pass',
                        'dkim':  (dkim_result or {}).get('status') == 'pass',
                    },
                    error_message=dmarc_result.get('error') or spf_result.get('reason') or None,
                )
            except Exception as e:
                logger.error("DMARCAnalysis save error: %s", e)

        return render(request, "i_dmarc_check.html", {
            "domain":       domain,
            "spf_result":   spf_result,
            "dmarc_result": dmarc_result,
            "dkim_result":  dkim_result,
            "active_tab":   "dmarc",
            "credits":      ip_current,
        })

    return render(request, "i_dmarc_check.html", {"credits": ip_current})


def validate_email_input(raw_data):
    if not raw_data:
        return False, "Input is empty"
    if len(raw_data.strip()) < 30:
        return False, "Input too short"
    required_signs = ["From:", "Received:", "Subject:", "Date:"]
    found = sum(1 for field in required_signs if field.lower() in raw_data.lower())
    if found < 2:
        return False, "Invalid email/header format"
    return True, "Valid"


def Header_Analysis(request):
    if not request.session.get("logged_in"):
        messages.warning(request, "You need to login first.")
        return redirect(reverse("login"))

    results = None
    user_id = get_user_id(request)

    if request.method == "POST":
        raw_text = request.POST.get("header", "").strip()
        upload_file = request.FILES.get("header_file")
        final_email = ""

        if upload_file:
            if upload_file.size > 50 * 1024 * 1024:
                messages.error(request, "File exceeds 50MB")
            elif not upload_file.name.lower().endswith(".txt"):
                messages.error(request, "Only .txt file allowed")
            else:
                try:
                    final_email = upload_file.read().decode("utf-8", errors="ignore").strip()
                except Exception as e:
                    messages.error(request, f"File read error: {str(e)}")
        elif raw_text:
            final_email = raw_text
        else:
            messages.error(request, "Paste email/header or upload file")

        if final_email:
            valid, msg = validate_email_input(final_email)
            if not valid:
                messages.error(request, msg)
            else:
                try:
                    analyzer = ProfessionalEmailAnalyzer(
                        final_email,
                        ip_blacklist_checker=ip_blacklists,
                        domain_blacklist_checker=domain_blacklists,
                    )
                    results = analyzer.analyze()
                except Exception as e:
                    messages.error(request, f"Analysis error: {str(e)}")

        if results:
            try:
                deduct_ac_credits(user_id, 1, ref_type='ip_check', description='Header Analysis')
            except Exception as e:
                logger.error("Credit update error: %s", e)

    ac_current = ac_used = 0
    plan_total = 0
    plan_name = plan_valid_till = None
    try:
        cobj = CurrentCredits.objects.filter(user_id=user_id).first()
        if cobj:
            ac_current = cobj.ac_current_credits or 0
            ac_used    = cobj.ac_used_credits    or 0
    except Exception:
        pass
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

    return render(request, "i_header_analysis.html", {
        "results":            results,
        "credits":            ac_current,
        "ac_current_credits": ac_current,
        "ac_total_credits":   plan_total,
        "ac_used_credits":    ac_used,
        "plan_name":          plan_name,
        "plan_valid_till":    plan_valid_till,
    })
