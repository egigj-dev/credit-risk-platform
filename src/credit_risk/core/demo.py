from __future__ import annotations

from datetime import datetime, timezone
from random import Random

PRODUCTS = [
    ("checking_account", "Checking account"),
    ("savings_account", "Savings account"),
    ("credit_card", "Credit card"),
    ("personal_loan", "Personal loan"),
    ("term_deposit", "Term deposit"),
    ("life_insurance", "Life insurance"),
]


def tier(score: float) -> str:
    if score < 0.12:
        return "LOW"
    if score < 0.28:
        return "MEDIUM"
    if score < 0.5:
        return "HIGH"
    return "CRITICAL"


def model_explanations(
    utilisation: float, days_past_due: int, pd_score: float, npl_score: float
) -> dict:
    """Deterministic, additive demo contributions shaped like production SHAP rows."""
    pd_known = 0.035 + utilisation * 0.38 + days_past_due / 260
    npl_known = pd_score * 0.74 + days_past_due / 180
    return {
        "type": "demo_contribution",
        "pd": [
            {"feature": "Credit utilisation", "value": utilisation, "shap_value": round(utilisation * 0.38, 4)},
            {"feature": "Days past due", "value": days_past_due, "shap_value": round(days_past_due / 260, 4)},
            {"feature": "Other model signals", "value": None, "shap_value": round(pd_score - pd_known, 4)},
        ],
        "npl": [
            {"feature": "PD score", "value": pd_score, "shap_value": round(pd_score * 0.74, 4)},
            {"feature": "Days past due", "value": days_past_due, "shap_value": round(days_past_due / 180, 4)},
            {"feature": "Other early-warning signals", "value": None, "shap_value": round(npl_score - npl_known, 4)},
        ],
    }


def recommendation_policy(pd_tier: str, npl_tier: str) -> dict:
    """Turn product-selling off when risk servicing should take priority."""
    tiers = {pd_tier.upper(), npl_tier.upper()}
    if "CRITICAL" in tiers:
        return {
            "mode": "INTERVENE",
            "title": "Immediate support and risk intervention",
            "message": "All product marketing is paused. Resolve hardship, delinquency, or data-quality concerns before any cross-sell review.",
            "actions": [
                {"priority": "Now", "title": "Assign a specialist case owner", "detail": "Review payment status, recent contact, vulnerability indicators, and any open disputes."},
                {"priority": "Today", "title": "Make a support-led contact", "detail": "Use the agreed channel and discuss affordable assistance—not a new credit product."},
                {"priority": "Review", "title": "Assess restructuring or hardship options", "detail": "Apply affordability, consent, and local regulatory controls before changing terms."},
            ],
        }
    if "HIGH" in tiers or "CONCERN" in tiers:
        return {
            "mode": "SUPPORT",
            "title": "Stabilise before recommending products",
            "message": "Product recommendations are paused while an advisor reviews affordability and early-warning drivers.",
            "actions": [
                {"priority": "Priority", "title": "Review affordability and exposure", "detail": "Confirm income, utilisation, arrears, and existing commitments with current data."},
                {"priority": "Next", "title": "Offer preventive support", "detail": "Discuss payment reminders, budgeting help, or a suitable servicing plan where permitted."},
                {"priority": "Exit gate", "title": "Reassess after risk improves", "detail": "Resume product eligibility only after a fresh score and policy checks clear the client."},
            ],
        }
    if "MEDIUM" in tiers or "WATCH" in tiers:
        return {"mode": "CAUTION", "title": "Recommend with additional checks", "message": "Show eligible products, but require affordability and suitability confirmation before fulfilment.", "actions": []}
    return {"mode": "GROW", "title": "Eligible for responsible recommendations", "message": "Ranked offers may be shown subject to normal eligibility, suitability, and cooldown controls.", "actions": []}


