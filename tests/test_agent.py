"""Agent internals: adapters, structured plans, command discovery, prompts."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from conftest import SOLUTIONS_DIR
from patchpilot_agent import (
    AnthropicAdapter,
    MockAdapter,
    OpenAICompatAdapter,
    build_adapter,
    build_command_plan,
    build_patch_messages,
    build_plan_messages,
    coerce_plan,
    describe_models,
    discover_commands,
    extract_json_object,
    parse_plan,
    render_context,
    resolve_commands,
    split_model,
    targeted_test_command,
)
from patchpilot_agent.adapters.mock import load_solutions
from patchpilot_core.enums import CommandKind
from patchpilot_core.errors import ConfigurationError, ModelAdapterError, StructuredOutputError
from patchpilot_core.models import (
    ChatMessage,
    CodeChunk,
    ContextPackage,
    IssueSpec,
    RepairPlan,
    RetrievedChunk,
    RunConfig,
    ScoreComponent,
    SymbolType,
)

# --------------------------------------------------------------------------- #
# Structured output
# --------------------------------------------------------------------------- #
VALID_PLAN = {
    "root_cause": "percentage divides by zero without a guard clause",
    "files_to_change": ["calc_service/operations.py"],
    "tests_to_run": ["tests/test_operations.py"],
    "patch_strategy": "add an early return of 0.0 when whole is zero",
    "assumptions": ["the docstring is authoritative"],
    "uncertainties": ["negative denominators are untested"],
    "confidence": 0.8,
}


class TestStructuredOutput:
    def test_parses_a_bare_json_object(self) -> None:
        plan = parse_plan(json.dumps(VALID_PLAN))
        assert plan.confidence == 0.8
        assert plan.files_to_change == ["calc_service/operations.py"]

    def test_recovers_json_from_a_code_fence(self) -> None:
        text = f"Here you go:\n```json\n{json.dumps(VALID_PLAN)}\n```\nHope that helps."
        assert parse_plan(text).root_cause.startswith("percentage divides")

    def test_recovers_json_surrounded_by_prose(self) -> None:
        text = f"I think the plan is {json.dumps(VALID_PLAN)} and that should do it."
        assert parse_plan(text).confidence == 0.8

    def test_ignores_braces_inside_strings(self) -> None:
        payload = dict(VALID_PLAN, root_cause="the dict {a: 1} is built wrongly here")
        assert parse_plan(json.dumps(payload)).root_cause.startswith("the dict")

    def test_rejects_an_empty_response(self) -> None:
        with pytest.raises(StructuredOutputError, match="empty response"):
            parse_plan("   ")

    def test_rejects_prose_with_no_object(self) -> None:
        with pytest.raises(StructuredOutputError, match="did not contain a JSON object"):
            parse_plan("I am not going to answer in JSON, sorry.")

    def test_rejects_a_plan_missing_required_substance(self) -> None:
        with pytest.raises(StructuredOutputError) as caught:
            coerce_plan({"root_cause": "idk", "patch_strategy": "fix it"})
        assert "invalid_fields" in caught.value.context

    def test_normalises_loose_but_recoverable_shapes(self) -> None:
        plan = coerce_plan(
            {
                **VALID_PLAN,
                "files_to_change": "a.py, b.py",
                "confidence": "0.42",
                "tests_to_run": None,
                "unexpected_key": "ignored",
            }
        )
        assert plan.files_to_change == ["a.py", "b.py"]
        assert plan.confidence == 0.42
        assert plan.tests_to_run == []

    def test_clamps_an_out_of_range_confidence(self) -> None:
        assert coerce_plan({**VALID_PLAN, "confidence": 7}).confidence == 1.0
        assert coerce_plan({**VALID_PLAN, "confidence": -3}).confidence == 0.0

    def test_extract_json_object_returns_a_mapping(self) -> None:
        assert extract_json_object('{"a": 1}') == {"a": 1}
        with pytest.raises(StructuredOutputError):
            extract_json_object("[1, 2, 3]")


# --------------------------------------------------------------------------- #
# Adapters
# --------------------------------------------------------------------------- #
class TestAdapterFactory:
    def test_splits_provider_and_model(self) -> None:
        assert split_model("openai:gpt-4o-mini") == ("openai", "gpt-4o-mini")
        assert split_model("mock:deterministic") == ("mock", "deterministic")

    def test_requires_a_provider_prefix(self) -> None:
        with pytest.raises(ConfigurationError, match="provider:model"):
            split_model("gpt-4o")

    def test_builds_the_mock_adapter_without_credentials(self, settings) -> None:
        adapter = build_adapter("mock:deterministic", settings)
        assert isinstance(adapter, MockAdapter)

    def test_rejects_an_unknown_mock_variant(self, settings) -> None:
        with pytest.raises(ConfigurationError, match="unknown mock variant"):
            build_adapter("mock:magic", settings)

    def test_hosted_providers_need_a_key_and_say_which_one(self, settings) -> None:
        with pytest.raises(ConfigurationError) as openai_error:
            build_adapter("openai:gpt-4o", settings)
        assert "PATCHPILOT_OPENAI_API_KEY" in openai_error.value.remediation

        with pytest.raises(ConfigurationError) as anthropic_error:
            build_adapter("anthropic:claude-sonnet-5", settings)
        assert "PATCHPILOT_ANTHROPIC_API_KEY" in anthropic_error.value.remediation

    def test_builds_hosted_adapters_when_configured(self, settings) -> None:
        settings.openai_api_key = "sk-test"
        settings.anthropic_api_key = "sk-ant-test"
        assert isinstance(build_adapter("openai:gpt-4o", settings), OpenAICompatAdapter)
        assert isinstance(build_adapter("anthropic:claude-sonnet-5", settings), AnthropicAdapter)

    def test_local_providers_need_no_key(self, settings) -> None:
        adapter = build_adapter("ollama:llama3", settings)
        assert isinstance(adapter, OpenAICompatAdapter)
        assert "11434" in adapter.base_url

    def test_unknown_providers_are_rejected_with_the_list(self, settings) -> None:
        with pytest.raises(ConfigurationError) as caught:
            build_adapter("hal9000:latest", settings)
        assert "Supported providers" in caught.value.remediation

    def test_describe_models_marks_unconfigured_ones_with_a_reason(self, settings) -> None:
        described = {info.id: info for info in describe_models(settings)}
        assert described["mock:deterministic"].available
        assert described["mock:deterministic"].is_test_double
        assert not described["openai:gpt-4o"].available
        assert "PATCHPILOT_OPENAI_API_KEY" in described["openai:gpt-4o"].reason


class TestOpenAICompatAdapter:
    def _adapter(self) -> OpenAICompatAdapter:
        return OpenAICompatAdapter("gpt-4o-mini", base_url="https://example.test/v1", api_key="k")

    def test_parses_a_successful_response_and_reported_usage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_post(url, **kwargs):
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 8},
                },
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        response = self._adapter().complete([ChatMessage(role="user", content="hi")])
        assert response.text == "hello"
        assert response.usage.input_tokens == 120
        assert response.raw["usage_estimated"] is False

    def test_estimates_usage_when_the_server_omits_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_post(url, **kwargs):
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "hello there"}}]},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        response = self._adapter().complete([ChatMessage(role="user", content="a longer prompt")])
        assert response.usage.total_tokens > 0
        assert response.raw["usage_estimated"] is True

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, "API key"),
            (404, "model name"),
            (429, "Rate limited"),
            (500, "provider is failing"),
        ],
    )
    def test_http_errors_become_actionable_adapter_errors(
        self, monkeypatch: pytest.MonkeyPatch, status: int, expected: str
    ) -> None:
        def fake_post(url, **kwargs):
            return httpx.Response(status, text="nope", request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)
        with pytest.raises(ModelAdapterError) as caught:
            self._adapter().complete([ChatMessage(role="user", content="hi")])
        assert expected in (caught.value.remediation or "")

    def test_a_network_failure_suggests_an_offline_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_post(url, **kwargs):
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(httpx, "post", fake_post)
        with pytest.raises(ModelAdapterError, match="failed"):
            self._adapter().complete([ChatMessage(role="user", content="hi")])

    def test_garbage_json_is_reported_clearly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_post(url, **kwargs):
            return httpx.Response(
                200, json={"unexpected": True}, request=httpx.Request("POST", url)
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        with pytest.raises(ModelAdapterError, match="unparseable"):
            self._adapter().complete([ChatMessage(role="user", content="hi")])


class TestAnthropicAdapter:
    def test_lifts_system_messages_and_reads_usage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def fake_post(url, **kwargs):
            captured.update(kwargs.get("json") or {})
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": "answer"}],
                    "usage": {"input_tokens": 10, "output_tokens": 3},
                    "stop_reason": "end_turn",
                },
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        response = AnthropicAdapter("claude-sonnet-5", api_key="k").complete(
            [
                ChatMessage(role="system", content="be careful"),
                ChatMessage(role="user", content="fix it"),
            ]
        )
        assert captured["system"] == "be careful"
        assert [message["role"] for message in captured["messages"]] == ["user"]
        assert response.text == "answer"
        assert response.usage.output_tokens == 3

    @pytest.mark.parametrize(
        ("model", "sends_temperature"),
        [
            ("claude-sonnet-5", False),
            ("claude-opus-5", False),
            ("claude-opus-4-7", False),
            ("claude-fable-5-1", False),
            ("claude-sonnet-4-6", True),
            ("claude-haiku-4-5", True),
            ("claude-haiku-4-5-20251001", True),
            ("claude-sonnet-4-20250514", True),
            ("claude-3-5-sonnet-20241022", True),
        ],
    )
    def test_temperature_is_sent_only_to_models_that_accept_it(
        self, monkeypatch: pytest.MonkeyPatch, model: str, sends_temperature: bool
    ) -> None:
        """Current models reject sampling parameters with HTTP 400."""
        captured: dict[str, object] = {}

        def fake_post(url, **kwargs):
            captured.update(kwargs.get("json") or {})
            return httpx.Response(
                200,
                json={"content": [{"type": "text", "text": "ok"}], "usage": {}},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        AnthropicAdapter(model, api_key="k").complete([ChatMessage(role="user", content="hi")])
        assert ("temperature" in captured) is sends_temperature

    def test_a_refusal_is_an_error_not_an_empty_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_post(url, **kwargs):
            return httpx.Response(
                200,
                json={
                    "content": [],
                    "stop_reason": "refusal",
                    "stop_details": {"type": "refusal", "category": "cyber"},
                    "usage": {"input_tokens": 5, "output_tokens": 0},
                },
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        with pytest.raises(ModelAdapterError, match="declined") as caught:
            AnthropicAdapter("claude-sonnet-5", api_key="k").complete(
                [ChatMessage(role="user", content="hi")]
            )
        assert "cyber" in caught.value.message

    def test_http_errors_carry_a_remediation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_post(url, **kwargs):
            return httpx.Response(401, text="bad key", request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)
        with pytest.raises(ModelAdapterError) as caught:
            AnthropicAdapter("claude-sonnet-5", api_key="k").complete(
                [ChatMessage(role="user", content="hi")]
            )
        assert "API key" in (caught.value.remediation or "")

    def test_garbage_json_is_reported_clearly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_post(url, **kwargs):
            return httpx.Response(200, text="<html>", request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)
        with pytest.raises(ModelAdapterError, match="unparseable"):
            AnthropicAdapter("claude-sonnet-5", api_key="k").complete(
                [ChatMessage(role="user", content="hi")]
            )


class TestMockAdapter:
    def test_every_scripted_solution_matches_its_fixture(self, fixture_repo: Path) -> None:
        solutions = load_solutions(SOLUTIONS_DIR)
        assert solutions, "no scripted solutions were loaded"
        adapter = MockAdapter("deterministic")
        adapter.bind_workspace(fixture_repo, "sha")
        matched = adapter.find_solution()
        assert matched is not None
        assert matched.name == "calc-service-zero-denominator"

    def test_a_solution_does_not_fire_against_the_wrong_repository(
        self, pipeline_repo: Path
    ) -> None:
        adapter = MockAdapter("deterministic")
        adapter.bind_workspace(pipeline_repo, "sha")
        assert adapter.find_solution().name == "text-pipeline-normalize-punctuation"

    def test_produces_a_valid_plan_then_a_valid_diff(self, fixture_repo: Path) -> None:
        adapter = MockAdapter("deterministic")
        adapter.bind_workspace(fixture_repo, "sha")

        plan_response = adapter.complete(
            [ChatMessage(role="user", content="PatchPilot-Task: PLAN (attempt 1)")]
        )
        plan = parse_plan(plan_response.text)
        assert "zero" in plan.root_cause.lower()

        patch_response = adapter.complete(
            [ChatMessage(role="user", content="PatchPilot-Task: GENERATE_PATCH (attempt 1)")]
        )
        assert patch_response.text.startswith("diff --git")
        assert "+    if whole == 0:" in patch_response.text

    def test_reports_insufficient_context_for_an_unknown_repository(self, tmp_path: Path) -> None:
        adapter = MockAdapter("deterministic")
        adapter.bind_workspace(tmp_path, "sha")
        response = adapter.complete(
            [ChatMessage(role="user", content="PatchPilot-Task: GENERATE_PATCH (attempt 1)")]
        )
        assert response.text == "INSUFFICIENT_CONTEXT"

    def test_the_broken_variant_never_emits_a_diff(self, fixture_repo: Path) -> None:
        adapter = MockAdapter("broken")
        adapter.bind_workspace(fixture_repo, "sha")
        response = adapter.complete(
            [ChatMessage(role="user", content="PatchPilot-Task: GENERATE_PATCH (attempt 1)")]
        )
        assert "diff" not in response.text.lower()

    def test_the_unsafe_variant_targets_a_protected_path(self, fixture_repo: Path) -> None:
        adapter = MockAdapter("unsafe")
        adapter.bind_workspace(fixture_repo, "sha")
        response = adapter.complete(
            [ChatMessage(role="user", content="PatchPilot-Task: GENERATE_PATCH (attempt 1)")]
        )
        assert ".github/workflows/ci.yml" in response.text

    def test_the_flaky_variant_fails_first_and_fixes_second(self, fixture_repo: Path) -> None:
        adapter = MockAdapter("flaky")
        adapter.bind_workspace(fixture_repo, "sha")
        context = "### calc_service/operations.py :: percentage (function, lines 17-25)"
        first = adapter.complete(
            [
                ChatMessage(
                    role="user",
                    content=f"PatchPilot-Task: GENERATE_PATCH (attempt 1)\n{context}",
                )
            ]
        )
        second = adapter.complete(
            [
                ChatMessage(
                    role="user",
                    content=f"PatchPilot-Task: GENERATE_PATCH (attempt 2)\n{context}",
                )
            ]
        )
        assert "not a hunk header" in first.text
        assert second.text.startswith("diff --git")

    def test_usage_and_latency_are_reported(self, fixture_repo: Path) -> None:
        adapter = MockAdapter("deterministic")
        adapter.bind_workspace(fixture_repo, "sha")
        response = adapter.complete([ChatMessage(role="user", content="x" * 400)])
        assert response.usage.input_tokens > 0
        assert response.latency_ms >= 0
        assert response.raw["provider"] == "mock"

    def test_an_unknown_variant_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown mock variant"):
            MockAdapter("clairvoyant")


# --------------------------------------------------------------------------- #
# Command discovery
# --------------------------------------------------------------------------- #
class TestCommandDiscovery:
    def test_finds_pytest_from_pyproject(self, fixture_repo: Path) -> None:
        found = discover_commands(fixture_repo)
        assert found.test == "python -m pytest -q"
        assert "pyproject.toml" in found.provenance["test"]

    def test_finds_pytest_from_test_files_alone(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass\n", encoding="utf-8")
        found = discover_commands(tmp_path)
        assert found.test == "python -m pytest -q"
        assert "test file" in found.provenance["test"]

    def test_finds_ruff_and_mypy_configuration(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.ruff]\nline-length = 100\n\n[tool.mypy]\nstrict = true\n", encoding="utf-8"
        )
        found = discover_commands(tmp_path)
        assert found.lint == "python -m ruff check ."
        assert found.typecheck == "python -m mypy ."

    def test_does_not_invent_a_setup_command(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
        found = discover_commands(tmp_path)
        assert found.setup is None
        assert "no network" in found.provenance["setup"]

    def test_finds_nothing_in_an_empty_directory(self, tmp_path: Path) -> None:
        found = discover_commands(tmp_path)
        assert found.test is None and found.lint is None

    def test_explicit_configuration_wins(self, fixture_repo: Path) -> None:
        merged = resolve_commands(
            RunConfig(validation_command="pytest -x -q"), discover_commands(fixture_repo)
        )
        assert merged.test == "pytest -x -q"
        assert merged.provenance["test"] == "supplied in the run configuration"

    def test_targets_the_tests_the_plan_named(self) -> None:
        plan = RepairPlan(
            root_cause="a real root cause statement",
            patch_strategy="a real strategy statement",
            tests_to_run=["tests/test_operations.py::test_percentage_with_zero_whole"],
        )
        assert targeted_test_command("python -m pytest -q", plan) == (
            "python -m pytest -q tests/test_operations.py::test_percentage_with_zero_whole"
        )

    def test_refuses_to_splice_a_suspicious_node_id(self) -> None:
        plan = RepairPlan(
            root_cause="a real root cause statement",
            patch_strategy="a real strategy statement",
            tests_to_run=["; rm -rf /"],
        )
        assert targeted_test_command("python -m pytest -q", plan) == "python -m pytest -q"

    @pytest.mark.parametrize(
        "node_id",
        ["--collect-only", "-p evil_plugin", "tests/test_x.py::test_y --co", "$(id)"],
    )
    def test_refuses_node_ids_that_would_become_options(self, node_id: str) -> None:
        plan = RepairPlan(
            root_cause="a real root cause statement",
            patch_strategy="a real strategy statement",
            tests_to_run=[node_id],
        )
        assert targeted_test_command("python -m pytest -q", plan) == "python -m pytest -q"

    def test_accepts_a_parametrised_node_id(self) -> None:
        plan = RepairPlan(
            root_cause="a real root cause statement",
            patch_strategy="a real strategy statement",
            tests_to_run=["tests/test_x.py::TestY::test_z[case-1]"],
        )
        assert targeted_test_command("python -m pytest -q", plan).endswith(
            " tests/test_x.py::TestY::test_z[case-1]"
        )

    def test_leaves_a_non_pytest_command_alone(self) -> None:
        plan = RepairPlan(
            root_cause="a real root cause statement",
            patch_strategy="a real strategy statement",
            tests_to_run=["tests/test_x.py"],
        )
        assert targeted_test_command("make test", plan) == "make test"

    def test_builds_the_baseline_phase_as_a_single_allowed_failure(
        self, fixture_repo: Path
    ) -> None:
        specs = build_command_plan(discover_commands(fixture_repo), RunConfig(), phase="baseline")
        assert [spec.kind for spec in specs] == [CommandKind.BASELINE]
        assert specs[0].allow_failure is True

    def test_builds_the_validation_phase_in_the_right_order(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\ntestpaths=['tests']\n\n[tool.ruff]\n\n[tool.mypy]\n",
            encoding="utf-8",
        )
        plan = RepairPlan(
            root_cause="a real root cause statement",
            patch_strategy="a real strategy statement",
            tests_to_run=["tests/test_x.py::test_one"],
        )
        specs = build_command_plan(discover_commands(tmp_path), RunConfig(), plan=plan)
        kinds = [spec.kind for spec in specs]
        assert kinds == [
            CommandKind.TEST,
            CommandKind.LINT,
            CommandKind.TYPECHECK,
            CommandKind.VALIDATION,
        ]
        # Only the final validation command decides pass or fail.
        assert specs[-1].allow_failure is False
        assert all(spec.allow_failure for spec in specs[:-1])


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
def _context() -> ContextPackage:
    chunk = CodeChunk(
        chunk_id="c1",
        repo_sha="sha",
        path="calc_service/operations.py",
        language="python",
        symbol="percentage",
        symbol_type=SymbolType.FUNCTION,
        start_line=17,
        end_line=25,
        content="def percentage(part, whole):\n    return part / whole * 100.0",
    )
    return ContextPackage(
        query="percentage crashes",
        chunks=[
            RetrievedChunk(
                chunk=chunk,
                score=1.5,
                rank=1,
                components=[
                    ScoreComponent(reason="semantic-similarity", weight=1.0, detail="cosine=0.4")
                ],
            )
        ],
        conventions={"README.md": "run pytest"},
        repo_tree_excerpt="calc_service/operations.py",
    )


class TestPrompts:
    def test_context_rendering_names_the_symbol_and_the_reason(self) -> None:
        rendered = render_context(_context())
        assert "calc_service/operations.py :: percentage" in rendered
        assert "semantic-similarity" in rendered
        assert "Repository conventions" in rendered

    def test_plan_prompt_carries_the_task_header(self) -> None:
        messages = build_plan_messages(IssueSpec(title="t", body="b"), _context(), attempt=2)
        joined = "\n".join(message.content for message in messages)
        assert "PatchPilot-Task: PLAN (attempt 2)" in joined
        assert "single JSON object" in joined

    def test_patch_prompt_includes_the_approved_plan(self) -> None:
        plan = RepairPlan(
            root_cause="a real root cause statement",
            patch_strategy="a real strategy statement",
            files_to_change=["calc_service/operations.py"],
        )
        messages = build_patch_messages(IssueSpec(title="t", body="b"), _context(), plan, attempt=1)
        joined = "\n".join(message.content for message in messages)
        assert "PatchPilot-Task: GENERATE_PATCH (attempt 1)" in joined
        assert "a real strategy statement" in joined
        assert "unified diff and nothing else" in joined

    def test_history_tells_the_model_what_already_failed(self) -> None:
        from patchpilot_core.models import AttemptRecord, PatchProposal, PatchValidationResult

        history = [
            AttemptRecord(
                attempt=1,
                patch=PatchProposal(attempt=1, diff="--- a/x\n+++ b/x\n"),
                validation=PatchValidationResult(
                    valid=False,
                    rejections=["protected-path"],
                    messages=["ci.yml is protected"],
                ),
                analysis="rejected before execution",
            )
        ]
        messages = build_plan_messages(
            IssueSpec(title="t", body="b"), _context(), attempt=2, history=history
        )
        joined = "\n".join(message.content for message in messages)
        assert "Previous attempts" in joined
        assert "ci.yml is protected" in joined
        assert "Do not repeat a patch" in joined


class TestIngestCacheLocation:
    """The cached checkout must always land inside the configured data directory."""

    def test_a_windows_style_absolute_path_caches_inside_the_data_dir(
        self, settings, fixture_repo: Path
    ) -> None:
        from patchpilot_agent import ingest_repository
        from patchpilot_core.models import RepositorySpec

        ingested = ingest_repository(RepositorySpec(url=str(fixture_repo)), settings)

        assert ingested.path.parent == settings.repo_cache_root
        assert settings.data_dir.resolve() in ingested.path.resolve().parents
        # The source tree is never used as a cache location.
        assert fixture_repo.resolve() not in ingested.path.resolve().parents
        assert not list(fixture_repo.parent.glob(f"{fixture_repo.name}-*"))

    def test_the_checkout_is_a_copy_not_the_source(self, settings, fixture_repo: Path) -> None:
        from patchpilot_agent import ingest_repository
        from patchpilot_core.models import RepositorySpec

        ingested = ingest_repository(RepositorySpec(url=str(fixture_repo)), settings)
        assert ingested.path != fixture_repo
        assert (ingested.path / "calc_service" / "operations.py").is_file()
        assert ingested.repo_sha.startswith("sha256:")

    def test_reingesting_identical_content_reuses_the_cache(
        self, settings, fixture_repo: Path
    ) -> None:
        from patchpilot_agent import ingest_repository
        from patchpilot_core.models import RepositorySpec

        spec = RepositorySpec(url=str(fixture_repo))
        first = ingest_repository(spec, settings)
        second = ingest_repository(spec, settings)
        assert first.path == second.path
        assert first.repo_sha == second.repo_sha


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        [
            "git",
            "-c",
            "user.name=PatchPilot Tests",
            "-c",
            "user.email=tests@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )


class TestIngestLocalGitCheckout:
    """A local git checkout is pinned to HEAD only while it matches HEAD."""

    def test_a_clean_checkout_is_pinned_to_its_commit(self, settings, fixture_repo: Path) -> None:
        from patchpilot_agent import ingest_repository
        from patchpilot_core.models import RepositorySpec

        _git(fixture_repo, "init", "-q")
        _git(fixture_repo, "add", "-A")
        _git(fixture_repo, "commit", "-q", "-m", "fixture")

        ingested = ingest_repository(RepositorySpec(url=str(fixture_repo)), settings)
        assert len(ingested.repo_sha) == 40
        assert "dirty" not in ingested.repo_sha

    def test_uncommitted_edits_change_the_pin(self, settings, fixture_repo: Path) -> None:
        from patchpilot_agent import ingest_repository
        from patchpilot_core.models import RepositorySpec

        _git(fixture_repo, "init", "-q")
        _git(fixture_repo, "add", "-A")
        _git(fixture_repo, "commit", "-q", "-m", "fixture")
        spec = RepositorySpec(url=str(fixture_repo))
        clean = ingest_repository(spec, settings).repo_sha

        target = fixture_repo / "calc_service" / "operations.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n# edit one\n", encoding="utf-8")
        first_edit = ingest_repository(spec, settings).repo_sha
        target.write_text(target.read_text(encoding="utf-8") + "# edit two\n", encoding="utf-8")
        second_edit = ingest_repository(spec, settings).repo_sha

        assert first_edit.startswith(f"{clean}+dirty.")
        # Different working trees must never share a pin, or a cached index of
        # one would be reused for the other.
        assert len({clean, first_edit, second_edit}) == 3
