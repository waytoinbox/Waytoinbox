from django.urls import path
from Email_validate_app import views

urlpatterns = [
    path("Reputation_Analysis/", views.Reputation_Analysis, name="Reputation_Analysis"),
    path("get-reputation-data/", views.get_reputation_data, name="get_reputation_data"),
    path("reputation/hide_row/", views.hide_reputation_row, name="hide_reputation_row"),
    path("Reputation_Analysis/<int:rep_id>/", views.reputation_detail, name="reputation_detail"),
]
