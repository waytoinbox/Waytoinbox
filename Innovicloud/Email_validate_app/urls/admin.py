from django.urls import path
from Email_validate_app.views import admin as av

urlpatterns = [
    # Dashboard
    path('wti-admin/',                                     av.admin_dashboard,           name='admin_dashboard'),

    # User Management
    path('wti-admin/users/',                               av.admin_users,               name='admin_users'),
    path('wti-admin/users/<int:uid>/',                     av.admin_user_detail,         name='admin_user_detail'),
    path('wti-admin/users/<int:uid>/edit/',                av.admin_user_edit,           name='admin_user_edit'),
    path('wti-admin/users/<int:uid>/toggle/',              av.admin_user_toggle,         name='admin_user_toggle'),
    path('wti-admin/users/<int:uid>/verify/',              av.admin_user_verify,         name='admin_user_verify'),
    path('wti-admin/users/<int:uid>/grant-admin/',         av.admin_user_grant_admin,    name='admin_user_grant_admin'),
    path('wti-admin/users/<int:uid>/credits/',             av.admin_user_credits,        name='admin_user_credits'),
    path('wti-admin/users/<int:uid>/reset-password/',      av.admin_user_reset_password, name='admin_user_reset_pw'),
    path('wti-admin/users/<int:uid>/delete/',              av.admin_user_delete,         name='admin_user_delete'),

    # Campaign Management
    path('wti-admin/campaigns/',                           av.admin_campaigns,           name='admin_campaigns'),
    path('wti-admin/campaigns/<int:cid>/',                 av.admin_campaign_detail,     name='admin_campaign_detail'),
    path('wti-admin/campaigns/<int:cid>/cancel/',          av.admin_campaign_cancel,     name='admin_campaign_cancel'),
    path('wti-admin/campaigns/<int:cid>/duplicate/',       av.admin_campaign_duplicate,  name='admin_campaign_duplicate'),
    path('wti-admin/campaigns/<int:cid>/archive/',         av.admin_campaign_archive,    name='admin_campaign_archive'),
    path('wti-admin/campaigns/<int:cid>/preview/',         av.admin_campaign_preview,    name='admin_campaign_preview'),

    # Email Validation Management
    path('wti-admin/validations/',                         av.admin_validations,         name='admin_validations'),
    path('wti-admin/validations/<int:fid>/',               av.admin_validation_detail,   name='admin_validation_detail'),
    path('wti-admin/validations/<int:fid>/cancel/',        av.admin_validation_cancel,   name='admin_validation_cancel'),
    path('wti-admin/validations/<int:fid>/delete/',        av.admin_validation_delete,   name='admin_validation_delete'),
    path('wti-admin/validations/<int:fid>/download/',      av.admin_validation_download, name='admin_validation_download'),

    # Lists & Segments
    path('wti-admin/lists/',                               av.admin_lists,               name='admin_lists'),
    path('wti-admin/lists/<int:lid>/',                     av.admin_list_detail,         name='admin_list_detail'),

    # Email Templates
    path('wti-admin/email-templates/',                              av.admin_templates,                    name='admin_email_templates'),
    path('wti-admin/email-templates/create/',                       av.admin_library_template_create,      name='admin_library_template_create'),
    path('wti-admin/email-templates/upload/',                       av.admin_library_template_upload,      name='admin_library_template_upload'),
    path('wti-admin/email-templates/<int:tid>/edit/',               av.admin_library_template_edit,        name='admin_library_template_edit'),
    path('wti-admin/email-templates/<int:tid>/toggle/',             av.admin_library_template_toggle,      name='admin_library_template_toggle'),
    path('wti-admin/email-templates/<int:tid>/delete/',             av.admin_library_template_delete,      name='admin_library_template_delete'),

    # Campaign Analytics
    path('wti-admin/analytics/',                           av.admin_analytics,           name='admin_analytics'),

    # Suppression Lists
    path('wti-admin/suppression/',                         av.admin_suppression,         name='admin_suppression'),

    # Email Verification
    path('wti-admin/email-verification/',                  av.admin_ev,                  name='admin_email_verification'),

    # Blocklist Monitor
    path('wti-admin/blocklist/',                           av.admin_blocklist,           name='admin_blocklist'),

    # Domain Reputation
    path('wti-admin/reputation/',                          av.admin_reputation,          name='admin_reputation'),

    # Header Analyzer
    path('wti-admin/headers/',                             av.admin_headers,             name='admin_headers'),

    # DMARC Analyzer
    path('wti-admin/dmarc/',                               av.admin_dmarc,               name='admin_dmarc'),

    # Payments
    path('wti-admin/payments/',                            av.admin_payments,            name='admin_payments'),

    # Subscriptions
    path('wti-admin/subscriptions/',                       av.admin_subscriptions,       name='admin_subscriptions'),
    path('wti-admin/subscriptions/<int:sid>/extend/',      av.admin_subs_extend,         name='admin_subs_extend'),

    # Coupons
    path('wti-admin/coupons/',                             av.admin_coupons,             name='admin_coupons'),
    path('wti-admin/coupons/create/',                      av.admin_coupon_create,       name='admin_coupon_create'),
    path('wti-admin/coupons/<int:coid>/edit/',             av.admin_coupon_edit,         name='admin_coupon_edit'),
    path('wti-admin/coupons/<int:coid>/toggle/',           av.admin_coupon_toggle,       name='admin_coupon_toggle'),
    path('wti-admin/coupons/<int:coid>/delete/',           av.admin_coupon_delete,       name='admin_coupon_delete'),

    # Notifications
    path('wti-admin/notifications/',                       av.admin_notifications,       name='admin_notifications'),

    # Audit Logs
    path('wti-admin/audit/',                               av.admin_audit,               name='admin_audit'),

    # System Monitoring
    path('wti-admin/system/',                              av.admin_system,              name='admin_system'),
    path('wti-admin/system/logs/',                         av.admin_system_logs,         name='admin_system_logs'),

    # Settings
    path('wti-admin/settings/',                            av.admin_settings,            name='admin_settings'),
    path('wti-admin/settings/update/',                     av.admin_settings_update,     name='admin_settings_update'),

    # Profile
    path('wti-admin/profile/',                             av.admin_profile,             name='admin_profile'),
]
