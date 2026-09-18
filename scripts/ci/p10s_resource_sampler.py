#!/usr/bin/env python3
"""P10-S API container/DB/Redis resource time-series sampler."""
from __future__ import annotations
import argparse,json,os,re,subprocess,time
from pathlib import Path
import psycopg,redis
def bytesv(s):
    m=re.fullmatch(r"([0-9.]+)([KMG]?i?B)",s.strip()); 
    if not m: raise ValueError(s)
    scale={"B":1,"KB":1000,"MB":1000**2,"GB":1000**3,"KiB":1024,"MiB":1024**2,"GiB":1024**3}[m.group(2)]
    return int(float(m.group(1))*scale)
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--duration-seconds",type=int,default=300); ap.add_argument("--interval-seconds",type=int,default=5)
    ap.add_argument("--container",default="p10s-api"); ap.add_argument("--output",required=True); a=ap.parse_args()
    db=os.environ["P10S_ADMIN_DATABASE_URL"]; r=redis.Redis.from_url(os.environ["REDIS_URL"],decode_responses=True)
    end=time.monotonic()+a.duration_seconds; started=time.monotonic(); rows=[]
    while time.monotonic()<end:
        raw=subprocess.check_output(["docker","stats","--no-stream","--format","{{json .}}",a.container],text=True); d=json.loads(raw)
        with psycopg.connect(db) as c:
            with c.cursor() as cur:
                cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()"); connections=int(cur.fetchone()[0])
        info=r.info("clients")
        rows.append({"elapsed_seconds":round(time.monotonic()-started,3),"cpu_percent":float(d["CPUPerc"].rstrip("%")),
                     "rss_bytes":bytesv(d["MemUsage"].split("/",1)[0].strip()),"pids":int(d["PIDs"]),
                     "db_connections":connections,"redis_connected_clients":int(info.get("connected_clients",0)),
                     "worker_broker_depth":int(r.llen("p10s-worker"))})
        Path(a.output).write_text("".join(json.dumps(x,sort_keys=True)+"\n" for x in rows))
        time.sleep(min(a.interval_seconds,max(0,end-time.monotonic())))
    print("P10S_RESOURCE_TIMESERIES=PASS"); return 0
if __name__=="__main__": raise SystemExit(main())
