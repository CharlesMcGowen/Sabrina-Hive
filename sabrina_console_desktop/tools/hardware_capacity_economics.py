"""Internal hardware-capacity and third-party rental economics.

Podman may isolate an approved workload, but it is not a marketplace, tenant
contract, billing system or authorization to expose hardware to third parties.
This module calculates capacity and receipt-backed cash only. It never starts a
container, opens a network port, accepts a tenant or spends revenue.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from tools.messaging_store import ROOT, now


DEFAULT_POLICY_PATH = ROOT / "config/hardware_rental_policy.json"
DEFAULT_LEDGER_PATH = ROOT / "data/hardware_capacity_economics.sqlite3"
ALLOWED_RENTAL_CASH_KINDS = {"rental_payment_received", "rental_refund", "rental_chargeback"}
REQUIRED_RECEIPT_FIELDS = ("external_reference", "verified_by", "verification_basis")


@dataclass(frozen=True)
class HardwareCapacityPlan:
    period_hours: float
    customer_reserved_hours: float = 0.0
    internal_reserved_hours: float = 0.0
    maintenance_hours: float = 0.0
    external_rental_hours: float = 0.0
    gross_rental_rate_per_hour: float = 0.0
    electricity_cost_per_hour: float = 0.0
    platform_fee_percent: float = 0.0
    maintenance_reserve_percent: float = 0.0
    other_variable_cost: float = 0.0


def load_hardware_rental_policy(path: str | Path | None = None) -> dict[str, Any]:
    value = json.loads(Path(path or DEFAULT_POLICY_PATH).read_text(encoding="utf-8-sig"))
    if value.get("version") != 1 or value.get("internal_only") is not True:
        raise ValueError("Hardware rental policy must be version 1 and internal only")
    if value.get("operator_approval_required") is not True:
        raise ValueError("Hardware rental policy must reserve approval for the operator")
    return value


def hardware_rental_readiness(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = policy or load_hardware_rental_policy()
    required = (
        "provider_or_marketplace",
        "gross_rental_rate_per_hour",
        "electricity_cost_per_hour",
        "platform_fee_percent",
        "maintenance_reserve_percent",
    )
    missing = [field for field in required if policy.get(field) is None]
    return {
        "version": 1,
        "internal_only": True,
        "complete": not missing,
        "missing_inputs": missing,
        "currency": policy.get("currency", "USD"),
        "podman_role": "container isolation only",
        "marketplace_role": policy.get("provider_or_marketplace"),
        "external_tenant_execution_enabled": bool(policy.get("external_tenant_execution_enabled", False)),
        "operator_approval_required": True,
        "customer_disclosure_allowed": False,
    }


def calculate_hardware_capacity(value: HardwareCapacityPlan) -> dict[str, Any]:
    numbers = asdict(value)
    if any(float(number) < 0 for number in numbers.values()):
        raise ValueError("Hardware capacity and cost inputs cannot be negative")
    for field in ("platform_fee_percent", "maintenance_reserve_percent"):
        if not 0 <= float(numbers[field]) <= 100:
            raise ValueError(field + " must be between 0 and 100")
    committed_hours = (
        value.customer_reserved_hours
        + value.internal_reserved_hours
        + value.maintenance_hours
        + value.external_rental_hours
    )
    if committed_hours > value.period_hours + 1e-9:
        raise ValueError("Hardware capacity plan is overcommitted")
    unused_hours = value.period_hours - committed_hours
    gross_revenue = value.external_rental_hours * value.gross_rental_rate_per_hour
    platform_fees = gross_revenue * value.platform_fee_percent / 100
    electricity = value.external_rental_hours * value.electricity_cost_per_hour
    maintenance_reserve = gross_revenue * value.maintenance_reserve_percent / 100
    net_contribution = max(
        0.0,
        gross_revenue - platform_fees - electricity - maintenance_reserve - value.other_variable_cost,
    )
    net_unit_contribution = max(
        0.0,
        value.gross_rental_rate_per_hour
        * (1 - value.platform_fee_percent / 100 - value.maintenance_reserve_percent / 100)
        - value.electricity_cost_per_hour,
    )
    customer_opportunity = value.customer_reserved_hours * net_unit_contribution
    return {
        "version": 1,
        "internal_only": True,
        "complete": True,
        "capacity": {
            "period_hours": round(value.period_hours, 6),
            "committed_hours": round(committed_hours, 6),
            "unused_hours": round(unused_hours, 6),
            "utilization_percent": round(
                committed_hours / value.period_hours * 100 if value.period_hours else 0.0,
                6,
            ),
            "customer_reserved_hours": round(value.customer_reserved_hours, 6),
            "external_rental_hours": round(value.external_rental_hours, 6),
        },
        "rental_economics": {
            "gross_revenue": round(gross_revenue, 6),
            "platform_fees": round(platform_fees, 6),
            "electricity_cost": round(electricity, 6),
            "maintenance_reserve": round(maintenance_reserve, 6),
            "other_variable_cost": round(value.other_variable_cost, 6),
            "net_contribution": round(net_contribution, 6),
            "net_contribution_per_rented_hour": round(
                net_contribution / value.external_rental_hours if value.external_rental_hours else 0.0,
                6,
            ),
            "net_opportunity_rate_per_reserved_hour": round(net_unit_contribution, 6),
            "customer_capacity_opportunity_cost": round(customer_opportunity, 6),
        },
        "podman_role": "container isolation only",
        "external_tenant_execution_authorized": False,
        "customer_disclosure_allowed": False,
        "customer_safe_projection": {},
    }


class HardwareRentalLedger:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or DEFAULT_LEDGER_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS rental_cash_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              asset_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              amount REAL NOT NULL,
              currency TEXT NOT NULL,
              provider TEXT NOT NULL,
              external_reference TEXT NOT NULL UNIQUE,
              verified_by TEXT NOT NULL,
              verification_basis TEXT NOT NULL,
              receipt TEXT NOT NULL,
              occurred_at TEXT NOT NULL
            )"""
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def record_cash_event(
        self,
        asset_id: str,
        kind: str,
        amount: float,
        currency: str,
        provider: str,
        receipt: dict[str, Any],
    ) -> int:
        if kind not in ALLOWED_RENTAL_CASH_KINDS:
            raise ValueError("Unsupported hardware rental cash event")
        if float(amount) <= 0:
            raise ValueError("Hardware rental cash amount must be positive")
        if not str(asset_id or "").strip() or not str(provider or "").strip():
            raise ValueError("Hardware asset and independent rental provider are required")
        currency = str(currency or "").strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("Hardware rental currency must be a three letter code")
        if not isinstance(receipt, dict):
            raise ValueError("Hardware rental cash requires a settlement receipt")
        missing = [field for field in REQUIRED_RECEIPT_FIELDS if not str(receipt.get(field, "")).strip()]
        if missing:
            raise ValueError("Hardware rental receipt is missing: " + ", ".join(missing))
        try:
            with self.db:
                cursor = self.db.execute(
                    """INSERT INTO rental_cash_events(
                      asset_id,kind,amount,currency,provider,external_reference,verified_by,
                      verification_basis,receipt,occurred_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(asset_id).strip(),
                        kind,
                        float(amount),
                        currency,
                        str(provider).strip(),
                        str(receipt["external_reference"]).strip(),
                        str(receipt["verified_by"]).strip(),
                        str(receipt["verification_basis"]).strip(),
                        json.dumps(receipt, ensure_ascii=False),
                        now(),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("Duplicate hardware rental settlement reference") from error
        return int(cursor.lastrowid)

    def cash_summary(self, asset_id: str) -> dict[str, Any]:
        rows = self.db.execute(
            """SELECT kind,currency,SUM(amount) AS total,COUNT(*) AS count
               FROM rental_cash_events WHERE asset_id=? GROUP BY kind,currency""",
            (asset_id,),
        ).fetchall()
        currencies = {row["currency"] for row in rows}
        if len(currencies) > 1:
            return {
                "internal_only": True,
                "complete": False,
                "reason": "mixed rental currencies require an operator-supplied conversion receipt",
                "currencies": sorted(currencies),
                "customer_disclosure_allowed": False,
            }
        totals = {kind: 0.0 for kind in ALLOWED_RENTAL_CASH_KINDS}
        counts = {kind: 0 for kind in ALLOWED_RENTAL_CASH_KINDS}
        for row in rows:
            totals[row["kind"]] = round(float(row["total"] or 0), 6)
            counts[row["kind"]] = int(row["count"] or 0)
        net_cash = max(
            0.0,
            totals["rental_payment_received"]
            - totals["rental_refund"]
            - totals["rental_chargeback"],
        )
        return {
            "internal_only": True,
            "complete": True,
            "currency": next(iter(currencies), None),
            "totals": totals,
            "counts": counts,
            "net_collected_rental_cash": round(net_cash, 6),
            "settlement_receipt_verified": counts["rental_payment_received"] > 0,
            "eligible_for_customer_engagement_revenue": False,
            "external_tenant_execution_authorized": False,
            "customer_disclosure_allowed": False,
        }
