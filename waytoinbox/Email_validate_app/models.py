import uuid
from datetime import time

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils.timezone import now
import pytz
from django.utils.timezone import localtime

# Custom User Manager
class UserManager(BaseUserManager):
    def create_user(self, user_name, user_email, password=None):
        if not user_email:
            raise ValueError("Users must have an email address")
        user = self.model(
            user_name=user_name,
            user_email=self.normalize_email(user_email),
            created_date=now(),
        )
        user.set_password(password)  # Hash password properly
        user.save(using=self._db)
        return user

    def create_superuser(self, user_name, user_email, password):
        user = self.create_user(user_name, user_email, password)
        user.is_admin = True
        user.is_superuser = True
        user.is_staff = True  # Important for Django admin access
        user.save(using=self._db)
        return user

    def get_by_natural_key(self, user_email):
        return self.get(user_email=user_email)


# Custom User Model for User data
class UserTable(AbstractBaseUser, PermissionsMixin):
    user_name = models.CharField(max_length=50)
    user_email = models.EmailField(max_length=225, unique=True)
    password = models.CharField(max_length=255) 
    is_verified = models.BooleanField(default=False)
    created_date = models.DateTimeField(default=now)
    updated_date = models.DateTimeField(auto_now=True)
    reset_token = models.CharField(max_length=225, null=True, blank=True, db_index=True)
    reset_token_expiry = models.DateTimeField(null=True, blank=True)

    company  = models.CharField(max_length=255, null=True, blank=True)
    role     = models.CharField(max_length=100, null=True, blank=True)
    timezone = models.CharField(max_length=100, null=True, blank=True)
    website  = models.URLField(max_length=255, null=True, blank=True)

    notify_job_complete  = models.BooleanField(default=True)
    notify_blocklist     = models.BooleanField(default=True)
    notify_payment       = models.BooleanField(default=True)
    notify_expiry        = models.BooleanField(default=True)
    notify_campaign      = models.BooleanField(default=True)
    notify_reputation    = models.BooleanField(default=True)
    notify_sender_verify = models.BooleanField(default=True)
    password_changed_at  = models.DateTimeField(null=True, blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_admin = models.BooleanField(default=False)

    objects = UserManager()

    USERNAME_FIELD = 'user_email'
    REQUIRED_FIELDS = ['user_name']

    def save(self, *args, **kwargs):
        if self.is_admin or self.is_superuser:
            self.is_staff = True
        super().save(*args, **kwargs)

    class Meta:
        db_table = 'user_table'


class LoginLog(models.Model):
    user       = models.ForeignKey(UserTable, on_delete=models.CASCADE, related_name='login_logs')
    ip_address = models.CharField(max_length=50, blank=True)
    browser    = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'login_log'
        ordering = ['-created_at']


class LoginActivity(models.Model):
    STATUS_CHOICES = [
        ('success', 'Success'),
        ('failed',  'Failed'),
    ]
    user       = models.ForeignKey(UserTable, on_delete=models.CASCADE, related_name='login_activities', null=True, blank=True)
    login_at   = models.DateTimeField(auto_now_add=True)
    logout_at  = models.DateTimeField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    browser    = models.CharField(max_length=100, blank=True)
    os         = models.CharField(max_length=100, blank=True)
    device     = models.CharField(max_length=100, blank=True)
    user_agent = models.TextField(blank=True)
    status     = models.CharField(max_length=10, choices=STATUS_CHOICES)

    class Meta:
        db_table = 'login_activity'
        ordering = ['-login_at']


class UserNotification(models.Model):
    user       = models.ForeignKey(UserTable, on_delete=models.CASCADE, related_name='notifications')
    type       = models.CharField(max_length=50)
    message    = models.CharField(max_length=255)
    url        = models.CharField(max_length=500, blank=True, default='')
    is_read    = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'user_notifications'
        ordering = ['-created_at']

    def __str__(self):
        return self.user_email

    def get_natural_key(self):
        return (self.user_email,)



# New Model for ListFiles Table
class ListFiles(models.Model):
    file_id = models.AutoField(primary_key=True)
    file_name = models.CharField(max_length=255, null=True, blank=True)
    table_name = models.CharField(max_length=225, unique=True, null=True, blank=True)
    user = models.ForeignKey(UserTable, on_delete=models.CASCADE, null=True, blank=True)
    insert_date = models.DateTimeField(null=True, blank=True)
    job_status = models.CharField(max_length=225, null=True, blank=True, db_index=True)
    total_count = models.IntegerField(null=True, blank=True)
    valid_count = models.IntegerField(null=True, blank=True)
    invalid_count = models.IntegerField(null=True, blank=True)
    unknown_count = models.IntegerField(null=True, blank=True)
    others_count = models.IntegerField(null=True, blank=True)
    valid_percentage = models.FloatField(null=True, blank=True)
    invalid_percentage = models.FloatField(null=True, blank=True)
    unknown_percentage = models.FloatField(null=True, blank=True)
    others_percentage = models.IntegerField(null=True, blank=True)
    credite_status = models.CharField(max_length=200, null=True, blank=True)
    free_analyze = models.FloatField(null=True, blank=True)
    started_at       = models.DateTimeField(null=True, blank=True)   # ← new
    completed_at     = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'list_files'
        

    def __str__(self):
        return self.file_name if self.file_name else f"File {self.file_id}"    



# New Model for CurrentCredits Table
class CurrentCredits(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(UserTable, on_delete=models.CASCADE, db_column='user_id', null=True, blank=True)
    vc_total_credits   = models.IntegerField(null=True, blank=True, default=0)  # Validation Credits
    vc_used_credits    = models.IntegerField(null=True, blank=True, default=0)
    vc_current_credits = models.IntegerField(null=True, blank=True, default=0)
    ac_total_credits   = models.IntegerField(null=True, blank=True, default=0)  # Analysis Credits
    ac_used_credits    = models.IntegerField(null=True, blank=True, default=0)
    ac_current_credits = models.IntegerField(null=True, blank=True, default=0)
    cc_total_credits   = models.IntegerField(null=True, blank=True, default=0)  # Contact Credits
    cc_used_credits    = models.IntegerField(null=True, blank=True, default=0)
    cc_current_credits = models.IntegerField(null=True, blank=True, default=0)

    class Meta:
        db_table = 'current_credits'

    def __str__(self):
        return f"Credits for {self.user.user_email if self.user else 'Unknown User'}"


# New Model for TotalCredits Table
class TotalCredits(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(UserTable, on_delete=models.CASCADE, db_column='user_id', null=True, blank=True)
    vc_credits     = models.IntegerField(null=True, blank=True)   # was buying_credits
    vc_buying_date = models.DateTimeField(null=True, blank=True)  # was buying_date
    ac_credits     = models.IntegerField(null=True, blank=True)   # was ip_credits
    ac_buying_date = models.DateTimeField(null=True, blank=True)  # was ip_buying_date
    cc_credits     = models.IntegerField(null=True, blank=True)
    cc_buying_date = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'total_credits'

    def __str__(self):
        return f"{self.user.user_email if self.user else 'Unknown'} - {self.vc_credits} VC credits"


# New Model for UsedCredits Table
class UsedCredits(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(UserTable, on_delete=models.CASCADE, db_column='user_id', null=True, blank=True)
    vc_used_credits = models.IntegerField(null=True, blank=True)  # was used_credits
    vc_used_date    = models.DateTimeField(null=True, blank=True)  # was used_date
    ac_used_credits = models.IntegerField(null=True, blank=True)  # was used_ip_credits
    ac_used_date    = models.DateTimeField(null=True, blank=True)  # was used_ip_date
    cc_used_credits = models.IntegerField(null=True, blank=True)
    cc_used_date    = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'used_credits'

    def __str__(self):
        return f"{self.user.user_email if self.user else 'Unknown'} used {self.vc_used_credits} VC credits"


# ── Service-based credit system ───────────────────────────────────────────────
# The seven credit-consuming services. These keys are used as ServiceCredit
# .service, CreditPackage.service and CreditAuditLog.credit_type values.
#
# NOTE: CreditAuditLog.credit_type was max_length=10 when only 'vc'/'ac'/'cc'
# existed; six of these keys are longer, so that column is widened to 20 in the
# same migration that adds these models. The legacy 'vc'/'ac'/'cc' values stay
# valid — historical audit rows are never rewritten.
SERVICE_CHOICES = [
    ('email_validation', 'Email Validation'),
    ('email_marketing',  'Email Marketing'),
    ('sales_outreach',   'Sales Outreach'),
    ('reputation',       'Reputation Analysis'),
    ('header_analysis',  'Email Header Analyzer'),
    ('ip_blocklist',     'IP Blocklist Monitor'),
    ('domain_blocklist', 'Domain Blocklist Monitor'),
]

SERVICE_KEYS = [key for key, _ in SERVICE_CHOICES]


class CreditAuditLog(models.Model):
    CREDIT_TYPES = [
        ('vc', 'Validation Credits'),
        ('ac', 'Analysis Credits'),
        ('cc', 'Contact Credits'),
    ] + SERVICE_CHOICES
    ENTRY_TYPES = [
        ('credit',     'Credit Added'),
        ('debit',      'Credit Used'),
        ('adjustment', 'Manual Adjustment'),
        ('refund',     'Refund'),
        ('expired',    'Expired'),
    ]
    REF_TYPES = [
        ('payg',         'Pay-As-You-Go Purchase'),
        ('subscription', 'Subscription Purchase'),
        ('validation',   'Email Validation'),
        ('campaign',     'Campaign Send'),
        ('ip_check',     'IP/Domain/Header/Reputation Check'),
        ('admin',        'Admin Adjustment'),
        ('service_purchase', 'Service Credit Purchase'),
        ('so_account',       'Sales Outreach Account Added'),
        # Reputation moved off the shared 'ip_check' catch-all when it was cut
        # over to its own service wallet. Header Analyzer and the two blocklist
        # monitors still use 'ip_check' until their own cutovers.
        ('reputation',       'Reputation Analysis'),
    ]

    user           = models.ForeignKey(UserTable, on_delete=models.CASCADE)
    credit_type    = models.CharField(max_length=20, choices=CREDIT_TYPES)
    entry_type     = models.CharField(max_length=20, choices=ENTRY_TYPES)
    amount         = models.IntegerField()
    balance_before = models.IntegerField(default=0)
    balance_after  = models.IntegerField(default=0)
    ref_type       = models.CharField(max_length=30, choices=REF_TYPES, blank=True)
    ref_id         = models.CharField(max_length=225, blank=True)
    description    = models.CharField(max_length=500, blank=True)
    created_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'credit_audit_log'
        indexes = [
            models.Index(fields=['user', 'credit_type', 'created_at']),
            models.Index(fields=['ref_type', 'ref_id']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user_id} | {self.credit_type} | {self.entry_type} | {self.amount}"


class ServiceCredit(models.Model):
    """Per-service credit wallet. One row per (user, service), created lazily
    on first purchase or first spend.

    Deliberately a SEPARATE table from CurrentCredits, for two reasons:

    1. Lock granularity. Every legacy deduct does
       CurrentCredits.objects.select_for_update().get(user_id=...), locking ONE
       row per user — so a bulk validation and a campaign send already
       serialise against each other. Per-service rows lock independently.
    2. These balances never expire. subscription_expiry_job used to zero
       CurrentCredits.ac/cc_current_credits nightly (it no longer does, and
       legacy balances are retained too); keeping the new balances in a
       different table makes "never expires" structural rather than a
       code-review promise that a future change to the expiry job could undo.
    """
    user            = models.ForeignKey(UserTable, on_delete=models.CASCADE,
                                        related_name='service_credits')
    service         = models.CharField(max_length=20, choices=SERVICE_CHOICES)
    balance         = models.BigIntegerField(default=0)   # spendable now
    total_purchased = models.BigIntegerField(default=0)   # reporting only
    total_used      = models.BigIntegerField(default=0)   # reporting only
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'service_credits'
        constraints = [
            models.UniqueConstraint(fields=['user', 'service'],
                                    name='uniq_service_credit_user_service'),
            models.CheckConstraint(condition=models.Q(balance__gte=0),
                                   name='service_credit_balance_nonneg'),
        ]
        indexes = [models.Index(fields=['user'], name='svc_credit_user_idx')]

    def __str__(self):
        return f"{self.user_id} | {self.service} | {self.balance}"


class CreditPackage(models.Model):
    """Server-side pricing catalogue. DB-backed so prices can be corrected
    without a deploy (the supplied ladders contain deliberate anomalies —
    see the seed migration).

    Two pricing modes:
      tier  — flat price for the whole band. Matched as min_qty <= qty <= max_qty
              (max_qty NULL = open-ended top band).
      block — price_usd per block_size units, charged as
              ceil(qty / block_size) * price_usd.
    """
    MODE_TIER  = 'tier'
    MODE_BLOCK = 'block'
    MODES = [(MODE_TIER, 'Flat tier price'), (MODE_BLOCK, 'Price per block')]

    service    = models.CharField(max_length=20, choices=SERVICE_CHOICES)
    mode       = models.CharField(max_length=10, choices=MODES, default=MODE_TIER)
    min_qty    = models.BigIntegerField(null=True, blank=True)   # tier mode
    max_qty    = models.BigIntegerField(null=True, blank=True)   # tier mode; NULL = open-ended
    block_size = models.BigIntegerField(null=True, blank=True)   # block mode
    price_usd  = models.DecimalField(max_digits=10, decimal_places=2)
    is_active  = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'credit_packages'
        ordering = ['service', 'sort_order']
        indexes = [
            models.Index(fields=['service', 'is_active', 'sort_order'],
                         name='credit_pkg_svc_active_idx'),
        ]

    def __str__(self):
        if self.mode == self.MODE_BLOCK:
            return f"{self.service} | {self.block_size}/block | ${self.price_usd}"
        return f"{self.service} | {self.min_qty}-{self.max_qty or '∞'} | ${self.price_usd}"


class ServiceOrder(models.Model):
    """A checkout intent: what the server quoted, before Razorpay confirms it.

    This is deliberately NOT a pending row in `Payment`. `Payment` is the
    receipts table — billing.receipt_list filters only on is_hidden, so an
    unpaid row written at order-creation time would show the customer a
    receipt for money they had not spent, and an abandoned checkout would
    leave that receipt behind forever. A Payment row is still created only on
    successful verification, exactly as the legacy flow does, so the receipt,
    invoice and PDF views need no changes at all.

    Its real job is to be the server's memory of the quote. The browser sends
    quantities to /subscription/order/ and nothing else; the price, the
    discount, the currency and the credit amounts are computed here and
    frozen into this row. /subscription/verify/ then reads the cart back from
    THIS ROW — never from the POST body — so a tampered verify request cannot
    change what gets credited.

    Exactly-once fulfilment: status starts 'created' and moves to 'paid'
    under select_for_update(). A replayed verify POST finds 'paid' and
    returns success without crediting again.
    """
    STATUS_CREATED  = 'created'
    STATUS_PAID     = 'paid'
    STATUS_FAILED   = 'failed'
    STATUS_CHOICES  = [
        (STATUS_CREATED, 'Created'),
        (STATUS_PAID,    'Paid'),
        (STATUS_FAILED,  'Failed'),
    ]

    user     = models.ForeignKey(UserTable, on_delete=models.CASCADE,
                                 related_name='service_orders')
    order_id = models.CharField(max_length=225, unique=True)   # Razorpay order id

    # The quote, frozen. cart_json is {service_key: quantity}; the *_cents
    # fields are integers because money must never round-trip through a float.
    cart_json       = models.JSONField()
    subtotal_cents  = models.BigIntegerField(default=0)
    discount_cents  = models.BigIntegerField(default=0)
    amount_cents    = models.BigIntegerField(default=0)   # what Razorpay was asked to charge
    currency        = models.CharField(max_length=8, default='USD')
    promo_code      = models.CharField(max_length=50, blank=True, default='')
    # Resolved at order time so fulfilment never has to look the code up
    # again — a coupon can be renamed or deactivated between checkout and
    # payment, and the order must still redeem the coupon it was priced with.
    # SET_NULL rather than CASCADE: deleting a coupon must not delete orders.
    coupon          = models.ForeignKey('Coupon', null=True, blank=True,
                                        on_delete=models.SET_NULL,
                                        related_name='service_orders')

    status     = models.CharField(max_length=10, choices=STATUS_CHOICES,
                                  default=STATUS_CREATED, db_index=True)
    payment    = models.OneToOneField('Payment', null=True, blank=True,
                                      on_delete=models.SET_NULL,
                                      related_name='service_order')
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at    = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'service_orders'
        indexes = [models.Index(fields=['user', 'status'], name='svc_order_user_status_idx')]

    def __str__(self):
        return f"{self.order_id} ({self.status})"


class CouponRedemption(models.Model):
    """The missing half of Coupon — admin CRUD existed, redemption did not
    (Coupon.used_count was never incremented anywhere).

    OneToOne on payment guarantees exactly-once redemption per payment at the
    database level, so a replayed payment verification cannot double-count.
    """
    coupon           = models.ForeignKey('Coupon', on_delete=models.CASCADE,
                                         related_name='redemptions')
    user             = models.ForeignKey(UserTable, on_delete=models.CASCADE,
                                         related_name='coupon_redemptions')
    payment          = models.OneToOneField('Payment', on_delete=models.CASCADE,
                                            related_name='coupon_redemption')
    discount_applied = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    redeemed_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'coupon_redemptions'
        ordering = ['-redeemed_at']
        indexes = [models.Index(fields=['coupon', 'user'], name='coupon_redeem_cu_idx')]

    def __str__(self):
        return f"{self.coupon_id} | {self.user_id} | {self.discount_applied}"



# New Model for Payment Table

class Payment(models.Model):
    user = models.ForeignKey(UserTable, on_delete=models.CASCADE)
    # DB-08b: unique constraint makes the idempotency guard race-condition-proof
    order_id = models.CharField(max_length=225, null=True, blank=True, unique=True)
    payment_id = models.CharField(max_length=225, null=True, blank=True, db_index=True)
    payer_id = models.CharField(max_length=225, null=True, blank=True)
    payer_name = models.CharField(max_length=225, null=True, blank=True)
    payer_email = models.CharField(max_length=225, null=True, blank=True)
    payer_address = models.CharField(max_length=225, null=True, blank=True)
    payer_method = models.CharField(max_length=225, null=True, blank=True)
    unit_price = models.CharField(max_length=225, null=True, blank=True)
    amount = models.CharField(max_length=225, null=True, blank=True)
    currency = models.CharField(max_length=225, null=True, blank=True)
    credits = models.CharField(max_length=225, null=True, blank=True)
    payment_time = models.DateTimeField(auto_now_add=True)
    description = models.CharField(max_length=225, null=True, blank=True)
    is_hidden = models.BooleanField(default=False)

    # ── Service-credit purchases (NULL on every legacy PAYG row) ──────────
    # cart_json is the authoritative record of what was bought: it is written
    # at order-creation time from a server-side quote, and it is what the
    # verification step reads to decide how many credits to grant. The browser
    # is never consulted at that point.
    cart_json       = models.JSONField(null=True, blank=True)
    promo_code      = models.CharField(max_length=50, blank=True, default='')
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2,
                                          null=True, blank=True)

    class Meta:
        db_table = 'payment'

    def __str__(self):
        return f"Payment {self.payment_id} by {self.payer_name}"
    
    
# New Model for EmailValidate Table
    
class EmailValidate(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey('UserTable', on_delete=models.CASCADE, db_column='user_id')
    email = models.CharField(max_length=255)
    mx_found = models.CharField(max_length=225)
    mx_record = models.CharField(max_length=225)
    insert_date = models.DateTimeField(default=now)
    is_hidden = models.BooleanField(default=False)

    class Meta:
        db_table = 'email_validate'
        indexes = [
            models.Index(fields=['user', 'insert_date'], name='ev_user_date_idx'),
        ]

    def __str__(self):
        return self.email
    
    
    
# -------------------------------------------------------------------------------------
# New Model for BlocklistMonitor Table

class BlocklistMonitor(models.Model):
    ip_id = models.AutoField(primary_key=True)
    user = models.ForeignKey(UserTable, on_delete=models.CASCADE, related_name='blocklist_monitors')
    ips = models.CharField(max_length=225)
    created_date = models.DateTimeField(default=now)
    deleted_date = models.DateTimeField(null=True, blank=True)
    last_monitor_date = models.DateTimeField(null=True, blank=True)
    listed_count = models.CharField(max_length=225, null=True, blank=True)  # Allow null values
    is_hidden = models.BooleanField(default=False)

    class Meta:
        db_table = 'blocklist_monitor'
        verbose_name = 'Blocklist Monitor'
        verbose_name_plural = 'Blocklist Monitors'

    def __str__(self):
        return f"{self.ips} monitored by {self.user.user_email}"
    
    
    
    
class Blacklists(models.Model):
    Blacklists_ID = models.AutoField(primary_key=True)
    Blacklists_name = models.CharField(max_length=225)
    dnsbl_domain = models.CharField(max_length=225)

    class Meta:
        db_table = 'blacklists'  # This defines the table name in the database.

    def __str__(self):
        return self.Blacklists_name    
    
    
    
    
    
class BlacklistStatus(models.Model):
    # user = models.ForeignKey(UserTable, on_delete=models.CASCADE, related_name='blacklist_statuses')
    ip = models.ForeignKey(BlocklistMonitor, on_delete=models.CASCADE, related_name='blacklist_statuses')
    ips = models.CharField(max_length=225)
    blacklists_name = models.CharField(max_length=225)
    status = models.CharField(max_length=225)
    created_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'blacklist_status'
        verbose_name = 'Blacklist Status'
        verbose_name_plural = 'Blacklist Statuses'

    def __str__(self):
        return f"Status for {self.ips} by {self.user.user_email}"    
    
    

class BlacklistListed(models.Model):
    # user = models.ForeignKey(UserTable, on_delete=models.CASCADE, related_name='blacklist_listed')
    ip = models.ForeignKey(BlocklistMonitor, on_delete=models.CASCADE, related_name='blacklist_listed')
    ips = models.CharField(max_length=225)
    blacklists_name = models.CharField(max_length=225)
    status = models.CharField(max_length=225)
    created_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'blacklist_listed'
        verbose_name = 'Blacklist Listed'
        verbose_name_plural = 'Blacklist Listed'

    def __str__(self):
        return f"Status for {self.ips} by {self.user.user_email}"      
    
    
    
    
# New Model for Payment Table

class SubsPayment(models.Model):
    user = models.ForeignKey(UserTable, on_delete=models.CASCADE)
    # DB-08b: unique constraint makes the idempotency guard race-condition-proof
    order_id = models.CharField(max_length=225, null=True, blank=True, unique=True)
    payment_id = models.CharField(max_length=225, null=True, blank=True, db_index=True)
    payer_id = models.CharField(max_length=225, null=True, blank=True)
    payer_name = models.CharField(max_length=225, null=True, blank=True)
    payer_email = models.CharField(max_length=225, null=True, blank=True)
    payer_address = models.CharField(max_length=225, null=True, blank=True)
    payer_method = models.CharField(max_length=225, null=True, blank=True)
    subs_plan = models.CharField(max_length=225, null=True, blank=True)
    plan_status = models.CharField(max_length=225, null=True, blank=True)
    amount = models.CharField(max_length=225, null=True, blank=True)
    currency = models.CharField(max_length=225, null=True, blank=True)
    vc_credits    = models.CharField(max_length=225, null=True, blank=True)  # was el_credits
    ac_credits    = models.CharField(max_length=225, null=True, blank=True)  # was ip_credits
    cc_credits    = models.CharField(max_length=225, null=True, blank=True)
    billing_cycle = models.CharField(max_length=20, default='monthly')
    payment_time  = models.DateTimeField(auto_now_add=True)
    valid_time    = models.DateTimeField(null=True, blank=True)
    description = models.CharField(max_length=225, null=True, blank=True)
    is_hidden = models.BooleanField(default=False)

    class Meta:
        db_table = 'subspayment'

    def __str__(self):
        return f"SubsPayment {self.payment_id} by {self.payer_name}"
    

# New Model for Email Validation Log
class EmailValidationLog(models.Model):
    user = models.ForeignKey(UserTable, on_delete=models.CASCADE)  # Track per user
    email = models.EmailField()
    validated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "EmailValidationLog"

    def __str__(self):
        return f"{self.email} by {self.user.username} on {self.validated_at}"
    
    
    
    
    
    
# -----------------------------------------------------------------------------
# Domain Blacklist

class DomainBlocklist(models.Model):
    domain_id = models.AutoField(primary_key=True)
    user = models.ForeignKey(UserTable, on_delete=models.CASCADE, related_name='domain_blocklist')
    domain = models.CharField(max_length=225)
    created_date = models.DateTimeField(default=now)
    deleted_date = models.DateTimeField(null=True, blank=True)
    last_monitor_date = models.DateTimeField(null=True, blank=True)
    listed_count = models.CharField(max_length=225, null=True, blank=True)  # Allow null values
    is_hidden = models.BooleanField(default=False)

    class Meta:
        db_table = 'domain_blocklist'
        verbose_name = 'Domain Blocklist'
        verbose_name_plural = 'Domain Blocklist'

    def __str__(self):
        return f"{self.domain} monitored by {self.user.user_email}"
    

    
    
class DomainBlacklistStatus(models.Model):
    # user = models.ForeignKey(UserTable, on_delete=models.CASCADE, related_name='blacklist_statuses')
    domain = models.ForeignKey(DomainBlocklist, on_delete=models.CASCADE, related_name='domain_blacklist_statuses')
    domains = models.CharField(max_length=225)
    blacklists_name = models.CharField(max_length=225)
    status = models.CharField(max_length=225)
    created_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'domain_blacklist_status'
        verbose_name = 'Domain Blacklist Status'
        verbose_name_plural = 'Domain Blacklist Statuses'

    def __str__(self):
        return f"Status for {self.domain} by {self.user.user_email}"    
    
    

class DomainBlacklistListed(models.Model):
    # user = models.ForeignKey(UserTable, on_delete=models.CASCADE, related_name='blacklist_listed')
    domain = models.ForeignKey(DomainBlocklist, on_delete=models.CASCADE, related_name='domain_blacklist_listed')
    domains = models.CharField(max_length=225)
    blacklists_name = models.CharField(max_length=225)
    status = models.CharField(max_length=225)
    created_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'domain_blacklist_listed'
        verbose_name = 'Domain Blacklist Listed'
        verbose_name_plural = 'Domain Blacklist Listed'

    def __str__(self):
        return f"Status for {self.domain} by {self.user.user_email}"   
    
    
    
class DomainBlacklists(models.Model):
    Blacklists_ID = models.AutoField(primary_key=True)
    Blacklists_name = models.CharField(max_length=225)
    dnsbl_domain = models.CharField(max_length=225)

    class Meta:
        db_table = 'domainblacklists'  # This defines the table name in the database.

    def __str__(self):
        return self.Blacklists_name    
    
    
    
    
    
    
class EmailHeader(models.Model):
    RISK_CHOICES = [('SAFE', 'Safe'), ('RISKY', 'Risky'), ('DANGEROUS', 'Dangerous')]
    STATUS_CHOICES = [('pass', 'Pass'), ('fail', 'Fail'), ('unknown', 'Unknown')]

    user = models.ForeignKey(UserTable, on_delete=models.CASCADE, null=True, blank=True)

    raw_header = models.TextField()

    origin_ip  = models.GenericIPAddressField(null=True, blank=True)
    from_email = models.EmailField(null=True, blank=True)
    to_email   = models.EmailField(null=True, blank=True)
    subject    = models.CharField(max_length=255, null=True, blank=True)

    spf_status  = models.CharField(max_length=10, choices=STATUS_CHOICES, default='unknown')
    dkim_status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='unknown')
    dmarc_status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='unknown')
    spam_score  = models.PositiveSmallIntegerField(default=0)
    risk_level  = models.CharField(max_length=10, choices=RISK_CHOICES, default='SAFE')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "EmailHeader"

    def __str__(self):
        return f"Header {self.id}"


class DMARCAnalysis(models.Model):
    user = models.ForeignKey(UserTable, on_delete=models.CASCADE, related_name='dmarc_analyses')
    domain = models.CharField(max_length=255)

    # SPF
    spf_status = models.CharField(max_length=10)
    spf_record = models.TextField(null=True, blank=True)
    spf_includes = models.JSONField(default=list, blank=True)

    # DMARC
    dmarc_status = models.CharField(max_length=10)
    dmarc_record = models.TextField(null=True, blank=True)
    dmarc_policy = models.CharField(max_length=20, null=True, blank=True)
    dmarc_subdomain_policy = models.CharField(max_length=20, null=True, blank=True)
    dmarc_alignment_spf = models.CharField(max_length=10, null=True, blank=True)
    dmarc_alignment_dkim = models.CharField(max_length=10, null=True, blank=True)
    dmarc_rua = models.CharField(max_length=500, null=True, blank=True)
    dmarc_ruf = models.CharField(max_length=500, null=True, blank=True)

    # DKIM
    dkim_status = models.CharField(max_length=10)
    dkim_selector = models.CharField(max_length=100, null=True, blank=True)
    dkim_record = models.TextField(null=True, blank=True)

    # Overall
    analysis_result = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(null=True, blank=True)

    is_hidden = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'dmarc_analysis'

    def __str__(self):
        return f"{self.domain} by {self.user_id}"
    
    



class APIKey(models.Model):
    id         = models.AutoField(primary_key=True)
    user       = models.ForeignKey(
                    UserTable,
                    on_delete=models.CASCADE,
                    db_column='user_id',
                    null=True, blank=True
                 )
    name       = models.CharField(max_length=100)
    key        = models.CharField(max_length=64, unique=True)
    # DB-10: SHA-256 hash of key — auth lookup uses this, not the raw key field
    key_hash   = models.CharField(max_length=64, unique=True, null=True, blank=True)
    is_active  = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "api_keys"


class AllEmails(models.Model):
    file_id    = models.IntegerField(db_index=True)
    user       = models.ForeignKey(UserTable, on_delete=models.CASCADE, db_index=True)
    table_name = models.CharField(max_length=225, db_index=True)
    completed_time = models.DateTimeField(null=True, blank=True)
    email      = models.EmailField(max_length=255, db_index=True)
    validation_results = models.CharField(max_length=50, null=True, blank=True)
    extra_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "all_emails"
        indexes = [
            models.Index(fields=["user", "file_id"]),
            models.Index(fields=["file_id", "validation_results"]),
        ]
        
        unique_together = [("file_id", "email")]

    def __str__(self):
        return f"{self.email} — {self.table_name}"


class Reputation(models.Model):
    user = models.ForeignKey(UserTable, on_delete=models.CASCADE)
    domain = models.CharField(max_length=255)
    status = models.CharField(max_length=100)
    is_hidden = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "reputation"

    def __str__(self):
        return f"{self.domain} - {self.status}"


class ReputationResults(models.Model):
    domain = models.CharField(max_length=255, db_index=True)
    date = models.DateField()
    spam_rate = models.FloatField(null=True, blank=True)
    domain_reputation = models.CharField(max_length=50, null=True, blank=True)
    ip_reputation = models.JSONField(default=list, blank=True)
    delivery_errors = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "reputation_results"
        indexes = [
            models.Index(fields=["domain", "date"]),
        ]

    def __str__(self):
        return f"{self.domain} - {self.date}"


class CampaignList(models.Model):
    STATUS_CHOICES = [('active', 'Active'), ('inactive', 'Inactive')]

    user = models.ForeignKey(UserTable, on_delete=models.CASCADE)
    list_name = models.CharField(max_length=255)
    tags = models.CharField(max_length=255,  default="")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='active')
    total_count = models.PositiveIntegerField(default=0)
    subscribed_count = models.PositiveIntegerField(default=0)
    neversubscribed_count = models.PositiveIntegerField(default=0)
    unsubscribed_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "campaign_list"

    def __str__(self):
        return f"{self.list_name} (user={self.user_id})"


class Segment(models.Model):
    STATUS_CHOICES = [('active', 'Active'), ('inactive', 'Inactive')]
    MATCH_CHOICES  = [('all', 'All (AND)'), ('any', 'Any (OR)')]

    user        = models.ForeignKey(UserTable, on_delete=models.CASCADE)
    name        = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')
    status      = models.CharField(max_length=10, choices=STATUS_CHOICES, default='active')
    match_type  = models.CharField(max_length=3,  choices=MATCH_CHOICES,  default='all')
    rules       = models.JSONField(default=dict)  # {"conditions": [{field, operator, value}, ...]}
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)
    deleted_at  = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'campaign_segments'

    def __str__(self):
        return f'{self.name} (user={self.user_id})'


class CampaignEmail(models.Model):
    SUBSCRIPTION_STATUS = [
        ('subscribed',       'Subscribed'),
        ('never_subscribed', 'Never Subscribed'),
        ('unsubscribed',     'Unsubscribed'),
    ]

    user = models.ForeignKey(UserTable, on_delete=models.CASCADE)
    list = models.ForeignKey(CampaignList, on_delete=models.CASCADE, related_name="emails")
    first_name = models.CharField(max_length=255, blank=True, default="")
    last_name = models.CharField(max_length=255, blank=True, default="")
    email = models.EmailField(max_length=255)
    subscribed = models.CharField(max_length=20, choices=SUBSCRIPTION_STATUS, default='subscribed')
    extra_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "campaign_emails"

    @property
    def display_name(self):
        full_name = f"{self.first_name or ''} {self.last_name or ''}".strip()
        if not full_name:
            full_name = self.email.split('@')[0] if self.email else '—'
        return full_name

    def __str__(self):
        return f"{self.email} - {self.list.list_name}"
    
    



class TemplateLibrary(models.Model):
    CATEGORY_CHOICES = (
        ("newsletter", "Newsletter"),
        ("promotion", "Promotion"),
        ("welcome", "Welcome"),
        ("announcement", "Announcement"),
        ("event", "Event"),
        ("custom", "Custom"),
    )
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=50,choices=CATEGORY_CHOICES,default="custom")
    subject = models.CharField(max_length=500,blank=True,default="")
    html_content = models.TextField()
    design_json = models.JSONField(null=True,blank=True)
    thumbnail = models.ImageField(upload_to="template_library/",null=True,blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True,blank=True)

    class Meta:
        db_table = "template_library"

    def __str__(self):
        return self.name


