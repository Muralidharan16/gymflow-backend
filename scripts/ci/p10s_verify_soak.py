#!/usr/bin/env python3
"""P10-S frozen-budget soak verifier."""
from __future__ import annotations
import argparse,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--http",required=True);ap.add_argument("--worker",required=True);ap.add_argument("--resources",required=True);ap.add_argument("--output",required=True);a=ap.parse_args()
    b=json.loads((ROOT/"docs/architecture/p10_performance_budgets.v1.json").read_text())["budgets"]["representative_http"]
    h=json.loads(Path(a.http).read_text()); w=json.loads(Path(a.worker).read_text()); rs=[json.loads(x) for x in Path(a.resources).read_text().splitlines() if x.strip()]
    errors=[]
    if h["duration_seconds"]<300 or len(h["windows"])<5: errors.append("soak duration/windows below P10-S minimum")
    for x in h["windows"]:
        if x["throughput_rps"]<b["min_throughput_rps"]: errors.append(f"window {x['window']} throughput below frozen floor")
        if x["http_errors"]>b["max_http_errors"] or x["server_errors_5xx"]>b["max_server_errors_5xx"] or x["exceptions"]: errors.append(f"window {x['window']} request errors")
        if x["latency"]["overall"]["p95_ms"]>b["max_overall_p95_ms"] or x["latency"]["overall"]["p99_ms"]>b["max_overall_p99_ms"]: errors.append(f"window {x['window']} overall latency exceeds frozen budget")
        if x["latency"]["writes"]["p95_ms"]>b["max_write_p95_ms"] or x["latency"]["writes"]["p99_ms"]>b["max_write_p99_ms"]: errors.append(f"window {x['window']} write latency exceeds frozen budget")
    if not rs: errors.append("no resource samples")
    elif max(x["rss_bytes"] for x in rs)>b["max_rss_bytes"]: errors.append("RSS exceeded frozen maximum")
    if w.get("final_broker_depth")!=0 or not w.get("continuous_worker") or w.get("external_provider_effects")!=0: errors.append("worker/backlog soak invariant failed")
    if rs and rs[-1]["worker_broker_depth"]!=0: errors.append("final sampled worker broker backlog nonzero")
    rec={"schema_version":1,"phase":"P10-S","duration_seconds":h["duration_seconds"],"windows":len(h["windows"]),"resource_samples":len(rs),
         "max_rss_bytes":max((x["rss_bytes"] for x in rs),default=0),"max_db_connections":max((x["db_connections"] for x in rs),default=0),
         "max_worker_broker_depth":max((x["worker_broker_depth"] for x in rs),default=0),"worker_items":w.get("items_processed",0),
         "provider_execution":"DEFERRED_FAIL_CLOSED","decision":"PASS" if not errors else "FAIL","errors":errors}
    Path(a.output).write_text(json.dumps(rec,indent=2,sort_keys=True)+"\n")
    if errors:
        [print("P10-S violation: "+e) for e in errors]; return 1
    print(json.dumps(rec,indent=2,sort_keys=True)); print("P10_SOAK=PASS"); print("P10_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED"); return 0
if __name__=="__main__": raise SystemExit(main())
