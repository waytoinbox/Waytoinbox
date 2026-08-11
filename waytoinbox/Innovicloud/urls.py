"""
URL configuration for Innovicloud project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.http import HttpResponse
from Email_validate_app.views.errors import handler400, handler403, handler404, handler500

handler400 = handler400
handler403 = handler403
handler404 = handler404
handler500 = handler500


def _robots_txt(request):
    # INF-15: disallow crawlers from authenticated and sensitive paths.
    # The secret admin URL is intentionally omitted — it must not be disclosed here.
    lines = [
        "User-agent: *",
        "Disallow: /login/",
        "Disallow: /signup/",
        "Disallow: /forgot-password/",
        "Disallow: /reset-password/",
        "Disallow: /dashboard/",
        "Disallow: /services/",
        "Disallow: /profile/",
        "Disallow: /billing/",
        "Disallow: /subscription/",
        "Disallow: /api/",
        "Disallow: /verify/",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")


urlpatterns = [
    path("robots.txt", _robots_txt),
    path(settings.ADMIN_URL, admin.site.urls),
    path('', include('Email_validate_app.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)