class UserTemplate(models.Model):

    user = models.ForeignKey(UserTable,on_delete=models.CASCADE)
    library_template = models.ForeignKey(TemplateLibrary,on_delete=models.SET_NULL,null=True,blank=True)
    name = models.CharField(max_length=255)
    subject = models.CharField(max_length=500,blank=True,default="")
    html_content = models.TextField()
    design_json = models.JSONField(null=True,blank=True)
    thumbnail = models.ImageField(upload_to="user_templates/",null=True,blank=True)
    used_in_campaign = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True,blank=True)

    class Meta:
        db_table = "user_templates"

    def __str__(self):
        return f"{self.name} ({self.user_id})"


def _template_image_path(instance, filename):
    import uuid, os
    ext = os.path.splitext(filename)[1].lower() or '.jpg'
    return f'template_images/{instance.user_id}/{uuid.uuid4().hex}{ext}'


class TemplateImage(models.Model):
    user         = models.ForeignKey(UserTable, on_delete=models.CASCADE)
    file         = models.ImageField(upload_to=_template_image_path)
    original_name = models.CharField(max_length=255)
    uploaded_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'template_images'

    def __str__(self):
        return f"{self.original_name} ({self.user_id})"


class Campaign(models.Model):

    STATUS_CHOICES = (
        ("draft", "Draft"),
        ("scheduled", "Scheduled"),
        ("sending", "Sending"),
        ("sent", "Sent"),
        ("failed", "Failed"),
        ("cancelled", "Cancelled"),
    )

    Campaign_ID = models.PositiveIntegerField(unique=True, null=True, blank=True, db_index=True)
    user = models.ForeignKey(UserTable,on_delete=models.CASCADE)
    campaign_name = models.CharField(max_length=255)
    campaign_list    = models.ForeignKey(CampaignList, on_delete=models.CASCADE, null=True, blank=True)
    campaign_segment = models.ForeignKey('Segment', on_delete=models.SET_NULL, null=True, blank=True)
    campaign_lists    = models.ManyToManyField(CampaignList, blank=True, related_name='multi_campaigns')
    campaign_segments = models.ManyToManyField('Segment',    blank=True, related_name='multi_campaigns')
    exclude_lists     = models.ManyToManyField(CampaignList, blank=True, related_name='excluded_campaigns')
    exclude_segments  = models.ManyToManyField('Segment',    blank=True, related_name='excluded_campaigns')
    template = models.ForeignKey(UserTemplate,on_delete=models.SET_NULL,null=True)
    sender_name = models.CharField(max_length=255)
    from_email = models.EmailField()
    reply_email = models.EmailField()
    schedule_at = models.DateTimeField(null=True,blank=True)
    schedule_timezone = models.CharField(max_length=64, default='Asia/Kolkata', blank=True)
    sent_at = models.DateTimeField(null=True,blank=True)
    sent_via = models.CharField(max_length=20, null=True, blank=True)  # 'ses' | 'mailgun'
    last_cloudwatch_sync = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20,choices=STATUS_CHOICES,default="draft")
    total_recipients = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True,blank=True)

    class Meta:
        db_table = "campaigns"

    def save(self, *args, **kwargs):
        if not self.Campaign_ID:
            # DB-05: lock the most-recent row so two concurrent creates can't
            # read the same Max and collide on the unique Campaign_ID constraint.
            from django.db import transaction, IntegrityError
            for _attempt in range(10):
                with transaction.atomic():
                    last_row = (
                        Campaign.objects
                        .select_for_update()
                        .order_by('-Campaign_ID')
                        .first()
                    )
                    last_id = (last_row.Campaign_ID if last_row and last_row.Campaign_ID else 999)
                    self.Campaign_ID = last_id + 1
                    try:
                        super().save(*args, **kwargs)
                        return
                    except IntegrityError:
                        self.Campaign_ID = None
            raise RuntimeError("Could not assign a unique Campaign_ID after 10 attempts")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"[{self.Campaign_ID}] {self.campaign_name}" if self.Campaign_ID else self.campaign_name


