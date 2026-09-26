"""Authentication, the error envelope, security headers, backpressure and probes."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from conftest import FIXTURE_REPOS
from fastapi.testclient import TestClient
from patchpilot_api.db import reset_engine
from patchpilot_api.main import create_app
from patchpilot_api.security import check_startup, key_matches, resolve_correlation_id
from patchpilot_core.config import Settings, get_settings, reset_settings
from patchpilot_core.errors import ConfigurationError

KEY = "k" * 32


def run_payload() -> dict:
    return {
        "repository_url": str(FIXTURE_REPOS / "calc_service"),
        "issue_text": "percentage(0, 0) raises ZeroDivisionError.",
    }


@pytest.fixture
def make_client(monkeypatch: pytest.MonkeyPatch) -> Iterator:
    clients: list[TestClient] = []

    def build(raise_server_exceptions: bool = True, **env: str) -> TestClient:
        for name, value in env.items():
            monkeypatch.setenv(f"PATCHPILOT_{name.upper()}", value)
        reset_settings()
        reset_engine()
        client = TestClient(
            create_app(get_settings(), start_worker=False),
            raise_server_exceptions=raise_server_exceptions,
        )
        client.__enter__()
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.__exit__(None, None, None)
    reset_engine()


class TestAuthentication:
    def test_open_by_default_for_local_development(self, make_client) -> None:
        assert make_client().get("/api/v1/system").status_code == 200

    def test_a_request_without_a_key_is_refused(self, make_client) -> None:
        client = make_client(api_keys=KEY)
        response = client.get("/api/v1/runs")
        assert response.status_code == 401
        assert response.json()["code"] == "unauthenticated"
        assert response.headers["www-authenticate"] == "Bearer"

    def test_a_wrong_key_is_refused(self, make_client) -> None:
        client = make_client(api_keys=KEY)
        response = client.get("/api/v1/runs", headers={"Authorization": "Bearer nope"})
        assert response.status_code == 401
        assert "not accepted" in response.json()["message"]

    @pytest.mark.parametrize("headers", [{"Authorization": f"Bearer {KEY}"}, {"X-API-Key": KEY}])
    def test_either_header_form_is_accepted(self, make_client, headers: dict) -> None:
        client = make_client(api_keys=f"other-key-000000000,{KEY}")
        assert client.get("/api/v1/runs", headers=headers).status_code == 200

    def test_every_api_route_is_guarded(self, make_client) -> None:
        client = make_client(api_keys=KEY)
        document = client.get("/openapi.json").json()
        for path, operations in document["paths"].items():
            if "{" in path or not path.startswith("/api/v1"):
                continue
            for method in operations:
                response = client.request(method, path)
                assert response.status_code == 401, f"{method.upper()} {path} is not guarded"

    def test_probes_and_docs_need_no_key(self, make_client) -> None:
        client = make_client(api_keys=KEY)
        for path in ("/health", "/ready", "/api/v1/health", "/openapi.json", "/"):
            assert client.get(path).status_code == 200, path

    def test_the_openapi_document_advertises_both_schemes(self, make_client) -> None:
        schemes = make_client().get("/openapi.json").json()["components"]["securitySchemes"]
        assert {"bearer", "apiKey"} <= set(schemes)

    def test_a_cors_preflight_is_answered_without_a_key(self, make_client) -> None:
        client = make_client(api_keys=KEY)
        response = client.options(
            "/api/v1/runs",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://localhost:5173"

    def test_a_401_is_readable_cross_origin(self, make_client) -> None:
        client = make_client(api_keys=KEY)
        response = client.get("/api/v1/runs", headers={"Origin": "http://localhost:5173"})
        assert response.status_code == 401
        assert response.headers["access-control-allow-origin"] == "http://localhost:5173"

    def test_key_comparison(self) -> None:
        assert key_matches(KEY, ["a", KEY])
        assert not key_matches(KEY + "x", [KEY])
        assert not key_matches("", [KEY])


class TestStartupChecks:
    def test_production_refuses_to_start_without_keys(self, settings: Settings) -> None:
        production = settings.model_copy(update={"environment": "production"})
        with pytest.raises(ConfigurationError) as caught:
            check_startup(production)
        assert "PATCHPILOT_API_KEYS" in (caught.value.remediation or "")

    def test_an_authenticating_proxy_can_be_declared(self, settings: Settings) -> None:
        production = settings.model_copy(
            update={"environment": "production", "allow_unauthenticated": True}
        )
        check_startup(production)

    def test_production_refuses_short_keys(self, settings: Settings) -> None:
        production = settings.model_copy(
            update={"environment": "production", "api_keys": ["short"]}
        )
        with pytest.raises(ConfigurationError):
            check_startup(production)

    def test_production_warns_about_risky_choices(self, settings: Settings) -> None:
        production = settings.model_copy(
            update={
                "environment": "production",
                "api_keys": [KEY],
                "allow_run_overrides": True,
            }
        )
        warnings = check_startup(production)
        assert any("SQLite" in warning for warning in warnings)
        assert any("OVERRIDES" in warning for warning in warnings)

    def test_create_app_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATCHPILOT_ENVIRONMENT", "production")
        reset_settings()
        with pytest.raises(ConfigurationError):
            create_app(get_settings(), start_worker=False)


class TestErrorEnvelope:
    def test_the_generated_correlation_id_is_in_the_body(self, make_client) -> None:
        """It used to be null unless the client had sent one itself."""
        response = make_client().get("/api/v1/runs/run_missing")
        assert response.status_code == 404
        assert response.json()["correlation_id"] == response.headers["x-correlation-id"]
        assert response.json()["correlation_id"]

    def test_a_forged_correlation_id_is_replaced(self, make_client) -> None:
        response = make_client().get(
            "/api/v1/system", headers={"x-correlation-id": 'x" level=CRITICAL ' + "a" * 80}
        )
        assert response.headers["x-correlation-id"] != 'x" level=CRITICAL ' + "a" * 80
        assert resolve_correlation_id("run-42:attempt.1") == "run-42:attempt.1"

    def test_an_unknown_route_uses_the_envelope(self, make_client) -> None:
        body = make_client().get("/api/v1/nothing-here").json()
        assert body["code"] == "not_found"
        assert body["correlation_id"]

    def test_a_crash_is_a_500_envelope_without_internals(self, make_client) -> None:
        client = make_client(raise_server_exceptions=False)

        @client.app.get("/api/v1/explode")  # type: ignore[attr-defined]
        def explode() -> None:
            raise RuntimeError("password=hunter2 in /srv/secret/path")

        response = client.get("/api/v1/explode")
        assert response.status_code == 500
        body = response.json()
        assert body["code"] == "internal_error"
        assert "hunter2" not in response.text
        assert body["correlation_id"] == response.headers["x-correlation-id"]
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_a_repository_error_is_a_flat_envelope(self, make_client) -> None:
        response = make_client().post(
            "/api/v1/runs",
            json={
                "repository_url": "https://example.com/not-github.git",
                "issue_number": 3,
            },
        )
        assert response.status_code == 400
        assert response.json()["code"] == "repository_error"


class TestHeaders:
    def test_security_headers_on_api_responses(self, make_client) -> None:
        response = make_client().get("/api/v1/system")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["cache-control"] == "no-store"
        assert "default-src 'none'" in response.headers["content-security-policy"]

    def test_the_docs_page_is_not_given_the_api_csp(self, make_client) -> None:
        response = make_client().get("/docs")
        assert "content-security-policy" not in response.headers


class TestBackpressure:
    def test_new_runs_are_refused_while_the_queue_is_full(self, make_client) -> None:
        client = make_client(max_queued_jobs="1")
        assert client.post("/api/v1/runs", json=run_payload()).status_code == 202
        response = client.post("/api/v1/runs", json=run_payload())
        assert response.status_code == 503
        assert response.json()["code"] == "queue_full"
        assert response.headers["retry-after"]
        assert client.get("/api/v1/runs").json()["total"] == 1


class TestProbes:
    def test_ready_reports_each_check(self, make_client) -> None:
        response = make_client().get("/ready")
        assert response.status_code == 200
        assert response.json()["checks"] == {"database": "ok", "migrations": "ok"}

    def test_ready_fails_when_the_expected_worker_is_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from patchpilot_api.main import readiness

        reset_settings()
        reset_engine()
        settings = get_settings()
        with TestClient(create_app(settings, start_worker=False)):
            ok, checks = readiness(settings, worker_expected=True)
        assert not ok
        assert checks["worker"] == "not running"
        reset_engine()

    def test_system_exposes_the_run_policy(self, make_client) -> None:
        body = make_client(max_repair_attempts="2").get("/api/v1/system").json()
        assert body["policy"]["max_repair_attempts"] == 2
        assert body["auth_enabled"] is False
