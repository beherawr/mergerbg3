"""Shared test helpers.

Tests work against the real example projects symlinked under
``tests/fixtures/``. The ``all_stats_txt`` etc. helpers walk every project
on the PROJECTS list so adding fixtures is a one-line change.
"""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
PROJECTS = [
    FIXTURES / "ShadowDance",
    FIXTURES / "Shadowdancer",
    FIXTURES / "Bloodfang",
    FIXTURES / "LampOfLuxury",
    FIXTURES / "Treehome",
]


def all_stats_txt() -> list[Path]:
    """Every `.txt` under `Public/.../Stats/Generated/Data/` in any fixture project."""
    results: list[Path] = []
    for project in PROJECTS:
        results.extend(project.glob("Public/*/Stats/Generated/Data/*.txt"))
    return sorted(results)


def all_stats_xml() -> list[Path]:
    """Every `.stats` file under `Editor/Mods/.../Stats/` in any fixture project."""
    results: list[Path] = []
    for project in PROJECTS:
        results.extend(project.glob("Editor/Mods/*/Stats/**/*.stats"))
    return sorted(results)


def all_lsx() -> list[Path]:
    """Every readable `.lsx` file in any fixture project (toolkit & mod meta)."""
    results: list[Path] = []
    for project in PROJECTS:
        results.extend(project.glob("**/*.lsx"))
    return sorted(results)