class CampaignEvent(models.Model):
    EVENT_TYPE_CHOICES = [
        ('send', 'Send'),
        ('delivery', 'Delivery'),
        ('open', 'Open'),
        ('click', 'Click'),
        ('bounce', 'Bounce'),
        ('complaint', 'Complaint'),
        ('reject', 'Reject'),
        ('unsubscribe', 'Unsubscribe'),
    ]

    user = models.ForeignKey(UserTable, on_delete=models.CASCADE)
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='events')
    campaign_email = models.ForeignKey(
        CampaignEmail, on_delete=models.SET_NULL, null=True, blank=True
    )
    email = models.EmailField()
    message_id = models.CharField(max_length=255)
    event_type = models.CharField(max_length=20, choices=EVENT_TYPE_CHOICES)
    event_time = models.DateTimeField()
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'campaign_events'
        # Prevent duplicate events for the same message + recipient + action
        unique_together = [('message_id', 'email', 'event_type')]
        indexes = [
            models.Index(fields=['campaign', 'event_type']),
            models.Index(fields=['message_id']),
        ]

    def __str__(self):
        return f"{self.event_type} – {self.email} ({self.message_id})"


class CampaignStats(models.Model):
    campaign = models.OneToOneField(Campaign, on_delete=models.CASCADE, related_name='stats')
    total_sent = models.PositiveIntegerField(default=0)
    total_delivered = models.PositiveIntegerField(default=0)
    total_opened = models.PositiveIntegerField(default=0)
    total_clicked = models.PositiveIntegerField(default=0)
    total_bounced = models.PositiveIntegerField(default=0)
    total_complaints = models.PositiveIntegerField(default=0)
    total_rejected = models.PositiveIntegerField(default=0)
    total_unsubscribed = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'campaign_stats'

    def __str__(self):
        return f"Stats for {self.campaign.campaign_name}"