class DemoPortfolio:
    """Deterministic in-memory dataset used for the zero-setup MVP experience."""

    def __init__(self, n_clients: int = 180, seed: int = 42) -> None:
        rng = Random(seed)
        self.clients: dict[int, dict] = {}
        self.accepted: set[tuple[int, str]] = set()
        self.run_history: list[dict] = []
        for client_id in range(100001, 100001 + n_clients):
            income = rng.randint(30_000, 180_000)
            utilisation = round(rng.uniform(0.02, 0.94), 2)
            late_days = rng.choice([0, 0, 0, 2, 5, 15, 35, 90])
            pd = min(
                0.92,
                max(0.015, 0.035 + utilisation * 0.38 + late_days / 260 + rng.uniform(-0.04, 0.04)),
            )
            npl = min(0.91, max(0.01, pd * 0.74 + late_days / 180 + rng.uniform(-0.035, 0.035)))
            owned = {code for code, _ in PRODUCTS if rng.random() < 0.32}
            pd_tier = tier(pd)
            npl_tier = tier(npl)
            policy = recommendation_policy(pd_tier, npl_tier)
            offers = self._offers(rng, pd, owned) if policy["mode"] not in {"SUPPORT", "INTERVENE"} else []
            self.clients[client_id] = {
                "client_id": client_id,
                "name": f"Client {client_id}",
                "segment": rng.choice(["Emerging", "Core", "Affluent", "Small business"]),
                "annual_income": income,
                "utilisation": utilisation,
                "days_past_due": late_days,
                "pd_score": round(pd, 4),
                "pd_tier": pd_tier,
                "npl_score": round(npl, 4),
                "npl_tier": npl_tier,
                "owned_products": sorted(owned),
                "offers": offers,
                "recommendation_policy": policy,
                "model_explanations": model_explanations(utilisation, late_days, round(pd, 4), round(npl, 4)),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

    @staticmethod
    def _offers(rng: Random, pd: float, owned: set[str]) -> list[dict]:
        if pd >= 0.5:
            return []
        candidates = [(code, label) for code, label in PRODUCTS if code not in owned]
        rng.shuffle(candidates)
        return [
            {
                "product_code": code,
                "product_name": label,
                "rank": rank,
                "reason": reason,
                "confidence": round(max(0.15, 0.88 - pd - rank * 0.09), 2),
            }
            for rank, (code, label) in enumerate(candidates[:3], 1)
            for reason in ["Fits current segment and product profile"]
        ]

    def summary(self) -> dict:
        values = list(self.clients.values())
        return {
            "client_count": len(values),
            "mean_pd": round(sum(x["pd_score"] for x in values) / len(values), 4),
            "high_risk_count": sum(x["pd_tier"] in {"HIGH", "CRITICAL"} for x in values),
            "protected_client_count": sum(
                x["recommendation_policy"]["mode"] in {"SUPPORT", "INTERVENE"}
                for x in values
            ),
            "npl_watchlist_count": sum(x["npl_tier"] in {"HIGH", "CRITICAL"} for x in values),
            "offer_count": sum(len(x["offers"]) for x in values),
            "last_run": self.run_history[-1] if self.run_history else None,
        }

    def list_clients(self, query: str = "", risk: str = "") -> list[dict]:
        query = query.lower().strip()
        rows = [
            x
            for x in self.clients.values()
            if not query or query in x["name"].lower() or query in str(x["client_id"])
        ]
        if risk:
            rows = [x for x in rows if x["pd_tier"] == risk.upper()]
        return sorted(rows, key=lambda x: x["pd_score"], reverse=True)

    def accept_offer(self, client_id: int, product_code: str) -> dict:
        client = self.clients[client_id]
        offer = next((x for x in client["offers"] if x["product_code"] == product_code), None)
        if not offer:
            raise KeyError("Offer is not available for this client")
        self.accepted.add((client_id, product_code))
        client["offers"] = [x for x in client["offers"] if x["product_code"] != product_code]
        return {"status": "accepted", "client_id": client_id, "product_code": product_code}

    def run_pipeline(self) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        steps = [
            "source_staging",
            "data_validation",
            "feature_store_rebuild",
            "pd_scoring",
            "npl_scoring",
            "nbo_scoring",
            "reporting_refresh",
        ]
        event = {
            "run_id": f"demo-{len(self.run_history) + 1}",
            "status": "success",
            "started_at": now,
            "steps": steps,
        }
        self.run_history.append(event)
        for client in self.clients.values():
            client["updated_at"] = now
        return event
