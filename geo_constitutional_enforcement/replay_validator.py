"""
geo_constitutional_enforcement/replay_validator.py
"""
import sys
import json
import time
from pathlib import Path
from decimal import Decimal
from typing import List, Dict, Any

from .canonical_serializer import canonical_json, canonical_hash
from .result import ConstitutionalResult

CORPUS_DIR = Path("tests/corpora")
MANIFEST_DIR = Path("tests/corpora/manifests")
BASELINE_FILE = MANIFEST_DIR / "baseline.sha256"

class MockModel:
    def __init__(self, **kwargs):
        for k, v in kwargs.items(): setattr(self, k, v)

class GeoReplayHarness:
    def __init__(self, seed_data: dict):
        self.countries = {c['id']: MockModel(**c) for c in seed_data['countries']}
        self.subdivisions = {s['id']: MockModel(**s) for s in seed_data['subdivisions']}
        self.cities = {c['id']: MockModel(**c) for c in seed_data['cities']}
        self.postal_codes = {p['id']: MockModel(**p) for p in seed_data['postal_codes']}
        self.postal_index = {}
        for p in self.postal_codes.values():
            co = self.countries[p.country_id]
            key = (co.iso2.upper(), p.postal_code)
            if key not in self.postal_index: self.postal_index[key] = []
            self.postal_index[key].append(p)

    def execute_postal_lookup(self, country_iso2: str, postal_code: str) -> List[Dict]:
        results = []
        key = (country_iso2.upper(), postal_code)
        postals = sorted(self.postal_index.get(key, []), key=lambda x: x.id)
        for p in postals:
            c = self.cities[p.city_id]
            s = self.subdivisions[p.subdivision_id]
            co = self.countries[p.country_id]
            resolved_timezone = p.timezone or c.timezone or s.timezone or co.timezone
            results.append({
                "postal_code_id": p.id,
                "postal_code": p.postal_code,
                "locality": getattr(p, 'locality', None),
                "city_id": c.id,
                "city_name": c.name,
                "subdivision_id": s.id,
                "subdivision_name": s.name,
                "country_iso2": co.iso2,
                "timezone": resolved_timezone,
                "latitude": Decimal(str(p.latitude)) if p.latitude else None,
                "longitude": Decimal(str(p.longitude)) if p.longitude else None
            })
        return results

    def generate_snapshot(self) -> Dict[str, Any]:
        return {
            "postal_lookups": {
                "BR_01000-000": self.execute_postal_lookup("BR", "01000-000"),
                "DE_80331": self.execute_postal_lookup("DE", "80331"),
                "JP_100-0000": self.execute_postal_lookup("JP", "100-0000"),
                "US_10001": self.execute_postal_lookup("US", "10001"),
                "US_10001-1234": self.execute_postal_lookup("US", "10001-1234")
            }
        }

def _compare_dicts(d1, d2, path=""):
    diffs = []
    for k in d1:
        if k not in d2: diffs.append(f"Missing key in actual: {path}/{k}")
        elif type(d1[k]) != type(d2[k]): diffs.append(f"Type mismatch at {path}/{k}: Expected {type(d1[k])}, got {type(d2[k])}")
        elif isinstance(d1[k], dict): diffs.extend(_compare_dicts(d1[k], d2[k], f"{path}/{k}"))
        elif isinstance(d1[k], list):
            if len(d1[k]) != len(d2[k]): diffs.append(f"List length mismatch at {path}/{k}: Expected {len(d1[k])}, got {len(d2[k])}")
            else:
                for i, (v1, v2) in enumerate(zip(d1[k], d2[k])):
                    if v1 != v2:
                        if isinstance(v1, (dict, list)): diffs.append(f"Nested divergence at {path}/{k}[{i}]")
                        else: diffs.append(f"Value mismatch at {path}/{k}[{i}]: Expected '{v1}', got '{v2}'")
        elif d1[k] != d2[k]: diffs.append(f"Value mismatch at {path}/{k}: Expected '{d1[k]}', got '{d2[k]}'")
    for k in d2:
        if k not in d1: diffs.append(f"Unexpected key in actual: {path}/{k}")
    return diffs

def verify(mode: str = "ci") -> ConstitutionalResult:
    start_time = time.time()
    
    seed_file = CORPUS_DIR / "geo_seed.json"
    if not seed_file.exists():
        return ConstitutionalResult("replay_validation", "fail", int((time.time() - start_time) * 1000), [], {}, f"Missing corpus: {seed_file}")
        
    try:
        seed_data = json.loads(seed_file.read_text())
    except Exception as e:
        return ConstitutionalResult("replay_validation", "fail", int((time.time() - start_time) * 1000), [], {}, f"Corpus parse error: {e}")
        
    harness = GeoReplayHarness(seed_data)
    snapshot = harness.generate_snapshot()
    actual_json = canonical_json(snapshot)
    actual_hash = canonical_hash(snapshot)

    if mode == "bootstrap":
        MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
        BASELINE_FILE.write_text(f"{actual_hash}  snapshot.json\n")
        (MANIFEST_DIR / "snapshot.json").write_text(actual_json)
        return ConstitutionalResult("replay_validation", "pass", int((time.time() - start_time) * 1000), ["Bootstrap baseline frozen."], {"hash": actual_hash})
    
    if not BASELINE_FILE.exists():
        return ConstitutionalResult("replay_validation", "fail", int((time.time() - start_time) * 1000), [], {}, f"Missing baseline {BASELINE_FILE}")
        
    expected_hash = BASELINE_FILE.read_text().strip().split()[0]
    
    if actual_hash != expected_hash:
        expected_json_file = MANIFEST_DIR / "snapshot.json"
        diagnostics = []
        if expected_json_file.exists():
            try:
                expected = json.loads(expected_json_file.read_text())
                actual = json.loads(actual_json)
                diagnostics = _compare_dicts(expected, actual, "$")
            except Exception as e:
                diagnostics.append(f"Diagnostic diffing failed: {e}")
                
        return ConstitutionalResult(
            "replay_validation", "fail", int((time.time() - start_time) * 1000), 
            diagnostics, 
            {"expected": expected_hash, "actual": actual_hash}, 
            "REPLAY DIVERGENCE DETECTED"
        )

    return ConstitutionalResult("replay_validation", "pass", int((time.time() - start_time) * 1000), ["Replay equivalence verified."], {"hash": actual_hash})

if __name__ == "__main__":
    if "--mode=bootstrap" in sys.argv:
        res = verify("bootstrap")
        sys.exit(0 if res.status == "pass" else 1)
