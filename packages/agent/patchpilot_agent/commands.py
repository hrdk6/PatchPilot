"""Discovering how a repository wants to be built, linted and tested.

Explicit configuration always wins: whatever the user typed into the run form is
used verbatim. Discovery only fills the gaps, and it records *why* it chose each
command so the run detail page can show "pytest, because pyproject.toml declares
[tool.pytest.ini_options]" rather than an unexplained shell string.
"""

from __future__ import annotations

import configparser
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from patchpilot_core.enums import CommandKind
from patchpilot_core.models import CommandSpec, RepairPlan, RunConfig

# A pytest node id the model asked for, e.g. ``tests/test_x.py::TestY::test_z[a-1]``.
# It is spliced into a shell command, so no shell metacharacters, no whitespace
# (which would split it into extra arguments) and no leading "-" (which would
# turn it into a pytest option such as ``--collect-only``).
PYTEST_NODE_RE = re.compile(r"^(?!-)[\w./-]+(::[\w\[\].-]+)*$")


@dataclass(slots=True)
class DiscoveredCommands:
    test: str | None = None
    lint: str | None = None
    typecheck: str | None = None
    setup: str | None = None
    provenance: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "test": self.test,
            "lint": self.lint,
            "typecheck": self.typecheck,
            "setup": self.setup,
            "provenance": self.provenance,
        }


