"""Supported command interface for the installed academic coordinator."""
from pathlib import Path
from datetime import datetime,timedelta
import argparse
import json
import os
import sys
from workflow import AcademicWorkflow,iso,digest
from native import NativeTasks,Model,commander_scope
from operations import collect_sources,import_school,import_mail,interpret_pending,verify_with,export_agenda


def load(config_path=None):
    path=Path(config_path) if config_path else Path(__file__).with_name('config.json')
    config=json.loads(path.read_text(encoding='utf-8'))
    return config,AcademicWorkflow(config['state_root'])


def submit(config,workflow,task_id):
    native=NativeTasks(config);task=native.get(task_id)
    if not task or task['created_by']!='academic-workflow':raise ValueError('Not an academic task')
    workspace=Path(task['workspace_path']).resolve()
    source=json.loads((workspace/'source.json').read_text(encoding='utf-8'))
    manifest,files=workflow.read_package(source,task)
    ok=native.submit(task_id,[f['path'] for f in files],str(manifest.get('summary') or '學習產物待獨立驗證。'))
    return {'ok':ok,'task_id':task_id,'status':'review' if ok else 'unchanged','notification':'internal_only'}


def status(config,workflow):
    native=NativeTasks(config)
    agenda=workflow.agenda()
    return dict(agenda=[{k:x.get(k) for k in ('key','revision','course','title','due_at','human_state','next_action','start_by','planning_status')} for x in agenda],
        sources=workflow.sources(),preparations=[dict(x,native_status=(native.get(x['task_id']) or {}).get('status')) for x in workflow.preparations() if x['task_id']],
        delivery=[dict(key=x['key'],state=x['state'],title=x['payload']['title']) for x in workflow.publications()],
        agenda_path=str(workflow.root/'exports/學業總覽.md'),last_tick=workflow.get_meta('last_tick'),
        calendar='local_provisional; authenticated calendar not verified')


def tick(config,workflow,*,collect=False):
    with commander_scope(config):
        from agent.estop import is_engaged
        if is_engaged():return {'state':'paused_by_native_estop'}
    with workflow.lease('tick',seconds=1800) as acquired:
        if not acquired:return {'state':'another_tick_active'}
        last=workflow.get_meta('last_collection_at')
        due=not last or datetime.fromisoformat(iso())-datetime.fromisoformat(last)>=timedelta(minutes=config.get('collection_minutes',30))
        if collect or due:collect_sources(workflow,config)
        model=Model(config);native=NativeTasks(config)
        interpreted=interpret_pending(workflow,model)
        recovered=workflow.recover_workers(native)
        reconciled=workflow.reconcile(native,verify_with(model))
        dispatched=workflow.dispatch(native,limit=config.get('max_active_preparations',2))
        export=export_agenda(workflow,native=native)
        result=dict(state='ok',at=iso(),interpretation=interpreted,recovery=recovered,reconciled=reconciled,dispatched=dispatched,agenda_path=str(export))
        # Publishing is enabled only after installation and isolated contract verification.
        if config.get('delivery_enabled'):
            from publisher import prepare_digest,TelegramTransport
            digest_result=prepare_digest(workflow,config,native=native)
            result['digest']=digest_result
            result['publication']=workflow.publish(TelegramTransport(config),kinds={'digest'})
        workflow.set_meta('last_tick',result)
        return result


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['status','tick','collect','interpret','dispatch','reconcile','export','submit','import-school','import-mail'])
    parser.add_argument('--config');parser.add_argument('--file');parser.add_argument('--task-id')
    args=parser.parse_args()
    config,workflow=load(args.config)
    if args.action=='status':result=status(config,workflow)
    elif args.action=='submit':result=submit(config,workflow,args.task_id)
    elif args.action=='import-school':import_school(workflow,json.loads(Path(args.file).read_text(encoding='utf-8-sig')));result={'ok':True}
    elif args.action=='import-mail':import_mail(workflow,json.loads(Path(args.file).read_text(encoding='utf-8-sig')));result={'ok':True}
    elif args.action=='collect':collect_sources(workflow,config);result={'sources':workflow.sources()}
    elif args.action=='interpret':result=interpret_pending(workflow,Model(config))
    elif args.action=='dispatch':result=workflow.dispatch(NativeTasks(config),limit=config.get('max_active_preparations',2))
    elif args.action=='reconcile':result=workflow.reconcile(NativeTasks(config),verify_with(Model(config)))
    elif args.action=='export':result={'path':str(export_agenda(workflow,native=NativeTasks(config)))}
    else:result=tick(config,workflow)
    print(json.dumps(result,ensure_ascii=False,default=str))


if __name__=='__main__':main()
