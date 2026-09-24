"""Unified-diff parsing/application and the patch safety policy.

The validator is the boundary between "a model said something" and "code runs on
this machine", so these tests are adversarial on purpose.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from patchpilot_agent import PatchPolicy, PatchValidator, apply_patch
from patchpilot_core.diffutil import (
    DiffApplyError,
    DiffParseError,
    apply_file_patch,
    apply_patch_to_tree,
    extract_diff_block,
    make_unified_diff,
    parse_unified_diff,
)
from patchpilot_core.enums import PatchRejectionReason
from patchpilot_core.models import PatchProposal

BEFORE = "def add(a, b):\n    return a - b\n"
AFTER = "def add(a, b):\n    return a + b\n"


def proposal(diff: str, attempt: int = 1) -> PatchProposal:
    return PatchProposal(attempt=attempt, diff=diff, model="mock:deterministic")


class TestDiffParsing:
    def test_parses_a_simple_diff(self) -> None:
        patches = parse_unified_diff(make_unified_diff("calc.py", BEFORE, AFTER))
        assert len(patches) == 1
        assert patches[0].target_path == "calc.py"
        assert patches[0].lines_added == 1
        assert patches[0].lines_removed == 1

    def test_strips_a_and_b_prefixes(self) -> None:
        diff = "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1 +1 @@\n-x\n+y\n"
        assert parse_unified_diff(diff)[0].target_path == "pkg/mod.py"

    def test_recognises_new_and_deleted_files(self) -> None:
        created = parse_unified_diff("--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+print('hi')\n")[
            0
        ]
        deleted = parse_unified_diff(
            "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-print('bye')\n"
        )[0]
        assert created.is_new_file and created.target_path == "new.py"
        assert deleted.is_deletion and deleted.target_path == "gone.py"

    def test_handles_multiple_files_and_hunks(self) -> None:
        diff = (
            "--- a/one.py\n+++ b/one.py\n@@ -1 +1 @@\n-a\n+b\n"
            "--- a/two.py\n+++ b/two.py\n@@ -1 +1 @@\n-c\n+d\n@@ -5 +5 @@\n-e\n+f\n"
        )
        patches = parse_unified_diff(diff)
        assert [patch.target_path for patch in patches] == ["one.py", "two.py"]
        assert len(patches[1].hunks) == 2

    def test_recomputes_lying_hunk_counts(self) -> None:
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,99 +1,99 @@\n-a\n+b\n"
        hunk = parse_unified_diff(diff)[0].hunks[0]
        assert hunk.old_count == 1 and hunk.new_count == 1

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "this is just prose about the bug",
            "@@ -1 +1 @@\n-a\n+b\n",  # hunk with no file header
        ],
    )
    def test_rejects_junk(self, text: str) -> None:
        with pytest.raises(DiffParseError):
            parse_unified_diff(text)

    def test_rejects_an_empty_hunk_body(self) -> None:
        with pytest.raises(DiffParseError):
            parse_unified_diff("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n")

    def test_extracts_a_diff_from_a_chatty_response(self) -> None:
        response = (
            "Sure! Here is the fix:\n\n```diff\n"
            "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n```\n\nLet me know!"
        )
        extracted = extract_diff_block(response)
        assert extracted.startswith("--- a/x.py")
        assert parse_unified_diff(extracted)[0].target_path == "x.py"

    def test_extracts_an_unfenced_diff_after_prose(self) -> None:
        response = "I think this is it:\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
        assert extract_diff_block(response).startswith("--- a/x.py")

    def test_no_diff_in_prose_returns_empty(self) -> None:
        assert extract_diff_block("I could not work out the fix.") == ""


class TestDiffApplication:
    def test_applies_a_clean_patch(self) -> None:
        patch = parse_unified_diff(make_unified_diff("calc.py", BEFORE, AFTER))[0]
        assert apply_file_patch(BEFORE, patch) == AFTER

    def test_tolerates_line_number_drift(self) -> None:
        """Real files shift; a hunk whose context matches nearby should still apply."""
        patch = parse_unified_diff(make_unified_diff("calc.py", BEFORE, AFTER))[0]
        shifted = "# a new header comment\n# and another\n" + BEFORE
        result = apply_file_patch(shifted, patch)
        assert result is not None and "return a + b" in result
        assert result.startswith("# a new header comment")

    def test_refuses_when_the_context_does_not_match(self) -> None:
        patch = parse_unified_diff(make_unified_diff("calc.py", BEFORE, AFTER))[0]
        with pytest.raises(DiffApplyError, match="does not match"):
            apply_file_patch("def multiply(a, b):\n    return a * b\n", patch)

    def test_preserves_a_missing_trailing_newline(self) -> None:
        patch = parse_unified_diff(
            "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n\\ No newline at end of file\n"
        )[0]
        assert apply_file_patch("a\n", patch) == "b"

    def test_creating_over_an_existing_file_is_refused(self) -> None:
        patch = parse_unified_diff("--- /dev/null\n+++ b/x.py\n@@ -0,0 +1 @@\n+new\n")[0]
        with pytest.raises(DiffApplyError, match="already exists"):
            apply_file_patch("existing\n", patch)

    def test_applies_to_a_directory_and_reports_what_changed(self, tmp_path: Path) -> None:
        (tmp_path / "calc.py").write_text(BEFORE, encoding="utf-8")
        report = apply_patch_to_tree(tmp_path, make_unified_diff("calc.py", BEFORE, AFTER))
        assert report.applied == ["calc.py"]
        assert (tmp_path / "calc.py").read_text(encoding="utf-8") == AFTER

    def test_two_sections_for_one_file_compose(self, tmp_path: Path) -> None:
        """A second section for the same file must build on the first, not replace it."""
        source = "HEADER = 1\n\n\ndef f():\n    return 0\n"
        target = tmp_path / "m.py"
        target.write_text(source, encoding="utf-8")
        diff = (
            "--- a/m.py\n+++ b/m.py\n@@ -1 +1,2 @@\n HEADER = 1\n+EXTRA = 2\n"
            "--- a/m.py\n+++ b/m.py\n@@ -4,2 +4,2 @@\n def f():\n-    return 0\n+    return 1\n"
        )
        apply_patch_to_tree(tmp_path, diff)
        result = target.read_text(encoding="utf-8")
        assert "EXTRA = 2" in result
        assert "return 1" in result

    def test_failure_leaves_the_tree_untouched(self, tmp_path: Path) -> None:
        (tmp_path / "ok.py").write_text("a\n", encoding="utf-8")
        (tmp_path / "bad.py").write_text("totally different\n", encoding="utf-8")
        diff = (
            "--- a/ok.py\n+++ b/ok.py\n@@ -1 +1 @@\n-a\n+b\n"
            "--- a/bad.py\n+++ b/bad.py\n@@ -1 +1 @@\n-expected\n+replacement\n"
        )
        with pytest.raises(DiffApplyError):
            apply_patch_to_tree(tmp_path, diff)
        assert (tmp_path / "ok.py").read_text(encoding="utf-8") == "a\n"

    @pytest.mark.parametrize(
        "path",
        ["../outside.py", "/etc/passwd", "C:/Windows/system32/x.py", "a/../../escape.py"],
    )
    def test_refuses_to_write_outside_the_workspace(self, tmp_path: Path, path: str) -> None:
        diff = f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+pwned\n"
        with pytest.raises(DiffApplyError):
            apply_patch_to_tree(tmp_path, diff)

    def test_deletes_a_file(self, tmp_path: Path) -> None:
        (tmp_path / "gone.py").write_text("x\n", encoding="utf-8")
        report = apply_patch_to_tree(tmp_path, "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n")
        assert report.deleted == ["gone.py"]
        assert not (tmp_path / "gone.py").exists()


class TestPatchValidator:
    @pytest.fixture
    def validator(self, fixture_repo: Path) -> PatchValidator:
        return PatchValidator(fixture_repo, PatchPolicy(max_files=3, max_lines=50))

    def _diff_for(self, repo: Path, relative: str, replacement: tuple[str, str]) -> str:
        before = (repo / relative).read_text(encoding="utf-8")
        after = before.replace(*replacement, 1)
        assert after != before, "test setup did not change anything"
        return make_unified_diff(relative, before, after)

    def test_accepts_a_real_fix(self, validator: PatchValidator, fixture_repo: Path) -> None:
        diff = self._diff_for(
            fixture_repo,
            "calc_service/operations.py",
            (
                "    return part / whole * 100.0",
                "    if whole == 0:\n        return 0.0\n    return part / whole * 100.0",
            ),
        )
        result = validator.validate(proposal(diff))
        assert result.valid, result.messages
        assert result.files_changed == 1
        assert result.lines_added >= 2

    def test_rejects_an_empty_response(self, validator: PatchValidator) -> None:
        result = validator.validate(proposal("INSUFFICIENT_CONTEXT"))
        assert not result.valid
        assert PatchRejectionReason.EMPTY in result.rejections

    def test_rejects_a_malformed_diff(self, validator: PatchValidator) -> None:
        result = validator.validate(proposal("--- a/x.py\n+++ b/x.py\n@@ nonsense @@\n+fix\n"))
        assert not result.valid
        assert PatchRejectionReason.MALFORMED in result.rejections

    @pytest.mark.parametrize(
        "path",
        [
            ".github/workflows/ci.yml",
            "poetry.lock",
            "package-lock.json",
            "Dockerfile",
            "docker-compose.yml",
            ".pre-commit-config.yaml",
            ".env",
            "deploy.pem",
        ],
    )
    def test_rejects_protected_paths(self, validator: PatchValidator, path: str) -> None:
        diff = f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new\n"
        result = validator.validate(proposal(diff))
        assert not result.valid
        assert PatchRejectionReason.PROTECTED_PATH in result.rejections

    def test_rejects_path_traversal(self, validator: PatchValidator) -> None:
        diff = "--- a/../../etc/passwd\n+++ b/../../etc/passwd\n@@ -1 +1 @@\n-a\n+b\n"
        result = validator.validate(proposal(diff))
        assert not result.valid
        assert PatchRejectionReason.PATH_TRAVERSAL in result.rejections

    def test_rejects_absolute_paths(self, validator: PatchValidator) -> None:
        diff = "--- a//etc/shadow\n+++ b//etc/shadow\n@@ -1 +1 @@\n-a\n+b\n"
        result = validator.validate(proposal(diff))
        assert not result.valid
        assert PatchRejectionReason.PATH_TRAVERSAL in result.rejections

    def test_rejects_hidden_files_by_default(self, validator: PatchValidator) -> None:
        diff = "--- a/.hidden.py\n+++ b/.hidden.py\n@@ -1 +1 @@\n-a\n+b\n"
        result = validator.validate(proposal(diff))
        assert not result.valid
        assert PatchRejectionReason.HIDDEN_FILE in result.rejections

    def test_an_explicit_allowance_overrides_the_policy(self, fixture_repo: Path) -> None:
        validator = PatchValidator(
            fixture_repo, PatchPolicy(allowed_globs=(".github/workflows/*.yml",))
        )
        diff = (
            "--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n"
            "@@ -1 +1 @@\n-old\n+new\n"
        )
        result = validator.validate(proposal(diff))
        assert PatchRejectionReason.PROTECTED_PATH not in result.rejections

    def test_rejects_binary_patches(self, validator: PatchValidator) -> None:
        diff = "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n"
        result = validator.validate(proposal(diff))
        assert not result.valid
        assert PatchRejectionReason.BINARY_PATCH in result.rejections

    def test_enforces_the_file_count_limit(self, fixture_repo: Path) -> None:
        validator = PatchValidator(fixture_repo, PatchPolicy(max_files=1))
        diff = (
            "--- a/one.py\n+++ b/one.py\n@@ -1 +1 @@\n-a\n+b\n"
            "--- a/two.py\n+++ b/two.py\n@@ -1 +1 @@\n-c\n+d\n"
        )
        result = validator.validate(proposal(diff))
        assert PatchRejectionReason.TOO_MANY_FILES in result.rejections

    def test_enforces_the_line_count_limit(self, fixture_repo: Path) -> None:
        validator = PatchValidator(fixture_repo, PatchPolicy(max_lines=2))
        body = "".join(f"+line{index}\n" for index in range(20))
        diff = f"--- a/x.py\n+++ b/x.py\n@@ -0,0 +1,20 @@\n{body}"
        result = validator.validate(proposal(diff))
        assert PatchRejectionReason.TOO_MANY_LINES in result.rejections

    def test_rejects_a_patch_that_does_not_apply(self, validator: PatchValidator) -> None:
        diff = (
            "--- a/calc_service/operations.py\n+++ b/calc_service/operations.py\n"
            "@@ -1,2 +1,2 @@\n-this line is not in the file\n+replacement\n"
        )
        result = validator.validate(proposal(diff))
        assert not result.valid
        assert PatchRejectionReason.APPLY_FAILED in result.rejections

    def test_detects_a_repeated_patch(self, validator: PatchValidator, fixture_repo: Path) -> None:
        diff = self._diff_for(
            fixture_repo,
            "calc_service/operations.py",
            (
                "    return part / whole * 100.0",
                "    return 0.0 if whole == 0 else part / whole * 100.0",
            ),
        )
        first = proposal(diff, attempt=1)
        second = proposal(diff, attempt=2)
        assert validator.validate(first).valid
        result = validator.validate(second, previous_hashes={first.normalized_hash()})
        assert PatchRejectionReason.DUPLICATE in result.rejections

    def test_whitespace_only_differences_hash_the_same(self) -> None:
        one = proposal("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a = 1\n+a  =  2\n")
        two = proposal("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a  =  1\n+a = 2\n")
        assert one.normalized_hash() == two.normalized_hash()

    def test_apply_patch_writes_to_a_workspace(self, fixture_repo: Path) -> None:
        before = (fixture_repo / "calc_service/operations.py").read_text(encoding="utf-8")
        after = before.replace(
            "    return part / whole * 100.0",
            "    if whole == 0:\n        return 0.0\n    return part / whole * 100.0",
            1,
        )
        diff = make_unified_diff("calc_service/operations.py", before, after)
        touched = apply_patch(fixture_repo, proposal(diff))
        assert touched == ["calc_service/operations.py"]
        assert "if whole == 0" in (fixture_repo / "calc_service/operations.py").read_text(
            encoding="utf-8"
        )
