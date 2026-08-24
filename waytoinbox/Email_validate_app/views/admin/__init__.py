from Email_validate_app.views.admin.dashboard import admin_dashboard
from Email_validate_app.views.admin.users import (
    admin_users, admin_user_detail, admin_user_edit,
    admin_user_toggle, admin_user_verify, admin_user_grant_admin,
    admin_user_credits, admin_user_reset_password, admin_user_delete,
)
from Email_validate_app.views.admin.campaigns import (
    admin_campaigns, admin_campaign_detail, admin_campaign_cancel,
    admin_campaign_duplicate, admin_campaign_archive, admin_campaign_preview,
)
from Email_validate_app.views.admin.validations import (
    admin_validations, admin_validation_detail, admin_validation_cancel,
    admin_validation_delete, admin_validation_download,
)
from Email_validate_app.views.admin.lists import admin_lists, admin_list_detail
from Email_validate_app.views.admin.email_templates import (
    admin_templates,
    admin_library_template_create,
    admin_library_template_upload,
    admin_library_template_edit,
    admin_library_template_toggle,
    admin_library_template_delete,
)
from Email_validate_app.views.admin.analytics import admin_analytics
from Email_validate_app.views.admin.suppression import admin_suppression
from Email_validate_app.views.admin.email_verification import admin_ev
from Email_validate_app.views.admin.blocklist import admin_blocklist
from Email_validate_app.views.admin.reputation import admin_reputation
from Email_validate_app.views.admin.headers import admin_headers
from Email_validate_app.views.admin.dmarc import admin_dmarc
from Email_validate_app.views.admin.payments import admin_payments
from Email_validate_app.views.admin.subscriptions import admin_subscriptions, admin_subs_extend
from Email_validate_app.views.admin.coupons import (
    admin_coupons, admin_coupon_create, admin_coupon_edit,
    admin_coupon_toggle, admin_coupon_delete,
)
from Email_validate_app.views.admin.notifications import admin_notifications
from Email_validate_app.views.admin.audit import admin_audit
from Email_validate_app.views.admin.system import admin_system, admin_system_logs
from Email_validate_app.views.admin.settings import admin_settings, admin_settings_update
from Email_validate_app.views.admin.profile import admin_profile
from Email_validate_app.views.admin.warmup import (
    admin_warmup_receivers,
    admin_warmup_receiver_oauth_start, admin_warmup_receiver_oauth_callback,
    admin_warmup_receiver_pause, admin_warmup_receiver_resume, admin_warmup_receiver_delete,
)
