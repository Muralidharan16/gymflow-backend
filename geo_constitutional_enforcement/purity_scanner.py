"""
geo_constitutional_enforcement/purity_scanner.py
"""
import ast
import time
import yaml
from pathlib import Path
from .result import ConstitutionalResult

RULES_FILE = Path("constitutional/deterministic_purity_rules.yaml")
BOUNDARIES_FILE = Path("constitutional/nondeterminism_boundaries.yaml")

class PurityVisitor(ast.NodeVisitor):
    def __init__(self, filepath: str, rules: dict, exemptions: list):
        self.filepath = filepath
        self.rules = rules
        self.exemptions = set([e["pattern"] for e in exemptions])
        self.violations = []
        self.prohibited_imports = set(rules.get("prohibited_patterns", {}).get("randomization", {}).get("imports", []))
        self.prohibited_calls = set()
        for category, config in rules.get("prohibited_patterns", {}).items():
            if "calls" in config:
                self.prohibited_calls.update(config["calls"])

    def _is_exempt(self, pattern: str) -> bool:
        return pattern in self.exemptions

    def visit_Import(self, node):
        for alias in node.names:
            if alias.name in self.prohibited_imports and not self._is_exempt(alias.name):
                self.violations.append(f"Line {node.lineno}: Prohibited import '{alias.name}'")
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module in self.prohibited_imports and not self._is_exempt(node.module):
            self.violations.append(f"Line {node.lineno}: Prohibited import from '{node.module}'")
        self.generic_visit(node)

    def visit_Call(self, node):
        call_name = ""
        if isinstance(node.func, ast.Name):
            call_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name):
                call_name = f"{node.func.value.id}.{node.func.attr}"
        
        if call_name and call_name in self.prohibited_calls and not self._is_exempt(call_name):
            self.violations.append(f"Line {node.lineno}: Prohibited call '{call_name}'")
                
        if isinstance(node.func, ast.Attribute):
            method_name = f"str.{node.func.attr}"
            if method_name in self.prohibited_calls and not self._is_exempt(method_name):
                self.violations.append(f"Line {node.lineno}: Prohibited method call '{method_name}'")

        self.generic_visit(node)
        
    def visit_Global(self, node):
        if "mutable_globals" in self.rules.get("prohibited_patterns", {}):
            self.violations.append(f"Line {node.lineno}: Prohibited mutable global '{node.names[0]}'")
        self.generic_visit(node)

def verify() -> ConstitutionalResult:
    start_time = time.time()
    
    if not RULES_FILE.exists():
        return ConstitutionalResult("purity_scan", "fail", int((time.time() - start_time) * 1000), [], {}, f"Missing {RULES_FILE}")
        
    rules_config = yaml.safe_load(RULES_FILE.read_text())
    
    boundaries = []
    if BOUNDARIES_FILE.exists():
        b_config = yaml.safe_load(BOUNDARIES_FILE.read_text())
        boundaries = [b["module"] for b in b_config.get("boundaries", [])]

    canonical_paths = rules_config.get("canonical_paths", [])
    exemptions_map = rules_config.get("exemptions", {})

    all_violations = []

    for path_str in sorted(canonical_paths):
        if path_str in boundaries:
            continue
            
        path = Path(path_str)
        if not path.exists():
            continue
            
        try:
            tree = ast.parse(path.read_text(), filename=path_str)
        except SyntaxError as e:
            return ConstitutionalResult("purity_scan", "fail", int((time.time() - start_time) * 1000), [], {}, f"Syntax error in {path_str}: {e}")

        exemptions = exemptions_map.get(path_str, [])
        visitor = PurityVisitor(path_str, rules_config, exemptions)
        visitor.visit(tree)
        
        for v in visitor.violations:
            all_violations.append(f"[{path_str}] {v}")

    if all_violations:
        return ConstitutionalResult("purity_scan", "fail", int((time.time() - start_time) * 1000), all_violations, {"violations": all_violations}, "Purity violations detected")

    return ConstitutionalResult("purity_scan", "pass", int((time.time() - start_time) * 1000), ["Deterministic purity verified."], {})
