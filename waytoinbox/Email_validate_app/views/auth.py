import logging

from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.contrib import messages
from django.urls import reverse
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.contrib.auth.tokens import default_token_generator

logger = logging.getLogger(__name__)
from Email_validate_app.models import UserTable, ListFiles
from Email_validate_app.forms import CustomSignupForm
from django.conf import settings
import json
from Email_validate_app.utils import get_user_id
from Email_validate_app.services.mailer import send_verification_email
from django.utils import timezone
from datetime import timedelta
import secrets
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.views.decorators.http import require_http_methods

from .billing import get_current_credit


def _parse_ua(ua_string):
    """Return (browser, os, device) strings parsed from a User-Agent header."""
    ua = ua_string or ''

    # Try the user-agents library first
    try:
        import user_agents as ua_lib
        parsed = ua_lib.parse(ua)
        browser = parsed.browser.family or ''
        os_name = parsed.os.family or ''
        if parsed.is_mobile:
            device = 'Mobile'
        elif parsed.is_tablet:
            device = 'Tablet'
        elif parsed.is_pc:
            device = 'Desktop'
        else:
            device = 'Unknown'
        # Library returns 'Other' for unrecognised values — fall through to string match
        if browser and browser != 'Other' and os_name and os_name != 'Other':
            return browser, os_name, device
    except Exception:
        pass

    # String-based fallback
    if   'Edg/'   in ua or 'EdgA/' in ua: browser = 'Edge'
    elif 'OPR/'   in ua or 'Opera'  in ua: browser = 'Opera'
    elif 'Chrome' in ua:                   browser = 'Chrome'
    elif 'Firefox'in ua:                   browser = 'Firefox'
    elif 'Safari' in ua:                   browser = 'Safari'
    elif ua:                               browser = 'Browser'
    else:                                  browser = '—'

    if   'Windows' in ua: os_name = 'Windows'
    elif 'Mac OS'  in ua: os_name = 'macOS'
    elif 'Android' in ua: os_name = 'Android'
    elif 'iPhone'  in ua or 'iPad' in ua: os_name = 'iOS'
    elif 'Linux'   in ua: os_name = 'Linux'
    elif ua:              os_name = 'Unknown'
    else:                 os_name = '—'

    if   'Mobile' in ua or 'Android' in ua: device = 'Mobile'
    elif 'iPad'   in ua:                    device = 'Tablet'
    else:                                   device = 'Desktop'

    return browser, os_name, device


def _get_client_ip(request):
    # INF-08: Only peel XFF headers up to the number of trusted proxies in front of gunicorn.
    # Taking the leftmost (attacker-controlled) entry without this check allows
    # any client to forge their IP and bypass per-IP rate limiting.
    from django.conf import settings as _s
    num_proxies = getattr(_s, 'NUM_PROXIES', 0)
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if num_proxies > 0 and xff:
        ips = [ip.strip() for ip in xff.split(',')]
        # The rightmost `num_proxies` entries were added by trusted proxies;
        # the entry just before them is the actual client IP.
        client_index = max(0, len(ips) - num_proxies)
        return ips[client_index] or None
    return request.META.get('REMOTE_ADDR') or None


def _record_login_activity(request, user, status):
    try:
        from Email_validate_app.models import LoginActivity
        ip = _get_client_ip(request)
        ua_string = request.META.get('HTTP_USER_AGENT', '')
        browser, os_name, device = _parse_ua(ua_string)
        LoginActivity.objects.create(
            user=user,
            ip_address=ip,
            browser=browser,
            os=os_name,
            device=device,
            user_agent=ua_string,
            status=status,
        )
    except Exception:
        pass


def services(request):
    if 'logged_in' not in request.session:
        return redirect('login')
    user_id = get_user_id(request)

    current_credits = 0

    if user_id:
        try:
            current_credits = get_current_credit(user_id)
        except Exception as e:
            logger.error("Error fetching credits: %s", e)
            messages.error(request, "An error occurred while fetching your credits. Please try again later.")
            current_credits = 0
    else:
        current_credits = None  # no session services i_bulk_email_verify
    return render(request, 'i_bulk_email_verify.html', {"credits": current_credits})


