"""Core utilities: cost estimation, text hygiene, identifiers, logging, models."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from patchpilot_core.config import Settings
from patchpilot_core.costs import estimate_cost, lookup_pricing, pricing_table
from patchpilot_core.enums import RunStatus, SandboxStatus
from patchpilot_core.ids import new_id, run_id
from patchpilot_core.logging import HumanFormatter, JsonFormatter, configure_logging, log_context
from patchpilot_core.models import (
    CommandResult,
    IssueSpec,
    PatchProposal,
    RepairPlan,
    RepositorySpec,
    SandboxExecution,
    TokenUsage,
)
from patchpilot_core.textutil import (
    estimate_tokens,
    extract_paths,
    redact_secrets,
    sanitize_output,
    strip_ansi,
    summarize_failure,
    truncate_middle,
)


class TestCosts:
    def test_estimates_from_the_pricing_table(self) -> None:
        estimate = estimate_cost(
            "openai:gpt-4o-mini", TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
        )
        assert estimate.input_usd == pytest.approx(0.15)
        assert estimate.output_usd == pytest.approx(0.60)
        assert estimate.total_usd == pytest.approx(0.75)
        assert estimate.pricing_known

    def test_never_invents_a_price(self) -> None:
        estimate = estimate_cost("mystery:model", TokenUsage(input_tokens=1000))
        assert estimate.pricing_known is False
        assert estimate.total_usd == 0.0
        assert "not estimated" in estimate.note
        # Token counts are still reported exactly.
        assert estimate.input_tokens == 1000

    def test_self_hosted_models_cost_nothing_per_token(self) -> None:
        estimate = estimate_cost("ollama:llama3", TokenUsage(input_tokens=10_000))
        assert estimate.pricing_known
        assert estimate.total_usd == 0.0

    def test_mock_models_are_free(self) -> None:
        assert estimate_cost("mock:deterministic", TokenUsage(input_tokens=99_999)).total_usd == 0.0

    def test_pricing_can_be_overridden_without_touching_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        override = tmp_path / "pricing.json"
        override.write_text(
            json.dumps({"openai:gpt-4o-mini": {"input_per_mtok": 99.0, "output_per_mtok": 1.0}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("PATCHPILOT_PRICING_FILE", str(override))
        pricing = lookup_pricing("openai:gpt-4o-mini")
        assert pricing is not None and pricing.input_per_mtok == 99.0
        assert "override" in pricing.source

    def test_a_missing_override_file_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATCHPILOT_PRICING_FILE", "/nope/does-not-exist.json")
        assert "openai:gpt-4o" in pricing_table()

    def test_the_note_warns_that_the_number_is_an_estimate(self) -> None:
        assert "verify" in estimate_cost("openai:gpt-4o", TokenUsage()).note.lower()


class TestTextUtilities:
    def test_strips_ansi_control_sequences(self) -> None:
        assert strip_ansi("\x1b[31mFAILED\x1b[0m tests/test_x.py") == "FAILED tests/test_x.py"

    @pytest.mark.parametrize(
        "secret",
        [
            "sk-abcdefghijklmnopqrstuvwxyz1234",
            "sk-ant-abcdefghijklmnopqrstuvwxyz",
            "ghp_abcdefghijklmnopqrstuvwxyz1234",
            "github_pat_abcdefghijklmnopqrstuvwx",
            "AKIAIOSFODNN7EXAMPLE",
        ],
    )
    def test_redacts_credential_shaped_strings(self, secret: str) -> None:
        redacted = redact_secrets(f"error: using {secret} failed")
        assert secret not in redacted
        assert "redacted" in redacted

    def test_redacts_assignments_and_bearer_headers(self) -> None:
        assert "hunter2" not in redact_secrets("DB_PASSWORD=hunter2")
        assert "abc.def" not in redact_secrets("Authorization: Bearer abc.def")

    def test_redacts_private_key_blocks(self) -> None:
        # Assembled at runtime so the literal never trips the repository's own
        # detect-private-key pre-commit hook; the redactor sees the whole block.
        marker = "RSA PRIVATE" + " KEY"
        key = f"-----BEGIN {marker}-----\nMIIEow...secret...\n-----END {marker}-----"
        assert "secret" not in redact_secrets(key)

    def test_truncation_keeps_the_head_and_the_tail(self) -> None:
        text = "HEAD" + ("x" * 5_000) + "TAIL"
        truncated, dropped = truncate_middle(text, 200)
        assert dropped > 0
        assert truncated.startswith("HEAD")
        assert truncated.endswith("TAIL")
        assert "omitted" in truncated

    def test_short_text_is_not_truncated(self) -> None:
        assert truncate_middle("short", 100) == ("short", 0)

    def test_sanitize_masks_host_paths(self) -> None:
        cleaned, _ = sanitize_output(
            r"error in C:\Users\me\workspace\x.py",
            10_000,
            (r"C:\Users\me\workspace", "<workspace>"),
        )
        assert "<workspace>" in cleaned
        assert "C:\\Users\\me" not in cleaned

    def test_failure_summarisation_keeps_the_signal(self) -> None:
        noisy = "\n".join(
            ["collecting ...", "." * 70, "progress noise"] * 20
            + [
                "E       ZeroDivisionError: division by zero",
                "tests/test_operations.py:22: ZeroDivisionError",
                "=== short test summary info ===",
                "FAILED tests/test_operations.py::test_percentage_with_zero_whole",
            ]
        )
        summary = summarize_failure(noisy)
        assert "ZeroDivisionError" in summary
        assert "FAILED tests/test_operations.py" in summary
        assert len(summary) < len(noisy)

    def test_failure_summarisation_falls_back_to_the_tail(self) -> None:
        assert summarize_failure("nothing interesting here\n" * 5)
        assert summarize_failure("") == ""

    def test_token_estimation_is_proportional(self) -> None:
        assert estimate_tokens("") == 0
        assert estimate_tokens("a" * 400) == 100

    def test_extracts_known_paths_from_free_text(self) -> None:
        known = ["calc_service/operations.py", "tests/test_operations.py"]
        found = extract_paths("the bug is in operations.py somewhere", known)
        assert "calc_service/operations.py" in found


class TestIdentifiers:
    def test_ids_are_prefixed_and_unique(self) -> None:
        identifiers = {run_id() for _ in range(500)}
        assert len(identifiers) == 500
        assert all(identifier.startswith("run_") for identifier in identifiers)

    def test_ids_sort_chronologically(self) -> None:
        """Ordering is guaranteed by the 10-character time prefix, not the suffix.

        Two ids minted in the same millisecond share a prefix and their relative
        order is arbitrary by design, so only the time component is compared.
        """
        prefix = slice(2, 12)
        first = new_id("x")
        time.sleep(0.005)
        second = new_id("x")
        assert first[prefix] <= second[prefix]
        assert len(first[prefix]) == 10


class TestLogging:
    def test_json_formatter_emits_one_object_per_line(self) -> None:
        record = logging.LogRecord("test", logging.INFO, __file__, 10, "hello", None, None)
        record.run_id = "run_1"
        payload = json.loads(JsonFormatter().format(record))
        assert payload["message"] == "hello"
        assert payload["level"] == "INFO"
        assert payload["run_id"] == "run_1"
        assert payload["ts"].endswith("Z")

    def test_human_formatter_shows_the_records_own_fields(self) -> None:
        record = logging.LogRecord(
            "patchpilot_evals.runner", logging.WARNING, __file__, 1, "skipping model", None, None
        )
        record.model = "openai:gpt-4o"
        record.component = "evals"
        line = HumanFormatter().format(record)
        assert "skipping model" in line
        assert "model=openai:gpt-4o" in line
        assert "component=" not in line

    def test_ambient_context_is_attached(self) -> None:
        record = logging.LogRecord("test", logging.INFO, __file__, 10, "x", None, None)
        with log_context(run_id="run_ctx", correlation_id="abc"):
            payload = json.loads(JsonFormatter().format(record))
        assert payload["run_id"] == "run_ctx"
        assert payload["correlation_id"] == "abc"

    def test_context_is_unbound_afterwards(self) -> None:
        with log_context(run_id="temp"):
            pass
        record = logging.LogRecord("test", logging.INFO, __file__, 10, "x", None, None)
        assert "run_id" not in json.loads(JsonFormatter().format(record))

    def test_configure_logging_installs_a_single_handler(self) -> None:
        configure_logging("INFO", json_output=True)
        root = logging.getLogger()
        assert len(root.handlers) == 1
        configure_logging("CRITICAL", json_output=False)


class TestDomainModels:
    def test_repository_slug_is_derived_from_the_url(self) -> None:
        assert RepositorySpec(url="https://github.com/owner/repo.git").slug == "repo"
        assert RepositorySpec(url="fixtures/repos/calc_service").slug == "calc_service"
        assert RepositorySpec(url="x", name="explicit").slug == "explicit"

    def test_unsupported_schemes_are_rejected_with_the_alternatives(self) -> None:
        with pytest.raises(ValueError, match="unsupported repository scheme"):
            RepositorySpec(url="ftp://example.com/repo")

    def test_issue_text_and_fingerprint(self) -> None:
        issue = IssueSpec(title="Title", body="Body")
        assert issue.text == "Title\n\nBody"
        assert issue.fingerprint() == IssueSpec(title="Title", body="Body").fingerprint()
        assert issue.fingerprint() != IssueSpec(title="Other", body="Body").fingerprint()

    def test_a_plan_must_contain_actual_explanation(self) -> None:
        with pytest.raises(ValueError, match="at least 10 characters"):
            RepairPlan(root_cause="idk", patch_strategy="a real strategy statement")

    def test_terminal_status_classification(self) -> None:
        assert not RunStatus.QUEUED.is_terminal
        assert not RunStatus.RUNNING.is_terminal
        for status in (
            RunStatus.FIXED,
            RunStatus.TESTS_FAILED,
            RunStatus.PATCH_INVALID,
            RunStatus.SANDBOX_FAILED,
            RunStatus.BUDGET_EXHAUSTED,
            RunStatus.CANCELLED,
            RunStatus.ERROR,
        ):
            assert status.is_terminal

    def test_token_usage_adds(self) -> None:
        total = TokenUsage(input_tokens=1, output_tokens=2) + TokenUsage(
            input_tokens=10, output_tokens=20
        )
        assert (total.input_tokens, total.output_tokens, total.total_tokens) == (11, 22, 33)

    def test_execution_reports_the_first_failing_command(self) -> None:
        execution = SandboxExecution(
            backend="local",
            results=[
                CommandResult(
                    kind="lint",
                    command="ruff check .",
                    exit_code=0,
                    status=SandboxStatus.COMPLETED,
                ),
                CommandResult(
                    kind="validation",
                    command="pytest -q",
                    exit_code=1,
                    status=SandboxStatus.COMPLETED,
                    stdout="1 failed",
                ),
            ],
        )
        assert execution.validation_passed is False
        assert "pytest -q" in execution.failure_excerpt()
        assert "1 failed" in execution.failure_excerpt()

    def test_a_passing_validation_is_recognised(self) -> None:
        execution = SandboxExecution(
            backend="local",
            results=[
                CommandResult(
                    kind="validation",
                    command="pytest -q",
                    exit_code=0,
                    status=SandboxStatus.COMPLETED,
                )
            ],
        )
        assert execution.validation_passed
        assert execution.failure_excerpt() == ""

    def test_patch_hash_ignores_context_and_headers(self) -> None:
        one = PatchProposal(
            attempt=1,
            diff="--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n context\n-old\n+new\n",
        )
        two = PatchProposal(
            attempt=2,
            diff="--- a/x.py\n+++ b/x.py\n@@ -9,3 +9,3 @@\n different context\n-old\n+new\n",
        )
        assert one.normalized_hash() == two.normalized_hash()

    def test_models_reject_unknown_fields(self) -> None:
        with pytest.raises(ValueError):
            IssueSpec(title="t", body="b", not_a_field=1)  # type: ignore[call-arg]


class TestRepositorySlug:
    """Regression tests for a bug that wrote cached checkouts outside the data dir.

    ``slug`` feeds a path join. Splitting a Windows path on ``/`` alone returned
    the entire path as the "name", which then behaved as an absolute path and
    made ``Path(cache_root) / name`` discard the cache root entirely -- so
    checkouts landed next to the source repository instead of under the
    configured data directory.
    """

    def test_handles_both_path_separators(self) -> None:
        windows = RepositorySpec(url=r"C:\Users\me\projects\calc_service")
        posix = RepositorySpec(url="/home/me/projects/calc_service")
        assert windows.slug == "calc_service"
        assert posix.slug == "calc_service"

    def test_handles_urls_and_trailing_separators(self) -> None:
        assert RepositorySpec(url="https://github.com/owner/repo.git").slug == "repo"
        assert RepositorySpec(url="https://github.com/owner/repo/").slug == "repo"
        assert RepositorySpec(url=r"C:\repos\thing\\").slug == "thing"

    def test_the_slug_is_always_a_single_safe_path_component(self) -> None:
        for url in (
            r"C:\Users\me\projects\calc_service",
            "https://github.com/owner/repo.git",
            "file:///tmp/weird name/../repo",
            "git@github.com:owner/repo.git",
        ):
            slug = RepositorySpec(url=url).slug
            assert "/" not in slug and "\\" not in slug, slug
            assert ".." not in slug, slug
            assert not Path(slug).is_absolute(), slug
            assert slug

    def test_an_explicit_name_is_still_sanitised(self) -> None:
        assert RepositorySpec(url="x", name="../../escape").slug == "escape"


class TestSettings:
    def test_cors_origins_accept_the_documented_comma_separated_form(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "PATCHPILOT_CORS_ORIGINS", "http://localhost:5173, http://127.0.0.1:5173"
        )
        assert Settings().cors_origins == ["http://localhost:5173", "http://127.0.0.1:5173"]

    def test_cors_origins_accept_a_json_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATCHPILOT_CORS_ORIGINS", '["https://dashboard.example"]')
        assert Settings().cors_origins == ["https://dashboard.example"]

    def test_the_example_env_file_loads(self) -> None:
        """Copying .env.example to .env is the documented first step; it must not crash."""
        settings = Settings(_env_file=REPO_ROOT / ".env.example")  # type: ignore[call-arg]
        assert "http://127.0.0.1:5173" in settings.cors_origins
