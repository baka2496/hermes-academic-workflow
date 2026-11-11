"""Install or update the academic workflow inside a Hermes data directory.

    py -3 install.py --hermes-home C:/path/to/hermes-home [--dry-run] [--force] [--create-cron]

Files that differ from the installed copy are left alone unless --force is given; then the
installed copy is kept as <name>.bak-<timestamp> first. config.json is created once and never
overwritten.
"""
from datetime import datetime
from pathlib import Path
import argparse
import filecmp
import json
import os
import shutil
import subprocess
import sys

REPO=Path(__file__).resolve().parent
MAPPING=[
    ('hermes/shared/academic-workflow','shared/academic-workflow'),
    ('hermes/shared/skills/academic-workflow','shared/skills/academic-workflow'),
    ('hermes/profiles/commander/scripts/academic-coordinator.py','profiles/commander/scripts/academic-coordinator.py'),
    ('hermes/profiles/commander/plugins/user-academic-workflow','profiles/commander/plugins/user-academic-workflow'),
    ('hermes/profiles/commander/plugins/user-academic-workflow','profiles/hermes-agent/plugins/user-academic-workflow'),
]
SKIP={'__pycache__','config.json'}


def files(source):
    if source.is_file():yield source,Path();return
    for path in sorted(source.rglob('*')):
        if path.is_file() and not SKIP&set(path.relative_to(source).parts) and path.suffix not in {'.pyc','.pyo'}:
            yield path,path.relative_to(source)


def install(home,*,dry_run=False,force=False):
    report=dict(copied=[],unchanged=[],conflicts=[],backed_up=[])
    stamp=datetime.now().strftime('%Y%m%d%H%M%S')
    for source,target in MAPPING:
        for path,relative in files(REPO/source):
            destination=home/target/relative if relative.parts else home/target
            if destination.exists() and filecmp.cmp(path,destination,shallow=False):
                report['unchanged'].append(str(destination));continue
            if destination.exists() and not force:
                report['conflicts'].append(str(destination));continue
            if not dry_run:
                destination.parent.mkdir(parents=True,exist_ok=True)
                if destination.exists():
                    backup=destination.with_name(destination.name+'.bak-'+stamp)
                    shutil.copy2(destination,backup);report['backed_up'].append(str(backup))
                shutil.copy2(path,destination)
            report['copied'].append(str(destination))
    config=home/'shared/academic-workflow/config.json'
    if not config.exists():
        values=json.loads((REPO/'hermes/shared/academic-workflow/config.example.json').read_text(encoding='utf-8'))
        values.update(hermes_root=home.as_posix(),runtime=(home/'shared/academic-workflow').as_posix(),
            state_root=(home/'shared/academic-state').as_posix())
        if not dry_run:
            config.parent.mkdir(parents=True,exist_ok=True)
            config.write_text(json.dumps(values,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        report['config_created']=str(config)
    return report


def yaml_gaps(home):
    gaps=[]
    for profile in ('commander','hermes-agent'):
        path=home/'profiles'/profile/'config.yaml'
        text=path.read_text(encoding='utf-8') if path.is_file() else ''
        if 'user-academic-workflow' not in text:gaps.append(profile+': plugins.enabled 缺少 user-academic-workflow')
        if (home/'shared/skills').as_posix() not in text.replace('\\','/'):gaps.append(profile+': skills.external_dirs 缺少 '+(home/'shared/skills').as_posix())
        if profile=='commander' and 'script_timeout_seconds' not in text:gaps.append('commander: 缺少 cron.script_timeout_seconds: 1500')
    return gaps


def create_cron(home):
    config=json.loads((home/'shared/academic-workflow/config.json').read_text(encoding='utf-8'))
    if 'path/to' in config.get('runtime_python',''):return 'skipped: 先在 config.json 填好 runtime_python'
    env=dict(os.environ,HERMES_HOME=str(home))
    listing=subprocess.run(['hermes','-p','commander','cron','list','--all'],env=env,capture_output=True,text=True,encoding='utf-8')
    if listing.returncode:return 'skipped: hermes cron list 失敗：'+(listing.stderr or '').strip()[:200]
    if 'academic-coordinator.py' in listing.stdout:return 'exists: 已有 academic-coordinator.py 排程'
    created=subprocess.run(['hermes','-p','commander','cron','create','*/5 * * * *','--name','小居學業工作流',
        '--script','academic-coordinator.py','--no-agent','--deliver','local','--failure-deliver','local',
        '--interpreter',config['runtime_python'],'--workdir',config['runtime'],
        '--timezone','Asia/Taipei','--catch-up','off','--paused'],env=env,capture_output=True,text=True,encoding='utf-8')
    return ('created (paused): '+created.stdout.strip()) if created.returncode==0 else 'failed: '+(created.stderr or '').strip()[:200]


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--hermes-home',default=os.environ.get('HERMES_HOME'))
    parser.add_argument('--dry-run',action='store_true');parser.add_argument('--force',action='store_true')
    parser.add_argument('--create-cron',action='store_true',help='若尚無排程，建立暫停中的五分鐘排程')
    args=parser.parse_args()
    if not args.hermes_home:parser.error('請用 --hermes-home 或 HERMES_HOME 指定 Hermes 資料目錄')
    home=Path(args.hermes_home).resolve()
    if not (home/'profiles/commander').is_dir():parser.error('找不到 '+str(home/'profiles/commander')+'；先建立 commander profile')
    result=install(home,dry_run=args.dry_run,force=args.force)
    result['config_yaml_todo']=yaml_gaps(home)
    if args.create_cron and not args.dry_run:result['cron']=create_cron(home)
    result['next']=['填好 shared/academic-workflow/config.json 的本機路徑' if result.get('config_created') else None,
        '執行 academic.py doctor 檢查設定','執行 academic.py telegram-test 確認 Telegram 送達，再把 delivery_enabled 改為 true',
        'hermes -p commander cron resume <排程 ID> 啟用排程']
    result['next']=[x for x in result['next'] if x]
    summary={k:(len(v) if k in {'copied','unchanged','backed_up'} else v) for k,v in result.items()}
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    raise SystemExit(1 if result['conflicts'] else 0)


if __name__=='__main__':main()
