from django.db.models import F
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt

from Email_validate_app.services.so_smtp import TRANSPARENT_GIF, SITE_URL


@never_cache
@csrf_exempt
def so_track_open(request, token):
    from Email_validate_app.models import SOCampaignContact, SOEvent, SOCampaign
    try:
        cc = SOCampaignContact.objects.select_related('prospect', 'campaign').get(
            tracking_token=token
        )
        SOEvent.objects.create(
            campaign=cc.campaign,
            prospect=cc.prospect,
            email=cc.email,
            event_type='opened',
            metadata={'ip': request.META.get('REMOTE_ADDR', '')},
        )
        SOCampaign.objects.filter(id=cc.campaign_id).update(total_opened=F('total_opened') + 1)
    except Exception:
        pass
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
            email=cc.email,
            event_type='clicked',
            metadata={'url': link.destination_url, 'ip': request.META.get('REMOTE_ADDR', '')},
        )
        SOCampaign.objects.filter(id=cc.campaign_id).update(total_clicked=F('total_clicked') + 1)
        return HttpResponseRedirect(link.destination_url)
    except Exception:
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