class CampaignTestSend(models.Model):
    campaign    = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='test_sends', null=True, blank=True)
    user        = models.ForeignKey(UserTable, on_delete=models.CASCADE, null=True, blank=True)
    template    = models.ForeignKey(UserTemplate, on_delete=models.SET_NULL, null=True, blank=True)
    recipients  = models.JSONField()
    sender_name = models.CharField(max_length=255, blank=True, default='')
    from_email  = models.EmailField(blank=True, default='')
    reply_email = models.EmailField(blank=True, default='')
    status      = models.CharField(max_length=10)  # 'success' | 'failed'
    error_log   = models.TextField(blank=True, default='')
    sent_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'campaign_test_send'

    def __str__(self):
        name = self.campaign.campaign_name if self.campaign else 'unsaved campaign'
        return f"Test send ({self.status}) for {name}"


class SenderDomain(models.Model):

    STATUS_CHOICES = (
        ('pending',  'Pending'),
        ('verified', 'Verified'),
        ('failed',   'Failed'),
    )
    DKIM_CHOICES = (
        ('NOT_STARTED',       'Not Started'),
        ('PENDING',           'Pending'),
        ('SUCCESS',           'Success'),
        ('FAILED',            'Failed'),
        ('TEMPORARY_FAILURE', 'Temporary Failure'),
    )

    user        = models.ForeignKey(UserTable, on_delete=models.CASCADE, null=True, blank=True)
    domain      = models.CharField(max_length=255)

    # Canonical status — reflects the active provider (EMAIL_PROVIDER setting)
    status      = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    dkim_status = models.CharField(max_length=20, choices=DKIM_CHOICES, default='NOT_STARTED')
    dkim_tokens = models.JSONField(default=list, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)

    # Per-provider tracking — registered with both on add; verified independently
    ses_status          = models.CharField(max_length=20, default='pending')
    ses_dkim_tokens     = models.JSONField(default=list, blank=True)
    ses_verified_at     = models.DateTimeField(null=True, blank=True)
    mailgun_status      = models.CharField(max_length=20, default='pending')
    mailgun_dkim_tokens = models.JSONField(default=list, blank=True)
    mailgun_verified_at = models.DateTimeField(null=True, blank=True)

    added_at   = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    is_hidden  = models.BooleanField(default=False)

    class Meta:
        db_table = 'sender_domains'

    def __str__(self):
        return f"{self.domain} ({self.status})"


class SenderEmailToken(models.Model):
    user       = models.ForeignKey(UserTable, on_delete=models.CASCADE, null=True, blank=True)
    email      = models.EmailField()
    token      = models.CharField(max_length=64, unique=True)
    confirmed  = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    is_hidden  = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'sender_email_tokens'

    def __str__(self):
        return f"{self.email} ({'confirmed' if self.confirmed else 'pending'})"


# ── Admin Console Models ──────────────────────────────────────────────────────

