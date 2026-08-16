from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS_DIR = ROOT / "app" / "tasks"
CELERY_APP = ROOT / "app" / "core" / "celery_app.py"


def _decorator_task_name(decorator: ast.expr, module_name: str, function_name: str) -> str | None:
    call = decorator if isinstance(decorator, ast.Call) else None
    target = call.func if call is not None else decorator
    is_task_decorator = (
        isinstance(target, ast.Name) and target.id in {"shared_task", "task"}
    ) or (
        isinstance(target, ast.Attribute) and target.attr == "task"
    )
    if not is_task_decorator:
        return None
    if call is not None:
        for keyword in call.keywords:
            if (
                keyword.arg == "name"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                return keyword.value.value
    return f"{module_name}.{function_name}"


def _registered_task_names() -> set[str]:
    task_names: set[str] = set()
    for path in sorted(TASKS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        module_name = f"app.tasks.{path.stem}"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                task_name = _decorator_task_name(decorator, module_name, node.name)
                if task_name is not None:
                    task_names.add(task_name)
                    break
    return task_names


def _beat_schedule_node() -> ast.Dict:
    tree = ast.parse(CELERY_APP.read_text(encoding="utf-8"), filename=str(CELERY_APP))
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (
            isinstance(target, ast.Attribute)
            and target.attr == "beat_schedule"
            and isinstance(target.value, ast.Attribute)
            and target.value.attr == "conf"
        ):
            continue
        if not isinstance(node.value, ast.Dict):
            raise AssertionError("Celery beat_schedule must remain a static dictionary")
        return node.value
    raise AssertionError("Celery beat_schedule assignment not found")


def _dict_string_value(node: ast.Dict, key_name: str) -> str | None:
    for key, value in zip(node.keys, node.values):
        if (
            isinstance(key, ast.Constant)
            and key.value == key_name
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            return value.value
    return None


def _beat_entries() -> dict[str, ast.Dict]:
    entries: dict[str, ast.Dict] = {}
    schedule = _beat_schedule_node()
    for key, value in zip(schedule.keys, schedule.values):
        if not (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and isinstance(value, ast.Dict)
        ):
            raise AssertionError("Celery beat_schedule entries must remain static dictionaries")
        entries[key.value] = value
    return entries


def _maintenance_task_names() -> set[str]:
    tree = ast.parse(CELERY_APP.read_text(encoding="utf-8"), filename=str(CELERY_APP))
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id != "MAINTENANCE_TASKS":
            continue
        if not isinstance(node.value, (ast.Tuple, ast.List)):
            raise AssertionError("MAINTENANCE_TASKS must remain a static sequence")
        values: set[str] = set()
        for element in node.value.elts:
            if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
                raise AssertionError("MAINTENANCE_TASKS entries must be literal task names")
            values.add(element.value)
        return values
    raise AssertionError("MAINTENANCE_TASKS assignment not found")


def _has_explicit_maintenance_queue(entry: ast.Dict) -> bool:
    for key, value in zip(entry.keys, entry.values):
        if not (isinstance(key, ast.Constant) and key.value == "options"):
            continue
        if not isinstance(value, ast.Dict):
            return False
        for option_key, option_value in zip(value.keys, value.values):
            if not (isinstance(option_key, ast.Constant) and option_key.value == "queue"):
                continue
            return isinstance(option_value, ast.Name) and option_value.id == "MAINTENANCE_QUEUE"
    return False


def test_every_beat_target_is_a_registered_task() -> None:
    registered = _registered_task_names()
    scheduled = {
        task_name
        for entry in _beat_entries().values()
        if (task_name := _dict_string_value(entry, "task")) is not None
    }
    missing = sorted(scheduled - registered)
    assert not missing, (
        "Celery Beat references task names that are not registered by app/tasks: "
        f"{missing}"
    )


def test_maintenance_tasks_are_registered_and_explicitly_routed() -> None:
    registered = _registered_task_names()
    maintenance = _maintenance_task_names()
    assert maintenance <= registered, (
        "MAINTENANCE_TASKS contains unregistered task names: "
        f"{sorted(maintenance - registered)}"
    )
    entries = _beat_entries()
    scheduled_by_task = {
        task_name: (schedule_name, entry)
        for schedule_name, entry in entries.items()
        if (task_name := _dict_string_value(entry, "task")) is not None
    }
    for task_name in maintenance:
        assert task_name in scheduled_by_task, (
            f"Maintenance task {task_name!r} is not present in the Beat schedule"
        )
        schedule_name, entry = scheduled_by_task[task_name]
        assert _has_explicit_maintenance_queue(entry), (
            f"Maintenance Beat entry {schedule_name!r} must explicitly route "
            "to MAINTENANCE_QUEUE"
        )
