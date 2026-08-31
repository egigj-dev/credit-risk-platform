from fastapi.testclient import TestClient

from credit_risk.api.main import create_app
from credit_risk.core.contracts import validate_data_dir
from credit_risk.core.demo import DemoPortfolio


def test_demo_portfolio_never_recommends_high_risk_clients():
    portfolio = DemoPortfolio(n_clients=80)
    for client in portfolio.clients.values():
        if (
            client["pd_tier"] in {"HIGH", "CRITICAL"}
            or client["npl_tier"] in {"HIGH", "CRITICAL"}
        ):
            assert not client["offers"]
            assert client["recommendation_policy"]["mode"] in {"SUPPORT", "INTERVENE"}


def test_demo_client_includes_additive_model_explanations():
    client = next(iter(DemoPortfolio(n_clients=5).clients.values()))
    explanations = client["model_explanations"]
    assert explanations["type"] == "demo_contribution"
    assert {row["feature"] for row in explanations["pd"]} >= {
        "Credit utilisation",
        "Days past due",
    }
    assert all("shap_value" in row for row in explanations["pd"] + explanations["npl"])


def test_summary_reports_all_support_first_clients():
    portfolio = DemoPortfolio(n_clients=80)
    expected = sum(
        client["recommendation_policy"]["mode"] in {"SUPPORT", "INTERVENE"}
        for client in portfolio.clients.values()
    )
    assert portfolio.summary()["protected_client_count"] == expected


def test_offer_acceptance_removes_offer():
    portfolio = DemoPortfolio(n_clients=80)
    client = next(row for row in portfolio.clients.values() if row["offers"])
    code = client["offers"][0]["product_code"]
    result = portfolio.accept_offer(client["client_id"], code)
    assert result["status"] == "accepted"
    assert code not in [offer["product_code"] for offer in client["offers"]]


def test_data_contract_reports_missing_files(tmp_path):
    assert len(validate_data_dir(tmp_path)) == 7


def test_api_dashboard_and_pipeline_contract(monkeypatch):
    monkeypatch.setenv("APP_MODE", "demo")
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/health/ready").json()["mode"] == "demo"
        assert client.get("/api/v1/summary").json()["client_count"] > 0
        listed = client.get("/api/v1/clients").json()
        detail = client.get(f"/api/v1/clients/{listed[0]['client_id']}").json()
        assert {"recommendation_policy", "model_explanations", "offers"} <= detail.keys()
        assert client.post("/api/v1/pipeline/run").json()["status"] == "success"
