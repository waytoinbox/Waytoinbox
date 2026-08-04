from django.urls import path, include

urlpatterns = [
    path('', include('Email_validate_app.urls.auth')),
    path('', include('Email_validate_app.urls.dashboard')),
    path('', include('Email_validate_app.urls.email_validation')),
    path('', include('Email_validate_app.urls.billing')),
    path('', include('Email_validate_app.urls.subscription')),
    path('', include('Email_validate_app.urls.blocklist')),
    path('', include('Email_validate_app.urls.dmarc')),
    path('', include('Email_validate_app.urls.campaigns')),
    path('', include('Email_validate_app.urls.contacts')),
    path('', include('Email_validate_app.urls.segments')),
    path('', include('Email_validate_app.urls.templates')),
    path('', include('Email_validate_app.urls.sender_verify')),
    path('', include('Email_validate_app.urls.reputation')),
    path('', include('Email_validate_app.urls.profile')),
    path('', include('Email_validate_app.urls.api')),
    path('', include('Email_validate_app.urls.admin')),
    path('', include('Email_validate_app.urls.analytics')),
]
