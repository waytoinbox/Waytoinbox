import json
import logging

from django.contrib.auth.tokens import default_token_generator
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from Email_validate_app.forms import CustomSignupForm
from Email_validate_app.models import UserTable
from Email_validate_app.services.email_domain_policy import is_free_email_domain
from Email_validate_app.services.mailer import send_verification_email
from Email_validate_app.utils import get_active_user, get_true_user

logger = logging.getLogger(__name__)


def sub_accounts_overview(request):
    """Main-Account-only page listing its own direct Sub Accounts.

    Gated on the ACTIVE account (see utils.get_active_user), same as every
    other page in the app -- while acting as a Sub Account this is exactly
    as unavailable as it would be to that Sub Account logged in directly,
    which is the correct behavior (a Sub Account never sees this page,
    however it got there).
    """
    if not request.session.get('logged_in'):
        return redirect('login')
    active_user = get_active_user(request)
    if not active_user or active_user.parent_account_id is not None:
        return redirect('dashboard')

    sub_accounts = UserTable.objects.filter(
        parent_account_id=active_user.id,
    ).order_by('user_email')

    return render(request, 'i_Sub_Accounts.html', {'sub_accounts': sub_accounts})


def create_sub_account(request):
    """A Main Account creates a new, fully independent Sub Account.

    Only reachable by a Main Account (active account has no parent of its
    own) -- a Sub Account, whether logged in directly or acted-into, can
    never create another Sub Account, since its own parent_account_id is
    set. Reuses the exact same form/password-hashing/verification-email
    mechanism as normal signup() (views/auth.py) -- the only differences
    are: the creator's own session is untouched (they are not the new
    account), and the new row's parent_account is set.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)
    if not request.session.get('logged_in'):
        return redirect('login')

    active_user = get_active_user(request)
    if not active_user or active_user.parent_account_id is not None:
        return JsonResponse(
            {'status': 'error', 'message': 'Only a Main Account can create Sub Accounts.'},
            status=403,
        )

    email = request.POST.get('user_email', '').strip()
    existing = UserTable.objects.filter(user_email=email).first()
    if existing:
        return JsonResponse({'status': 'error', 'message': 'This email is already registered.'})

    # Same business-email-only policy as normal signup (views/auth.py) --
    # reused as-is, not duplicated or weakened.
    if is_free_email_domain(email):
        return JsonResponse({
            'status': 'error',
            'message': 'Please use a business email address for Sub Accounts. Personal '
                       'email providers (Gmail, Outlook, Hotmail, Yahoo, etc.) are not supported.',
        })

    # A Sub Account must share its Main Account's email domain. This is a
    # business rule only -- ownership/isolation is still decided solely by
    # parent_account (set below), never by domain, so two unrelated Main
    # Accounts on the same domain remain fully isolated from each other.
    main_domain = active_user.user_email.rsplit('@', 1)[-1].strip().lower()
    sub_domain = email.rsplit('@', 1)[-1].strip().lower() if '@' in email else ''
    if sub_domain != main_domain:
        return JsonResponse({
            'status': 'error',
            'message': 'Sub Account email must use the same email domain as the Main Account '
                       f'(@{main_domain}).',
        })

    form = CustomSignupForm(request.POST)
    if not form.is_valid():
        errors = {f: e[0] for f, e in form.errors.items()}
        return JsonResponse({'status': 'error', 'message': 'Please fix the errors below.', 'errors': errors})

    user = form.save(commit=False)
    user.set_password(form.cleaned_data['password'])
    user.is_verified = False
    user.parent_account = active_user
    user.save()

    # Same verification email/token mechanism as normal signup -- a Sub
    # Account is not auto-verified just to simplify this endpoint.
    token = default_token_generator.make_token(user)
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    verification_link = request.build_absolute_uri(reverse('verify_email', args=[uid, token]))
    send_verification_email(user.user_email, user.user_name, verification_link)

    return JsonResponse({'status': 'ok', 'email': user.user_email})


def switch_account(request):
    """Main Account switches into one of its own direct Sub Accounts.

    Authorization is always evaluated against the TRUE authenticated
    identity (session['logged_in']), never the current acting_as_email --
    switching changes what "active" means, so it can't be gated by the
    active account itself. target.parent_account_id must equal the true
    user's id exactly; existence of the target email is not sufficient.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    true_user = get_true_user(request)
    if not true_user:
        return JsonResponse({'status': 'error', 'message': 'Login required.'}, status=401)
    if true_user.parent_account_id is not None:
        return JsonResponse(
            {'status': 'error', 'message': 'Only a Main Account can switch accounts.'},
            status=403,
        )

    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        data = {}
    target_email = (data.get('email') or '').strip()

    target = UserTable.objects.filter(user_email=target_email).first()
    if not target or target.parent_account_id != true_user.id:
        return JsonResponse({'status': 'error', 'message': 'Account not found.'}, status=404)

    request.session['acting_as_email'] = target.user_email
    request.session.cycle_key()
    request.session.modified = True

    return JsonResponse({'status': 'ok', 'active_account': target.user_email})


def return_to_main(request):
    """Clears the acting_as_email context, returning to the Main Account.

    Only the true authenticated Main Account may call this -- a directly
    logged-in Sub Account has no acting context to clear and is rejected
    the same way switch_account rejects it.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    true_user = get_true_user(request)
    if not true_user:
        return JsonResponse({'status': 'error', 'message': 'Login required.'}, status=401)
    if true_user.parent_account_id is not None:
        return JsonResponse(
            {'status': 'error', 'message': 'No account to return to.'}, status=403,
        )

    request.session.pop('acting_as_email', None)
    request.session.cycle_key()
    request.session.modified = True

    return JsonResponse({'status': 'ok'})
