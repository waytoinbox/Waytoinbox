from django.urls import path
from Email_validate_app import views

urlpatterns = [
    path("dashboard/", views.dashboard, name="dashboard"),
    path("dashboard/chart/", views.dashboard_chart_data, name="dashboard_chart_data"),
    path("get_data/", views.get_data, name="get_data"),
]
