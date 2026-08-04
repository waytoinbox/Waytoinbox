from django.urls import path
from Email_validate_app.views import analytics as views

urlpatterns = [
    path('analytics/',               views.analytics_overview,          name='analytics_overview'),
    path('analytics/email-validation/',  views.email_validation_analytics,  name='email_analytics'),
    path('analytics/email-campaigns/',   views.campaign_analytics,          name='campaign_analytics'),
    path('analytics/blocklist/',         views.blocklist_analytics,         name='blocklist_analytics'),
    path('analytics/reputation/',        views.reputation_analytics,        name='reputation_analytics'),
    path('analytics/dmarc/',             views.dmarc_analytics,             name='dmarc_analytics'),
    path('analytics/header-analysis/',   views.header_analysis_analytics,   name='header_analysis_analytics'),
]
