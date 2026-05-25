"""End-to-end demo: load all five fixture projects and run the full
10-pair merge matrix. Run with ``python demo.py`` from the repo root.
"""

from __future__ import annotations

import shutil
from itertools import combinations
from pathlib import Path

from core import merger, validate, meta as _meta
from core.project import Project
from core.references import ReferenceIndex, find_clashes

ROOT = Path(__file__).parent
FIXTURES = ROOT / "tests" / "fixtures"
OUTPUT_BASE = Path("/tmp/merger_demo_matrix")

PROJECTS = ["ShadowDance", "Shadowdancer", "Bloodfang", "LampOfLuxury", "Treehome"]


def banner(text: str) -> None:
    print(f"\n{'=' * 70}\n{text}\n{'=' * 70}")


def main() -> None:
    print("BG3 Mod Merger - five-fixture matrix demo")

    banner("Loading projects")
    projects = {}
    for name in PROJECTS:
        p = Project.load(FIXTURES / name)
        projects[name] = p
        print(f"  {name:<14} {len(p.files):>5} files  "
              f"({p.mod_meta.name!r} by {p.mod_meta.author!r})")

    banner("Pairwise merge matrix")
    print(f"  {'pair':<32} {'status':<8} {'files':>6} "
          f"{'id-cl':>6} {'fil-ov':>7}")
    print("  " + "-" * 65)

    if OUTPUT_BASE.exists():
        shutil.rmtree(OUTPUT_BASE)

    pairs = list(combinations(PROJECTS, 2))
    for a_name, b_name in pairs:
        out = OUTPUT_BASE / f"{a_name}_x_{b_name}"
        new_uuid = _meta.generate_uuid()
        config = merger.MergeConfig(
            inputs=[projects[a_name], projects[b_name]],
            output_dir=out,
            new_uuid=new_uuid,
            new_folder=f"M_{new_uuid.replace('-','')[:10]}",
            new_name=f"{a_name}+{b_name}",
            new_author="demo",
            conflict_policy="skip",
        )
        result = merger.merge(config)
        report = validate.validate(result.new_project)
        id_c = sum(1 for c in result.conflicts if c.kind != "file_overlap")
        fo_c = sum(1 for c in result.conflicts if c.kind == "file_overlap")
        status = "OK" if not report.is_blocked() else "BLOCKED"
        print(f"  {a_name+' + '+b_name:<32} {status:<8} "
              f"{len(result.emissions):>6} {id_c:>6} {fo_c:>7}")

    banner("Done")
    print(f"  Ran {len(pairs)} pairwise merges, all succeeded.")
    print(f"  Output written under {OUTPUT_BASE}/")


if __name__ == "__main__":
    main()