class AdminActivity(models.Model):
    """Audit log for every admin action."""
    admin       = models.ForeignKey(UserTable, on_delete=models.SET_NULL, null=True, related_name='admin_actions')
    action      = models.CharField(max_length=100)
    module      = models.CharField(max_length=50)
    target_type = models.CharField(max_length=50, blank=True)
    target_id   = models.CharField(max_length=50, blank=True)
    target_repr = models.CharField(max_length=255, blank=True)
    old_value   = models.JSONField(null=True, blank=True)
    new_value   = models.JSONField(null=True, blank=True)
    ip_address  = models.GenericIPAddressField(null=True, blank=True)
    user_agent  = models.TextField(blank=True)
    status      = models.CharField(max_length=20, default='success')
    notes       = models.TextField(blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering   = ['-created_at']
        db_table   = 'admin_activity'

    def __str__(self):
        return f"{self.admin} | {self.action} | {self.created_at:%Y-%m-%d %H:%M}"


class Coupon(models.Model):
    DISCOUNT_TYPES = [
        ('percentage',    'Percentage'),
        ('fixed_credits', 'Fixed Credits'),
        ('fixed_amount',  'Fixed Amount'),
    ]
    code           = models.CharField(max_length=50, unique=True)
    discount_type  = models.CharField(max_length=20, choices=DISCOUNT_TYPES)
    discount_value = models.DecimalField(max_digits=10, decimal_places=2)
    max_uses       = models.PositiveIntegerField(null=True, blank=True)
    used_count     = models.PositiveIntegerField(default=0)
    valid_from     = models.DateTimeField()
    valid_until    = models.DateTimeField(null=True, blank=True)
    is_active      = models.BooleanField(default=True)
    description    = models.TextField(blank=True)
    created_at     = models.DateTimeField(auto_now_add=True)
    created_by     = models.ForeignKey(UserTable, on_delete=models.SET_NULL, null=True, related_name='created_coupons')

    # ── Customer-side redemption controls (all optional/defaulted) ────────
    # NULL per_user_limit = unlimited per user. min_order_amount 0 = no
    # minimum. Blank applicable_services = valid on every service; otherwise a
    # comma-separated list of SERVICE_CHOICES keys.
    per_user_limit      = models.PositiveIntegerField(null=True, blank=True)
    min_order_amount    = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    applicable_services = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        db_table = 'coupons'
        ordering = ['-created_at']

    def __str__(self):
        return self.code


class AppSetting(models.Model):
    SETTING_TYPES = [
        ('text',    'Text'),
        ('number',  'Number'),
        ('boolean', 'Boolean'),
        ('json',    'JSON'),
    ]
    GROUPS = [
        ('site',     'Site'),
        ('email',    'Email'),
        ('aws',      'AWS'),
        ('credits',  'Credits'),
        ('limits',   'Limits'),
        ('features', 'Features'),
    ]
    key          = models.CharField(max_length=100, unique=True)
    value        = models.TextField()
    label        = models.CharField(max_length=200)
    description  = models.TextField(blank=True)
    setting_type = models.CharField(max_length=20, choices=SETTING_TYPES, default='text')
    group        = models.CharField(max_length=50, choices=GROUPS, default='site')
    updated_at   = models.DateTimeField(auto_now=True)
    updated_by   = models.ForeignKey(UserTable, on_delete=models.SET_NULL, null=True, blank=True, related_name='updated_settings')

    class Meta:
        db_table = 'app_settings'
        ordering = ['group', 'key']

    def __str__(self):
        return f"{self.group}.{self.key}"


class GuestActivity(models.Model):
    ACTIVITY_CHOICES = [
        ('email_verify', 'Email Verify'),
        ('dmarc_check',  'DMARC Check'),
    ]
    ip_address    = models.GenericIPAddressField()
    activity_type = models.CharField(max_length=20, choices=ACTIVITY_CHOICES)
    input_value   = models.CharField(max_length=320)
    result        = models.CharField(max_length=50, blank=True)
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'guest_activity'
        ordering = ['-created_at']
        indexes  = [
            models.Index(fields=['ip_address', 'activity_type']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"{self.activity_type} | {self.ip_address} | {self.input_value} | {self.result}"


# ── Sales Outreach ────────────────────────────────────────────────────────────

class EmailAccount(models.Model):
    STATUS_CHOICES = (
        ('connected', 'Connected'),
        ('failed',    'Failed'),
        ('unchecked', 'Unchecked'),
    )
    PROVIDER_CHOICES = (
        ('google',    'Google'),
        ('microsoft', 'Microsoft 365'),
    )

    user        = models.ForeignKey(UserTable, on_delete=models.CASCADE, related_name='email_accounts')
    provider    = models.CharField(max_length=20, choices=PROVIDER_CHOICES)
    first_name  = models.CharField(max_length=100, blank=True)
    last_name   = models.CharField(max_length=100, blank=True)
    email       = models.EmailField(max_length=255)
    smtp_host   = models.CharField(max_length=255)
    smtp_port   = models.IntegerField(default=587)
    username    = models.CharField(max_length=255)
    password    = models.CharField(max_length=500)
    daily_limit = models.IntegerField(default=500)
    status      = models.CharField(max_length=20, choices=STATUS_CHOICES, default='unchecked')
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)
    deleted_at  = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'email_accounts'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.email} ({self.provider})"


# ── Sales Outreach (SO) — fully isolated, no shared tables with Email Marketing ──

class SOEmailAccount(models.Model):
    STATUS_CHOICES   = (('connected', 'Connected'), ('failed', 'Failed'), ('unchecked', 'Unchecked'))
    PROVIDER_CHOICES = (('google', 'Google'), ('microsoft', 'Microsoft 365'))

    user         = models.ForeignKey(UserTable, on_delete=models.CASCADE, related_name='so_email_accounts')
    provider     = models.CharField(max_length=20, choices=PROVIDER_CHOICES)
    display_name = models.CharField(max_length=200, blank=True)
    email        = models.EmailField(max_length=255)
    smtp_host    = models.CharField(max_length=255)
    smtp_port    = models.IntegerField(default=587)
    imap_host    = models.CharField(max_length=255, blank=True)
    imap_port    = models.IntegerField(default=993)
    imap_ssl     = models.BooleanField(default=True)
    username     = models.CharField(max_length=255)
    password     = models.CharField(max_length=500)   # signing.dumps(pwd, salt='so-ea-pwd')
    daily_limit  = models.IntegerField(default=50)
    status       = models.CharField(max_length=20, choices=STATUS_CHOICES, default='unchecked')
    warmup_enabled  = models.BooleanField(default=False)
    last_imap_sync  = models.DateTimeField(null=True, blank=True)
    # Cached discovered Sent-folder name (e.g. "[Gmail]/Sent Mail", "Sent Items") —
    # discovered once via IMAP LIST and cached here so it isn't re-discovered on
    # every sync. See services/so_imap.py::_discover_sent_folder.
    sent_folder  = models.CharField(max_length=255, blank=True, default='')
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)
    deleted_at   = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'so_email_accounts'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.email} ({self.provider})"


class SOEmailAccountWarmup(models.Model):
    # 'active' runs continuously and indefinitely — reaching ramp_up_days only
    # caps the daily target at daily_target, it never auto-stops sending or
    # flips status. 'stopped'/'completed' are only ever set by an explicit
    # user action (see services/warmup.py), never by dispatcher/task logic.
    STATUS_CHOICES    = (('active', 'Active'), ('paused', 'Paused'), ('stopped', 'Stopped'), ('completed', 'Completed'))
    account           = models.OneToOneField(SOEmailAccount, on_delete=models.CASCADE, related_name='warmup')
    daily_target      = models.IntegerField(default=40)
    daily_current     = models.IntegerField(default=2)
    ramp_up_days      = models.IntegerField(default=30)
    ramp_up_increment = models.IntegerField(default=2)
    started_at        = models.DateTimeField(null=True, blank=True)
    status            = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')

    class Meta:
        db_table = 'so_email_account_warmups'


class SOProspect(models.Model):
    # Mirrors CampaignEmail.subscribed so Sales Outreach and Email Marketing
    # speak the same consent language.
    STATUS_CHOICES = (
        ('subscribed',       'Subscribed'),
        ('unsubscribed',     'Unsubscribed'),
        ('never_subscribed', 'Never Subscribed'),
    )

    user       = models.ForeignKey(UserTable, on_delete=models.CASCADE, related_name='so_prospects')
    first_name = models.CharField(max_length=100, blank=True)
    last_name  = models.CharField(max_length=100, blank=True)
    email      = models.EmailField(max_length=255)
    company    = models.CharField(max_length=255, blank=True)
    phone      = models.CharField(max_length=50, blank=True)
    status     = models.CharField(max_length=20, choices=STATUS_CHOICES, default='subscribed')
    extra_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'so_prospects'
        ordering = ['-created_at']
        indexes  = [
            models.Index(fields=['user', 'email']),
            models.Index(fields=['user', 'status']),
            models.Index(fields=['email']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f"{self.email}"


class SOList(models.Model):
    STATUS_CHOICES = [('active', 'Active'), ('inactive', 'Inactive')]

    user        = models.ForeignKey(UserTable, on_delete=models.CASCADE, related_name='so_lists')
    name        = models.CharField(max_length=255)
    tags        = models.CharField(max_length=255, blank=True, default='')
    status      = models.CharField(max_length=10, choices=STATUS_CHOICES, default='active')
    total_count           = models.PositiveIntegerField(default=0)
    subscribed_count      = models.PositiveIntegerField(default=0)
    neversubscribed_count = models.PositiveIntegerField(default=0)
    unsubscribed_count    = models.PositiveIntegerField(default=0)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)
    deleted_at  = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'so_lists'
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class SOSegment(models.Model):
    STATUS_CHOICES = [('active', 'Active'), ('inactive', 'Inactive')]
    MATCH_CHOICES  = [('all', 'All (AND)'), ('any', 'Any (OR)')]

    user        = models.ForeignKey(UserTable, on_delete=models.CASCADE, related_name='so_segments')
    name        = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')
    status      = models.CharField(max_length=10, choices=STATUS_CHOICES, default='active')
    match_type  = models.CharField(max_length=3,  choices=MATCH_CHOICES,  default='all')
    rules       = models.JSONField(default=dict)  # {"groups": [{connector, match_type, conditions:[...]}]}
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)
    deleted_at  = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'so_segments'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} (user={self.user_id})'


class SOListProspect(models.Model):
    so_list  = models.ForeignKey(SOList,     on_delete=models.CASCADE, related_name='list_prospects')
    prospect = models.ForeignKey(SOProspect, on_delete=models.CASCADE, related_name='prospect_lists')
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table        = 'so_list_prospects'
        unique_together = [('so_list', 'prospect')]


class SOCampaign(models.Model):
    STATUS_CHOICES = (
        ('draft',     'Draft'),
        ('scheduled', 'Scheduled'),
        ('sending',   'Sending'),
        ('sent',      'Sent'),
        ('paused',    'Paused'),
        ('failed',    'Failed'),
        ('cancelled', 'Cancelled'),
    )

    SEND_MODE_CHOICES = (('single', 'Single'), ('sequence', 'Sequence'))

    user            = models.ForeignKey(UserTable, on_delete=models.CASCADE, related_name='so_campaigns')
    name            = models.CharField(max_length=255)
    recipient_lists    = models.ManyToManyField(SOList, blank=True, db_table='so_campaign_lists',
                                                related_name='campaigns')
    recipient_segments = models.ManyToManyField(SOSegment, blank=True, db_table='so_campaign_segments',
                                                related_name='campaigns')
    exclude_lists      = models.ManyToManyField(SOList, blank=True, db_table='so_campaign_exclude_lists',
                                                related_name='excluded_campaigns')
    exclude_segments   = models.ManyToManyField(SOSegment, blank=True, db_table='so_campaign_exclude_segments',
                                                related_name='excluded_campaigns')
    # subject / preview_text / html_body mirror step 1 variation A so the existing
    # one-shot sender keeps working while the sequence engine is not built yet.
    subject         = models.CharField(max_length=500)
    preview_text    = models.CharField(max_length=200, blank=True)
    html_body       = models.TextField()
    send_mode       = models.CharField(max_length=10, choices=SEND_MODE_CHOICES, default='single')
    from_name       = models.CharField(max_length=255, blank=True, default='')
    reply_to        = models.CharField(max_length=255, blank=True, default='')
    status          = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    schedule_at     = models.DateTimeField(null=True, blank=True)
    schedule_timezone = models.CharField(max_length=64, default='Asia/Kolkata')
    sent_at         = models.DateTimeField(null=True, blank=True)

    # Sending Days & Hours — an ongoing constraint on every send this campaign
    # ever makes (not just step 1's launch, which schedule_at/schedule_timezone
    # already cover). Evaluated in schedule_timezone. Defaults represent
    # "unrestricted" (every day, effectively the full day) so existing campaigns
    # behave exactly as before this field existed — it's opt-in, narrowed only
    # when a user explicitly turns the UI toggle on. See services/so_drip.py
    # _in_send_window / _next_window_start for enforcement.
    send_weekdays   = models.CharField(max_length=27, default='mon,tue,wed,thu,fri,sat,sun')
    send_hour_start = models.TimeField(default=time(0, 0, 0))
    send_hour_end   = models.TimeField(default=time(23, 59, 59))
    # Per-campaign override of the global ENABLE_EMAIL_TRACKING setting —
    # AND'd together at send time (services/so_drip.py::send_next_step), so
    # this can only ever narrow tracking further, never re-enable it when
    # the account-wide flag is off. default=True preserves today's effective
    # behavior for every existing campaign (previously the only gate was the
    # global flag, i.e. every campaign behaved as if this were True).
    tracking_enabled = models.BooleanField(default=True)
    total_sent         = models.PositiveIntegerField(default=0)
    total_delivered    = models.PositiveIntegerField(default=0)
    total_opened       = models.PositiveIntegerField(default=0)
    total_clicked      = models.PositiveIntegerField(default=0)
    total_replied      = models.PositiveIntegerField(default=0)
    total_unsubscribed = models.PositiveIntegerField(default=0)
    total_bounced      = models.PositiveIntegerField(default=0)
    total_complained   = models.PositiveIntegerField(default=0)
    total_failed       = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'so_campaigns'
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class SOSequenceStep(models.Model):
    """One email in a Sales Outreach sequence.

    `wait_days` / `wait_hours` are the delay BEFORE this step fires, so the gap
    travels with its own step when steps are reordered. Step 0 is always 0.
    """

    campaign   = models.ForeignKey(SOCampaign, on_delete=models.CASCADE, related_name='steps')
    order      = models.PositiveIntegerField(default=0)          # 0-based
    wait_days  = models.PositiveIntegerField(default=0)
    wait_hours = models.PositiveIntegerField(default=0)
    name       = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'so_sequence_steps'
        ordering = ['order']
        indexes  = [models.Index(fields=['campaign', 'order'], name='so_seq_step_camp_order_idx')]

    def __str__(self):
        return f'Step {self.order + 1} (campaign={self.campaign_id})'