def logout(request):
    try:
        from Email_validate_app.models import LoginActivity
        from django.utils.timezone import now as tz_now
        user_id = get_user_id(request)
        if user_id:
            last = LoginActivity.objects.filter(
                user_id=user_id, status='success', logout_at__isnull=True
            ).first()
            if last:
                last.logout_at = tz_now()
                last.save(update_fields=['logout_at'])
    except Exception:
        pass
    request.session.flush()
    messages.info(request, "You have been logged out.")
    return redirect('login')


def signup(request):
    if request.method == 'POST':
        ip = _get_client_ip(request)
        if _rate_check(ip, 'signup', 5):
            return JsonResponse({"status": "error", "message": "Too many signup attempts. Please try again later."}, status=429)
        _rate_increment(ip, 'signup', 600)  # 10-minute window

        email = request.POST.get('user_email', '').strip()
        existing = UserTable.objects.filter(user_email=email).first()
        if existing:
            if not existing.is_verified:
                token = default_token_generator.make_token(existing)
                uid = urlsafe_base64_encode(force_bytes(existing.pk))
                verification_link = request.build_absolute_uri(reverse('verify_email', args=[uid, token]))
                send_verification_email(existing.user_email, existing.user_name, verification_link)
                return JsonResponse({"status": "info", "message": "A new verification email has been sent. Please check your inbox."})
            else:
                return JsonResponse({"status": "error", "message": "This email is already registered. Please log in."})

        form = CustomSignupForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.set_password(form.cleaned_data['password'])
            user.is_verified = False
            user.save()

            token = default_token_generator.make_token(user)
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            verification_link = request.build_absolute_uri(reverse('verify_email', args=[uid, token]))
            send_verification_email(user.user_email, user.user_name, verification_link)
            return JsonResponse({"status": "ok", "message": "Signup successful! A verification email has been sent."})
        else:
            errors = {f: e[0] for f, e in form.errors.items()}
            return JsonResponse({"status": "error", "message": "Please fix the errors below.", "errors": errors})
    else:
        form = CustomSignupForm()

    return render(request, "i_signup.html", {"form": form})


_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCKOUT_SECONDS = 900  # 15 minutes


def _rate_check(ip, cache_key, max_attempts):
    """Returns True (blocked) if ip has exceeded max_attempts for this key."""
    from django.core.cache import cache
    return cache.get(f'{cache_key}:{ip}', 0) >= max_attempts


def _rate_increment(ip, cache_key, window_seconds):
    from django.core.cache import cache
    key = f'{cache_key}:{ip}'
    cache.set(key, cache.get(key, 0) + 1, window_seconds)


def _login_rate_check(ip):
    from django.core.cache import cache
    key = f'login_fail:{ip}'
    attempts = cache.get(key, 0)
    return attempts >= _LOGIN_MAX_ATTEMPTS, attempts


def _login_record_failure(ip):
    from django.core.cache import cache
    key = f'login_fail:{ip}'
    attempts = cache.get(key, 0) + 1
    cache.set(key, attempts, _LOGIN_LOCKOUT_SECONDS)


def _login_clear_failures(ip):
    from django.core.cache import cache
    cache.delete(f'login_fail:{ip}')


def login(request):
    if request.method == 'POST':
        ip = _get_client_ip(request)

        # SEC-04: block IPs with too many recent failures
        is_blocked, attempts = _login_rate_check(ip)
        if is_blocked:
            logger.warning("Login blocked for IP %s after %d attempts", ip, attempts)
            return JsonResponse({"status": "error", "message": "Too many failed attempts. Please try again in 15 minutes."}, status=429)

        useremail = request.POST.get('email')
        userpassword = request.POST.get('password')

        try:
            user = UserTable.objects.get(user_email=useremail)
        except UserTable.DoesNotExist:
            _login_record_failure(ip)
            _record_login_activity(request, user=None, status='failed')
            return JsonResponse({"status": "error", "message": "Your email is not registered, please sign up!"})

        if not user.is_verified:
            return JsonResponse({"status": "error", "message": "Please verify your email before logging in."})

        if user.check_password(userpassword):
            _login_clear_failures(ip)
            # INF-05: rotate session key before storing auth data to prevent session fixation
            request.session.cycle_key()
            request.session['logged_in'] = useremail
            request.session['is_admin']  = user.is_admin
            request.session.modified = True
            _record_login_activity(request, user=user, status='success')
            return JsonResponse({"status": "ok", "redirect": reverse('dashboard')})
        else:
            _login_record_failure(ip)
            _record_login_activity(request, user=user, status='failed')
            return JsonResponse({"status": "error", "message": "Invalid email or password."})

    return render(request, 'i_login.html')


