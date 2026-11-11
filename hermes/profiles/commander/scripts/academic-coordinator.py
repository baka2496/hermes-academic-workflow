"""Hermes no-agent scheduler entry. All user delivery belongs to the durable publisher."""
from contextlib import redirect_stdout
from pathlib import Path
import os
import sys

RUNTIME = Path(__file__).resolve().parents[3] / 'shared' / 'academic-workflow'
sys.path.insert(0,str(RUNTIME))
from academic import load,tick
from workflow import iso


def report(config,workflow,**outcome):
    # Alerting must never turn a successful tick into a failure, or hide the original error.
    try:
        from publisher import scheduler_outcome
        with redirect_stdout(sys.stderr):scheduler_outcome(workflow,config,**outcome)
    except Exception:pass


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    config,workflow=load(RUNTIME/'config.json')
    started=iso()
    try:
        with redirect_stdout(sys.stderr):result=tick(config,workflow)
        workflow.set_meta('last_scheduler_run',dict(started=started,finished=iso(),pid=os.getpid(),state=result['state']))
    except Exception as exc:
        workflow.set_meta('last_scheduler_failure',dict(started=started,finished=iso(),error_type=type(exc).__name__))
        report(config,workflow,ok=False,error_type=type(exc).__name__)
        print('NO_REPLY')
        raise SystemExit(1)
    report(config,workflow,ok=True)
    print('NO_REPLY')