def _read_pyproject(root: Path) -> dict:
    file = root / "pyproject.toml"
    if not file.is_file():
        return {}
    try:
        return tomllib.loads(file.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return {}


def _read_setup_cfg(root: Path) -> configparser.ConfigParser | None:
    file = root / "setup.cfg"
    if not file.is_file():
        return None
    parser = configparser.ConfigParser()
    try:
        parser.read(file, encoding="utf-8")
    except (configparser.Error, OSError):
        return None
    return parser


def discover_commands(root: Path) -> DiscoveredCommands:
    """Inspect a checkout and propose commands, with a reason for each."""
    root = Path(root)
    found = DiscoveredCommands()
    pyproject = _read_pyproject(root)
    tools = pyproject.get("tool", {}) if isinstance(pyproject, dict) else {}
    setup_cfg = _read_setup_cfg(root)

    # ------------------------------------------------------------------ tests
    if "pytest" in tools:
        found.test = "python -m pytest -q"
        found.provenance["test"] = "pyproject.toml declares [tool.pytest.ini_options]"
    elif (root / "pytest.ini").is_file():
        found.test = "python -m pytest -q"
        found.provenance["test"] = "pytest.ini is present"
    elif (root / "tox.ini").is_file() and "pytest" in (root / "tox.ini").read_text(
        encoding="utf-8", errors="replace"
    ):
        found.test = "python -m pytest -q"
        found.provenance["test"] = "tox.ini references pytest"
    elif setup_cfg is not None and setup_cfg.has_section("tool:pytest"):
        found.test = "python -m pytest -q"
        found.provenance["test"] = "setup.cfg declares [tool:pytest]"
    else:
        test_files = _find_test_files(root)
        if test_files:
            found.test = "python -m pytest -q"
            found.provenance["test"] = (
                f"{len(test_files)} pytest-style test file(s) found, e.g. {test_files[0]}"
            )
        elif (root / "manage.py").is_file():
            found.test = "python manage.py test"
            found.provenance["test"] = "manage.py suggests a Django project"

    # ------------------------------------------------------------------- lint
    if "ruff" in tools or (root / "ruff.toml").is_file() or (root / ".ruff.toml").is_file():
        found.lint = "python -m ruff check ."
        found.provenance["lint"] = "a Ruff configuration is present"
    elif (root / ".flake8").is_file() or (
        setup_cfg is not None and setup_cfg.has_section("flake8")
    ):
        found.lint = "python -m flake8"
        found.provenance["lint"] = "a flake8 configuration is present"

    # -------------------------------------------------------------- typecheck
    if "mypy" in tools or (root / "mypy.ini").is_file():
        found.typecheck = "python -m mypy ."
        found.provenance["typecheck"] = "a mypy configuration is present"
    elif "pyright" in tools or (root / "pyrightconfig.json").is_file():
        found.typecheck = "python -m pyright"
        found.provenance["typecheck"] = "a pyright configuration is present"

    # ------------------------------------------------------------------ setup
    # Deliberately left unset by default: installing dependencies needs network
    # access, which the sandbox denies unless the run explicitly enables it.
    if (root / "requirements.txt").is_file():
        found.provenance["setup"] = (
            "requirements.txt found. Dependency installation is NOT run by default "
            "because the sandbox has no network; set a setup command and "
            "network=bridge if the repository needs it."
        )

    return found


def _find_test_files(root: Path, limit: int = 5) -> list[str]:
    hits: list[str] = []
    for pattern in ("test_*.py", "*_test.py"):
        for path in root.rglob(pattern):
            relative = path.relative_to(root).as_posix()
            if any(
                part in {".venv", "venv", "node_modules", ".git"} for part in relative.split("/")
            ):
                continue
            hits.append(relative)
            if len(hits) >= limit:
                return hits
    return hits


def resolve_commands(config: RunConfig, discovered: DiscoveredCommands) -> DiscoveredCommands:
    """Explicit run configuration overrides discovery."""
    merged = DiscoveredCommands(
        test=config.validation_command or discovered.test,
        lint=config.lint_command or discovered.lint,
        typecheck=config.typecheck_command or discovered.typecheck,
        setup=config.setup_command or discovered.setup,
        provenance=dict(discovered.provenance),
    )
    for key, value in (
        ("test", config.validation_command),
        ("lint", config.lint_command),
        ("typecheck", config.typecheck_command),
        ("setup", config.setup_command),
    ):
        if value:
            merged.provenance[key] = "supplied in the run configuration"
    return merged


def targeted_test_command(base_command: str | None, plan: RepairPlan | None) -> str | None:
    """Narrow the test command to the node ids the plan named, when that is safe."""
    if not base_command or plan is None or not plan.tests_to_run:
        return base_command
    if "pytest" not in base_command:
        return base_command
    selected = [item for item in plan.tests_to_run if PYTEST_NODE_RE.match(item.strip())]
    if not selected:
        return base_command
    return f"{base_command} " + " ".join(selected[:8])


def build_command_plan(
    commands: DiscoveredCommands,
    config: RunConfig,
    *,
    plan: RepairPlan | None = None,
    include_setup: bool = True,
    phase: str = "validation",
) -> list[CommandSpec]:
    """Assemble the ordered command list for one sandbox execution.

    ``phase="baseline"`` runs the suite on the unpatched tree to confirm the bug
    reproduces. ``phase="validation"`` runs the targeted tests first (fast signal),
    then lint, then type checking, then the full suite.
    """
    specs: list[CommandSpec] = []
    if include_setup and commands.setup:
        specs.append(
            CommandSpec(
                kind=CommandKind.SETUP,
                command=commands.setup,
                timeout_seconds=config.limits.timeout_seconds * 2,
            )
        )

    if phase == "baseline":
        baseline = config.baseline_command or commands.test
        if baseline:
            specs.append(
                CommandSpec(
                    kind=CommandKind.BASELINE,
                    command=baseline,
                    timeout_seconds=config.limits.timeout_seconds,
                    allow_failure=True,
                )
            )
        return specs

    targeted = targeted_test_command(commands.test, plan)
    if targeted and commands.test and targeted != commands.test:
        specs.append(
            CommandSpec(
                kind=CommandKind.TEST,
                command=targeted,
                timeout_seconds=config.limits.timeout_seconds,
                allow_failure=True,
            )
        )
    if commands.lint:
        specs.append(
            CommandSpec(
                kind=CommandKind.LINT,
                command=commands.lint,
                timeout_seconds=config.limits.timeout_seconds,
                allow_failure=True,
            )
        )
    if commands.typecheck:
        specs.append(
            CommandSpec(
                kind=CommandKind.TYPECHECK,
                command=commands.typecheck,
                timeout_seconds=config.limits.timeout_seconds,
                allow_failure=True,
            )
        )
    if commands.test:
        specs.append(
            CommandSpec(
                kind=CommandKind.VALIDATION,
                command=commands.test,
                timeout_seconds=config.limits.timeout_seconds,
            )
        )
    return specs