class SOSequenceVariant(models.Model):
    """An A/B variation of a sequence step — its own subject, preheader and body."""

    step       = models.ForeignKey(SOSequenceStep, on_delete=models.CASCADE, related_name='variants')
    label      = models.CharField(max_length=2, default='A')     # 'A'..'D'
    name       = models.CharField(max_length=255, blank=True, default='')
    subject    = models.CharField(max_length=500, blank=True, default='')
    preheader  = models.CharField(max_length=200, blank=True, default='')
    html_body  = models.TextField(blank=True, default='')
    weight     = models.PositiveIntegerField(default=1)
    is_active  = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'so_sequence_variants'
        ordering = ['label']

    def __str__(self):
        return f'{self.step_id}{self.label}'


class SOSequenceCondition(models.Model):
    """V3.1 branching foundation — a single YES/NO fork evaluated at a specific
    main-sequence step.

    trigger_type='no_event_after_days' extends (does not replace) the
    existing SOSubsequence "no reply after N days" mechanism, and reuses its
    exact eligibility shape: status must still be 'active'/'completed' — a
    reply/bounce/complaint/unsubscribe has already moved anything else to
    'stopped' via so_drip.stop(), on a wholly separate code path, before this
    is ever evaluated — and enough time must have elapsed since the
    contact's last successful send. See
    services/so_subsequence.py::eligible_condition_branch.

    Scoped to the MAIN sequence only — source_step/yes_target_step/
    no_target_step are all SOSequenceStep, never SOSubsequenceStep — so this
    stays free of the step-number ambiguity that already exists between the
    main sequence and subsequence tracks (current_step is reused/reset by
    both, see SOCampaignContact.active_subsequence below).

    'no_event_after_days' (V3.1), 'clicked' (V3.3), 'opened' (V3.6 Phase 2),
    and 'replied' (V3.7) are implemented. 'clicked'/'opened'/'replied' use a
    DIFFERENT matching semantic than 'no_event_after_days' — see
    services/so_subsequence.py::eligible_condition_branch for why (in
    short: 'no_event_after_days' preemptively reroutes a contact before
    source_step would otherwise be sent, so it's only ever found while
    current_step == source_step.order; 'clicked'/'opened'/'replied' check
    engagement with a step that has ALREADY been sent, so they must remain
    findable even once current_step has moved well past source_step —
    guarded against re-firing on every later dispatcher tick via
    last_condition_id).
    'opened' relies on SOOpenPixel (V3.6 Phase 1) for exact per-step
    attribution — the legacy contact-scoped open pixel (still served
    unmodified by views/so_tracking.py::so_track_open) always produces
    step_order=NULL events, which can never match this trigger type's
    exact-step filter, so a legacy open can never satisfy it.
    'replied' relies on services/so_imap.py::_record_once computing a
    precise step_order for 'replied' SOEvent rows from the reply's own
    threading headers (matched against the exact per-step 'sent' SOEvent
    history), falling back to the pre-V3.7 heuristic only when no precise
    match is possible — see that function's docstring. Never satisfied by
    an out-of-office auto-reply (SOEvent.metadata['oof']), and never
    ignores event_count_threshold's absence the way clicked/opened can use
    it — one genuine reply is always sufficient, so this trigger type never
    reads event_count_threshold at all.
    bounced/complained/unsubscribed/event_count_gte remain deferred to
    later V3 phases — TRIGGER_CHOICES is kept extensible for them, but
    nothing here evaluates them yet.
    """
    TRIGGER_CHOICES = (
        ('no_event_after_days', 'No event after N days'),
        ('clicked',             'Clicked'),
        ('opened',               'Opened'),
        ('replied',              'Replied'),
    )

    campaign = models.ForeignKey(SOCampaign, on_delete=models.CASCADE, related_name='conditions')
    # Which step this condition evaluates after. Nullable at the field level,
    # but a condition with no source_step is never evaluated — see
    # eligible_condition_branch, which matches this against the contact's
    # current position before doing anything else.
    source_step = models.ForeignKey(SOSequenceStep, on_delete=models.CASCADE, null=True, blank=True,
                                    related_name='conditions_as_source')
    trigger_type = models.CharField(max_length=30, choices=TRIGGER_CHOICES, default='no_event_after_days')
    wait_days    = models.PositiveIntegerField(default=0)
    # Reserved for a later V3 trigger type (e.g. "open count >= N");
    # unused by 'no_event_after_days'.
    event_count_threshold = models.PositiveIntegerField(null=True, blank=True)
    yes_target_step = models.ForeignKey(SOSequenceStep, on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name='conditions_as_yes_target')
    no_target_step  = models.ForeignKey(SOSequenceStep, on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name='conditions_as_no_target')
    is_active  = models.BooleanField(default=True)
    # V4.0 — optional membership in a SOConditionGroup. NULL (the default,
    # and every row that existed before V4.0) behaves exactly as today: this
    # condition is evaluated standalone by eligible_condition_branch, with
    # its OWN yes_target_step/no_target_step/wait_days. Once set, this
    # condition's yes_target_step/no_target_step/wait_days are ignored —
    # the group is the sole owner of the branch decision and its targets
    # (see SOConditionGroup below) — eligible_condition_branch's own query
    # excludes group__isnull=False rows so a grouped condition is never
    # independently evaluated on top of its group. SET_NULL (not CASCADE):
    # deleting a group must detach its member conditions, never delete them.
    group = models.ForeignKey('SOConditionGroup', on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='conditions')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'so_sequence_conditions'
        indexes  = [models.Index(fields=['campaign', 'source_step'], name='so_seqcond_camp_step_idx')]

    def clean(self):
        from django.core.exceptions import ValidationError

        errors = {}
        if self.source_step_id and self.source_step.campaign_id != self.campaign_id:
            errors['source_step'] = 'Source step must belong to the same campaign as this condition.'
        if self.yes_target_step_id and self.yes_target_step.campaign_id != self.campaign_id:
            errors['yes_target_step'] = 'Yes-target step must belong to the same campaign as this condition.'
        if self.no_target_step_id and self.no_target_step.campaign_id != self.campaign_id:
            errors['no_target_step'] = 'No-target step must belong to the same campaign as this condition.'
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f'Condition {self.id} ({self.trigger_type}, campaign={self.campaign_id})'


class SOConditionGroup(models.Model):
    """V4.0 — combine 2+ existing SOSequenceCondition rows (source_step
    unchanged, still a single YES/NO fork) with AND/OR logic, without
    altering how a standalone (group=NULL) condition works at all.

    Deliberately owns the target steps and wait_days itself, rather than
    reusing a member condition's own fields — a member's own
    yes_target_step/no_target_step/wait_days are simply never read once it
    has a group (see SOSequenceCondition.group's own comment), so there is
    exactly one place a reader looks to find "what does this branch do."

    V4.0 scope, enforced at the views/so_sender.py write path (not here —
    same layering as SOSequenceCondition, whose own clean() is likewise
    never the sole enforcement point): every member condition must share
    this group's exact source_step (one shared sent_at reference point for
    wait_days — see services/so_subsequence.py::_eval_group), a group needs
    2+ members, a condition belongs to at most one group, no nested groups
    (this model has no FK to another group), and member trigger_type is
    restricted to 'clicked'/'opened'/'replied' — 'no_event_after_days' has
    no independent "is this true right now" predicate to AND/OR combine
    (it is a pure timeout), so it is never a valid group member.
    """
    LOGIC_CHOICES = (
        ('and', 'AND — every condition must be true'),
        ('or',  'OR — any condition may be true'),
    )

    campaign = models.ForeignKey(SOCampaign, on_delete=models.CASCADE, related_name='groups')
    source_step = models.ForeignKey(SOSequenceStep, on_delete=models.CASCADE, null=True, blank=True,
                                    related_name='condition_groups_as_source')
    logic = models.CharField(max_length=3, choices=LOGIC_CHOICES, default='and')
    wait_days = models.PositiveIntegerField(default=0)
    yes_target_step = models.ForeignKey(SOSequenceStep, on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name='condition_groups_as_yes_target')
    no_target_step  = models.ForeignKey(SOSequenceStep, on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name='condition_groups_as_no_target')
    is_active  = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'so_condition_groups'
        indexes  = [models.Index(fields=['campaign', 'source_step'], name='so_condgrp_camp_step_idx')]

    def __str__(self):
        return f'Group {self.id} ({self.logic}, campaign={self.campaign_id})'


class SOEmailAccountRotation(models.Model):
    campaign = models.ForeignKey(SOCampaign,    on_delete=models.CASCADE, related_name='account_rotations')
    account  = models.ForeignKey(SOEmailAccount, on_delete=models.CASCADE, related_name='campaign_rotations')
    weight   = models.PositiveIntegerField(default=1)
    order    = models.PositiveIntegerField(default=0)

    class Meta:
        db_table        = 'so_email_account_rotations'
        unique_together = [('campaign', 'account')]
        ordering        = ['order']


class SOEmailAccountDailyUsage(models.Model):
    """Atomic per-account-per-UTC-day send counter.

    Reserved BEFORE a send attempt via a conditional UPDATE (sent_count__lt=
    daily_limit), released if the attempt fails — see services/so_drip.py
    _reserve_quota_slot / _release_quota_slot. daily_limit is enforced per
    account globally, across every campaign that account is used in, since it's
    a real per-mailbox constraint, not a per-campaign one.
    """
    account    = models.ForeignKey(SOEmailAccount, on_delete=models.CASCADE, related_name='daily_usage')
    date       = models.DateField()
    sent_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table        = 'so_email_account_daily_usage'
        unique_together = [('account', 'date')]


class SOCampaignContact(models.Model):
    # 'pending'/'sent'/'failed'/'skipped' are the legacy one-shot values, kept for
    # backward compatibility with existing rows. 'active'/'sending'/'completed'/
    # 'stopped' drive the multi-step sequence engine (tasks/so_send_campaign.py).
    STATUS_CHOICES = (
        ('pending',   'Pending'),
        ('active',    'Active'),
        ('sending',   'Sending'),
        ('sent',      'Sent'),
        ('completed', 'Completed'),
        ('stopped',   'Stopped'),
        ('failed',    'Failed'),
        ('skipped',   'Skipped'),
    )

    campaign       = models.ForeignKey(SOCampaign,  on_delete=models.CASCADE,   related_name='campaign_contacts')
    prospect       = models.ForeignKey(SOProspect,  on_delete=models.SET_NULL,  null=True, blank=True,
                                       related_name='campaign_contacts')
    email          = models.CharField(max_length=255)
    tracking_token = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)
    message_id     = models.CharField(max_length=255, blank=True)
    status         = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    sent_at        = models.DateTimeField(null=True, blank=True)
    error          = models.TextField(blank=True)

    # ── Sequence state (Phase 1) ────────────────────────────────────────────
    # current_step: the step `order` still owed to this recipient; 0 = step 1 not
    # yet sent. Reaching len(steps) means the sequence is done for them.
    current_step   = models.PositiveIntegerField(default=0)
    variant_label  = models.CharField(max_length=2, default='A')
    next_action_at = models.DateTimeField(null=True, blank=True)
    completed_at   = models.DateTimeField(null=True, blank=True)
    attempts       = models.PositiveIntegerField(default=0)

    # Sender account assigned once at enrollment (round-robin across the campaign's
    # selected accounts) and reused for every step of this recipient's sequence —
    # see services/so_drip.py::_get_contact_account. NULL on legacy rows created
    # before this field existed; those self-heal on their next send.
    account = models.ForeignKey('SOEmailAccount', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='so_campaign_contacts')

    # NULL = following the main sequence (campaign.steps). Set once a "no reply
    # after N days" trigger fires (services/so_subsequence.py::branch_contact) —
    # from that point on, current_step indexes into active_subsequence.steps
    # instead of campaign.steps. See so_drip.py::_resolve_step_and_variant.
    active_subsequence = models.ForeignKey('SOSubsequence', on_delete=models.SET_NULL, null=True, blank=True,
                                           related_name='subsequence_contacts')

    # ── V3.1 branching foundation ───────────────────────────────────────────
    # Append-only audit/debug breadcrumb of how this contact reached its
    # current position, e.g. "main:0>cond:12:yes>main:3" — never read by the
    # dispatcher or send path (current_step/active_subsequence remain the
    # only fields that actually drive sending). See
    # services/so_subsequence.py::branch_via_condition.
    branch_path = models.CharField(max_length=500, blank=True, default='')
    # Deliberately NOT a FK — purely a "what last routed this contact"
    # reference for analytics/debugging, the same not-a-FK rationale as
    # SOEvent.message_id above (a hard FK to SOSequenceCondition would force
    # this column null on delete and lose the audit trail; a plain id never
    # does).
    last_condition_id = models.PositiveIntegerField(null=True, blank=True, default=None)
    # V4.0 — the group-branching mirror of last_condition_id above, kept as
    # a SEPARATE field rather than overloading last_condition_id with two
    # different id-namespaces (a SOSequenceCondition.id vs a
    # SOConditionGroup.id) — see services/so_subsequence.py::branch_via_group.
    # Same not-a-FK rationale as last_condition_id.
    last_condition_group_id = models.PositiveIntegerField(null=True, blank=True, default=None)

    class Meta:
        db_table        = 'so_campaign_contacts'
        unique_together = [('campaign', 'email')]
        indexes         = [
            models.Index(fields=['campaign', 'status']),
            models.Index(fields=['status', 'next_action_at'], name='so_cc_status_next_idx'),
        ]


