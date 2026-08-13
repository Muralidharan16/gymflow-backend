#!/usr/bin/env python3
"""Closed Alembic graph/compile inventory for P2E/P2F certification."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "alembic" / "versions"


def _literal_assignment(tree: ast.Module, name: str):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise ValueError(f"missing {name}")


def main() -> int:
    files = sorted(path for path in VERSIONS.glob("*.py") if path.name != "__init__.py")
    revisions: dict[str, tuple[Path, tuple[str, ...]]] = {}
    errors: list[str] = []

    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
            compile(source, str(path), "exec")
            tree = ast.parse(source, filename=str(path))
            revision = _literal_assignment(tree, "revision")
            down = _literal_assignment(tree, "down_revision")
        except Exception as exc:
            errors.append(f"{path.relative_to(ROOT)}: parse/compile contract failed: {exc}")
            continue

        if not isinstance(revision, str) or not revision:
            errors.append(f"{path.relative_to(ROOT)}: revision must be a non-empty string")
            continue
        if revision in revisions:
            errors.append(
                f"duplicate revision {revision}: {revisions[revision][0].name}, {path.name}"
            )
            continue

        if down is None:
            parents: tuple[str, ...] = ()
        elif isinstance(down, str):
            parents = (down,)
        elif isinstance(down, (tuple, list)) and all(isinstance(item, str) for item in down):
            parents = tuple(down)
        else:
            errors.append(f"{path.relative_to(ROOT)}: unsupported down_revision={down!r}")
            continue
        revisions[revision] = (path, parents)

    children: dict[str, set[str]] = {revision: set() for revision in revisions}
    for revision, (path, parents) in revisions.items():
        for parent in parents:
            if parent not in revisions:
                errors.append(
                    f"{path.relative_to(ROOT)}: down_revision {parent!r} does not resolve"
                )
            else:
                children[parent].add(revision)

    roots = sorted(revision for revision, (_, parents) in revisions.items() if not parents)
    heads = sorted(revision for revision, child_set in children.items() if not child_set)
    if len(roots) != 1:
        errors.append(f"expected exactly one Alembic root; found {roots!r}")
    if len(heads) != 1:
        errors.append(f"expected exactly one Alembic head; found {heads!r}")
    if len(revisions) != len(files):
        errors.append(
            f"migration file/revision mismatch: files={len(files)} revisions={len(revisions)}"
        )

    if not errors and roots:
        visited: set[str] = set()
        stack = [roots[0]]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            stack.extend(sorted(children[current]))
        missing = sorted(set(revisions) - visited)
        if missing:
            errors.append(f"orphan/unreachable Alembic revisions: {missing!r}")

    if errors:
        print("P2E Alembic graph guard FAILED")
        for error in errors:
            print(f"- {error}")
        return 1

    print(
        "P2E Alembic graph guard passed "
        f"files={len(files)} root={roots[0]} head={heads[0]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
