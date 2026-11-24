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


def preparing(config):
    return config.get('preparation_enabled',True) is not False


def load(config_path=None):
    path=Path(config_path) if config_path else Path(__file__).with_name('config.json')
    # utf-8-sig: Windows PowerShell 5.1 and some editors save config.json with a BOM.
    config=json.loads(path.read_text(encoding='utf-8-sig'))
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
        health_alerts=workflow.get_meta('health_alerted',[]),scheduler_failures=workflow.get_meta('scheduler_failure_count',0),
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
        # Automatic study materials are optional; when off, no cards are opened or verified.
        if preparing(config):
            recovered=workflow.recover_workers(native)
            reconciled=workflow.reconcile(native,verify_with(model))
            dispatched=workflow.dispatch(native,limit=config.get('max_active_preparations',2))
        else:recovered=reconciled=dispatched=[]
        export=export_agenda(workflow,native=native,preparation=preparing(config))
        result=dict(state='ok',at=iso(),interpretation=interpreted,recovery=recovered,reconciled=reconciled,dispatched=dispatched,agenda_path=str(export))
        # Publishing is enabled only after installation and isolated contract verification.
        if config.get('delivery_enabled'):
            from publisher import prepare_digest,prepare_health,TelegramTransport
            result['digest']=prepare_digest(workflow,config,native=native)
            result['health']=prepare_health(workflow,config,recovery=recovered)
            result['publication']=workflow.publish(TelegramTransport(config),kinds={'digest','health'},limit=2)
        workflow.set_meta('last_tick',result)
        return result


# Phrases that ask for tables. A phrase preceded by a negation ("不要用表格、│ 分欄…") is the replacement rule.
TABLE_INSTRUCTION=r'三個反引號包住|反引號包住等寬|│ ?分欄|偏好少字和表格|小表|用表格'
NEGATION=r'不要|不用|禁止|避免|別用'


def asks_for_table(text):
    import re
    # The window includes the match itself so "不用表格" (the 用 belongs to the match) counts as negated.
    return any(not re.search(NEGATION,text[max(0,m.start()-12):m.end()]) for m in re.finditer(TABLE_INSTRUCTION,text))


def table_instructions(root):
    """Instructions that make a Telegram bot answer with tables (unreadable on a phone).

    Checked: the root SOUL.md (template for new bots) and, for every profile with a Telegram
    bot, its SOUL.md, its skills and its scheduled-job prompts (a schedule prompt can carry its
    own format rule, as the Mail Assistant's morning digest did)."""
    root=Path(root)
    def telegram_bot(profile):
        try:return any(line.startswith('TELEGRAM_BOT_TOKEN=') for line in (profile/'.env').read_text(encoding='utf-8').splitlines())
        except OSError:return False
    def read(path):
        try:return path.read_text(encoding='utf-8-sig')
        except OSError:return ''
    found=[]
    texts=[('SOUL.md',read(root/'SOUL.md'))]
    for profile in sorted(p for p in root.glob('profiles/*') if telegram_bot(p)):
        texts+=[(f.relative_to(root).as_posix(),read(f)) for f in [profile/'SOUL.md']+sorted(profile.glob('skills/**/SKILL.md'))]
        try:jobs=json.loads(read(profile/'cron/jobs.json') or '[]')
        except ValueError:jobs=[]
        if isinstance(jobs,dict):jobs=jobs.get('jobs',[])
        texts+=[(profile.relative_to(root).as_posix()+'/cron/'+str(j.get('id')),j.get('prompt') or '') for j in jobs
            if isinstance(j,dict) and j.get('enabled',True)]
    return [name for name,text in texts if asks_for_table(text)]


def delivery_problem(workflow,*,now=None,hours=48):
    """A message when every Telegram send in the last `hours` failed, else None.
    A healthy probe from a shell once hid three days of failed digests, so judge by real sends."""
    since=(datetime.fromisoformat(iso(now))-timedelta(hours=hours)).isoformat()
    recent=[r for r in workflow.delivery_receipts() if str(r.get('updated_at') or '')>=since]
    failed=[r for r in recent if r['state']=='failed']
    if not recent or len(failed)<len(recent):return None
    last=max(failed,key=lambda r:str(r.get('updated_at') or ''))['receipt'] or {}
    return '最近 '+str(hours)+' 小時 '+str(len(failed))+' 次送出全部失敗（'+str(last.get('detail') or last.get('error_type') or '原因未記錄')+'）'