class SOTrackedLink(models.Model):
    campaign_contact = models.ForeignKey(SOCampaignContact, on_delete=models.CASCADE, related_name='tracked_links')
    token           = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)
    destination_url = models.TextField()
    # V3.2 — exact step attribution. Unlike the open pixel (one per-CONTACT
    # tracking_token, reused unchanged across every step's email), a
    # SOTrackedLink row is created fresh per send, per link
    # (services/so_smtp.py::inject_tracking), so it can record exactly which
    # step generated it — no join/inference needed later. NULL on every row
    # created before this field existed (never backfilled — see
    # services/so_smtp.py::inject_tracking's step_order parameter for how
    # new rows get it).
    step_order = models.PositiveSmallIntegerField(null=True, blank=True, default=None)
    created_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'so_tracked_links'


class SOOpenPixel(models.Model):
    """V3.6 — exact step attribution for opens.

    The legacy open pixel (still served unmodified by views/so_tracking.py::
    so_track_open) embeds cc.tracking_token, ONE per-CONTACT token reused
    unchanged across every step's email — so an open of that pixel can never
    identify which step's email produced it. SOTrackedLink can't be reused
    for this either: it's created only when the body actually contains a
    link, its destination_url/redirect semantics are specific to clicks, and
    conflating "static pixel request" with "click redirect target" inside
    one model/view would risk the working, already-tested click-tracking
    path this phase is required to leave untouched.

    So this is a deliberately separate, minimal model, created fresh per
    send (services/so_smtp.py::inject_tracking) exactly like SOTrackedLink
    already is — one row per tracking-enabled step-send, never more. A
    click on step 0's link still carries step_order=0 no matter how many
    later steps have since been sent; the same is now true for opens.
    NULL step_order is never produced by this model (inject_tracking only
    ever builds one when it has a real step_order in hand) — NULL opens
    only ever come from the legacy cc.tracking_token pixel, which this
    model has no relationship to at all.
    """
    campaign_contact = models.ForeignKey(SOCampaignContact, on_delete=models.CASCADE, related_name='open_pixels')
    token      = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)
    step_order = models.PositiveSmallIntegerField(null=True, blank=True, default=None)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'so_open_pixels'
        # No unique_together on (campaign_contact, step_order): current_step
        # resets to 0 when a contact branches into a subsequence
        # (services/so_subsequence.py::branch_contact), so the SAME
        # step_order value legitimately recurs across a contact's different
        # tracks/sends over their lifetime -- discovered via a real send
        # failure (IntegrityError) in V3.6 Phase 1 testing, not a guess.
        # Attribution never relied on this pair being unique anyway: each
        # row's own random `token` is what a later open resolves against,
        # exactly like SOTrackedLink (which has no such constraint either,
        # for the same reason).


class SOEvent(models.Model):
    EVENT_CHOICES = (
        ('sent',         'Sent'),
        ('delivered',    'Delivered'),
        ('opened',       'Opened'),
        ('clicked',      'Clicked'),
        ('replied',      'Replied'),
        ('unsubscribed', 'Unsubscribed'),
        ('bounced',      'Bounced'),
        ('complained',   'Complained'),
    )

    campaign   = models.ForeignKey(SOCampaign,  on_delete=models.CASCADE,  related_name='events')
    prospect   = models.ForeignKey(SOProspect,  on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='so_events')
    # The sender account that actually sent the campaign email this event is
    # about — always copied from an already-correctly-scoped source
    # (SOCampaignContact.account, or the `account` a send already has in
    # hand), never independently looked up or guessed from the email
    # address. SET_NULL so deleting an account doesn't destroy event history,
    # it just loses that one attribution.
    account    = models.ForeignKey('SOEmailAccount', on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='events')
    # Identifies which outbound message (SOCampaignContact.message_id /
    # SOMessage.message_id) this event is about. A plain identifier rather
    # than a real FK to SOMessage on purpose: at 'sent'/'delivered' creation
    # time (services/so_drip.py::_record_success) the SOMessage row for this
    # send doesn't exist yet — it's written a few lines later by
    # _record_conversation_send — so a hard FK would either be null at
    # creation (defeating the point) or force reordering tested V1 send
    # logic. The identifier is always known up front (it's generated before
    # either row is written) and lets any reader join to SOMessage via
    # (account, message_id) whenever that row does exist.
    message_id = models.CharField(max_length=255, blank=True, default='', db_index=True)
    email      = models.CharField(max_length=255)
    event_type = models.CharField(max_length=20, choices=EVENT_CHOICES)
    metadata   = models.JSONField(default=dict, blank=True)
    # V3.2 — a typed, exact step number, populated only where the writer
    # already has reliable step context at write time (see
    # services/so_drip.py::_record_success for sent/delivered,
    # services/so_imap.py::_record_once for replied/bounced/complained, and
    # views/so_tracking.py::so_track_click via SOTrackedLink.step_order).
    # Deliberately left NULL wherever no reliable source exists — most
    # notably 'opened' events, since the open pixel uses one per-contact
    # tracking_token reused across every step, so no artifact reachable from
    # so_track_open identifies which step's email produced a given open.
    # Never backfilled on historical rows.
    step_order = models.PositiveSmallIntegerField(null=True, blank=True, default=None)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'so_events'
        indexes  = [
            models.Index(fields=['campaign', 'email', 'event_type']),
            models.Index(fields=['campaign', 'event_type']),
            models.Index(fields=['campaign', 'step_order', 'event_type'], name='so_event_camp_step_type_idx'),
        ]


class SOTag(models.Model):
    """Reusable per-user tag catalog for inbox conversations (not freeform
    per-row strings), so tags can be reused/autocompleted like PlusVibe/Instantly."""

    user       = models.ForeignKey(UserTable, on_delete=models.CASCADE, related_name='so_tags')
    name       = models.CharField(max_length=50)
    color      = models.CharField(max_length=7, default='#0099CC')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table        = 'so_tags'
        unique_together = [('user', 'name')]
        ordering        = ['name']

    def __str__(self):
        return self.name


class SOConversation(models.Model):
    """The Inbox's unit of work — one per SOCampaignContact, created lazily on
    that contact's first outbound send or first inbound reply.

    Folder membership (Needs Reply / Waiting / Interested / Out of Office / ...)
    is deliberately NOT stored as separate booleans — it's derived at query time
    from `last_message_direction` + `is_archived` + `classification`, so there is
    one source of truth instead of flags that could drift out of sync.
    """
    CLASSIFICATION_CHOICES = (
        ('interested',     'Interested'),
        ('meeting',        'Meeting'),
        ('question',       'Question'),
        ('not_interested', 'Not Interested'),
        ('out_of_office',  'Out of Office'),
        ('unsubscribe',    'Unsubscribed'),
        ('wrong_person',   'Wrong Person'),
        ('positive',       'Positive'),
        ('negative',       'Negative'),
    )
    DIRECTION_CHOICES = (('outbound', 'Outbound'), ('inbound', 'Inbound'))

    # Nullable — a conversation no longer requires a campaign enrollment (Unibox
    # upgrade). `thread_key` is the real grouping/dedup identity now: 'cc:<id>'
    # for campaign-linked threads (mirrors the uniqueness the old required
    # OneToOne gave), 'acct:<account_id>:<email>' for everything else. MySQL
    # doesn't support conditional/partial unique indexes, so thread_key is one
    # always-present unique column rather than a nullable-FK-based constraint.
    campaign_contact = models.OneToOneField(SOCampaignContact, on_delete=models.CASCADE,
                                            null=True, blank=True, related_name='conversation')
    # Denormalized from campaign_contact for cheap filtering without a join.
    campaign   = models.ForeignKey(SOCampaign, on_delete=models.CASCADE, null=True, blank=True,
                                   related_name='conversations')
    thread_key = models.CharField(max_length=300, unique=True, db_index=True)
    prospect = models.ForeignKey(SOProspect, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='so_conversations')
    account  = models.ForeignKey(SOEmailAccount, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='conversations')
    email    = models.CharField(max_length=255)

    is_unread      = models.BooleanField(default=False)
    is_archived    = models.BooleanField(default=False)
    classification = models.CharField(max_length=20, choices=CLASSIFICATION_CHOICES, blank=True, default='')
    tags           = models.ManyToManyField(SOTag, blank=True, related_name='conversations')

    subject                = models.CharField(max_length=500, blank=True, default='')
    last_message_at        = models.DateTimeField(null=True, blank=True)
    last_message_preview   = models.CharField(max_length=300, blank=True, default='')
    last_message_direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'so_conversations'
        indexes  = [
            models.Index(fields=['campaign', 'is_unread']),
            models.Index(fields=['campaign', 'classification']),
            models.Index(fields=['campaign', 'is_archived']),
            models.Index(fields=['account', 'is_unread']),
            models.Index(fields=['account', 'is_archived']),
            models.Index(fields=['account', 'classification']),
            models.Index(fields=['-last_message_at']),
        ]

    def __str__(self):
        return f'Conversation({self.email}, campaign={self.campaign_id})'