@require_http_methods(["GET", "POST"])
def forgot_password(request):
    if request.method == 'POST':
        ip = _get_client_ip(request)
        if _rate_check(ip, 'forgot_pw', 3):
            return JsonResponse({"status": "error", "message": "Too many requests. Please try again in 10 minutes."}, status=429)
        _rate_increment(ip, 'forgot_pw', 600)  # 10-minute window

        email = request.POST.get('email')

        if not email:
            return JsonResponse({"status": "error", "message": "Please enter your email address."})

        # Always return the same message whether the email exists or not (prevents enumeration).
        _SUCCESS_MSG = "If that email is registered, a reset link has been sent."
        try:
            user = UserTable.objects.filter(user_email=email).first()

            if user:
                reset_token = secrets.token_urlsafe(20)
                reset_token_expiry = timezone.now() + timedelta(hours=1)
                user.reset_token = reset_token
                user.reset_token_expiry = reset_token_expiry
                user.save()

                reset_link = request.build_absolute_uri(
                    reverse('reset_password', kwargs={'token': reset_token})
                )
                email_content = render_to_string('i_password_reset_email.html', {'reset_link': reset_link})

                try:
                    send_mail(
                        subject='Password Reset',
                        message='',
                        from_email=settings.EMAIL_HOST_USER,
                        recipient_list=[email],
                        html_message=email_content,
                        fail_silently=False,
                    )
                except Exception as mail_error:
                    logger.error("Mail sending error: %s", mail_error)

            return JsonResponse({"status": "success", "message": _SUCCESS_MSG})

        except Exception as e:
            logger.error("Unexpected error in forgot_password: %s", e)
            return JsonResponse({"status": "error", "message": "An unexpected error occurred. Please try again later."})

    return render(request, 'i_forgot_password.html')


@require_http_methods(["GET", "POST"])
def reset_password(request, token):
    try:
        user = UserTable.objects.filter(reset_token=token).first()

        if not user:
            messages.error(request, 'The reset token is invalid.')
            return redirect('forgot_password')

        reset_token_expiry = user.reset_token_expiry
        current_datetime = timezone.now()

        if reset_token_expiry < current_datetime:
            messages.error(request, 'The reset token is invalid or has expired.')
            return redirect('forgot_password')

        if request.method == 'POST':
            new_password = request.POST.get('new_password')

            if not new_password:
                return JsonResponse({"status": "error", "message": "Please enter a new password."})

            user.set_password(new_password)
            user.updated_date = current_datetime
            user.reset_token = None
            user.reset_token_expiry = None
            user.save()
            # INF-06: rotate session key so the reset-flow session cannot be replayed
            request.session.cycle_key()

            return JsonResponse({"status": "ok", "message": "Password reset successful. You can now log in.", "redirect": reverse('login')})

    except Exception as e:
        logger.error("Database error in reset_password: %s", e)
        return JsonResponse({"status": "error", "message": "An error occurred. Please try again later."})

    return render(request, 'i_reset_password.html', {'token': token})


def verify_email(request, uidb64, token):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = UserTable.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, UserTable.DoesNotExist):
        user = None

    if user is None:
        messages.error(request, "The verification link is invalid or has expired.")
        return redirect('signup')

    if user.is_verified:
        messages.info(request, "Your email is already verified. Please log in.")
        return redirect('login')

    if default_token_generator.check_token(user, token):
        user.is_verified = True
        user.save()
        send_welcome_email(user.user_name, user.user_email)
        send_admin_signup_notification(user.user_name, user.user_email)
        messages.success(request, "Your email has been verified! You can now log in.")
        return redirect('login')
    else:
        messages.error(request, "The verification link is invalid or has expired.")
        return redirect('signup')