def doctor(config,workflow,*,probe=True):
    """Check every local dependency the scheduler needs and report what to fix."""
    import subprocess
    checks=[]
    def check(name,ok,fix='',required=True):checks.append(dict(name=name,ok=bool(ok),fix='' if ok else fix,required=required))
    for key in ('hermes_root','runtime','tronclass_repo','mailbox','mail_python','runtime_python'):
        value=str(config.get(key,''))
        check('path:'+key,value and 'path/to' not in value and Path(value).exists(),'在 config.json 把 '+key+' 改成本機實際路徑')
    from native import resolve_payload
    try:
        payload=resolve_payload(str(config.get('hermes_payload','')))
        check('hermes_payload',True)
        check('hermes_payload_current',Path(str(config.get('hermes_payload','')))==payload,
            'Hermes App 已更新，目前自動改用 '+payload.as_posix()+'；可把 config.json 的 hermes_payload 改成這個路徑',required=False)
    except FileNotFoundError:check('hermes_payload',False,'找不到 Hermes 桌面版的 agent-payload；在 config.json 設定 hermes_payload')
    root=Path(config.get('hermes_root',''))
    check('tronclass_python',(Path(config.get('tronclass_repo',''))/'.venv/Scripts/python.exe').is_file(),'在 FJU-TronClass-MCP 建立 .venv 並完成登入')
    check('kanban_board',(root/'kanban/boards/command-center/kanban.db').is_file(),'執行 hermes kanban boards create command-center')
    for profile in ('commander','hermes-agent'):
        path=root/'profiles'/profile/'config.yaml'
        text=path.read_text(encoding='utf-8') if path.is_file() else ''
        check('plugin_enabled:'+profile,'user-academic-workflow' in text,'在 profiles/'+profile+'/config.yaml 的 plugins.enabled 加入 user-academic-workflow')
        check('plugin_installed:'+profile,(root/'profiles'/profile/'plugins/user-academic-workflow/__init__.py').is_file(),'執行 install.py 複製外掛')
        if profile=='commander':check('cron_timeout','script_timeout_seconds' in text,'在 commander config.yaml 設定 cron.script_timeout_seconds: 1500',required=False)
    for soul in table_instructions(root):
        check('phone_format:'+soul,False,soul+' 要求 Telegram 用表格或程式碼區塊；手機上會折行變亂，改成每項一行的短條列',required=False)
    try:
        subprocess.run([config['runtime_python'],'-c','import pydantic,openai,jiter,psutil'],check=True,capture_output=True,timeout=60,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        check('runtime_modules',True)
    except Exception:check('runtime_modules',False,'在 runtime_python 安裝 pydantic openai jiter psutil')
    last=workflow.get_meta('last_tick') or {}
    fresh=last.get('at') and datetime.fromisoformat(iso())-datetime.fromisoformat(last['at'])<timedelta(minutes=20)
    check('scheduler_recent',fresh,'排程超過 20 分鐘沒有完成；用 hermes -p commander cron list --all 確認已 resume',required=False)
    failures=workflow.get_meta('scheduler_failure_count',0) or 0
    check('scheduler_failures',not failures,'最近排程失敗 '+str(failures)+' 次：'+str((workflow.get_meta('last_scheduler_failure') or {}).get('error_type')),required=False)
    for source in workflow.sources():
        if source['scope']=='collector' or source['source']=='mail:gmail':
            check('source:'+source['source']+':'+source['scope'],source['complete'],'來源錯誤：'+','.join(map(str,source['errors'])),required=False)
    check('delivery_enabled',config.get('delivery_enabled'),'Telegram 送達尚未啟用；確認 telegram-test 成功後把 delivery_enabled 改為 true',required=False)
    problem=delivery_problem(workflow)
    if config.get('delivery_enabled'):
        check('delivery_recent',not problem,(problem or '')+'；修好後執行 academic.py telegram-test',required=False)
    from native import stale_gateways
    stale=stale_gateways()
    check('gateway_current',not stale,'機器人還在跑舊版 Hermes（'+'、'.join(stale)+'）；執行 hermes gateway restart',required=False)
    from native import plugin_newer_than_gateway
    changed=plugin_newer_than_gateway(Path(__file__).parent)
    check('plugin_current',not changed,'學業程式（'+'、'.join(changed)+'）在機器人啟動後改過，機器人還在用舊版；執行 hermes gateway restart（不要加 -p，單一機器人重啟不會重新載入）',required=False)
    if probe and config.get('delivery_enabled'):
        # The scheduler runs this code AS commander with a secret-free environment; probing from a
        # shell succeeds even when that setup cannot find the bot token, so re-check in that setup.
        env={k:v for k,v in os.environ.items() if not k.upper().startswith('TELEGRAM_')}
        env['HERMES_HOME']=str(root/'profiles/commander')
        code=('import sys;sys.path.insert(0,sys.argv[1]);from academic import load;from publisher import TelegramTransport;'
            'c,_=load(sys.argv[2]);TelegramTransport(c).credentials()')
        try:
            subprocess.run([config['runtime_python'],'-c',code,str(config['runtime']),str(Path(config['runtime'])/'config.json')],
                check=True,capture_output=True,timeout=120,env=env,cwd=str(config['runtime']),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            check('telegram_scheduled',True)
        except Exception:check('telegram_scheduled',False,'排程執行時讀不到 commander 的 TELEGRAM_BOT_TOKEN 或 home_channel；確認 profiles/commander/.env 與 config.yaml',required=False)
    if probe:
        try:
            from publisher import TelegramTransport
            TelegramTransport(config).probe();check('telegram',True)
        except Exception as exc:check('telegram',False,'Telegram 無法驗證（'+type(exc).__name__+'）：在 commander 設定 TELEGRAM_BOT_TOKEN 與私人 home_channel.chat_id',required=bool(config.get('delivery_enabled')))
    return dict(ok=all(c['ok'] for c in checks if c['required']),checks=checks)


def telegram_test(config):
    from publisher import TelegramTransport
    transport=TelegramTransport(config)
    identity=transport.probe()
    receipt=transport.send_text('✅ 學業工作流 Telegram 測試訊息。收到這則代表摘要與附件可以送達這個私人對話。')
    return dict(identity,message_id=receipt['message_id'])


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['status','tick','collect','interpret','dispatch','reconcile','export','submit','import-school','import-mail','doctor','telegram-test'])
    parser.add_argument('--config');parser.add_argument('--file');parser.add_argument('--task-id')
    parser.add_argument('--no-probe',action='store_true',help='doctor: skip the Telegram API check')
    args=parser.parse_args()
    config,workflow=load(args.config)
    if args.action=='status':result=status(config,workflow)
    elif args.action=='doctor':
        result=doctor(config,workflow,probe=not args.no_probe)
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
        raise SystemExit(0 if result['ok'] else 1)
    elif args.action=='telegram-test':result=telegram_test(config)
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
