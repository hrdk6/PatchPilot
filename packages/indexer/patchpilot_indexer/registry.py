"""Parser registry: maps a file extension to a structural parser.

Adding JavaScript/TypeScript support means writing a class with ``parse`` and
``chunk`` (the ``CodeParser`` protocol in ``patchpilot_core.interfaces``) and
calling :func:`register_parser`. Nothing else in the system changes.
"""

from __future__ import annotations

from pathlib import Path

from patchpilot_core.interfaces import CodeParser

from .python_parser import PythonParser, TextParser

_REGISTRY: dict[str, CodeParser] = {}


def register_parser(parser: CodeParser) -> None:
    for extension in parser.extensions:
        _REGISTRY[extension.lower()] = parser


def parser_for(path: str) -> CodeParser | None:
    return _REGISTRY.get(Path(path).suffix.lower())


def registered_languages() -> list[str]:
    return sorted({parser.language for parser in _REGISTRY.values()})


def reset_registry() -> None:
    _REGISTRY.clear()
    register_default_parsers()


def register_default_parsers() -> None:
    register_parser(PythonParser())
    register_parser(TextParser())


register_default_parsers()
