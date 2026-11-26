"""Internal-only engagement cost, capacity, and GPU-reinvestment ledger."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tools.customer_engagement import load_customer_policy
from tools.cyber_sales import EngagementEconomics, calculate_gpu_reinvestment, load_commercial_policy
from tools.messaging_store import ROOT


DEFAULT_LEDGER_PATH = ROOT / "data/engagement_economics.sqlite3"
ALLOWED_USAGE_KINDS = {
    "local_gpu_hours",
    "runpod_gpu_hours",
    "reserved_hardware_hours",
    "human_hours",
    "cloud_input_tokens",
    "cloud_output_tokens",
    "other_direct_cost",
}
ALLOWED_CASH_KINDS = {"invoice_issued", "payment_received", "refund", "chargeback"}
ALLOWED_MILESTONE_KINDS = {
    "contract_signed",
    "testing_authorized",
    "delivery_completed",
    "customer_acceptance",
}
REQUIRED_RECEIPT_FIELDS = ("external_reference", "verified_by", "verification_basis")


@dataclass(frozen=True)
class EngagementResourceUsage:
    local_gpu_hours: float = 0.0
    runpod_gpu_hours: float = 0.0
    reserved_hardware_hours: float = 0.0
    human_hours: float = 0.0
    cloud_input_tokens: int = 0
    cloud_output_tokens: int = 0
    cloud_model: str = ""
    cloud_usage_by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    other_direct_cost: float = 0.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rates_and_policy(
    commercial: dict[str, Any] | None = None,
    customer_policy: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    commercial = commercial or load_commercial_policy()
    customer_policy = customer_policy or load_customer_policy()
    rates = dict(customer_policy.get("internal_cost_defaults", {}))
    for key in ("runpod_gpu_hourly_cost", "reserved_hardware_revenue_per_hour"):
        if commercial.get(key) is not None:
            rates[key] = commercial[key]
    if rates.get("reserved_hardware_revenue_per_hour") is None:
        rates["reserved_hardware_revenue_per_hour"] = (
            commercial.get("podman_revenue_per_reserved_hour")
            if commercial.get("podman_revenue_per_reserved_hour") is not None
            else rates.get("podman_reserved_hourly_opportunity_cost")
        )
    return commercial, rates


def economics_readiness(
    *,
    commercial: dict[str, Any] | None = None,
    customer_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    commercial, rates = _rates_and_policy(commercial, customer_policy)
    required_policy = (
        "customer_price", "tax_reserve_percent", "contingency_percent",
        "gpu_reinvestment_percent", "gpu_target_cost",
    )
    required_rates = (
        "local_gpu_hourly_cost", "runpod_gpu_hourly_cost",
        "reserved_hardware_revenue_per_hour", "human_hourly_cost",
    )
    missing = [f"commercial_policy.{key}" for key in required_policy if commercial.get(key) is None]
    missing += [f"internal_rates.{key}" for key in required_rates if rates.get(key) is None]
    return {
        "version": 1,
        "internal_only": True,
        "complete": not missing,
        "missing_inputs": missing,
        "currency": commercial.get("currency", rates.get("currency", "USD")),
        "provider": commercial.get("compute_capacity_provider"),
        "notes": [
            "Podman is a container engine, not a rental marketplace.",
            "RunPod expense and foregone third-party hardware-rental revenue are modeled separately.",
            "Unknown prices and rates remain null and are never inferred or customer-disclosed.",
        ],
    }


def calculate_engagement_economics(
    usage: EngagementResourceUsage,
    *,
    commercial: dict[str, Any] | None = None,
    customer_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    values = asdict(usage)
    numeric = [
        value for key, value in values.items()
        if key not in {"cloud_model", "cloud_usage_by_model"}
    ]
    if any(float(value) < 0 for value in numeric):
        raise ValueError("Resource usage cannot be negative")
    commercial, rates = _rates_and_policy(commercial, customer_policy)
    readiness = economics_readiness(commercial=commercial, customer_policy=customer_policy)
    missing = list(readiness["missing_inputs"])
    cloud_usage_by_model = {
        str(model): {
            "input_tokens": int(counts.get("input_tokens", 0)),
            "output_tokens": int(counts.get("output_tokens", 0)),
        }
        for model, counts in usage.cloud_usage_by_model.items()
    }
    if not cloud_usage_by_model and (usage.cloud_input_tokens or usage.cloud_output_tokens):
        if usage.cloud_model:
            cloud_usage_by_model[usage.cloud_model] = {
                "input_tokens": int(usage.cloud_input_tokens),
                "output_tokens": int(usage.cloud_output_tokens),
            }
        else:
            missing.append("usage.cloud_model")
    if any(
        count < 0
        for counts in cloud_usage_by_model.values()
        for count in counts.values()
    ):
        raise ValueError("Cloud token usage cannot be negative")
    input_rates = rates.get("cloud_input_cost_per_million_tokens", {})
    output_rates = rates.get("cloud_output_cost_per_million_tokens", {})
    for model in cloud_usage_by_model:
        if input_rates.get(model) is None:
            missing.append(f"internal_rates.cloud_input_cost_per_million_tokens.{model}")
        if output_rates.get(model) is None:
            missing.append(f"internal_rates.cloud_output_cost_per_million_tokens.{model}")
    if missing:
        return {
            **readiness,
            "complete": False,
            "missing_inputs": list(dict.fromkeys(missing)),
            "usage": values,
            "customer_safe_projection": {},
        }
    components = {
        "local_gpu": usage.local_gpu_hours * float(rates["local_gpu_hourly_cost"]),
        "runpod_gpu": usage.runpod_gpu_hours * float(rates["runpod_gpu_hourly_cost"]),
        "reserved_hardware_opportunity": usage.reserved_hardware_hours * float(rates["reserved_hardware_revenue_per_hour"]),
        "human": usage.human_hours * float(rates["human_hourly_cost"]),
        "cloud": sum(
            counts["input_tokens"] / 1_000_000 * float(input_rates[model])
            + counts["output_tokens"] / 1_000_000 * float(output_rates[model])
            for model, counts in cloud_usage_by_model.items()
        ),
        "other_direct": usage.other_direct_cost,
    }
    components = {key: round(value, 6) for key, value in components.items()}
    direct_cost = round(sum(components.values()), 6)
    reinvestment = calculate_gpu_reinvestment(EngagementEconomics(
        customer_price=float(commercial["customer_price"]),
        direct_internal_cost=direct_cost,
        tax_reserve_percent=float(commercial["tax_reserve_percent"]),
        contingency_percent=float(commercial["contingency_percent"]),
        gpu_reinvestment_percent=float(commercial["gpu_reinvestment_percent"]),
        gpu_target_cost=float(commercial["gpu_target_cost"]),
        gpu_fund_current_balance=float(commercial.get("gpu_fund_current_balance", 0)),
    ))
    return {
        "version": 1,
        "internal_only": True,
        "complete": True,
        "currency": readiness["currency"],
        "provider": readiness["provider"],
        "usage": values,
        "components": components,
        "direct_internal_cost": direct_cost,
        "reinvestment": reinvestment,
        "customer_safe_projection": {
            "currency": readiness["currency"],
            "approved_customer_price": float(commercial["customer_price"]),
        },
    }


class EngagementEconomicsLedger:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or DEFAULT_LEDGER_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS usage_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              engagement TEXT NOT NULL,
              kind TEXT NOT NULL,
              quantity REAL NOT NULL,
              unit TEXT NOT NULL,
              source TEXT NOT NULL,
              receipt TEXT NOT NULL,
              occurred_at TEXT NOT NULL
            )"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS cash_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              engagement TEXT NOT NULL,
              kind TEXT NOT NULL,
              amount REAL NOT NULL,
              currency TEXT NOT NULL,
              external_reference TEXT NOT NULL,
              verified_by TEXT NOT NULL,
              verification_basis TEXT NOT NULL,
              receipt TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              UNIQUE(engagement,kind,external_reference)
            )"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS gpu_allocation_proposals(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              engagement TEXT NOT NULL,
              calculation_hash TEXT NOT NULL,
              policy_hash TEXT NOT NULL,
              proposed_amount REAL NOT NULL,
              approved_amount REAL,
              currency TEXT NOT NULL,
              status TEXT NOT NULL,
              operator_approval_id TEXT,
              approved_by TEXT,
              calculation TEXT NOT NULL,
              created_at TEXT NOT NULL,
              approved_at TEXT,
              UNIQUE(engagement,calculation_hash)
            )"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS milestone_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              engagement TEXT NOT NULL,
              kind TEXT NOT NULL,
              external_reference TEXT NOT NULL,
              verified_by TEXT NOT NULL,
              verification_basis TEXT NOT NULL,
              receipt TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              UNIQUE(engagement,kind,external_reference)
            )"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS gpu_purchase_authorizations(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              allocation_id INTEGER NOT NULL UNIQUE,
              engagement TEXT NOT NULL,
              calculation_hash TEXT NOT NULL,
              authorization_hash TEXT NOT NULL UNIQUE,
              vendor TEXT NOT NULL,
              item TEXT NOT NULL,
              sku TEXT NOT NULL,
              quote_reference TEXT NOT NULL,
              quote_url TEXT NOT NULL,
              subtotal REAL NOT NULL,
              tax REAL NOT NULL,
              shipping REAL NOT NULL,
              total REAL NOT NULL,
              currency TEXT NOT NULL,
              warranty TEXT NOT NULL,
              return_policy TEXT NOT NULL,
              operator_approval_id TEXT NOT NULL,
              approved_by TEXT NOT NULL,
              status TEXT NOT NULL,
              transaction_reference TEXT UNIQUE,
              execution_receipt TEXT,
              created_at TEXT NOT NULL,
              executed_at TEXT,
              UNIQUE(vendor,quote_reference)
            )"""
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def record(self, engagement: str, kind: str, quantity: float, unit: str, source: str, receipt: dict[str, Any] | None = None) -> int:
        if kind not in ALLOWED_USAGE_KINDS:
            raise ValueError("Unsupported economics event kind")
        if float(quantity) < 0:
            raise ValueError("Usage quantity cannot be negative")
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO usage_events(engagement,kind,quantity,unit,source,receipt,occurred_at) VALUES(?,?,?,?,?,?,?)",
                (engagement, kind, float(quantity), unit, source, json.dumps(receipt or {}), utc_now()),
            )
        return int(cursor.lastrowid)

    def usage(self, engagement: str, *, cloud_model: str = "") -> EngagementResourceUsage:
        totals = {kind: 0.0 for kind in ALLOWED_USAGE_KINDS}
        for row in self.db.execute(
            "SELECT kind,SUM(quantity) AS total FROM usage_events WHERE engagement=? GROUP BY kind", (engagement,)
        ):
            totals[row["kind"]] = float(row["total"] or 0)
        cloud_usage_by_model: dict[str, dict[str, int]] = {}
        unassigned = {"input_tokens": 0, "output_tokens": 0}
        token_kind_to_key = {
            "cloud_input_tokens": "input_tokens",
            "cloud_output_tokens": "output_tokens",
        }
        for row in self.db.execute(
            "SELECT kind,quantity,receipt FROM usage_events WHERE engagement=? AND kind IN ('cloud_input_tokens','cloud_output_tokens')",
            (engagement,),
        ):
            key = token_kind_to_key[row["kind"]]
            try:
                receipt = json.loads(row["receipt"] or "{}")
            except (TypeError, json.JSONDecodeError):
                receipt = {}
            model = str(receipt.get("model", "")).strip()
            if model:
                counts = cloud_usage_by_model.setdefault(model, {"input_tokens": 0, "output_tokens": 0})
                counts[key] += int(row["quantity"])
            else:
                unassigned[key] += int(row["quantity"])
        if any(unassigned.values()) and cloud_model:
            counts = cloud_usage_by_model.setdefault(cloud_model, {"input_tokens": 0, "output_tokens": 0})
            counts["input_tokens"] += unassigned["input_tokens"]
            counts["output_tokens"] += unassigned["output_tokens"]
        inferred_model = cloud_model
        if not any(unassigned.values()) and len(cloud_usage_by_model) == 1:
            inferred_model = next(iter(cloud_usage_by_model))
        return EngagementResourceUsage(
            local_gpu_hours=totals["local_gpu_hours"],
            runpod_gpu_hours=totals["runpod_gpu_hours"],
            reserved_hardware_hours=totals["reserved_hardware_hours"],
            human_hours=totals["human_hours"],
            cloud_input_tokens=int(totals["cloud_input_tokens"]),
            cloud_output_tokens=int(totals["cloud_output_tokens"]),
            cloud_model=inferred_model,
            cloud_usage_by_model=cloud_usage_by_model,
            other_direct_cost=totals["other_direct_cost"],
        )

    def record_cash_event(
        self,
        engagement: str,
        kind: str,
        amount: float,
        currency: str,
        receipt: dict[str, Any],
    ) -> int:
        if kind not in ALLOWED_CASH_KINDS:
            raise ValueError("Unsupported cash event kind")
        if float(amount) <= 0:
            raise ValueError("Cash event amount must be positive")
        currency = str(currency or "").strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("Cash event currency must be a three letter code")
        if not isinstance(receipt, dict):
            raise ValueError("Cash events require a verification receipt")
        missing = [field for field in REQUIRED_RECEIPT_FIELDS if not str(receipt.get(field, "")).strip()]
        if missing:
            raise ValueError("Cash receipt is missing: " + ", ".join(missing))
        values = {field: str(receipt[field]).strip() for field in REQUIRED_RECEIPT_FIELDS}
        try:
            with self.db:
                cursor = self.db.execute(
                    """INSERT INTO cash_events(
                      engagement,kind,amount,currency,external_reference,verified_by,verification_basis,receipt,occurred_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        engagement,
                        kind,
                        float(amount),
                        currency,
                        values["external_reference"],
                        values["verified_by"],
                        values["verification_basis"],
                        json.dumps(receipt, ensure_ascii=False),
                        utc_now(),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("Duplicate cash receipt reference") from error
        return int(cursor.lastrowid)

    def cash_summary(self, engagement: str) -> dict[str, Any]:
        rows = self.db.execute(
            "SELECT kind,currency,SUM(amount) AS total,COUNT(*) AS count FROM cash_events WHERE engagement=? GROUP BY kind,currency",
            (engagement,),
        ).fetchall()
        currencies = {row["currency"] for row in rows}
        if len(currencies) > 1:
            return {
                "internal_only": True,
                "complete": False,
                "reason": "mixed currencies require an operator supplied conversion receipt",
                "currencies": sorted(currencies),
            }
        totals = {kind: 0.0 for kind in ALLOWED_CASH_KINDS}
        counts = {kind: 0 for kind in ALLOWED_CASH_KINDS}
        for row in rows:
            totals[row["kind"]] = round(float(row["total"] or 0), 6)
            counts[row["kind"]] = int(row["count"] or 0)
        net_collected = max(0.0, totals["payment_received"] - totals["refund"] - totals["chargeback"])
        return {
            "internal_only": True,
            "complete": True,
            "currency": next(iter(currencies), None),
            "totals": totals,
            "counts": counts,
            "net_collected_cash": round(net_collected, 6),
            "invoice_balance": round(max(0.0, totals["invoice_issued"] - totals["payment_received"] + totals["refund"] + totals["chargeback"]), 6),
            "payment_receipt_verified": counts["payment_received"] > 0,
        }

    def record_milestone_event(
        self,
        engagement: str,
        kind: str,
        receipt: dict[str, Any],
    ) -> int:
        if kind not in ALLOWED_MILESTONE_KINDS:
            raise ValueError("Unsupported engagement milestone kind")
        if not isinstance(receipt, dict):
            raise ValueError("Engagement milestones require a verification receipt")
        missing = [field for field in REQUIRED_RECEIPT_FIELDS if not str(receipt.get(field, "")).strip()]
        if missing:
            raise ValueError("Milestone receipt is missing: " + ", ".join(missing))
        values = {field: str(receipt[field]).strip() for field in REQUIRED_RECEIPT_FIELDS}
        try:
            with self.db:
                cursor = self.db.execute(
                    """INSERT INTO milestone_events(
                      engagement,kind,external_reference,verified_by,verification_basis,receipt,occurred_at
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        engagement,
                        kind,
                        values["external_reference"],
                        values["verified_by"],
                        values["verification_basis"],
                        json.dumps(receipt, ensure_ascii=False),
                        utc_now(),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("Duplicate milestone receipt reference") from error
        return int(cursor.lastrowid)

    def milestone_summary(self, engagement: str) -> dict[str, Any]:
        counts = {kind: 0 for kind in ALLOWED_MILESTONE_KINDS}
        for row in self.db.execute(
            "SELECT kind,COUNT(*) AS count FROM milestone_events WHERE engagement=? GROUP BY kind",
            (engagement,),
        ):
            counts[row["kind"]] = int(row["count"] or 0)
        required = {
            "contract_signed": counts["contract_signed"] > 0,
            "testing_authorized": counts["testing_authorized"] > 0,
            "delivery_completed": counts["delivery_completed"] > 0,
        }
        return {
            "internal_only": True,
            "counts": counts,
            "contract_signed_verified": required["contract_signed"],
            "testing_authorized_verified": required["testing_authorized"],
            "delivery_completed_verified": required["delivery_completed"],
            "customer_acceptance_verified": counts["customer_acceptance"] > 0,
            "first_job_completed_verified": all(required.values()),
        }

    @staticmethod
    def _policy_hash(commercial: dict[str, Any], customer_policy: dict[str, Any]) -> str:
        payload = json.dumps({"commercial": commercial, "customer_policy": customer_policy}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()

    def collected_cash_reinvestment_status(
        self,
        engagement: str,
        *,
        commercial: dict[str, Any] | None = None,
        customer_policy: dict[str, Any] | None = None,
        cloud_model: str = "",
    ) -> dict[str, Any]:
        commercial = commercial or load_commercial_policy()
        customer_policy = customer_policy or load_customer_policy()
        cash = self.cash_summary(engagement)
        milestones = self.milestone_summary(engagement)
        economics = calculate_engagement_economics(
            self.usage(engagement, cloud_model=cloud_model),
            commercial=commercial,
            customer_policy=customer_policy,
        )
        base = {
            "version": 1,
            "internal_only": True,
            "engagement": engagement,
            "cash": cash,
            "milestones": milestones,
            "first_job_completed_verified": milestones["first_job_completed_verified"],
            "economics_complete": economics.get("complete", False),
            "purchase_authorized": False,
            "customer_disclosure_allowed": False,
        }
        if not cash.get("complete") or not economics.get("complete"):
            return {
                **base,
                "complete": False,
                "eligible_for_allocation_proposal": False,
                "missing_inputs": economics.get("missing_inputs", []),
                "reason": cash.get("reason") or "engagement economics are incomplete",
            }
        net_collected = float(cash["net_collected_cash"])
        approved_price = float(commercial["customer_price"])
        collected_used = min(net_collected, approved_price)
        reinvestment = calculate_gpu_reinvestment(EngagementEconomics(
            customer_price=collected_used,
            direct_internal_cost=float(economics["direct_internal_cost"]),
            tax_reserve_percent=float(commercial["tax_reserve_percent"]),
            contingency_percent=float(commercial["contingency_percent"]),
            gpu_reinvestment_percent=float(commercial["gpu_reinvestment_percent"]),
            gpu_target_cost=float(commercial["gpu_target_cost"]),
            gpu_fund_current_balance=float(commercial.get("gpu_fund_current_balance", 0)),
        ))
        eligible = bool(
            milestones["first_job_completed_verified"]
            and cash["payment_receipt_verified"]
            and reinvestment["gpu_reinvestment"] > 0
        )
        if not milestones["first_job_completed_verified"]:
            reason = "verified contract, testing authorization, and delivery completion receipts are required"
        elif not cash["payment_receipt_verified"]:
            reason = "no verified collected cash is available"
        elif reinvestment["gpu_reinvestment"] <= 0:
            reason = "no verified distributable collected cash is available"
        else:
            reason = "verified completed work and collected cash support an allocation proposal"
        calculation = {
            **base,
            "complete": True,
            "currency": economics["currency"],
            "approved_customer_price": approved_price,
            "net_collected_cash": net_collected,
            "collected_cash_used": collected_used,
            "excess_cash_unallocated": round(max(0.0, net_collected - approved_price), 6),
            "direct_internal_cost": economics["direct_internal_cost"],
            "reserves_and_reinvestment": reinvestment,
            "eligible_for_allocation_proposal": eligible,
            "reason": reason,
        }
        calculation["policy_hash"] = self._policy_hash(commercial, customer_policy)
        calculation["calculation_hash"] = hashlib.sha256(
            json.dumps(calculation, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        return calculation

    def propose_gpu_allocation(
        self,
        engagement: str,
        *,
        commercial: dict[str, Any] | None = None,
        customer_policy: dict[str, Any] | None = None,
        cloud_model: str = "",
    ) -> dict[str, Any]:
        status = self.collected_cash_reinvestment_status(
            engagement,
            commercial=commercial,
            customer_policy=customer_policy,
            cloud_model=cloud_model,
        )
        if not status.get("eligible_for_allocation_proposal"):
            raise ValueError("No verified completed engagement and collected cash are eligible for a GPU allocation proposal")
        amount = float(status["reserves_and_reinvestment"]["gpu_reinvestment"])
        with self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO gpu_allocation_proposals(
                  engagement,calculation_hash,policy_hash,proposed_amount,currency,status,calculation,created_at
                ) VALUES(?,?,?,?,?,'proposed',?,?)""",
                (
                    engagement,
                    status["calculation_hash"],
                    status["policy_hash"],
                    amount,
                    status["currency"],
                    json.dumps(status, ensure_ascii=False),
                    utc_now(),
                ),
            )
        row = self.db.execute(
            "SELECT * FROM gpu_allocation_proposals WHERE engagement=? AND calculation_hash=?",
            (engagement, status["calculation_hash"]),
        ).fetchone()
        return {
            "id": int(row["id"]),
            "engagement": row["engagement"],
            "status": row["status"],
            "proposed_amount": row["proposed_amount"],
            "currency": row["currency"],
            "calculation_hash": row["calculation_hash"],
            "purchase_authorized": False,
            "customer_disclosure_allowed": False,
        }

    def approve_gpu_allocation_reservation(
        self,
        allocation_id: int,
        *,
        operator_approval_id: str,
        approved_by: str,
        approved_amount: float | None = None,
        commercial: dict[str, Any] | None = None,
        customer_policy: dict[str, Any] | None = None,
        cloud_model: str = "",
    ) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM gpu_allocation_proposals WHERE id=?", (int(allocation_id),)).fetchone()
        if row is None:
            raise KeyError(allocation_id)
        if row["status"] != "proposed":
            raise ValueError("GPU allocation is not awaiting approval")
        if not str(operator_approval_id or "").strip() or not str(approved_by or "").strip():
            raise ValueError("Operator approval identity is required")
        current = self.collected_cash_reinvestment_status(
            row["engagement"],
            commercial=commercial,
            customer_policy=customer_policy,
            cloud_model=cloud_model,
        )
        if current.get("calculation_hash") != row["calculation_hash"] or current.get("policy_hash") != row["policy_hash"]:
            raise ValueError("GPU allocation calculation is stale")
        amount = float(row["proposed_amount"] if approved_amount is None else approved_amount)
        if amount <= 0 or amount > float(row["proposed_amount"]):
            raise ValueError("Approved amount must be positive and no greater than the proposal")
        with self.db:
            self.db.execute(
                """UPDATE gpu_allocation_proposals SET status='reserved',approved_amount=?,operator_approval_id=?,
                   approved_by=?,approved_at=? WHERE id=? AND status='proposed'""",
                (amount, str(operator_approval_id).strip(), str(approved_by).strip(), utc_now(), int(allocation_id)),
            )
        return {
            "id": int(allocation_id),
            "status": "reserved",
            "approved_amount": amount,
            "currency": row["currency"],
            "operator_approval_id": str(operator_approval_id).strip(),
            "purchase_authorized": False,
            "next_gate": "operator separately approves an exact vendor, item, price, tax, warranty, and purchase transaction",
            "customer_disclosure_allowed": False,
        }

    def get_gpu_purchase_authorization(self, authorization_id: int) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM gpu_purchase_authorizations WHERE id=?", (int(authorization_id),)
        ).fetchone()
        if row is None:
            raise KeyError(authorization_id)
        value = dict(row)
        value["execution_receipt"] = (
            json.loads(value["execution_receipt"]) if value.get("execution_receipt") else None
        )
        value["purchase_authorized"] = value["status"] in {"authorized", "executed"}
        value["purchase_executed"] = value["status"] == "executed"
        value["customer_disclosure_allowed"] = False
        return value

    def authorize_gpu_purchase(
        self,
        allocation_id: int,
        *,
        quote: dict[str, Any],
        operator_approval_id: str,
        approved_by: str,
        commercial: dict[str, Any] | None = None,
        customer_policy: dict[str, Any] | None = None,
        cloud_model: str = "",
    ) -> dict[str, Any]:
        allocation = self.db.execute(
            "SELECT * FROM gpu_allocation_proposals WHERE id=?", (int(allocation_id),)
        ).fetchone()
        if allocation is None:
            raise KeyError(allocation_id)
        if allocation["status"] != "reserved":
            raise ValueError("GPU purchase requires an operator-reserved allocation")
        purchase_approval = str(operator_approval_id or "").strip()
        approver = str(approved_by or "").strip()
        if not purchase_approval or not approver:
            raise ValueError("Exact GPU purchase approval identity is required")
        if purchase_approval == str(allocation["operator_approval_id"] or "").strip():
            raise ValueError("GPU purchase requires a separate operator approval from the allocation reservation")
        if not isinstance(quote, dict):
            raise ValueError("GPU purchase requires an exact vendor quote")
        string_fields = (
            "vendor", "item", "sku", "quote_reference", "quote_url",
            "currency", "warranty", "return_policy",
        )
        missing = [field for field in string_fields if not str(quote.get(field, "")).strip()]
        if missing:
            raise ValueError("GPU quote is missing: " + ", ".join(missing))
        parsed_url = urlparse(str(quote["quote_url"]).strip())
        if parsed_url.scheme != "https" or not parsed_url.hostname:
            raise ValueError("GPU quote URL must be an HTTPS vendor URL")
        amounts = {}
        for field in ("subtotal", "tax", "shipping", "total"):
            try:
                amounts[field] = round(float(quote[field]), 2)
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("GPU quote amount is invalid: " + field) from error
            if amounts[field] < 0:
                raise ValueError("GPU quote amounts cannot be negative")
        expected_total = round(amounts["subtotal"] + amounts["tax"] + amounts["shipping"], 2)
        if abs(expected_total - amounts["total"]) > 0.005:
            raise ValueError("GPU quote total does not equal subtotal, tax, and shipping")
        currency = str(quote["currency"]).strip().upper()
        if currency != str(allocation["currency"]).upper():
            raise ValueError("GPU quote currency does not match the reserved allocation")
        if amounts["total"] > float(allocation["approved_amount"] or 0):
            raise ValueError("GPU quote exceeds the operator-reserved allocation")
        current = self.collected_cash_reinvestment_status(
            allocation["engagement"],
            commercial=commercial,
            customer_policy=customer_policy,
            cloud_model=cloud_model,
        )
        if (
            current.get("calculation_hash") != allocation["calculation_hash"]
            or current.get("policy_hash") != allocation["policy_hash"]
        ):
            raise ValueError("GPU purchase allocation calculation is stale")
        normalized_quote = {
            **{field: str(quote[field]).strip() for field in string_fields},
            **amounts,
            "currency": currency,
        }
        authorization_hash = hashlib.sha256(json.dumps(
            {
                "allocation_id": int(allocation_id),
                "quote": normalized_quote,
                "operator_approval_id": purchase_approval,
                "approved_by": approver,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()).hexdigest()
        existing = self.db.execute(
            "SELECT id,authorization_hash FROM gpu_purchase_authorizations WHERE allocation_id=?",
            (int(allocation_id),),
        ).fetchone()
        if existing is not None:
            if existing["authorization_hash"] != authorization_hash:
                raise ValueError("GPU allocation already has a different exact purchase authorization")
            return self.get_gpu_purchase_authorization(existing["id"])
        with self.db:
            cursor = self.db.execute(
                """INSERT INTO gpu_purchase_authorizations(
                  allocation_id,engagement,calculation_hash,authorization_hash,vendor,item,sku,
                  quote_reference,quote_url,subtotal,tax,shipping,total,currency,warranty,return_policy,
                  operator_approval_id,approved_by,status,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'authorized',?)""",
                (
                    int(allocation_id),
                    allocation["engagement"],
                    allocation["calculation_hash"],
                    authorization_hash,
                    normalized_quote["vendor"],
                    normalized_quote["item"],
                    normalized_quote["sku"],
                    normalized_quote["quote_reference"],
                    normalized_quote["quote_url"],
                    amounts["subtotal"],
                    amounts["tax"],
                    amounts["shipping"],
                    amounts["total"],
                    currency,
                    normalized_quote["warranty"],
                    normalized_quote["return_policy"],
                    purchase_approval,
                    approver,
                    utc_now(),
                ),
            )
        return self.get_gpu_purchase_authorization(int(cursor.lastrowid))

    def record_gpu_purchase_execution(
        self,
        authorization_id: int,
        *,
        receipt: dict[str, Any],
        commercial: dict[str, Any] | None = None,
        customer_policy: dict[str, Any] | None = None,
        cloud_model: str = "",
    ) -> dict[str, Any]:
        authorization = self.get_gpu_purchase_authorization(authorization_id)
        if authorization["status"] != "authorized":
            raise ValueError("GPU purchase authorization is not awaiting execution evidence")
        if not isinstance(receipt, dict):
            raise ValueError("GPU purchase execution requires a vendor receipt")
        required = (
            "external_reference", "vendor_order_id", "verified_by", "verification_basis",
            "charged_total", "currency",
        )
        missing = [field for field in required if not str(receipt.get(field, "")).strip()]
        if missing:
            raise ValueError("GPU purchase receipt is missing: " + ", ".join(missing))
        allocation = self.db.execute(
            "SELECT * FROM gpu_allocation_proposals WHERE id=?", (authorization["allocation_id"],)
        ).fetchone()
        current = self.collected_cash_reinvestment_status(
            authorization["engagement"],
            commercial=commercial,
            customer_policy=customer_policy,
            cloud_model=cloud_model,
        )
        if (
            current.get("calculation_hash") != allocation["calculation_hash"]
            or current.get("policy_hash") != allocation["policy_hash"]
        ):
            raise ValueError("GPU purchase authorization became stale before execution")
        try:
            charged_total = round(float(receipt["charged_total"]), 2)
        except (TypeError, ValueError) as error:
            raise ValueError("GPU purchase charged total is invalid") from error
        if abs(charged_total - float(authorization["total"])) > 0.005:
            raise ValueError("GPU purchase receipt does not match the authorized total")
        currency = str(receipt["currency"]).strip().upper()
        if currency != authorization["currency"]:
            raise ValueError("GPU purchase receipt currency does not match authorization")
        transaction_reference = str(receipt["external_reference"]).strip()
        try:
            with self.db:
                changed = self.db.execute(
                    """UPDATE gpu_purchase_authorizations SET status='executed',transaction_reference=?,
                       execution_receipt=?,executed_at=? WHERE id=? AND status='authorized'""",
                    (
                        transaction_reference,
                        json.dumps(receipt, ensure_ascii=False),
                        utc_now(),
                        int(authorization_id),
                    ),
                ).rowcount
                if changed != 1:
                    raise ValueError("GPU purchase execution claim was lost")
        except sqlite3.IntegrityError as error:
            raise ValueError("Duplicate GPU purchase transaction reference") from error
        value = self.get_gpu_purchase_authorization(authorization_id)
        value["asset_received_verified"] = False
        value["next_gate"] = "operator verifies delivery, serial number, condition, and asset inventory entry"
        return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect internal customer-engagement economics readiness")
    parser.add_argument("command", choices=["status"])
    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps(economics_readiness(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
