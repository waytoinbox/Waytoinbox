from django.urls import path
from Email_validate_app.views import so_track_open, so_track_pixel, so_track_click, so_unsubscribe

urlpatterns = [
    path('so/track/open/<uuid:token>/',   so_track_open,   name='so_track_open'),
    # V3.6 — new per-send open-tracking endpoint (SOOpenPixel), additive.
    # so_track_open above is untouched and keeps resolving every
    # already-sent email's legacy pixel URL indefinitely.
    path('so/track/pixel/<uuid:token>/',  so_track_pixel,  name='so_track_pixel'),
    path('so/track/click/<uuid:token>/',  so_track_click,  name='so_track_click'),
    path('so/unsubscribe/<uuid:token>/',  so_unsubscribe,  name='so_unsubscribe'),
]
