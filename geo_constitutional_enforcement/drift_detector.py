"""
geo_constitutional_enforcement/drift_detector.py
"""
import sys
import time
import yaml
import ast
from pathlib import Path
from typing import List
from .result import ConstitutionalResult

REGISTRY_DIR = Path("constitutional/semantic_registry")

def _get_model_ast(model_path: str = "app/models/geo.py") -> ast.Module:
    path = Path(model_path)
    if not path.exists():
        raise FileNotFoundError(f"Missing model file: {model_path}")
    return ast.parse(path.read_text(), filename=model_path)

def extract_model_attributes(model_node: ast.ClassDef) -> List[str]:
    attrs = []
    for node in model_node.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    attrs.append(target.id)
    return sorted(attrs)

def verify_model_sync(registry_file: Path, geo_ast: ast.Module) -> List[str]:
    try:
        spec = yaml.safe_load(registry_file.read_text())
    except Exception as e:
        return [f"Failed to parse {registry_file.name}: {e}"]

    model_name = spec.get("source_model", "").split("::")[-1]
    if not model_name or model_name.startswith("app/models"):
        if "ENUM" in spec.get("type", ""):
            return [] 

    class_node = None
    for node in geo_ast.body:
        if isinstance(node, ast.ClassDef) and node.name == model_name:
            class_node = node
            break
            
    if not class_node:
        return [f"Model class {model_name} not found in source code"]

    actual_attrs = set(extract_model_attributes(class_node))
    expected_attrs = set()
    
    if "primary_key" in spec:
        expected_attrs.add(spec["primary_key"]["name"])
    
    for section in ["identity_fields", "foreign_keys", "required_fields", "optional_fields", "timestamp_fields", "relationships"]:
        for field in spec.get(section, []):
            expected_attrs.add(field["name"])

    violations = []
    for expected in sorted(expected_attrs):
        if expected not in actual_attrs:
            violations.append(f"Registry field '{expected}' missing from model {model_name}")
            
    return violations

def verify() -> ConstitutionalResult:
    start_time = time.time()
    
    if not REGISTRY_DIR.exists():
        return ConstitutionalResult("semantic_drift", "fail", int((time.time() - start_time) * 1000), [], {}, f"Registry missing: {REGISTRY_DIR}")

    try:
        geo_ast = _get_model_ast()
    except Exception as e:
        return ConstitutionalResult("semantic_drift", "fail", int((time.time() - start_time) * 1000), [], {}, f"AST error: {e}")

    all_violations = []
    metadata_violations = {}
    
    for yaml_file in sorted(REGISTRY_DIR.glob("*.yaml")):
        violations = verify_model_sync(yaml_file, geo_ast)
        if violations:
            metadata_violations[yaml_file.name] = violations
            for v in violations:
                all_violations.append(f"[{yaml_file.name}] {v}")

    if all_violations:
        return ConstitutionalResult(
            "semantic_drift", "fail", int((time.time() - start_time) * 1000), all_violations, 
            {"violations": metadata_violations}, "Semantic drift detected"
        )

    return ConstitutionalResult("semantic_drift", "pass", int((time.time() - start_time) * 1000), ["Semantic registry sync verified."], {})
