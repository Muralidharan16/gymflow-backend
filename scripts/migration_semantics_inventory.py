from __future__ import annotations

import argparse
import ast
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


MIGRATION_ROOT = Path("alembic/versions")


@dataclass(frozen=True)
class Finding:
    severity: str
    category: str
    path: str
    function: str
    line: int
    snippet: str


_SQL_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "critical",
        "truncate",
        re.compile(r"\bTRUNCATE\b", re.IGNORECASE),
    ),
    (
        "critical",
        "drop-cascade",
        re.compile(r"\bDROP\b[\s\S]{0,240}?\bCASCADE\b", re.IGNORECASE),
    ),
    (
        "high",
        "create-extension",
        re.compile(r"\bCREATE\s+EXTENSION\b", re.IGNORECASE),
    ),
    (
        "high",
        "drop-extension",
        re.compile(r"\bDROP\s+EXTENSION\b", re.IGNORECASE),
    ),
    (
        "high",
        "create-or-replace",
        re.compile(r"\bCREATE\s+OR\s+REPLACE\b", re.IGNORECASE),
    ),
    (
        "medium",
        "ddl-if-not-exists",
        re.compile(
            r"\b(?:CREATE|ALTER)\b[\s\S]{0,160}?\bIF\s+NOT\s+EXISTS\b",
            re.IGNORECASE,
        ),
    ),
    (
        "medium",
        "ddl-if-exists",
        re.compile(
            r"\bDROP\b[\s\S]{0,160}?\bIF\s+EXISTS\b",
            re.IGNORECASE,
        ),
    ),
    (
        "medium",
        "data-delete",
        re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE),
    ),
    (
        "medium",
        "data-update",
        re.compile(r"\bUPDATE\s+[A-Za-z_\"]", re.IGNORECASE),
    ),
)


def _normalize(value: str, limit: int = 260) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("<dynamic>")
        return "".join(parts)
    return None


def _is_op_execute(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
        and bool(node.args)
    )


def _find_sql_risks(path: Path, tree: ast.Module) -> list[Finding]:
    findings: list[Finding] = []
    for function in (
        node for node in tree.body if isinstance(node, ast.FunctionDef)
    ):
        for node in ast.walk(function):
            if not isinstance(node, ast.Call) or not _is_op_execute(node):
                continue
            sql = _literal_string(node.args[0])
            if sql is None:
                continue
            for severity, category, pattern in _SQL_RULES:
                if pattern.search(sql):
                    findings.append(
                        Finding(
                            severity=severity,
                            category=category,
                            path=str(path),
                            function=function.name,
                            line=node.lineno,
                            snippet=_normalize(sql),
                        )
                    )
    return findings


def _find_noop_downgrade(path: Path, tree: ast.Module) -> list[Finding]:
    downgrade = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "downgrade"
        ),
        None,
    )
    if downgrade is None:
        return [
            Finding(
                severity="critical",
                category="missing-downgrade",
                path=str(path),
                function="<module>",
                line=1,
                snippet="migration has no downgrade() function",
            )
        ]

    statements = [
        statement
        for statement in downgrade.body
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        )
    ]
    if not statements or all(isinstance(statement, ast.Pass) for statement in statements):
        return [
            Finding(
                severity="critical",
                category="noop-downgrade",
                path=str(path),
                function="downgrade",
                line=downgrade.lineno,
                snippet="downgrade() has no executable inverse",
            )
        ]
    return []


def _find_autocommit(path: Path, tree: ast.Module) -> list[Finding]:
    findings: list[Finding] = []
    for function in (
        node for node in tree.body if isinstance(node, ast.FunctionDef)
    ):
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "autocommit_block"
            ):
                findings.append(
                    Finding(
                        severity="medium",
                        category="autocommit-block",
                        path=str(path),
                        function=function.name,
                        line=node.lineno,
                        snippet="Alembic autocommit_block()",
                    )
                )
    return findings


def _find_python_cascade_literals(path: Path, tree: ast.Module) -> list[Finding]:
    findings: list[Finding] = []
    for function in (
        node for node in tree.body if isinstance(node, ast.FunctionDef)
    ):
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg not in {"ondelete", "onupdate"}:
                    continue
                value = _literal_string(keyword.value)
                if value and value.upper() == "CASCADE":
                    findings.append(
                        Finding(
                            severity="info",
                            category=f"foreign-key-{keyword.arg}-cascade",
                            path=str(path),
                            function=function.name,
                            line=node.lineno,
                            snippet=f"{keyword.arg}='CASCADE'",
                        )
                    )
    return findings


def scan(paths: Iterable[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(paths):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        findings.extend(_find_noop_downgrade(path, tree))
        findings.extend(_find_sql_risks(path, tree))
        findings.extend(_find_autocommit(path, tree))
        findings.extend(_find_python_cascade_literals(path, tree))
    severity_order = {"critical": 0, "high": 1, "medium": 2, "info": 3}
    return sorted(
        findings,
        key=lambda item: (
            severity_order[item.severity],
            item.path,
            item.line,
            item.category,
        ),
    )


def _markdown(findings: list[Finding], migration_count: int) -> str:
    counts: dict[str, int] = {}
    categories: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
        categories[finding.category] = categories.get(finding.category, 0) + 1

    lines = [
        "# Migration semantics risk inventory",
        "",
        f"Scanned migration files: **{migration_count}**",
        f"Total findings: **{len(findings)}**",
        "",
        "This report is an inventory for manual root-cause review. A finding is not automatically a defect, and a clean report alone would not prove production readiness.",
        "",
        "## Severity counts",
        "",
    ]
    for severity in ("critical", "high", "medium", "info"):
        lines.append(f"- {severity}: {counts.get(severity, 0)}")
    lines.extend(["", "## Category counts", ""])
    for category, count in sorted(categories.items()):
        lines.append(f"- {category}: {count}")

    lines.extend(["", "## Findings", ""])
    for finding in findings:
        lines.extend(
            [
                f"### {finding.severity.upper()} — {finding.category}",
                f"- file: `{finding.path}`",
                f"- function: `{finding.function}`",
                f"- line: {finding.line}",
                f"- snippet: `{finding.snippet.replace('`', chr(39))}`",
                "",
            ]
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-output", required=True)
    parser.add_argument("--markdown-output", required=True)
    args = parser.parse_args()

    paths = [
        path
        for path in MIGRATION_ROOT.glob("*.py")
        if path.name != "__init__.py"
    ]
    findings = scan(paths)

    json_path = Path(args.json_output)
    markdown_path = Path(args.markdown_output)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)

    json_path.write_text(
        json.dumps(
            {
                "migration_count": len(paths),
                "finding_count": len(findings),
                "findings": [asdict(finding) for finding in findings],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(
        _markdown(findings, len(paths)),
        encoding="utf-8",
    )

    critical = sum(finding.severity == "critical" for finding in findings)
    high = sum(finding.severity == "high" for finding in findings)
    medium = sum(finding.severity == "medium" for finding in findings)
    print(
        f"migration_semantics_inventory migrations={len(paths)} "
        f"findings={len(findings)} critical={critical} high={high} medium={medium}"
    )


if __name__ == "__main__":
    main()
