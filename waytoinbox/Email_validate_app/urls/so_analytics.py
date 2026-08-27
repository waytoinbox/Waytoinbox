from django.urls import path
from Email_validate_app.views import so_analytics as views

urlpatterns = [
    path('Sales-Outreach/sender/<int:cid>/analytics-data/', views.so_campaign_analytics_data, name='so_campaign_analytics_data'),
    path('Sales-Outreach/analytics/',                        views.so_analytics_overview,      name='so_analytics_overview'),
    path('Sales-Outreach/analytics/data/',                    views.so_analytics_overview_data, name='so_analytics_overview_data'),
]
