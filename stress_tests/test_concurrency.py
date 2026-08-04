"""
Waytoinbox — Celery / concurrency stress tests
=================================================
Covers:
  DB-04  atomic credit operations (no lost updates, no negative balance)
  DB-05  Campaign_ID uniqueness under concurrent saves
  DB-08  payment idempotency guard (no double-credit on replay)
  EXTRA  audit-log consistency (balance_before/after never gaps)

Requirements (already installed):
    pip install pytest pytest-django

Run from repo root:
    pytest stress_tests/ -v --tb=short

Every test uses TransactionTestCase semantics (@pytest.mark.django_db(transaction=True))
so that select_for_update() behaves the same as in production.
"""
import threading
import pytest

from Email_validate_app.models import (
    UserTable, CurrentCredits, Campaign, Payment, CreditAuditLog,
)
from Email_validate_app.services.credit_manager import (
    insert_vc_credits,
    deduct_vc_credits,
)


# ─── helpers ────────────────────────────────────────────────────────────────

def _make_user(tag: str) -> UserTable:
    """Create a minimal, verified test user unique per test."""
    u = UserTable(
        user_email=f"stress_{tag}@waytoinbox-test.invalid",
        user_name="Stress Tester",
        is_verified=True,
    )
    u.set_password("StressTest123!")
    u.save()
    return u


def _run_threads(fn, n: int) -> tuple[list, list]:
    """Start *n* threads running *fn*, return (results, errors)."""
    results, errors = [], []
    lock = threading.Lock()

    def wrapper():
        try:
            v = fn()
            with lock:
                results.append(v)
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=wrapper) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


# ─── DB-04: concurrent credit insertions — no lost updates ──────────────────

@pytest.mark.django_db(transaction=True)
def test_concurrent_credit_inserts_no_lost_updates():
    """
    50 threads each add 10 VC credits to the same user.
    Final balance must be exactly 500 — any shortfall means a lost update.

    The CurrentCredits row is created BEFORE threads start: the get_or_create
    inside insert_vc_credits deadlocks in MySQL InnoDB when N threads race to
    create the same row simultaneously (error 1213).  Pre-creating the row is
    also the production invariant — the row is created at signup/first payment.
    """
    N = 50
    EACH = 10
    user = _make_user("insert")
    # Pre-create the CurrentCredits row so threads only do UPDATE, not INSERT.
    CurrentCredits.objects.create(user_id=user.id)

    _, errors = _run_threads(
        lambda: insert_vc_credits(None, user.id, EACH, ref_type="stress"),
        N,
    )

    assert not errors, (
        f"Unexpected errors during insert: {errors}\n"
        "If you see MySQL deadlock (1213), the CurrentCredits row was missing "
        "before concurrent inserts — ensure it is created at user signup."
    )

    cc = CurrentCredits.objects.get(user_id=user.id)
    assert cc.vc_current_credits == N * EACH, (
        f"Lost-update detected: expected {N * EACH}, got {cc.vc_current_credits}"
    )


# ─── DB-04: concurrent credit deductions — no negative balance ──────────────

@pytest.mark.django_db(transaction=True)
def test_concurrent_credit_deductions_no_negative_balance():
    """
    10 threads each try to deduct 20 credits from a 100-credit balance.
    Exactly 5 should succeed; the other 5 must get ValueError.
    Balance must never go below zero.
    """
    INITIAL = 100
    EACH = 20
    N = 10  # only INITIAL // EACH = 5 can succeed
    user = _make_user("deduct")
    insert_vc_credits(None, user.id, INITIAL, ref_type="stress_seed")

    successes, failures = [], []
    lock = threading.Lock()

    def try_deduct():
        try:
            deduct_vc_credits(user.id, EACH, ref_type="stress")
            with lock:
                successes.append(1)
        except ValueError:
            with lock:
                failures.append(1)

    threads = [threading.Thread(target=try_deduct) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    cc = CurrentCredits.objects.get(user_id=user.id)

    assert cc.vc_current_credits >= 0, (
        f"Balance went negative ({cc.vc_current_credits}) — select_for_update() broken"
    )
    assert len(successes) == INITIAL // EACH, (
        f"Expected {INITIAL // EACH} successes, got {len(successes)} "
        f"(failures={len(failures)})"
    )
    assert cc.vc_current_credits == INITIAL - len(successes) * EACH


# ─── DB-04: audit log — balance_before / after chain is unbroken ────────────

