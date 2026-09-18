#!/usr/bin/env python3
"""P10-S continuous representative HTTP soak with per-window evidence."""
from __future__ import annotations
import argparse, asyncio, json, math, sys, time
from collections import Counter
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import httpx
from scripts.ci.p10b_load_calibration import token_for_tenant, request_headers, percentile, deterministic_uuid

async def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--base-url",required=True); ap.add_argument("--secret-key",required=True)
    ap.add_argument("--duration-seconds",type=int,default=300)
    ap.add_argument("--window-seconds",type=int,default=60)
    ap.add_argument("--concurrency",type=int,default=24); ap.add_argument("--output",required=True)
    a=ap.parse_args()
    if a.duration_seconds < 300 or a.window_seconds < 30 or a.duration_seconds % a.window_seconds:
        raise SystemExit("P10-S requires >=300s and evenly divisible windows >=30s")
    auth=[token_for_tenant(a.secret_key,n) for n in range(1,9)]
    limits=httpx.Limits(max_connections=max(64,a.concurrency*2),max_keepalive_connections=max(32,a.concurrency))
    timeout=httpx.Timeout(10.0,connect=5.0)
    started=time.monotonic(); deadline=started+a.duration_seconds
    windows=[{"lat":[],"read":[],"write":[],"status":Counter(),"exc":Counter(),"methods":Counter()} for _ in range(a.duration_seconds//a.window_seconds)]
    seq=0; lock=asyncio.Lock()

    async def next_seq():
        nonlocal seq
        async with lock: seq+=1; return seq

    async with httpx.AsyncClient(base_url=a.base_url.rstrip("/"),timeout=timeout,limits=limits,http2=False) as client:
        ready=await client.get("/_system/ready")
        if ready.status_code != 200 or ready.json() != {"status":"ready"}: raise RuntimeError("API not ready")
        for i,(org,_,tok) in enumerate(auth,1):
            r=await client.get(f"/organizations/{org}/members",params={"page":1,"page_size":50},headers=request_headers(tok,org))
            if r.status_code != 200 or r.json().get("total",0) < 500: raise RuntimeError(f"tenant {i} preflight failed")
        async def worker(wid:int):
            op=0
            while time.monotonic()<deadline:
                s=await next_seq(); elapsed=time.monotonic()-started
                wi=min(int(elapsed//a.window_seconds),len(windows)-1); bucket=windows[wi]
                ti=(wid+op)%len(auth); tenant=ti+1; org,_,tok=auth[ti]; headers=request_headers(tok,org)
                write=s%10==0; t=time.perf_counter()
                try:
                    if write:
                        branch=deterministic_uuid(f"p9m-branch-{tenant}-{((s-1)%3)+1}")
                        payload={"name":f"P10-S Member {tenant}-{s}","phone":f"6{tenant:02d}{s%10_000_000:07d}",
                                 "date_of_birth":"1992-01-01","emergency_contact_name":"8111111111",
                                 "emergency_contact_phone":"9222222222","home_branch_id":branch}
                        r=await client.post(f"/organizations/{org}/members",json=payload,headers=headers); bucket["methods"]["POST"]+=1
                    else:
                        r=await client.get(f"/organizations/{org}/members",params={"page":(s%10)+1,"page_size":50},headers=headers); bucket["methods"]["GET"]+=1
                    ms=(time.perf_counter()-t)*1000; bucket["lat"].append(ms); bucket["write" if write else "read"].append(ms); bucket["status"][r.status_code]+=1
                except Exception as e:
                    ms=(time.perf_counter()-t)*1000; bucket["lat"].append(ms); bucket["write" if write else "read"].append(ms); bucket["exc"][type(e).__name__]+=1
                op+=1
        await asyncio.gather(*(worker(i) for i in range(a.concurrency)))
    def summ(v):
        return {"count":len(v),"p50_ms":round(percentile(v,.5),3),"p95_ms":round(percentile(v,.95),3),"p99_ms":round(percentile(v,.99),3)}
    out=[]
    for i,b in enumerate(windows):
        total=sum(b["status"].values())+sum(b["exc"].values()); errs=sum(c for code,c in b["status"].items() if code>=400)
        out.append({"window":i+1,"seconds":a.window_seconds,"requests":total,"throughput_rps":round(total/a.window_seconds,3),
                    "http_errors":errs,"server_errors_5xx":sum(c for code,c in b["status"].items() if code>=500),
                    "exceptions":dict(b["exc"]),"status_counts":{str(k):v for k,v in b["status"].items()},
                    "latency":{"overall":summ(b["lat"]),"reads":summ(b["read"]),"writes":summ(b["write"])}})
    Path(a.output).write_text(json.dumps({"schema_version":1,"phase":"P10-S","duration_seconds":a.duration_seconds,"concurrency":a.concurrency,"windows":out},indent=2,sort_keys=True)+"\n")
    print("P10S_HTTP_SOAK_CAPTURE=PASS"); return 0
if __name__=="__main__": raise SystemExit(asyncio.run(main()))
