from django.urls import path
from Email_validate_app import views

urlpatterns = [
    path("pricing/", views.pricing, name="pricing"),
    path("pricing/services/", views.services, name="service"),
    path("pricing/order_payment/", views.order_payment, name="order_payment"),
    path("pricing/order_payment/payment/", views.payment, name="payment"),
    path("Receipt/", views.receipt_list, name="receipt_list"),
    path("preview/<int:id>/", views.preview, name="preview"),
    path("generate-pdf/<int:id>/", views.generate_pdf, name="generate_pdf"),
    path("billing/hide_row/", views.hide_billing_row, name="hide_billing_row"),
    path("Contact_Us/", views.contact_us, name="contact_us"),
]
