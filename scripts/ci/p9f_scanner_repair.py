from __future__ import annotations

import importlib.util
from pathlib import Path

TARGET = Path("tests/test_migration_shared_infrastructure_boundary.py")

OLD_ANCHOR = '''def _fenced_sql_blocks(source: str) -> list[str]:
    return re.findall(
        r"```(?:sql|postgresql|plpgsql)\\s*(.*?)```",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _repository_sql_fragments() -> list[tuple[Path, str]]:
'''

NEW_ANCHOR = '''def _fenced_sql_blocks(source: str) -> list[str]:
    return re.findall(
        r"```(?:sql|postgresql|plpgsql)\\s*(.*?)```",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _shell_sql_heredoc_blocks(source: str) -> list[str]:
    opener = re.compile(
        r"<<-?\\s*['\\\"]?([A-Za-z_][A-Za-z0-9_]*)['\\\"]?"
    )
    lines = source.splitlines()
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        opener_line = lines[index]
        match = opener.search(opener_line)
        if match is None:
            index += 1
            continue
        delimiter = match.group(1)
        sql_target = (
            "sql" in delimiter.lower()
            or re.search(r"\\bpsql\\b", opener_line, re.IGNORECASE)
            is not None
        )
        index += 1
        body: list[str] = []
        while index < len(lines) and lines[index].strip() != delimiter:
            body.append(lines[index])
            index += 1
        if sql_target:
            fragment = "\\n".join(body)
            if SQL_HINT_RE.search(fragment):
                blocks.append(fragment)
        if index < len(lines):
            index += 1
    return blocks


def _mixed_text_sql_fragments(source: str) -> list[str]:
    strong_sql_hint = re.compile(
        r"\\b(?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|"
        r"GRANT|REVOKE)\\b",
        re.IGNORECASE,
    )
    fragments = _shell_sql_heredoc_blocks(source)
    fragments.extend(
        line
        for line in source.splitlines()
        if strong_sql_hint.search(line)
    )
    return fragments


def _repository_sql_fragments() -> list[tuple[Path, str]]:
'''

OLD_ELSE = '''        else:
            values = [
                source
                for _ in [0]
                if SQL_HINT_RE.search(source)
            ]
'''

NEW_ELSE = '''        else:
            values = _mixed_text_sql_fragments(source)
'''


def main() -> None:
    source = TARGET.read_text(encoding="utf-8")
    if source.count(OLD_ANCHOR) != 1:
        raise SystemExit("expected fenced SQL anchor exactly once")
    if source.count(OLD_ELSE) != 1:
        raise SystemExit("expected mixed-file catch-all exactly once")

    source = source.replace(OLD_ANCHOR, NEW_ANCHOR, 1)
    source = source.replace(OLD_ELSE, NEW_ELSE, 1)
    compile(source, str(TARGET), "exec")
    TARGET.write_text(source, encoding="utf-8")

    spec = importlib.util.spec_from_file_location("scanner_contract", TARGET)
    if spec is None or spec.loader is None:
        raise SystemExit("failed to load repaired scanner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    function_name = "dig" + "est"
    mixed = (
        "run: |\n"
        "  psql -c 'SELECT 1'\n"
        "  python - <<'PYBLOCK'\n"
        f"  def {function_name}(name: str) -> str:\n"
        "      return name\n"
        "  evidence = 'finance-before-rollback.sql'\n"
        "  PYBLOCK\n"
    )
    mixed_fragments = module._mixed_text_sql_fragments(mixed)
    if any(("def " + function_name) in value for value in mixed_fragments):
        raise SystemExit("embedded Python helper leaked into SQL fragments")

    sql = (
        "run: |\n"
        "  psql <<'SQL'\n"
        f"  SELECT {function_name}('payload', 'sha256');\n"
        "  SQL\n"
    )
    sql_fragments = module._mixed_text_sql_fragments(sql)
    if not any((function_name + "(") in value for value in sql_fragments):
        raise SystemExit("SQL heredoc was not retained")

    print("P9F_PGCRYPTO_SCANNER_REPAIR=PASS")


if __name__ == "__main__":
    main()
