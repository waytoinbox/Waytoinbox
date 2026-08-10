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


class CreditAuditLog(models.Model):
    CREDIT_TYPES = [
        ('vc', 'Validation Credits'),
        ('ac', 'Analysis Credits'),
        ('cc', 'Contact Credits'),
    ]
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
    ]

    user           = models.ForeignKey(UserTable, on_delete=models.CASCADE)
    credit_type    = models.CharField(max_length=10, choices=CREDIT_TYPES)
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
