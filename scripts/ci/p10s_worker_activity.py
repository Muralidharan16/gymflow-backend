#!/usr/bin/env python3
"""P10-S long-lived worker activity against real Redis/PostgreSQL."""
from __future__ import annotations
import argparse,json,os,signal,subprocess,sys,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from scripts.ci.p10b_durable_queue_calibration import seed_authority,terminal_snapshot,controller,queue_depth,worker_environment,worker_ping
BATCH_SIZE=10
def wait(pred,desc,timeout=30):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if pred(): return
        time.sleep(.1)
    raise RuntimeError("timeout "+desc)
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--duration-seconds",type=int,default=300); ap.add_argument("--interval-seconds",type=int,default=5)
    ap.add_argument("--output",required=True); ap.add_argument("--log",required=True); a=ap.parse_args()
    queue="p10s-worker"; host=f"p10s-{uuid.uuid4().hex}@localhost"; log=Path(a.log); log.parent.mkdir(parents=True,exist_ok=True)
    h=log.open("w"); p=subprocess.Popen([sys.executable,"-m","celery","-A","app.core.celery_app:celery_app","worker","--pool=prefork","--concurrency=2",
        "--prefetch-multiplier=1",f"--queues={queue}",f"--hostname={host}","--without-gossip","--without-mingle","--loglevel=WARNING"],
        cwd=ROOT,env=worker_environment(),stdout=h,stderr=subprocess.STDOUT,text=True,start_new_session=True)
    samples=[]; total=0; started=time.monotonic()
    try:
        wait(lambda: p.poll() is None and worker_ping(host),"worker readiness",45)
        while time.monotonic()-started < a.duration_seconds:
            cycle=time.monotonic(); ids=seed_authority(
                BATCH_SIZE,
                process_after="2000-01-01T00:00:00+00:00",
            ); total+=BATCH_SIZE; ctl=controller()
            try:
                ctl.send_task("app.tasks.branch_outbox_poller.run",queue=queue)
                ctl.send_task("app.tasks.branch_outbox_poller.run",queue=queue)
            finally: ctl.close()
            wait(lambda: terminal_snapshot(ids).get("exact_terminal")==BATCH_SIZE,"durable cycle",30)
            wait(lambda: queue_depth(queue)==0,"broker cycle drain",15)
            st=terminal_snapshot(ids)
            samples.append({"elapsed_seconds":round(time.monotonic()-started,3),"exact_terminal":st.get("exact_terminal",0),"broker_depth":queue_depth(queue)})
            remaining=a.interval_seconds-(time.monotonic()-cycle)
            if remaining>0: time.sleep(min(remaining,max(0,a.duration_seconds-(time.monotonic()-started))))
        if p.poll() is not None: raise RuntimeError("soak worker exited")
        rec={"schema_version":1,"phase":"P10-S","worker_hostname":host,"continuous_worker":True,"items_processed":total,
             "worker_batch_size":BATCH_SIZE,"worker_interval_seconds":a.interval_seconds,
             "target_worker_items_per_second":BATCH_SIZE/a.interval_seconds,
             "synthetic_process_after_priority":"2000-01-01T00:00:00+00:00",
             "samples":samples,"final_broker_depth":queue_depth(queue),"external_provider_effects":0,"durable_business_authority":"postgresql"}
        Path(a.output).write_text(json.dumps(rec,indent=2,sort_keys=True)+"\n"); print("P10S_WORKER_ACTIVITY=PASS"); return 0
    finally:
        if p.poll() is None:
            os.killpg(p.pid,signal.SIGTERM)
            try:p.wait(15)
            except subprocess.TimeoutExpired: os.killpg(p.pid,signal.SIGKILL); p.wait(5)
        h.close()
if __name__=="__main__": raise SystemExit(main())
