from django.urls import path
from Email_validate_app.views import so_track_open, so_track_click, so_unsubscribe

urlpatterns = [
    path('so/track/open/<uuid:token>/',   so_track_open,   name='so_track_open'),
    path('so/track/click/<uuid:token>/',  so_track_click,  name='so_track_click'),
    path('so/unsubscribe/<uuid:token>/',  so_unsubscribe,  name='so_unsubscribe'),
]
