"""The server's configuration, not the request, decides what a run may do."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import FIXTURE_REPOS
from fastapi.testclient import TestClient
from patchpilot_api.db import reset_engine
from patchpilot_api.main import create_app
from patchpilot_api.policy import build_run_config, check_repository_source, dataset_path
from patchpilot_api.schemas import RunCreate
from patchpilot_core.config import Settings, get_settings, reset_settings
from patchpilot_core.errors import PolicyError, ValidationError
from patchpilot_evals import DEFAULT_DATASET_DIR


def request(**overrides) -> RunCreate:
    payload = {
        "repository_url": str(FIXTURE_REPOS / "calc_service"),
        "issue_text": "percentage(0, 0) raises ZeroDivisionError.",
    }
    payload.update(overrides)
    return RunCreate.model_validate(payload)


def production(tmp_path: Path, **overrides) -> Settings:
    values = {
        "environment": "production",
        "data_dir": tmp_path / "data",
        "api_keys": ["test-key"],
        "sandbox_backend": "docker",
    }
    values.update(overrides)
    return Settings(**values)


class TestDefaultsComeFromTheServer:
    def test_every_operator_sandbox_limit_reaches_the_run(self, settings: Settings) -> None:
        """These settings used to be silently ignored for API-created runs."""
        configured = settings.model_copy(
            update={
                "sandbox_pids_limit": 64,
                "sandbox_max_output_bytes": 1000,
                "sandbox_timeout_seconds": 90,
                "sandbox_memory_mb": 512,
                "sandbox_cpus": 0.5,
                "sandbox_user": "20000:20000",
                "max_repair_attempts": 2,
                "retrieval_top_k": 7,
            }
        )
        config = build_run_config(request(), configured)
        assert config.limits.pids == 64
        assert config.limits.max_output_bytes == 1000
        assert config.limits.timeout_seconds == 90
        assert config.limits.memory_mb == 512
        assert config.limits.cpus == 0.5
        assert config.limits.user == "20000:20000"
        assert config.max_repair_attempts == 2
        assert config.retrieval_top_k == 7
        assert config.model == configured.default_model

    def test_a_request_can_tighten_the_defaults(self, settings: Settings) -> None:
        config = build_run_config(
            request(timeout_seconds=30, memory_mb=256, max_repair_attempts=1), settings
        )
        assert config.limits.timeout_seconds == 30
        assert config.limits.memory_mb == 256
        assert config.max_repair_attempts == 1


class TestCaps:
    def test_the_retry_budget_is_a_hard_cap(self, settings: Settings) -> None:
        with pytest.raises(PolicyError) as caught:
            build_run_config(
                request(max_repair_attempts=settings.max_repair_attempts + 1), settings
            )
        assert "max_repair_attempts" in caught.value.context["violations"][0]

    def test_resource_caps_apply_even_where_overrides_are_allowed(self, settings: Settings) -> None:
        capped = settings.model_copy(
            update={
                "sandbox_max_timeout_seconds": 60,
                "sandbox_max_memory_mb": 512,
                "sandbox_max_cpus": 1.0,
            }
        )
        assert capped.run_overrides_allowed
        with pytest.raises(PolicyError) as caught:
            build_run_config(request(timeout_seconds=61, memory_mb=1024, cpus=2), capped)
        assert len(caught.value.context["violations"]) == 3


class TestOverrides:
    @pytest.mark.parametrize(
        "field",
        [
            {"network": "bridge"},
            {"sandbox_image": "attacker/image:latest"},
            {"allowed_write_globs": [".github/**"]},
            {"max_patch_files": 500},
            {"max_patch_lines": 50_000},
        ],
    )
    def test_production_refuses_to_loosen_the_policy(self, tmp_path: Path, field: dict) -> None:
        with pytest.raises(PolicyError):
            build_run_config(request(**field), production(tmp_path))

    def test_production_refuses_the_unisolated_backend(self, tmp_path: Path) -> None:
        with pytest.raises(PolicyError) as caught:
            build_run_config(request(sandbox_backend="local"), production(tmp_path))
        assert "local" in caught.value.context["violations"][0]

    def test_an_operator_can_allow_overrides_explicitly(self, tmp_path: Path) -> None:
        config = build_run_config(
            request(network="bridge", allowed_write_globs=["docs/**"]),
            production(tmp_path, allow_run_overrides=True),
        )
        assert config.limits.network == "bridge"
        assert config.allowed_write_globs == ["docs/**"]

    def test_locally_overrides_remain_available(self, settings: Settings) -> None:
        config = build_run_config(request(network="bridge"), settings)
        assert config.limits.network == "bridge"

    def test_every_violation_is_reported_at_once(self, tmp_path: Path) -> None:
        with pytest.raises(PolicyError) as caught:
            build_run_config(
                request(network="bridge", sandbox_image="x:1", max_repair_attempts=9),
                production(tmp_path),
            )
        assert len(caught.value.context["violations"]) == 3


class TestModels:
    def test_an_unconfigured_model_is_refused_before_queueing(self, settings: Settings) -> None:
        with pytest.raises(ValidationError) as caught:
            build_run_config(request(model="openai:gpt-4o"), settings)
        assert "PATCHPILOT_OPENAI_API_KEY" in (caught.value.remediation or "")

    def test_an_unknown_provider_is_refused(self, settings: Settings) -> None:
        with pytest.raises(ValidationError):
            build_run_config(request(model="nonsense:model"), settings)


class TestRepositorySources:
    def test_remote_urls_are_always_accepted(self, tmp_path: Path) -> None:
        check_repository_source("https://github.com/owner/repo", production(tmp_path))

    def test_production_refuses_host_paths(self, tmp_path: Path) -> None:
        for url in ("/etc", "file:///root", "relative/path"):
            with pytest.raises(PolicyError):
                check_repository_source(url, production(tmp_path))

    def test_roots_confine_local_paths(self, settings: Settings, tmp_path: Path) -> None:
        confined = settings.model_copy(update={"local_repository_roots": [FIXTURE_REPOS]})
        check_repository_source(str(FIXTURE_REPOS / "calc_service"), confined)
        with pytest.raises(PolicyError):
            check_repository_source(str(tmp_path), confined)
        # A traversal that resolves outside the root is outside the root.
        with pytest.raises(PolicyError):
            check_repository_source(str(FIXTURE_REPOS / ".." / ".."), confined)

    def test_roots_are_parsed_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("PATCHPILOT_LOCAL_REPOSITORY_ROOTS", f"{tmp_path},{FIXTURE_REPOS}")
        reset_settings()
        assert get_settings().local_repository_roots == [tmp_path.resolve(), FIXTURE_REPOS]


class TestDatasets:
    def test_a_name_resolves_inside_the_dataset_directory(self) -> None:
        path = dataset_path("patchpilot-fixtures", DEFAULT_DATASET_DIR)
        assert path.parent == DEFAULT_DATASET_DIR

    @pytest.mark.parametrize(
        "name", ["../../pyproject.toml", "/etc/passwd", ".hidden", "sub/dir", ""]
    )
    def test_paths_are_refused(self, name: str) -> None:
        with pytest.raises(ValidationError):
            dataset_path(name, DEFAULT_DATASET_DIR)


# --------------------------------------------------------------------------- #
# Through HTTP
# --------------------------------------------------------------------------- #
@pytest.fixture
def client() -> Iterator[TestClient]:
    reset_engine()
    app = create_app(get_settings(), start_worker=False)
    with TestClient(app) as test_client:
        yield test_client
    reset_engine()


class TestHttp:
    def test_the_persisted_config_carries_the_server_limits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATCHPILOT_SANDBOX_PIDS_LIMIT", "77")
        monkeypatch.setenv("PATCHPILOT_SANDBOX_TIMEOUT_SECONDS", "45")
        reset_settings()
        reset_engine()
        with TestClient(create_app(get_settings(), start_worker=False)) as client:
            created = client.post(
                "/api/v1/runs",
                json={
                    "repository_url": str(FIXTURE_REPOS / "calc_service"),
                    "issue_text": "percentage(0, 0) raises ZeroDivisionError.",
                },
            )
            assert created.status_code == 202, created.text
            detail = client.get(f"/api/v1/runs/{created.json()['id']}").json()
        assert detail["config"]["limits"]["pids"] == 77
        assert detail["config"]["limits"]["timeout_seconds"] == 45
        reset_engine()

    def test_a_policy_violation_is_a_422_listing_every_reason(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/runs",
            json={
                "repository_url": str(FIXTURE_REPOS / "calc_service"),
                "issue_text": "bug",
                "max_repair_attempts": 9,
            },
        )
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "policy_violation"
        assert body["context"]["violations"]
        assert client.get("/api/v1/runs").json()["total"] == 0

    def test_an_unconfigured_model_is_refused_at_creation(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/runs",
            json={
                "repository_url": str(FIXTURE_REPOS / "calc_service"),
                "issue_text": "bug",
                "model": "anthropic:claude-opus-5-5",
            },
        )
        assert response.status_code == 422
        assert "PATCHPILOT_ANTHROPIC_API_KEY" in response.json()["remediation"]

    def test_a_benchmark_cannot_name_a_file_outside_the_dataset_directory(
        self, client: TestClient
    ) -> None:
        response = client.post(
            "/api/v1/benchmarks",
            json={"dataset": "../../pyproject.toml", "models": ["mock:deterministic"]},
        )
        assert response.status_code == 422

    def test_oversized_issue_text_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/runs",
            json={
                "repository_url": str(FIXTURE_REPOS / "calc_service"),
                "issue_text": "x" * 200_000,
            },
        )
        assert response.status_code == 422
