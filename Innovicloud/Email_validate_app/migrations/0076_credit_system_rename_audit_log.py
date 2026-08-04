"""
Migration 0076 — Credit system rename + audit log

Renames:
  CurrentCredits: total_credits → vc_total_credits, used_credits → vc_used_credits,
                  current_credits → vc_current_credits, ip_* → ac_* (6 fields)
  TotalCredits:   buying_credits → vc_credits, buying_date → vc_buying_date,
                  ip_credits → ac_credits, ip_buying_date → ac_buying_date
  UsedCredits:    used_credits → vc_used_credits, used_date → vc_used_date,
                  used_ip_credits → ac_used_credits, used_ip_date → ac_used_date
  SubsPayment:    el_credits → vc_credits, ip_credits → ac_credits

Adds:
  SubsPayment.billing_cycle
  CreditAuditLog model

All renames use RenameField — no data is lost.
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('Email_validate_app', '0075_currentcredits_cc_current_credits_and_more'),
    ]

    operations = [

        # ── CurrentCredits: EL fields → vc_* ──────────────────────────────────
        migrations.RenameField(
            model_name='currentcredits',
            old_name='total_credits',
            new_name='vc_total_credits',
        ),
        migrations.RenameField(
            model_name='currentcredits',
            old_name='used_credits',
            new_name='vc_used_credits',
        ),
        migrations.RenameField(
            model_name='currentcredits',
            old_name='current_credits',
            new_name='vc_current_credits',
        ),

        # ── CurrentCredits: ip_* fields → ac_* ───────────────────────────────
        migrations.RenameField(
            model_name='currentcredits',
            old_name='ip_total_credits',
            new_name='ac_total_credits',
        ),
        migrations.RenameField(
            model_name='currentcredits',
            old_name='ip_used_credits',
            new_name='ac_used_credits',
        ),
        migrations.RenameField(
            model_name='currentcredits',
            old_name='ip_current_credits',
            new_name='ac_current_credits',
        ),

        # ── Add default=0 to all 9 CurrentCredits fields ─────────────────────
        migrations.AlterField(
            model_name='currentcredits',
            name='vc_total_credits',
            field=models.IntegerField(blank=True, default=0, null=True),
        ),
        migrations.AlterField(
            model_name='currentcredits',
            name='vc_used_credits',
            field=models.IntegerField(blank=True, default=0, null=True),
        ),
        migrations.AlterField(
            model_name='currentcredits',
            name='vc_current_credits',
            field=models.IntegerField(blank=True, default=0, null=True),
        ),
        migrations.AlterField(
            model_name='currentcredits',
            name='ac_total_credits',
            field=models.IntegerField(blank=True, default=0, null=True),
        ),
        migrations.AlterField(
            model_name='currentcredits',
            name='ac_used_credits',
            field=models.IntegerField(blank=True, default=0, null=True),
        ),
        migrations.AlterField(
            model_name='currentcredits',
            name='ac_current_credits',
            field=models.IntegerField(blank=True, default=0, null=True),
        ),
        migrations.AlterField(
            model_name='currentcredits',
            name='cc_total_credits',
            field=models.IntegerField(blank=True, default=0, null=True),
        ),
        migrations.AlterField(
            model_name='currentcredits',
            name='cc_used_credits',
            field=models.IntegerField(blank=True, default=0, null=True),
        ),
        migrations.AlterField(
            model_name='currentcredits',
            name='cc_current_credits',
            field=models.IntegerField(blank=True, default=0, null=True),
        ),

        # ── TotalCredits renames ──────────────────────────────────────────────
        migrations.RenameField(
            model_name='totalcredits',
            old_name='buying_credits',
            new_name='vc_credits',
        ),
        migrations.RenameField(
            model_name='totalcredits',
            old_name='buying_date',
            new_name='vc_buying_date',
        ),
        migrations.RenameField(
            model_name='totalcredits',
            old_name='ip_credits',
            new_name='ac_credits',
        ),
        migrations.RenameField(
            model_name='totalcredits',
            old_name='ip_buying_date',
            new_name='ac_buying_date',
        ),

        # ── UsedCredits renames ───────────────────────────────────────────────
        migrations.RenameField(
            model_name='usedcredits',
            old_name='used_credits',
            new_name='vc_used_credits',
        ),
        migrations.RenameField(
            model_name='usedcredits',
            old_name='used_date',
            new_name='vc_used_date',
        ),
        migrations.RenameField(
            model_name='usedcredits',
            old_name='used_ip_credits',
            new_name='ac_used_credits',
        ),
        migrations.RenameField(
            model_name='usedcredits',
            old_name='used_ip_date',
            new_name='ac_used_date',
        ),

        # ── SubsPayment renames + billing_cycle ───────────────────────────────
        migrations.RenameField(
            model_name='subspayment',
            old_name='el_credits',
            new_name='vc_credits',
        ),
        migrations.RenameField(
            model_name='subspayment',
            old_name='ip_credits',
            new_name='ac_credits',
        ),
        migrations.AddField(
            model_name='subspayment',
            name='billing_cycle',
            field=models.CharField(default='monthly', max_length=20),
        ),

        # ── New CreditAuditLog model ──────────────────────────────────────────
        migrations.CreateModel(
            name='CreditAuditLog',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('credit_type', models.CharField(choices=[('vc', 'Validation Credits'), ('ac', 'Analysis Credits'), ('cc', 'Contact Credits')], max_length=10)),
                ('entry_type', models.CharField(choices=[('credit', 'Credit Added'), ('debit', 'Credit Used'), ('adjustment', 'Manual Adjustment'), ('refund', 'Refund'), ('expired', 'Expired')], max_length=20)),
                ('amount', models.IntegerField()),
                ('balance_before', models.IntegerField(default=0)),
                ('balance_after', models.IntegerField(default=0)),
                ('ref_type', models.CharField(blank=True, choices=[('payg', 'Pay-As-You-Go Purchase'), ('subscription', 'Subscription Purchase'), ('validation', 'Email Validation'), ('campaign', 'Campaign Send'), ('ip_check', 'IP/Domain/Header/Reputation Check'), ('admin', 'Admin Adjustment')], max_length=30)),
                ('ref_id', models.CharField(blank=True, max_length=225)),
                ('description', models.CharField(blank=True, max_length=500)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'credit_audit_log',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='creditauditlog',
            index=models.Index(fields=['user', 'credit_type', 'created_at'], name='credit_audi_user_id_idx'),
        ),
        migrations.AddIndex(
            model_name='creditauditlog',
            index=models.Index(fields=['ref_type', 'ref_id'], name='credit_audi_ref_type_idx'),
        ),
    ]
