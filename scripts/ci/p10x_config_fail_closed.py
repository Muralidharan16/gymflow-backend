#!/usr/bin/env python3
"""P10-X production secrets/config fail-closed probe."""
from __future__ import annotations
import json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
BASE={
"ENVIRONMENT":"production","DOERS_PROCESS_PROFILE":"api",
"DATABASE_URL":"postgresql+asyncpg://app_test_runtime:p10x@127.0.0.1:5432/doers",
"AUTH_DATABASE_URL":"postgresql+asyncpg://auth_test_runtime:p10x@127.0.0.1:5432/doers",
"REDIS_URL":"redis://127.0.0.1:6379/0","CELERY_BROKER_URL":"redis://127.0.0.1:6379/1","CELERY_RESULT_BACKEND":"redis://127.0.0.1:6379/2",
"SECRET_KEY":"p10x-synthetic-secret-not-production","AWS_ACCESS_KEY_ID":"p10x-synthetic","AWS_SECRET_ACCESS_KEY":"p10x-synthetic",
"S3_BUCKET_NAME":"p10x-synthetic","NOTIFICATION_EMAIL_PROVIDER_MODE":"disabled","SEARCH_PROVIDER_MODE":"disabled",
"PLATFORM_BILLING_PROVIDER_MODE":"disabled","PLATFORM_BILLING_CHECKOUT":"false","PLATFORM_BILLING_WEBHOOK_PROCESSING":"false",
"PLATFORM_BILLING_DUNNING_TRANSITIONS":"false","PLATFORM_BILLING_NOTIFICATIONS":"false","LOG_LEVEL":"info",
}
CODE="from app.core.config import settings; import json; print(json.dumps({'environment':settings.ENVIRONMENT,'profile':settings.process_profile,'notification':settings.NOTIFICATION_EMAIL_PROVIDER_MODE,'search':settings.SEARCH_PROVIDER_MODE,'billing':settings.PLATFORM_BILLING_PROVIDER_MODE,'log_level':settings.LOG_LEVEL}))"
def run(overrides=None, remove=()):
    env={k:v for k,v in os.environ.items() if k in {"PATH","HOME","LANG","LC_ALL","PYTHONPATH"}}
    env.update(BASE); [env.pop(k,None) for k in remove]; env.update(overrides or {})
    return subprocess.run([sys.executable,"-s","-c",CODE],cwd=ROOT,env=env,text=True,capture_output=True)
def require_failure(name,result,needle):
    if result.returncode==0 or needle.lower() not in (result.stdout+result.stderr).lower():
        raise RuntimeError(f"{name} did not fail closed as required: rc={result.returncode}\n{result.stdout}\n{result.stderr}")
def main():
    valid=run()
    if valid.returncode!=0: raise RuntimeError("valid disabled-provider production config failed:\n"+valid.stderr)
    state=json.loads(valid.stdout.strip().splitlines()[-1])
    if state!={"environment":"production","profile":"api","notification":"disabled","search":"disabled","billing":"disabled","log_level":"info"}:
        raise RuntimeError(f"unexpected valid production state: {state}")
    require_failure("missing SECRET_KEY",run(remove=("SECRET_KEY",)),"secret_key")
    require_failure("empty SECRET_KEY",run({"SECRET_KEY":""}),"mandatory security configuration")
    require_failure("empty AWS_ACCESS_KEY_ID",run({"AWS_ACCESS_KEY_ID":""}),"aws_access_key_id")
    require_failure("empty AWS_SECRET_ACCESS_KEY",run({"AWS_SECRET_ACCESS_KEY":""}),"aws_secret_access_key")
    require_failure("production debug",run({"LOG_LEVEL":"debug"}),"debug or trace logging is forbidden")
    for path in (ROOT/"Dockerfile",ROOT/"docker-compose.yml"):
        if "--reload" in path.read_text(encoding="utf-8"): raise RuntimeError(f"production reload token present in {path}")
    print(json.dumps({"schema_version":1,"phase":"P10-X","valid_state":state,"negative_cases":5,"provider_execution":"DEFERRED_FAIL_CLOSED","decision":"PASS"},sort_keys=True))
    print("P10_SECRETS_CONFIG_FAIL_CLOSED=PASS"); print("P10_REFUND_PROVIDER_EXECUTION=DEFERRED_FAIL_CLOSED"); return 0
if __name__=="__main__": raise SystemExit(main())
