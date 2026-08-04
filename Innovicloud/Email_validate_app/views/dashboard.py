import logging

logger = logging.getLogger(__name__)

from django.shortcuts import render, redirect
from django.urls import reverse
from django.http import JsonResponse
from django.contrib import messages

from Email_validate_app.models import ListFiles
from Email_validate_app.utils import get_user_id
from Email_validate_app.services.dashboard_service import (
    get_dashboard_context, get_chart_data,
)

from .billing import get_current_credit


def dashboard(request):
    if not request.session.get('logged_in'):
        return redirect('login')

    user_id = get_user_id(request)
    ctx     = get_dashboard_context(user_id)
    return render(request, 'i_Dashboard.html', ctx)


def dashboard_chart_data(request):
    if not request.session.get('logged_in'):
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    try:
        user_id = get_user_id(request)
        try:
            days = int(request.GET.get('range', 30))
            if days not in (30, 90):
                days = 30
        except (ValueError, TypeError):
            days = 30
        data = get_chart_data(user_id, days)
    except Exception as e:
        logger.exception("dashboard_chart_data error: %s", e)
        return JsonResponse({'error': str(e)}, status=500)
    return JsonResponse(data)


def home(request):
    if request.session.get('logged_in'):
        return redirect(reverse('dashboard'))

    current_credits = 0
    user_id = get_user_id(request)

    if user_id:
        try:
            current_credits = get_current_credit(user_id)
        except Exception as e:
            logger.error("Error fetching credits: %s", e)

    return render(request, 'i_home.html', {
        'mx_found': None,
        'session': request.session,
        'credits': current_credits
    })


def get_data(request):
    if request.method == 'POST':
        user_id = get_user_id(request)

        if user_id:
            try:
                current_credits = get_current_credit(user_id)
            except Exception as e:
                logger.error("Error fetching credits: %s", e)
                messages.error(request, "An error occurred while fetching your credits. Please try again later.")
                current_credits = 0
        else:
            current_credits = None

        if not user_id:
            return JsonResponse({'error': 'User not logged in'}, status=401)

        import json as _json
        data = _json.loads(request.body)
        status_filter = data.get('status', 'all')

        query = ListFiles.objects.filter(user_id=user_id).exclude(
            job_status__iexact="Deleted"
        ).order_by('-file_id')

        if status_filter == "Processing":
            query = query.filter(job_status="Processing")
        elif status_filter == "Completed":
            query = query.filter(job_status="Complete")
        elif status_filter == "Stopped":
            query = query.filter(job_status="Stopped")
        elif status_filter == "Unpurchased":
            query = query.filter(job_status="Complete").exclude(credite_status="Credited")
        elif status_filter == "all":
            query = query.filter(table_name__isnull=False)

        # DB-09: explicit field list prevents new internal columns from leaking to client
        return JsonResponse({
            'current_credits': current_credits,
            'data': list(query.values(
                'file_id', 'file_name', 'table_name', 'job_status',
                'total_count', 'valid_count', 'invalid_count', 'unknown_count', 'others_count',
                'valid_percentage', 'invalid_percentage', 'unknown_percentage', 'others_percentage',
                'credite_status', 'free_analyze', 'insert_date',
            ))
        })