class SOMessage(models.Model):
    """One row per actual email — inbound reply or outbound send/reply.

    `is_sequence_step` distinguishes an automated step send from a manual reply
    typed in the composer; both are `direction='outbound'`.
    """
    DIRECTION_CHOICES = (('outbound', 'Outbound'), ('inbound', 'Inbound'))

    conversation     = models.ForeignKey(SOConversation, on_delete=models.CASCADE, related_name='messages')
    # Denormalized from conversation.account — lets a single physical email be
    # deduped account-wide (see Meta.unique_together) even when it's re-observed
    # under a different conversation/thread_key (e.g. a campaign send later seen
    # again via a Sent-folder scan).
    account          = models.ForeignKey(SOEmailAccount, on_delete=models.SET_NULL, null=True, blank=True,
                                         related_name='messages')
    direction        = models.CharField(max_length=10, choices=DIRECTION_CHOICES)
    is_sequence_step = models.BooleanField(default=False)

    subject   = models.CharField(max_length=500, blank=True, default='')
    body_html = models.TextField(blank=True, default='')
    body_text = models.TextField(blank=True, default='')

    from_email = models.CharField(max_length=255)
    to_email   = models.CharField(max_length=255)
    cc_email   = models.CharField(max_length=1000, blank=True, default='')
    bcc_email  = models.CharField(max_length=1000, blank=True, default='')

    # NULL (not '') when no Message-ID header was present — MySQL unique indexes
    # treat repeated '' as duplicates but allow unlimited NULLs, and this field
    # is now part of a real dedup constraint (see unique_together below).
    message_id  = models.CharField(max_length=255, blank=True, null=True, default=None)
    # Fallback dedup key for a message that had NO Message-ID header — a SHA-256
    # fingerprint of (account, direction, from, to, normalized subject,
    # normalized body, exact parsed sent_at), computed and populated only when
    # message_id is None (see services/so_inbox.py::_headerless_fingerprint).
    # Same NULL-not-'' reasoning as message_id above: for a message that DOES
    # have a Message-ID, this stays NULL so it never collides with anything
    # under the second unique_together entry below.
    headerless_fingerprint = models.CharField(max_length=64, blank=True, null=True, default=None)
    in_reply_to = models.CharField(max_length=255, blank=True, default='')

    has_attachments = models.BooleanField(default=False)

    sent_at    = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'so_messages'
        indexes  = [
            models.Index(fields=['conversation', 'created_at']),
            models.Index(fields=['message_id']),
        ]
        unique_together = [
            ('account', 'message_id'),
            ('account', 'headerless_fingerprint'),
        ]

    def __str__(self):
        return f'{self.direction} message({self.conversation_id})'


class SOConversationNote(models.Model):
    """Private note visible only to the user — never sent to the prospect.
    Kept separate from SOMessage so it doesn't carry meaningless email-only
    fields (from_email/direction) and is visually unambiguous in the timeline."""

    conversation = models.ForeignKey(SOConversation, on_delete=models.CASCADE, related_name='notes')
    user         = models.ForeignKey(UserTable, on_delete=models.CASCADE, related_name='so_conversation_notes')
    body         = models.TextField()
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'so_conversation_notes'
        ordering = ['created_at']


class SOSubsequence(models.Model):
    """A branching follow-up track: 'if no reply within trigger_days, move the
    contact onto this track's own steps instead.' Chained via `order` — a
    contact becomes eligible for the subsequence at order=k+1 once trigger_days
    have passed with no reply since their last send on order=k (or the main
    sequence, if k is the first one). See services/so_subsequence.py.

    Mirrors SOCampaign -> SOSequenceStep -> SOSequenceVariant structurally
    (own Step/Variant models below) rather than making the main-sequence models
    polymorphic — keeps the main sequence's send path completely untouched.
    """
    TRIGGER_CHOICES = (
        ('no_reply', 'No reply'),
    )

    campaign      = models.ForeignKey(SOCampaign, on_delete=models.CASCADE, related_name='subsequences')
    name          = models.CharField(max_length=255, blank=True, default='')
    order         = models.PositiveIntegerField(default=0)
    trigger_type  = models.CharField(max_length=20, choices=TRIGGER_CHOICES, default='no_reply')
    trigger_days  = models.PositiveIntegerField(default=3)
    is_active     = models.BooleanField(default=True)
    created_at    = models.DateTimeField(auto_now_add=True)
    updated_at    = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'so_subsequences'
        ordering = ['order']
        indexes  = [models.Index(fields=['campaign', 'order'], name='so_subseq_camp_order_idx')]

    def __str__(self):
        return self.name or f'Subsequence {self.order + 1} (campaign={self.campaign_id})'


class SOSubsequenceStep(models.Model):
    """One email in a subsequence — exact structural mirror of SOSequenceStep."""

    subsequence = models.ForeignKey(SOSubsequence, on_delete=models.CASCADE, related_name='steps')
    order       = models.PositiveIntegerField(default=0)
    wait_days   = models.PositiveIntegerField(default=0)
    wait_hours  = models.PositiveIntegerField(default=0)
    name        = models.CharField(max_length=255, blank=True, default='')
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'so_subsequence_steps'
        ordering = ['order']
        indexes  = [models.Index(fields=['subsequence', 'order'], name='so_substep_seq_order_idx')]

    def __str__(self):
        return f'Step {self.order + 1} (subsequence={self.subsequence_id})'


class SOSubsequenceVariant(models.Model):
    """An A/B variation of a subsequence step — exact structural mirror of
    SOSequenceVariant."""

    step       = models.ForeignKey(SOSubsequenceStep, on_delete=models.CASCADE, related_name='variants')
    label      = models.CharField(max_length=2, default='A')
    name       = models.CharField(max_length=255, blank=True, default='')
    subject    = models.CharField(max_length=500, blank=True, default='')
    preheader  = models.CharField(max_length=200, blank=True, default='')
    html_body  = models.TextField(blank=True, default='')
    weight     = models.PositiveIntegerField(default=1)
    is_active  = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'so_subsequence_variants'
        ordering = ['label']

    def __str__(self):
        return f'{self.step_id}{self.label}'


# ── Warmup ────────────────────────────────────────────────────────────────
# Warms up SOEmailAccount senders by sending low-volume, ramping traffic to a
# single fixed, admin-managed pool of receiver Gmail accounts
# (WarmupReceiverAccount, OAuth2 via the Gmail API) shared across every user's
# senders, then checking landing location (Inbox/Spam/Other/Not Found) and
# clearing UNREAD via the API. End users never connect or manage receivers —
# see views/admin/warmup.py. Sender-side ramp config already existed
# (SOEmailAccountWarmup, above) before any of this was built — these models
# are the receiver pool and the per-message tracking it needed.

class WarmupReceiverAccount(models.Model):
    STATUS_CHOICES = (
        ('pending',   'Pending'),
        ('connected', 'Connected'),
        ('paused',    'Paused'),
        ('revoked',   'Revoked'),
    )

    # The pool is shared/global, NOT scoped per app-user — every sender across
    # every user draws from the same rows (services/warmup.py::
    # create_pending_messages_for_sender no longer filters on this field).
    # Kept only as an audit trail of which admin ran the OAuth connect flow;
    # never used to scope a query.
    user     = models.ForeignKey(UserTable, on_delete=models.CASCADE, related_name='warmup_receiver_accounts')
    email    = models.EmailField(max_length=255)
    # Fernet-encrypted OAuth refresh token (services/warmup_crypto.py) — never
    # logged, never returned by any view/API. No access_token/expiry fields:
    # the access token is always refreshed on demand from this refresh token,
    # since Gmail API isn't called often enough per receiver to justify
    # caching a second secret.
    refresh_token_encrypted = models.TextField(blank=True, default='')
    status         = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    last_checked_at  = models.DateTimeField(null=True, blank=True)
    # Last time this receiver was picked for a new warmup message — drives
    # fair least-recently-used rotation across the shared pool (see
    # services/warmup.py::create_pending_messages_for_sender). Null sorts
    # first so brand-new receivers get used before any repeat.
    last_assigned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'warmup_receiver_accounts'
        ordering = ['-created_at']
        # Pool-wide queries (status/deleted_at, never user) drive selection —
        # see create_pending_messages_for_sender — so indexes lead with those,
        # not user. The admin list page's own "connected by" filter is the
        # only place that still queries by user, hence it stays indexed too.
        indexes  = [
            models.Index(fields=['status', 'deleted_at']),
            models.Index(fields=['status', 'last_assigned_at']),
            models.Index(fields=['user', 'deleted_at']),
        ]

    def __str__(self):
        return self.email


class WarmupMessage(models.Model):
    """One warmup email's full lifecycle: send -> verify -> landing result.
    One row per email (not a separate job + append-only event log) — unlike
    SOEvent, a warmup email has exactly one lifecycle and one landing
    determination, not a stream of events."""

    STATUS_CHOICES = (
        ('pending',      'Pending'),
        ('sending',      'Sending'),
        ('sent',         'Sent'),
        ('checking',     'Checking'),
        ('completed',    'Completed'),
        ('send_failed',  'Send Failed'),
        ('cancelled',    'Cancelled'),
    )
    LANDING_CHOICES = (
        ('inbox',     'Inbox'),
        ('spam',      'Spam'),
        ('other',     'Other'),
        ('not_found', 'Not Found'),
    )

    sender_account   = models.ForeignKey(SOEmailAccount, on_delete=models.SET_NULL, null=True, blank=True,
                                          related_name='warmup_messages')
    sender_email     = models.CharField(max_length=255, blank=True, default='')
    receiver_account = models.ForeignKey(WarmupReceiverAccount, on_delete=models.SET_NULL, null=True, blank=True,
                                          related_name='warmup_messages')
    receiver_email   = models.CharField(max_length=255, blank=True, default='')

    # WTI-WARMUP-<12 hex chars>, embedded in the subject — what the Gmail
    # checker searches for. unique=True is the final idempotency backstop
    # against ever creating two rows for "the same" warmup email.
    identifier  = models.CharField(max_length=64, unique=True, db_index=True)
    subject     = models.CharField(max_length=500, blank=True, default='')
    message_id  = models.CharField(max_length=255, blank=True, default='')

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    # landing_location always records the ORIGINAL classification at first
    # detection and is NEVER overwritten by a later spam rescue — rescued_to_
    # inbox is the separate fact that the move happened. Losing the
    # distinction would make it impossible to see both "how much landed in
    # spam" and "how much of that we actually fixed."
    landing_location  = models.CharField(max_length=20, choices=LANDING_CHOICES, null=True, blank=True)
    rescued_to_inbox  = models.BooleanField(default=False)
    was_unread        = models.BooleanField(null=True, blank=True)
    marked_read       = models.BooleanField(default=False)

    send_attempts  = models.PositiveIntegerField(default=0)
    check_attempts = models.PositiveIntegerField(default=0)
    error          = models.TextField(blank=True, default='')  # never contains credentials/tokens

    # scheduled_for staggers volume across the day (not all at once).
    # check_after is when the next verification attempt is due — set to
    # sent_at + WARMUP_INITIAL_CHECK_DELAY_MINUTES initially, then pushed
    # forward by WARMUP_CHECK_BACKOFF_MINUTES on each not-found/error retry.
    scheduled_for = models.DateTimeField()
    sent_at       = models.DateTimeField(null=True, blank=True)
    check_after   = models.DateTimeField(null=True, blank=True)
    checked_at    = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'warmup_messages'
        ordering = ['-created_at']
        indexes  = [
            models.Index(fields=['status', 'scheduled_for'], name='warmup_msg_status_sched_idx'),
            models.Index(fields=['status', 'check_after'], name='warmup_msg_status_check_idx'),
            models.Index(fields=['sender_account', 'created_at'], name='warmup_msg_sender_created_idx'),
            models.Index(fields=['receiver_account', 'created_at'], name='warmup_msg_recv_created_idx'),
        ]

    def __str__(self):
        return self.identifier


class WarmupDailyUsage(models.Model):
    """Atomic per-account-per-UTC-day warmup send counter — byte-for-byte
    mirror of SOEmailAccountDailyUsage's shape/pattern, kept as its own
    table (not shared with campaign drip sending) so warmup volume and real
    campaign volume are independent pools that can't eat into each other."""

    account    = models.ForeignKey(SOEmailAccount, on_delete=models.CASCADE, related_name='warmup_daily_usage')
    date       = models.DateField()
    sent_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table        = 'warmup_daily_usage'
        unique_together = [('account', 'date')]
