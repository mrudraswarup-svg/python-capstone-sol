"""
CLI entrypoint.

Subcommands to implement (Day 4):
    billing init                              -- create / migrate the DB
    billing customer add <name> <email> <country> [--state CODE]
    billing plan list
    billing subscribe <customer_id> <plan_id> [--trial-days N] [--discount CODE]
    billing bill run [--date YYYY-MM-DD]
    billing invoice show <invoice_id>          -- prints PLAIN TEXT invoice
    billing upgrade <subscription_id> <new_plan_id> [--date YYYY-MM-DD]   (STRETCH)
    billing demo                              -- run the scripted scenario

Use argparse with subparsers. Keep each subcommand handler in its own function.

PDF rendering is OUT OF SCOPE for the core project — `invoice show` should
print a clean PLAIN-TEXT invoice (see helper `format_invoice_text` below).
PDF generation is BONUS: see `billing_engine/pdf/renderer.py`.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal

from billing_engine.models import Invoice, Customer, Plan, PricingType, BillingPeriod, Subscription, SubscriptionStatus, LedgerDirection
from billing_engine.money import Money
from billing_engine.db import Database
from billing_engine.billing.cycle import BillingCycle
from billing_engine.billing.dunning import DunningProcess, DunningState
from billing_engine.payments.gateway import ScriptedGateway, PaymentResult


def format_invoice_text(invoice: Invoice, customer_name: str, plan_name: str) -> str:
    """Render an invoice as a plain-text receipt. Pure function — easy to test."""
    lines = []
    lines.append("=" * 60)
    lines.append(f"INVOICE INV-{invoice.id}".center(60))
    lines.append("=" * 60)
    lines.append(f"Customer: {customer_name}")
    lines.append(f"Plan:     {plan_name}")
    lines.append(f"Period:   {invoice.period_start} → {invoice.period_end}")
    lines.append("-" * 60)
    
    # Line items
    for item in invoice.line_items:
        desc = f"{item.kind.value}: {item.description}" if item.kind else item.description
        amt = f"{item.amount.amount:.2f} {item.amount.currency}"
        # Right-align amount
        lines.append(f"{desc:<40} {amt:>15}")
    
    lines.append("-" * 60)
    lines.append(f"{'Subtotal:':<40} {invoice.subtotal.amount:>14.2f} {invoice.subtotal.currency}")
    if invoice.discount_total.amount > 0:
        lines.append(f"{'Discount:':<40} {-invoice.discount_total.amount:>14.2f} {invoice.discount_total.currency}")
    if invoice.tax_total.amount > 0:
        lines.append(f"{'Tax:':<40} {invoice.tax_total.amount:>14.2f} {invoice.tax_total.currency}")
    lines.append("-" * 60)
    lines.append(f"{'TOTAL:':<40} {invoice.total.amount:>14.2f} {invoice.total.currency}")
    lines.append(f"Status: {invoice.status.value}")
    lines.append("=" * 60)
    
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="billing", description="Subscription Billing CLI")
    parser.add_argument("--db", default="billing.db", help="path to SQLite database file")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="initialize the database")
    sub.add_parser("demo", help="run the demo scenario")

    # customer add
    cust_p = sub.add_parser("customer", help="customer commands")
    cust_sub = cust_p.add_subparsers(dest="customer_cmd", required=True)
    cust_add = cust_sub.add_parser("add", help="add a new customer")
    cust_add.add_argument("name")
    cust_add.add_argument("email")
    cust_add.add_argument("country", help="ISO-2 country code, e.g. IN")
    cust_add.add_argument("--state", default="", help="state code, e.g. KA")

    # plan list
    plan_p = sub.add_parser("plan", help="plan commands")
    plan_sub = plan_p.add_subparsers(dest="plan_cmd", required=True)
    plan_sub.add_parser("list", help="list all plans")

    # subscribe
    sub_p = sub.add_parser("subscribe", help="subscribe a customer to a plan")
    sub_p.add_argument("customer_id", type=int)
    sub_p.add_argument("plan_id", type=int)
    sub_p.add_argument("--trial-days", type=int, default=0, dest="trial_days")
    sub_p.add_argument("--discount", default=None, help="discount code")

    # bill run
    bill_p = sub.add_parser("bill", help="billing commands")
    bill_sub = bill_p.add_subparsers(dest="bill_cmd", required=True)
    bill_run = bill_sub.add_parser("run", help="run the billing cycle")
    bill_run.add_argument("--date", default=None, help="billing date YYYY-MM-DD (default: today)")

    # invoice show
    inv_p = sub.add_parser("invoice", help="invoice commands")
    inv_sub = inv_p.add_subparsers(dest="invoice_cmd", required=True)
    inv_show = inv_sub.add_parser("show", help="show an invoice as plain text")
    inv_show.add_argument("invoice_id", type=int)

    # upgrade (stretch)
    upg_p = sub.add_parser("upgrade", help="upgrade subscription mid-cycle")
    upg_p.add_argument("subscription_id", type=int)
    upg_p.add_argument("new_plan_id", type=int)
    upg_p.add_argument("--date", default=None, help="switch date YYYY-MM-DD (default: today)")

    args = parser.parse_args(argv)

    if args.cmd == "init":
        db = Database(args.db)
        db.init_schema()
        print(f"Database initialized: {args.db}")
        return 0

    if args.cmd == "demo":
        return run_demo()

    db = Database(args.db)
    return _dispatch(args, db)


def _make_strategy_factory():
    from billing_engine.pricing.flat import FlatRate
    from billing_engine.pricing.usage import UsageBased

    def factory(plan):
        config = json.loads(plan.config_json) if plan.config_json and plan.config_json != "{}" else {}
        if plan.pricing_type == PricingType.USAGE:
            return UsageBased(Money(config.get("unit_price", "0"), plan.currency))
        return FlatRate(Money(config.get("amount", "0"), plan.currency))

    return factory


def _make_discount_factory(discount_repo):
    from billing_engine.discounts.percentage import PercentageDiscount
    from billing_engine.discounts.fixed import FixedAmountDiscount
    from billing_engine.discounts.first_month_free import FirstMonthFree

    def factory(discount_id):
        if discount_id is None:
            return None
        row = discount_repo.get_by_code(str(discount_id))
        if row is None:
            return None
        if row["discount_type"] == "PERCENT":
            return PercentageDiscount(Decimal(row["value"]))
        if row["discount_type"] == "FIXED":
            return FixedAmountDiscount(Money(row["value"], row["currency"]))
        if row["discount_type"] == "FIRST_MONTH_FREE":
            return FirstMonthFree()
        return None

    return factory


def _make_tax_factory():
    from billing_engine.taxes.base import TaxCalculator, TaxContext

    def factory(customer):
        calc = TaxCalculator.for_country(customer.country_code)
        ctx = TaxContext(customer_country=customer.country_code, customer_state=customer.state_code or "")
        return calc, ctx

    return factory


def _dispatch(args, db) -> int:
    from billing_engine.db.repository import (
        CustomerRepository, PlanRepository, SubscriptionRepository,
        UsageRecordRepository, InvoiceRepository, InvoiceLineItemRepository,
        LedgerRepository, PaymentAttemptRepository, DiscountRepository,
    )

    customers = CustomerRepository(db)
    plans = PlanRepository(db)
    subscriptions = SubscriptionRepository(db)
    usage = UsageRecordRepository(db)
    invoices = InvoiceRepository(db)
    line_items = InvoiceLineItemRepository(db)
    ledger = LedgerRepository(db)
    attempts = PaymentAttemptRepository(db)
    discounts = DiscountRepository(db)

    if args.cmd == "customer" and args.customer_cmd == "add":
        cust = customers.add(Customer(None, args.name, args.email, args.country, args.state))
        print(f"Customer added: id={cust.id}  name={cust.name}  email={cust.email}")
        return 0

    if args.cmd == "plan" and args.plan_cmd == "list":
        all_plans = plans.list_all()
        if not all_plans:
            print("No plans found.")
        for plan in all_plans:
            print(f"id={plan.id}  {plan.name:<20}  {plan.pricing_type.value:<10}  {plan.billing_period.value:<8}  {plan.currency}")
        return 0

    if args.cmd == "subscribe":
        plan = plans.get(args.plan_id)
        if plan is None:
            print(f"Plan {args.plan_id} not found.", file=sys.stderr)
            return 1
        start = date.today()
        if plan.billing_period == BillingPeriod.MONTHLY:
            m = start.month % 12 + 1
            y = start.year + (1 if start.month == 12 else 0)
            import calendar
            end = date(y, m, min(start.day, calendar.monthrange(y, m)[1]))
        else:
            import calendar
            y = start.year + 1
            end = date(y, start.month, min(start.day, calendar.monthrange(y, start.month)[1]))
        trial_end = (start + timedelta(days=args.trial_days)) if args.trial_days > 0 else None
        status = SubscriptionStatus.TRIAL if trial_end else SubscriptionStatus.ACTIVE

        # Resolve discount code to id
        discount_id = None
        if args.discount:
            row = discounts.get_by_code(args.discount)
            if row is None:
                print(f"Discount code '{args.discount}' not found.", file=sys.stderr)
                return 1
            discount_id = row["id"]

        sub = subscriptions.add(Subscription(
            None, args.customer_id, args.plan_id, status, start, end,
            trial_end=trial_end, discount_id=discount_id,
        ))
        print(f"Subscription created: id={sub.id}  status={sub.status.value}  period={sub.current_period_start} → {sub.current_period_end}")
        return 0

    if args.cmd == "bill" and args.bill_cmd == "run":
        as_of = date.fromisoformat(args.date) if args.date else date.today()
        cycle = BillingCycle(
            db=db,
            customer_repo=customers,
            plan_repo=plans,
            subscription_repo=subscriptions,
            usage_repo=usage,
            invoice_repo=invoices,
            line_item_repo=line_items,
            ledger_repo=ledger,
            strategy_factory=_make_strategy_factory(),
            discount_factory=_make_discount_factory(discounts),
            tax_factory=_make_tax_factory(),
        )
        result = cycle.run(as_of=as_of)
        print(f"Billing cycle complete as of {as_of}: {result.invoices_created} created, "
              f"{result.invoices_skipped_duplicate} skipped, {result.trials_activated} trials activated.")
        return 0

    if args.cmd == "invoice" and args.invoice_cmd == "show":
        invoice = invoices.get(args.invoice_id)
        if invoice is None:
            print(f"Invoice {args.invoice_id} not found.", file=sys.stderr)
            return 1
        # Fetch line items
        line_item_list = line_items.list_for_invoice(invoice.id)
        invoice.line_items.extend(line_item_list)
        # Resolve customer and plan names via subscription
        sub = subscriptions.get(invoice.subscription_id)
        customer_name = customers.get(sub.customer_id).name if sub else "Unknown"
        plan_name = plans.get(sub.plan_id).name if sub else "Unknown"
        print(format_invoice_text(invoice, customer_name, plan_name))
        return 0

    if args.cmd == "upgrade":
        switch_date = date.fromisoformat(args.date) if args.date else date.today()
        cycle = BillingCycle(
            db=db,
            customer_repo=customers,
            plan_repo=plans,
            subscription_repo=subscriptions,
            usage_repo=usage,
            invoice_repo=invoices,
            line_item_repo=line_items,
            ledger_repo=ledger,
            strategy_factory=_make_strategy_factory(),
            discount_factory=_make_discount_factory(discounts),
            tax_factory=_make_tax_factory(),
        )
        cycle.upgrade_subscription(args.subscription_id, args.new_plan_id, switch_date)
        print(f"Subscription {args.subscription_id} upgraded to plan {args.new_plan_id} on {switch_date}.")
        return 0

    print(f"Unknown command '{args.cmd}'", file=sys.stderr)
    return 2


def run_demo() -> int:
    """Scripted end-to-end scenario for the `demo` subcommand.

    Should mirror `tests/test_demo_scenario.py::TestEndToEndScenario::test_full_lifecycle`
    and print a human-readable summary to stdout.
    """
    import tempfile
    import os
    from tests.conftest import (
        make_flat_strategy_factory, make_discount_factory, make_no_tax_factory,
    )
    
    print("\n" + "=" * 60)
    print("BILLING ENGINE DEMO".center(60))
    print("=" * 60 + "\n")
    
    # Initialize database (file-backed for proper connection handling)
    print("1. Initializing database...")
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(db_path)
    db.init_schema()
    from billing_engine.db.repository import (
        CustomerRepository, PlanRepository, SubscriptionRepository,
        UsageRecordRepository, InvoiceRepository, InvoiceLineItemRepository,
        LedgerRepository, PaymentAttemptRepository,
    )
    customers = CustomerRepository(db)
    plans = PlanRepository(db)
    subscriptions = SubscriptionRepository(db)
    usage = UsageRecordRepository(db)
    invoices = InvoiceRepository(db)
    line_items = InvoiceLineItemRepository(db)
    ledger = LedgerRepository(db)
    attempts = PaymentAttemptRepository(db)
    
    # Create customer
    print("2. Creating customer...")
    cust = customers.add(Customer(None, "Alice Smith", "alice@example.com", "IN", "KA"))
    print(f"   ✓ Customer created: {cust.name} ({cust.email})")
    
    # Create plan
    print("3. Creating plan...")
    plan = plans.add(Plan(
        None, "Pro", PricingType.FLAT, BillingPeriod.MONTHLY, "INR",
    ))
    print(f"   ✓ Plan created: {plan.name}")
    
    # Create subscription
    print("4. Creating subscription...")
    sub = subscriptions.add(Subscription(
        None, cust.id, plan.id, SubscriptionStatus.ACTIVE,
        date(2026, 1, 1), date(2026, 2, 1),
    ))
    print(f"   ✓ Subscription created (period: {sub.current_period_start} → {sub.current_period_end})")
    
    # Run billing cycle
    print("5. Running billing cycle...")
    cycle = BillingCycle(
        db=db,
        customer_repo=customers,
        plan_repo=plans,
        subscription_repo=subscriptions,
        usage_repo=usage,
        invoice_repo=invoices,
        line_item_repo=line_items,
        ledger_repo=ledger,
        strategy_factory=make_flat_strategy_factory({"Pro": Money("1000", "INR")}),
        discount_factory=make_discount_factory({}),
        tax_factory=make_no_tax_factory(),
    )
    result = cycle.run(as_of=date(2026, 2, 1))
    print(f"   ✓ Billing cycle complete: {result.invoices_created} invoice(s) created")
    
    # Check subscription period advanced
    sub_after = subscriptions.get(sub.id)
    print(f"   ✓ Subscription period advanced: {sub_after.current_period_start} → {sub_after.current_period_end}")
    
    # Check ledger
    print("6. Checking ledger...")
    ledger_entries = ledger.list_for_customer(cust.id)
    print(f"   ✓ Ledger entries: {len(ledger_entries)}")
    for entry in ledger_entries:
        direction_str = "DEBIT " if entry.direction == LedgerDirection.DEBIT else "CREDIT"
        print(f"      {direction_str}: {entry.amount.amount} {entry.amount.currency}")
    
    # Get invoice and process payment
    print("7. Processing payment...")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM invoices WHERE subscription_id=?", (sub.id,)
        ).fetchone()
    invoice = invoices.get(row["id"])
    print(f"   ✓ Invoice {invoice.id} status: {invoice.status.value}")
    
    dunning = DunningProcess(
        gateway=ScriptedGateway([PaymentResult(True)]),
        invoice_repo=invoices,
        ledger_repo=ledger,
        subscription_repo=subscriptions,
        attempt_repo=attempts,
    )
    outcome = dunning.attempt(invoice, cust.id, datetime(2026, 2, 1, 10, 0))
    print(f"   ✓ Payment attempt: {outcome.state.value}")
    
    # Verify payment posted
    invoice_paid = invoices.get(invoice.id)
    print(f"   ✓ Invoice now: {invoice_paid.status.value}")
    
    # Final ledger check
    print("8. Final ledger state...")
    final_entries = ledger.list_for_customer(cust.id)
    print(f"   ✓ Total ledger entries: {len(final_entries)}")
    net_balance = sum(
        (e.amount.amount if e.direction == LedgerDirection.DEBIT else -e.amount.amount)
        for e in final_entries
    )
    print(f"   ✓ Net balance: {net_balance} INR")
    
    print("\n" + "=" * 60)
    print("DEMO COMPLETE".center(60))
    print("=" * 60 + "\n")
    
    # Cleanup
    try:
        from pathlib import Path
        Path(db_path).unlink()
    except Exception:
        pass
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
