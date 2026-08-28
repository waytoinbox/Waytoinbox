import logging

from django.db.models import F
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt

from Email_validate_app.services.so_smtp import TRANSPARENT_GIF, SITE_URL

logger = logging.getLogger('Email_validate_app.views')


@never_cache
@csrf_exempt
def so_track_open(request, token):
    from Email_validate_app.models import SOCampaignContact, SOEvent, SOCampaign
    try:
        cc = SOCampaignContact.objects.select_related('prospect', 'campaign').get(
            tracking_token=token
        )
        # V3.2 — step_order is deliberately left unset (NULL) here. The open
        # pixel URL embeds only cc.tracking_token, ONE per-contact token
        # reused unchanged across every step's email (see
        # services/so_smtp.py::inject_tracking) — nothing reachable from this
        # token identifies which step's email produced this specific open.
        # cc.current_step at THIS moment is not a substitute: it's always
        # "the step still owed", already advanced past whatever step was
        # last sent by the time any open could possibly fire, and further
        # wrong for a delayed open of an older email after later steps have
        # since gone out. Populating it here would be a guess, not an
        # attribution — so open events carry no step_order in V3.2.
        SOEvent.objects.create(
            campaign=cc.campaign,
            prospect=cc.prospect,
            # cc.account_id/cc.message_id are the exact sender account and
            # Message-ID this contact's email was actually sent with/from —
            # already-scoped fields on the row itself, not a lookup or guess.
            account_id=cc.account_id,
            message_id=cc.message_id,
            email=cc.email,
            event_type='opened',
            metadata={'ip': request.META.get('REMOTE_ADDR', '')},
        )
        SOCampaign.objects.filter(id=cc.campaign_id).update(total_opened=F('total_opened') + 1)
    except Exception:
        # Never surface an error to the recipient's mail client — this must
        # always fall through to a normal GIF response — but a real DB/
        # lookup failure here is otherwise completely invisible, so log it.
        logger.exception('so_track_open: failed to record open for token=%s', token)
    return HttpResponse(
        TRANSPARENT_GIF,
        content_type='image/gif',
        headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0',
        },
    )


@never_cache
@csrf_exempt
def so_track_pixel(request, token):
    """V3.6 — resolves the new per-send open-tracking token (SOOpenPixel),
    giving 'opened' events exact step_order attribution. Does not replace
    or alter so_track_open above, which keeps resolving every already-sent
    email's legacy per-contact tracking_token pixel unchanged and
    indefinitely — new emails simply embed this route's URL instead (see
    services/so_smtp.py::inject_tracking)."""
    from Email_validate_app.models import SOOpenPixel, SOEvent, SOCampaign
    try:
        pixel = SOOpenPixel.objects.select_related(
            'campaign_contact__prospect', 'campaign_contact__campaign',
        ).get(token=token)
        cc = pixel.campaign_contact
        SOEvent.objects.create(
            campaign=cc.campaign,
            prospect=cc.prospect,
            # cc.account_id/cc.message_id are the exact sender account and
            # Message-ID this contact's email was actually sent with/from —
            # already-scoped fields on the row itself, not a lookup or guess
            # (same reasoning so_track_open already uses above).
            account_id=cc.account_id,
            message_id=cc.message_id,
            email=cc.email,
            event_type='opened',
            metadata={'ip': request.META.get('REMOTE_ADDR', '')},
            # V3.6 — exact, not inferred: this SOOpenPixel row was created
            # fresh for this one send, so its own step_order is definitively
            # the step whose email contained this exact pixel, regardless of
            # how far the contact has progressed since (same reasoning
            # so_track_click already uses for SOTrackedLink.step_order).
            step_order=pixel.step_order,
        )
        SOCampaign.objects.filter(id=cc.campaign_id).update(total_opened=F('total_opened') + 1)
    except Exception:
        # Same rule as so_track_open above — always fall through to a
        # normal GIF response, but log the real failure since it would
        # otherwise be silently invisible.
        logger.exception('so_track_pixel: failed to record open for token=%s', token)
    return HttpResponse(
        TRANSPARENT_GIF,
        content_type='image/gif',
        headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0',
        },
    )


@never_cache
@csrf_exempt
def so_track_click(request, token):
    from Email_validate_app.models import SOTrackedLink, SOEvent, SOCampaign
    try:
        link = SOTrackedLink.objects.select_related(
            'campaign_contact__campaign', 'campaign_contact__prospect'
        ).get(token=token)
        cc = link.campaign_contact
        SOEvent.objects.create(
            campaign=cc.campaign,
            prospect=cc.prospect,
            account_id=cc.account_id,
            message_id=cc.message_id,
            email=cc.email,
            event_type='clicked',
            metadata={'url': link.destination_url, 'ip': request.META.get('REMOTE_ADDR', '')},
            # V3.2 — exact, not inferred: this SOTrackedLink row was created
            # fresh for this one send (services/so_smtp.py::inject_tracking),
            # so its own step_order (possibly NULL, for a link generated
            # before this field existed) is definitively the step whose
            # email contained the exact link that was clicked.
            step_order=link.step_order,
        )
        SOCampaign.objects.filter(id=cc.campaign_id).update(total_clicked=F('total_clicked') + 1)
        return HttpResponseRedirect(link.destination_url)
    except Exception:
        # Never surface an error to whoever clicked — always fall through to
        # the SITE_URL fallback redirect — but log the real failure since it
        # would otherwise be silently invisible. Deliberately no destination
        # URL in the log here: on a lookup failure `link` may not be bound,
        # and even when it is, the URL itself isn't needed to diagnose why
        # the lookup/record step failed.
        logger.exception('so_track_click: failed to record click for token=%s', token)
        return HttpResponseRedirect(SITE_URL)


@csrf_exempt
def so_unsubscribe(request, token):
    from Email_validate_app.models import SOCampaignContact
    try:
        cc = SOCampaignContact.objects.select_related('prospect', 'campaign').get(
            tracking_token=token
        )
        valid = True
        email = cc.email
    except SOCampaignContact.DoesNotExist:
        cc    = None
        valid = False
        email = ''

    if request.method == 'POST' and valid:
        from Email_validate_app.services.so_inbox import unsubscribe_contact

        unsubscribe_contact(cc)
        return render(request, 'i_SO_Unsubscribe.html', {'confirmed': True, 'email': email})

    return render(request, 'i_SO_Unsubscribe.html', {
        'valid': valid, 'email': email, 'confirmed': False, 'token': token,
    })