@pytest.mark.django_db(transaction=True)
def test_audit_log_balance_chain_integrity():
    """
    After concurrent deductions the CreditAuditLog rows must form an
    unbroken chain: each row's balance_after == next row's balance_before.
    A gap means the lock was not held during the log write.
    """
    INITIAL = 200
    EACH = 10
    N = 10
    user = _make_user("audit")
    insert_vc_credits(None, user.id, INITIAL, ref_type="stress_seed")

    def deduct():
        try:
            deduct_vc_credits(user.id, EACH, ref_type="stress")
        except ValueError:
            pass

    threads = [threading.Thread(target=deduct) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    logs = list(
        CreditAuditLog.objects.filter(user_id=user.id, credit_type="vc", entry_type="debit")
        .order_by("id")
    )
    assert logs, "No debit audit entries created"

    gaps = []
    for i in range(len(logs) - 1):
        if logs[i].balance_after != logs[i + 1].balance_before:
            gaps.append(
                f"Row {logs[i].id}: balance_after={logs[i].balance_after} "
                f"but next balance_before={logs[i + 1].balance_before}"
            )
    assert not gaps, "Audit log chain has gaps (lock not held during log write):\n" + "\n".join(gaps)


# ─── DB-05: Campaign_ID uniqueness under concurrency ────────────────────────

@pytest.mark.django_db(transaction=True)
def test_concurrent_campaign_save_unique_ids():
    """
    20 threads simultaneously call Campaign.save() on new instances.
    Every resulting Campaign_ID must be unique — duplicates mean the
    select_for_update() retry loop in Campaign.save() is not working.
    """
    N = 20
    user = _make_user("campaign")
    created_ids = []
    errors = []
    lock = threading.Lock()

    def create_campaign():
        try:
            c = Campaign(
                user=user,
                campaign_name="Stress Campaign",
                sender_name="Stress Tester",
                from_email="stress@waytoinbox-test.invalid",
                reply_email="stress@waytoinbox-test.invalid",
                status="draft",
            )
            c.save()
            with lock:
                created_ids.append(c.Campaign_ID)
        except Exception as exc:
            with lock:
                errors.append(str(exc))

    threads = [threading.Thread(target=create_campaign) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Campaign save() raised unexpected errors:\n" + "\n".join(errors)
    assert len(created_ids) == N, (
        f"Only {len(created_ids)} of {N} campaigns saved without error"
    )
    duplicates = [id for id in created_ids if created_ids.count(id) > 1]
    assert not duplicates, (
        f"Duplicate Campaign_IDs detected: {sorted(set(duplicates))}\n"
        f"All IDs: {sorted(created_ids)}"
    )


# ─── DB-08: payment idempotency guard — no double-credit on replay ───────────

@pytest.mark.django_db(transaction=True)
def test_payment_replay_credits_added_exactly_once():
    """
    8 threads simultaneously submit the same order_id (simulates a network
    retry or double-click on the payment button).
    Credits must be added exactly once, not 8 times.

    NOTE: this test exercises the CHECK in billing.py:
        if Payment.objects.filter(order_id=order_id).exists(): return

    If this test fails it means the guard is a race — the fix is to add
    unique=True to Payment.order_id at the DB level (see note at bottom).
    """
    CREDIT_AMOUNT = 500
    ORDER_ID = "order_stress_idempotency_001"
    N = 8
    user = _make_user("payment")

    # Create the CurrentCredits row directly — insert_vc_credits(amount=0)
    # returns early without touching the DB, so the row would be missing.
    cc = CurrentCredits.objects.create(user_id=user.id)

    credited = []
    lock = threading.Lock()

    def process_payment():
        from django.db import transaction, IntegrityError
        try:
            with transaction.atomic():
                if Payment.objects.filter(order_id=ORDER_ID).exists():
                    return  # already processed — mirror billing.py guard
                Payment.objects.create(
                    user=user,
                    order_id=ORDER_ID,
                    payment_id=f"pay_stress_{threading.get_ident()}",
                    amount="5.00",
                    credits=str(CREDIT_AMOUNT),
                )
                insert_vc_credits(None, user.id, CREDIT_AMOUNT, ref_type="stress", ref_id=ORDER_ID)
                with lock:
                    credited.append(1)
        except IntegrityError:
            # unique constraint violation if order_id has unique=True — expected
            pass

    threads = [threading.Thread(target=process_payment) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    cc = CurrentCredits.objects.get(user_id=user.id)
    assert len(credited) == 1, (
        f"Payment processed {len(credited)} times (expected 1) — idempotency guard is a race.\n"
        "Fix: add unique=True to Payment.order_id at the DB level."
    )
    assert cc.vc_current_credits == CREDIT_AMOUNT, (
        f"Expected {CREDIT_AMOUNT} VC credits, got {cc.vc_current_credits} — double-credit detected."
    )


# ─── combined: insert + deduct under high contention ────────────────────────

@pytest.mark.django_db(transaction=True)
def test_mixed_insert_and_deduct_balance_never_negative():
    """
    25 threads inserting 10 credits and 25 threads deducting 10 credits run
    simultaneously. Balance must never go negative regardless of ordering.
    """
    N_EACH = 25
    EACH = 10
    SEED = N_EACH * EACH  # start with enough to cover all deductions
    user = _make_user("mixed")
    insert_vc_credits(None, user.id, SEED, ref_type="stress_seed")

    def insert():
        insert_vc_credits(None, user.id, EACH, ref_type="stress")

    def deduct():
        try:
            deduct_vc_credits(user.id, EACH, ref_type="stress")
        except ValueError:
            pass  # insufficient credits — acceptable

    threads = (
        [threading.Thread(target=insert) for _ in range(N_EACH)]
        + [threading.Thread(target=deduct) for _ in range(N_EACH)]
    )
    # shuffle so inserts and deducts interleave
    import random
    random.shuffle(threads)
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    cc = CurrentCredits.objects.get(user_id=user.id)
    assert cc.vc_current_credits >= 0, (
        f"Balance went negative ({cc.vc_current_credits}) under mixed load"
    )
