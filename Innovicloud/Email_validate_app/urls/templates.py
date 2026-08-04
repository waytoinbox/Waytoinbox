from django.urls import path
from Email_validate_app import views

urlpatterns = [
    path("Email_Campaigns/templates/", views.templates_page, name="templates_page"),
    path("Email_Campaigns/templates/builder/", views.template_builder, name="template_builder"),
    path("Email_Campaigns/templates/save/", views.save_user_template, name="save_user_template"),
    path("Email_Campaigns/templates/<int:template_id>/delete/", views.delete_user_template, name="delete_user_template"),
    path("Email_Campaigns/templates/<int:template_id>/duplicate/", views.duplicate_user_template, name="duplicate_user_template"),
    path("Email_Campaigns/template-library/<int:template_id>/use/", views.use_library_template, name="use_library_template"),
    path("Email_Campaigns/template-image/upload/", views.upload_template_image, name="upload_template_image"),
]
