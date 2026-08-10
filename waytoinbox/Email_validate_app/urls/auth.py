from django.urls import path
from Email_validate_app import views

urlpatterns = [
    path("", views.home, name="home"),
    path("signup/", views.signup, name="signup"),
    path("login/", views.login, name="login"),
    path("logout/", views.logout, name="logout"),
    path("verify/<uidb64>/<token>/", views.verify_email, name="verify_email"),
    path("services/", views.services, name="services"),
    path("forgot-password/", views.forgot_password, name="forgot_password"),
    path("reset-password/<str:token>/", views.reset_password, name="reset_password"),
]
