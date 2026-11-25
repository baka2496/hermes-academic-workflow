"""Everything the owner has going on, built straight from the source records.

Bots answer "目前有什麼事情" by relaying this output instead of picking items from memory, so an
answer can never silently drop an item or change a date (a model with less reasoning dropped
pending items; a model with more reasoning dropped others). Output is phone-formatted: one item
per line, no tables.

    python brief.py [--config config.json] [--days 14] [--json]
"""
from datetime import datetime,timedelta
from pathlib import Path
import argparse
import json
import re
import sys

WEEKDAY='一二三四五六日'
DONE_TRIP={'已完成','已取消','已結束','已放棄'}


def when(due,clock):
    # TronClass writes "end of 12/01" as 12/02 00:00. The daily digest says 12/01 24:00; this list
    # said 12/02, so the same homework showed two different days (11/25).
    if due.strftime('%H:%M')=='00:00':
        end=due-timedelta(minutes=1)
        if end.date()==clock.date():return '今天24:00'
        if end.date()==(clock+timedelta(days=1)).date():return '明天24:00'
        return end.strftime('%m/%d')+'（'+WEEKDAY[end.weekday()]+'）24:00'
    day=due.strftime('%m/%d')
    if due.date()==clock.date():return '今天'+due.strftime('%H:%M')
    if due.date()==(clock+timedelta(days=1)).date():return '明天'+due.strftime('%H:%M')
    return day+'（'+WEEKDAY[due.weekday()]+'）'


def clean(text):
    # 11/25 the owner: titles cut to 12 characters with "…" ("國際關係與亞太現勢 國關…" twice) hid what
    # each item was. Telegram wraps long lines on the phone, so titles are shown in full.
    return re.sub(r'\s+',' ',re.sub(r'[【】\[\]「」"“”]','',text or '')).strip()


def academic(workflow,clock,days):
    shown,later,undated,overdue=[],0,0,[]
    for item in workflow.agenda(now=clock.isoformat()):
        if item.get('human_state')!='open':
            if item.get('human_state')=='overdue' and item.get('due_at'):overdue.append(item)
            continue
        if not item.get('due_at'):undated+=1;continue
        due=datetime.fromisoformat(item['due_at'])
        if due-clock>timedelta(days=days):later+=1;continue
        course=re.sub(r'[（(][^）)]*[）)]?$','',item.get('course') or '').strip()
        title=re.sub(r'[【】\[\]「」"“”]','',item.get('title','')).replace(course,'').strip() if course else item.get('title','')
        # Same rule as the overview table: a date read out of an announcement still needs checking.
        shown.append(dict(due=due,name=clean((course+' ' if course else '')+title),when=when(due,clock),
            tentative=item.get('confidence')=='needs_check' or bool(item.get('deadline_conflicts'))))
    return sorted(shown,key=lambda x:x['due']),later,undated


def dates_in(text):
    return set(re.findall(r'20\d\d-\d\d-\d\d',text or ''))


def trips(path,clock):
    try:records=json.loads(Path(path).read_text(encoding='utf-8')).get('opportunities',[])
    except (OSError,ValueError):return None
    result=[]
    for record in records:
        decision=record.get('user_decision')
        decision=decision.get('status') if isinstance(decision,dict) else decision
        if decision!='pending' and record.get('status')!='已選定':continue
        if record.get('status') in DONE_TRIP:continue
        start,end=record.get('outbound_date'),record.get('return_date')
        if end and end<clock.date().isoformat():continue
        span=(start[5:].replace('-','/')+('–'+end[5:].replace('-','/') if end and end!=start else '')) if start else '日期未定'
        problems=[]
        # A calendar hold or venue that does not match the trip's own dates means the record contradicts
        # itself (e.g. an overnight trip with a one-day hold): say so instead of picking one version.
        hold=(record.get('calendar_hold_check') or {}).get('details','')
        if start and dates_in(hold) and not dates_in(hold)&{start,end}:problems.append('日期')
        elif start and end and dates_in(hold) and start!=end and dates_in(hold)=={end}:problems.append('天數')
        venue=(record.get('venue_research') or {}).get('venue','')
        place=re.match(r'([^\s（(]{2})',venue)
        if place and place.group(1) not in (record.get('destination','')+record.get('title','')):problems.append('地點')
        status=record.get('status','')
        label={'不符合':'不符合條件，待你決定'}.get(status,status)
        title=re.sub(r'（.*?）|規劃|\(.*?\)','',record.get('title','')).strip()
        result.append(dict(id=record.get('id'),name=clean(title),when=span,status=label,problems=problems))
    return result


def build(config,workflow,*,now=None,days=14):
    clock=datetime.fromisoformat(now) if now else datetime.now().astimezone()
    items,later,undated=academic(workflow,clock,days)
    lines=['📚 學業（'+str(days)+' 天內）']
    # Name on its own line, date under it: a long name wraps without pushing the date off screen.
    for x in items:lines+=['• '+x['name'],'  '+x['when']+('＊' if x['tentative'] else '')]
    if not items:lines.append('• 沒有')
    extra=[]
    if later:extra.append(str(later)+' 項較晚')
    if undated:extra.append(str(undated)+' 項日期待確認')
    if extra:lines.append('＋另有 '+'、'.join(extra))
    if any(x['tentative'] for x in items):lines.append('＊日期由公告推導，待核對')
    trip_path=config.get('trip_opportunities') or str(Path(config.get('hermes_root',''))/'shared/travel-workflow/opportunities.json')
    found=trips(trip_path,clock)
    lines.append('🧳 旅遊')
    if found is None:lines.append('• 旅遊紀錄讀不到（未確認）')
    else:
        for x in found:
            lines.append('• '+x['name'])
            lines.append('  '+x['when']+'｜'+x['status']+('｜⚠️紀錄矛盾：'+'、'.join(x['problems']) if x['problems'] else ''))
        if not found:lines.append('• 沒有進行中的旅遊')
    return dict(text='\n'.join(lines),academic=[{k:v for k,v in x.items() if k!='due'} for x in items],
        academic_later=later,academic_undated=undated,trips=found,generated_at=clock.isoformat(timespec='minutes'))


def bare(name):
    return re.sub(r'[（(][^）)]*[）)]?$','',name or '').strip().lower()


def course(workflow,name,*,now=None,announcements=3):
    """One course's latest announcements and open deadlines, from the locally collected TronClass data.
    "體育課在哪裡上課" took 3.5 minutes of searching files and logs; the answer was already stored here."""
    clock=datetime.fromisoformat(now) if now else datetime.now().astimezone()
    wanted=bare(re.sub(r'課$','',(name or '').strip()))
    items=workflow.inbox_items()
    raw={x.get('key'):x for x in items}
    names=sorted({x.get('course') for x in items if x.get('course')})
    matched=[n for n in names if wanted and (wanted in bare(n) or bare(n) in wanted)]
    if not matched:
        return dict(text='找不到「'+(name or '')+'」這門課。目前的課：'+'、'.join(names),matched=[])
    lines=[]
    checked=max((s['observed_at'] for s in workflow.sources() if s['source']=='tronclass'),default='')
    for title in matched:
        lines.append('🏫 '+title+('（資料 '+checked[11:16]+' 更新）' if checked else ''))
        posts=sorted((x for x in items if x.get('course')==title and x.get('kind')=='announcement' and not x.get('closed')),
            key=lambda x:x.get('published_at') or '',reverse=True)[:announcements]
        lines.append('📢 最新公告' if posts else '📢 沒有公告')
        for post in posts:
            day=datetime.fromisoformat(post['published_at']) if post.get('published_at') else None
            stamp='今天' if day and day.date()==clock.date() else day.strftime('%m/%d') if day else '日期未知'
            lines.append('• '+stamp+' '+clean(post.get('title'))+'：'+re.sub(r'\s+',' ',post.get('description') or '').strip()[:120])
        open_items=[x for x in workflow.agenda(now=clock.isoformat()) if x.get('course')==title and x.get('human_state')=='open']
        if open_items:
            lines.append('📝 待辦')
            for item in sorted(open_items,key=lambda x:x.get('due_at') or '9999')[:5]:
                due=datetime.fromisoformat(item['due_at']) if item.get('due_at') else None
                lines+=['• '+clean(item.get('title')),'  '+(when(due,clock) if due else '日期待確認')]
                # The question sheet of "HW#3" is its PDF; without the name the bot went hunting for it.
                for file in files_of(raw.get(item['key'],{})):lines.append('  📎 '+file['name'])
    return dict(text='\n'.join(lines),matched=matched,checked_at=checked)


def files_of(item):
    return [x for x in ((item.get('source_requirements') or {}).get('attachments') or [])
        if isinstance(x,dict) and x.get('id') and x.get('name') and not x.get('deleted')]


def cli_env(base=None):
    """Environment for the TronClass CLI's own interpreter. Inside the gateway PYTHONPATH points at
    Hermes's site-packages, and the CLI then crashed importing Hermes's pydantic_core (11/18: every
    download from Telegram failed while the same command worked from a shell)."""
    import os
    env={k:v for k,v in (os.environ if base is None else base).items()
        if k.upper() not in {'PYTHONPATH','PYTHONHOME','VIRTUAL_ENV','PYTHONSTARTUP','PYTHONNOUSERSITE'}}
    env.update(PYTHONIOENCODING='utf-8',PYTHONUTF8='1',NO_COLOR='1',TERM='dumb')
    return env


def attachment(config,workflow,name,title='',*,run=None,now=None):
    """Download a course item's TronClass attachments (a homework's question sheet, lecture notes) and
    return their local paths. "傳題目給我" took over five minutes of searching files and a failed hand-off
    although the file name was already stored; the TronClass CLI fetches it in about three seconds."""
    import subprocess
    found=course(workflow,name,now=now)
    if not found['matched']:return dict(text=found['text'],files=[])
    candidates=[x for x in workflow.inbox_items() if x.get('course') in found['matched'] and files_of(x) and not x.get('closed')]
    wanted=re.sub(r'[\s#＃【】「」]','',title or '').lower()
    norm=lambda text:re.sub(r'[\s#＃【】「」]','',text or '').lower()
    if wanted:
        candidates=[x for x in candidates if wanted in norm((x.get('title') or '')+' '.join(f['name'] for f in files_of(x)))]
        # "20260923 上課講義" also matches "20260923 上課講義 勘誤": the exact title, else the shortest, wins.
        candidates.sort(key=lambda x:(norm(x.get('title'))!=wanted,len(norm(x.get('title')))))
    else:
        # No title: the open homework due first, else the newest post with files.
        open_keys={x['key'] for x in workflow.agenda(now=now) if x.get('human_state')=='open'}
        homework=sorted((x for x in candidates if x.get('kind')=='homework' and x.get('key') in open_keys),key=lambda x:x.get('due_at') or '9999')
        candidates=homework[:1] or sorted(candidates,key=lambda x:x.get('published_at') or '',reverse=True)[:1]
    if not candidates:
        return dict(text='「'+(name or '')+(' '+title if title else '')+'」在 TronClass 紀錄裡沒有附件（未確認是否只在課堂發放）。',files=[])
    item=candidates[0]
    folder=Path(config.get('state_dir') or workflow.root)/'attachments'/re.sub(r'[^0-9A-Za-z_-]','_',item.get('id') or item['key'])
    folder.mkdir(parents=True,exist_ok=True)
    cli=Path(config['tronclass_repo'])/'.venv/Scripts/fjumcp.exe'
    # The CLI prints "✓" with rich; under a cp950 console that crashes unless output is UTF-8.
    run=run or (lambda args:subprocess.run(args,capture_output=True,timeout=90,cwd=str(cli.parent.parent.parent),
        env=cli_env(),
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)))
    errors=[]
    files,failed=[],[]
    for file in files_of(item):
        target=folder/Path(file['name']).name
        if not (target.exists() and (not file.get('size') or target.stat().st_size==file['size'])):
            try:
                done=run([str(cli),'download','upload',str(file['id']),'-d',str(folder)])
                code=getattr(done,'returncode',0)
                if code:errors.append(file['name']+': exit '+str(code)+' '+(getattr(done,'stderr',b'') or b'')[-300:].decode('utf-8','replace'))
            except Exception as exc:errors.append(file['name']+': '+type(exc).__name__+' '+str(exc)[:200])
        if target.exists() and target.stat().st_size>0:files.append(str(target))
        else:failed.append(file['name'])
    lines=['📎 '+(item.get('course') or '')+' '+(item.get('title') or '')+'：'+str(len(files))+' 個檔案']
    if failed:lines.append('⚠️ 下載失敗：'+'、'.join(failed)+'（TronClass 登入可能過期）')
    if failed and errors:
        import logging;logging.getLogger(__name__).warning('attachment download failed: %s',' | '.join(errors))
    return dict(text='\n'.join(lines),files=files,failed=failed,errors=errors if failed else [],item=item['key'],
        reply='回覆時每個檔案單獨一行寫 MEDIA:檔案路徑，檔案就會傳到 Telegram。')


DRAFT_NOTE='⚠️ 只存成草稿，還沒正式繳交：請到 TronClass 作業頁確認檔案後按「繳交作業」。'


def draft(config,workflow,name,title,paths,*,roots=(),run=None,now=None):
    """Save photos the owner sent as the draft of an open homework.

    11/19: the owner sent a photo of a finished derivation and asked to hand it in. The TronClass
    tool only saves drafts by design (the owner presses 「繳交作業」 on the page), so this never submits."""
    import subprocess
    sys.path.insert(0,str(Path(__file__).parent))
    import vision
    paths=[p for p in (paths or []) if p]
    if not paths:return dict(error='no_files',text='沒有要上傳的圖片或檔案路徑。')
    bad=[p for p in paths if not vision.allowed(p,roots) and not allowed_file(p,roots)]
    if bad:return dict(error='file_not_allowed',text='只能上傳本人剛傳給機器人的檔案：'+'、'.join(Path(p).name for p in bad))
    found=course(workflow,name,now=now)
    if not found['matched']:return dict(error='course_not_found',text=found['text'])
    now=datetime.fromisoformat(now) if isinstance(now,str) else (now or datetime.now().astimezone())
    wanted=re.sub(r'[\s#＃【】「」]','',title or '').lower()
    norm=lambda text:re.sub(r'[\s#＃【】「」]','',text or '').lower()
    homework=[x for x in workflow.inbox_items() if x.get('course') in found['matched'] and x.get('kind')=='homework'
              and not x.get('closed') and not x.get('submitted') and re.match(r'homework:\d+:\d+$',x.get('id') or '')]
    if wanted:homework=[x for x in homework if wanted in norm(x.get('title'))]
    homework.sort(key=lambda x:(norm(x.get('title'))!=wanted,x.get('due_at') or '9999'))
    if not homework:
        return dict(error='homework_not_found',text='「'+(name or '')+' '+(title or '')+'」找不到還沒交的 TronClass 作業，未上傳。')
    if not wanted and len(homework)>1:
        return dict(error='ambiguous',text='這門課有 '+str(len(homework))+' 份未交作業，要交哪一份？'+'、'.join(x.get('title') or '' for x in homework))
    item=homework[0]
    due=datetime.fromisoformat(item['due_at']).astimezone(now.tzinfo) if item.get('due_at') else None
    if due and due<now:
        return dict(error='past_due',text=(item.get('course') or '')+' '+(item.get('title') or '')+' 已過截止時間，未上傳；請到 TronClass 確認是否還能補交。')
    activity=int(item['id'].split(':')[2])
    # Telegram names photos img_920d149bd7b2.jpg; the teacher sees the file name, so upload copies
    # named after the homework (HW3_1.jpg, HW3_2.jpg …).
    import shutil
    stem=re.sub(r'[^0-9A-Za-z一-鿿_-]+','',item.get('title') or 'homework') or 'homework'
    folder=Path(config.get('state_dir') or workflow.root)/'drafts'/str(activity)/datetime.now().strftime('%Y%m%d-%H%M%S')
    folder.mkdir(parents=True,exist_ok=True)
    named=[]
    for n,p in enumerate(paths,1):
        target=folder/(stem+('_'+str(n) if len(paths)>1 else '')+Path(p).suffix.lower())
        shutil.copyfile(p,target);named.append(target)
    repo=Path(config['tronclass_repo'])
    python=repo/'.venv/Scripts/python.exe'
    script=Path(__file__).with_name('tronclass_draft.py')
    run=run or (lambda args:subprocess.run(args,capture_output=True,timeout=240,cwd=str(repo),env=cli_env(),
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)))
    try:
        done=run([str(python),'-X','utf8',str(script),str(repo),str(activity)]+[str(p) for p in named])
        out=done.stdout.decode('utf-8','replace') if isinstance(done.stdout,bytes) else str(done.stdout or '')
        result=json.loads(out.strip().splitlines()[-1])
    except Exception as exc:result=dict(ok=False,error=type(exc).__name__,message=str(exc)[:200])
    label=(item.get('course') or '')+' '+(item.get('title') or '')
    if not result.get('ok'):
        import logging;logging.getLogger(__name__).warning('draft upload failed: %s',result)
        return dict(error='upload_failed',text='❌ '+label+' 草稿上傳失敗（'+str(result.get('message') or result.get('error'))[:120]+'），TronClass 上沒有變更或只上傳了一部分，請到作業頁確認。')
    if not result.get('added') and result.get('already'):
        # A resent photo already in the draft is not uploaded again (it would become HW3(1).jpg …).
        head='✅ '+label+' 的草稿裡已經有這張，沒有重複上傳'
    else:head=('✅ 已存成 '+label+' 的草稿（'+str(len(result.get('added') or []))+' 個檔案'
           +('，保留原本草稿的 '+str(result['kept'])+' 個' if result.get('kept') else '')
           +('，換掉內容相同的舊檔 '+'、'.join(result['replaced']) if result.get('replaced') else '')+'）')
    lines=[head,
           # TronClass renames a clash with the owner's file storage (HW3.jpg -> HW3(1).jpg); show what it kept.
           *(['草稿裡的檔案：'+'、'.join(result['draft_files'])] if result.get('draft_files') else []),
           '截止：'+(when(due,now)+' '+due.strftime('%H:%M') if due and when(due,now)[-1]=='）' else when(due,now)) if due else '截止：未確認',DRAFT_NOTE]
    if item.get('url'):lines.append(item['url'])
    return dict(text='\n'.join(lines),item=item.get('id'),draft=result,submitted=False)


def allowed_file(path,roots):
    try:target=Path(path).resolve(strict=True)
    except (OSError,RuntimeError):return False
    return target.is_file() and target.suffix.lower() in {'.pdf','.docx','.doc','.zip','.txt','.md'} and any(target.is_relative_to(Path(r).resolve()) for r in roots)


def _run_sis(config,sub_args,*,run=None,timeout=180):
    import subprocess
    repo_raw=str(config.get('sis_repo') or '').strip()
    if not repo_raw or 'path/to' in repo_raw:
        return None,dict(error='sis_not_configured',text='尚未設定輔大學生資訊平台工具（config.json 的 sis_repo）。',files=[])
    repo=Path(repo_raw)
    cli=repo/'.venv/Scripts/fjusis.exe'
    cmd=[str(cli)] if cli.is_file() else [str(repo/'.venv/Scripts/python.exe'),'-m','fju_sis']
    runner=run or (lambda argv:subprocess.run(argv,capture_output=True,timeout=timeout,cwd=str(repo),env=cli_env(),
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)))
    try:
        done=runner(cmd+sub_args)
        code=getattr(done,'returncode',0)
        out=done.stdout.decode('utf-8','replace') if isinstance(done.stdout,bytes) else str(done.stdout or '')
        err=done.stderr.decode('utf-8','replace') if isinstance(done.stderr,bytes) else str(done.stderr or '')
        if code:
            detail=(err or out).strip().splitlines()[-1] if (err or out).strip() else ('exit '+str(code))
            return None,dict(error='sis_failed',text='⚠️ 學生資訊平台操作失敗：'+detail+'（若尚未登入或權杖過期，請在電腦終端機執行 fjusis login）',files=[])
        return json.loads(out),None
    except Exception as exc:
        return None,dict(error=type(exc).__name__,text='⚠️ 學生資訊平台操作失敗：'+str(exc)[:180],files=[])


def transcript(config,workflow,query='',*,run=None):
    """Download official digital semester transcript PDF(s) via FJU-SIS-MCP (fjusis CLI)."""
    folder=Path(config.get('state_dir') or workflow.root)/'transcripts'
    folder.mkdir(parents=True,exist_ok=True)
    q=str(query or '')
    no_ranking=bool(re.search(r'不含排名|不要排名|無排名|no[-_ ]?ranking',q,re.I))
    force_refresh=bool(re.search(r'重新下載|重抓|強制更新',q))
    sems=[(int(a),int(b)) for a,b in re.findall(r'(\d{3})\s*(?:[-/－—]|學年度?第?)\s*([12])',q)]
    is_range=len(sems)==2 and bool(re.search(r'到|至|~|～|－',q))
    args=['transcript','download','--out',str(folder),'--json']
    if len(sems)==1 and not is_range:
        args+=['--hy',str(sems[0][0]),'--ht',str(sems[0][1])]
    else:
        args.append('--all')
    if no_ranking:args.append('--no-ranking')
    if force_refresh:args.append('--refresh')
    payload,err=_run_sis(config,args,run=run)
    if err:return err
    items=payload.get('files') or []
    if is_range:
        lo,hi=sorted(sems)
        items=[x for x in items if (int(x.get('hy',0)),int(x.get('ht',0)))>=lo and (int(x.get('hy',0)),int(x.get('ht',0)))<=hi]
    elif len(sems)>1:
        wanted=set(sems)
        items=[x for x in items if (int(x.get('hy',0)),int(x.get('ht',0))) in wanted]
    files=[str(x['path']) for x in items if x.get('path') and Path(x['path']).is_file()]
    if not files:
        return dict(text='目前在輔大學生資訊平台查無可下載的數位成績單。',files=[])
    mode='不含排名' if no_ranking else '含排名'
    sems_txt='、'.join(str(x.get('hy',''))+'-'+str(x.get('ht','')) for x in items if x.get('path'))
    lines=['📄 官方數位成績單（'+mode+'，共 '+str(len(files))+' 份）：'+sems_txt]
    return dict(text='\n'.join(lines),files=files,
        reply='回覆時每個檔案單獨一行寫 MEDIA:檔案路徑，檔案就會傳到 Telegram。')


def enrollment(config,workflow,query='',*,run=None):
    """Download official digital enrollment certificate PDF via FJU-SIS-MCP."""
    folder=Path(config.get('state_dir') or workflow.root)/'enrollment'
    folder.mkdir(parents=True,exist_ok=True)
    q=str(query or '')
    lang='en' if re.search(r'英文|english|\ben\b',q,re.I) else 'zh'
    sem=re.search(r'(\d{3})\s*(?:[-/－—]|學年度?第?)\s*([12])',q)
    args=['enrollment','download','--out',str(folder),'--lang',lang,'--json']
    if sem:args+=['--hy',sem.group(1),'--ht',sem.group(2)]
    payload,err=_run_sis(config,args,run=run)
    if err:return err
    path=str(payload.get('path') or '')
    if not path or not Path(path).is_file():
        return dict(text='目前查無可下載的數位在學證明。',files=[])
    label='英文版' if lang=='en' else '中文版'
    return dict(text='📄 官方數位在學證明（'+str(payload.get('hy',''))+'-'+str(payload.get('ht',''))+' '+label+'）',
        files=[path],reply='回覆時單獨一行寫 MEDIA:檔案路徑，檔案就會傳到 Telegram。')


def sis_query(config,workflow,kind='',query='',*,run=None):
    """Read-only queries against FJU SIS: registration status, courses/gen-edu search, leave list/stats."""
    k=str(kind or '').strip().lower()
    q=str(query or '').strip()
    if k in {'registration','reg','註冊'}:
        data,err=_run_sis(config,['registration','--json'],run=run)
        if err:return err
        st='已完成註冊' if data.get('registerStatus')==1 else '目前非註冊查詢期間或未確認'
        lines=['📋 '+str(data.get('hy',''))+'-'+str(data.get('ht',''))+' 註冊狀況：'+st]
        for item in (data.get('errorFields') or [])[:10]:
            lines.append('• '+str(item.get('errCna') or '')+'：'+str(item.get('organizerNote') or ''))
        return dict(text='\n'.join(lines),data=data)
    if k in {'courses','course','開課','通識'}:
        gen=bool(k=='通識' or re.search(r'通識',q))
        args=['courses','--page-size','10','--json']
        if gen:args.append('--gen-edu')
        kw=re.sub(r'通識|課程|開課|查詢|查','',q).strip()
        if kw and not gen:args+=['--keyword',kw]
        data,err=_run_sis(config,args,run=run)
        if err:return err
        rows=(data.get('result') or [])[:10]
        if not rows:return dict(text='查無符合條件的課程。',data=data)
        lines=['🔎 查詢到 '+str(data.get('totalItemCount',len(rows)))+' 門課（顯示前 '+str(len(rows))+' 門）：']
        for c in rows:
            lines.append('• '+str(c.get('couCNa') or '')+'｜'+str(c.get('tchCNa') or '')+'｜'+str(c.get('credit') or '')+'學分')
        return dict(text='\n'.join(lines),data=data)
    if k in {'leave','leaves','請假','缺曠'}:
        lst,err=_run_sis(config,['leave','list','--json'],run=run)
        if err:return err
        stat,err2=_run_sis(config,['leave','stat','--json'],run=run)
        lines=['🗓️ 請假紀錄與統計']
        if not err2 and isinstance(stat,dict):
            lines.append('本學期請假總計：'+str(stat.get('sumLeaveSect',0))+' 節（已核准 '+str(stat.get('sumLeaveSectYes',0))+'／未核准 '+str(stat.get('sumLeaveSectNo',0))+'）')
        rows=(lst.get('result') or [])[:8]
        if not rows:lines.append('本學期目前無請假紀錄。')
        for r in rows:
            lines.append('• #'+str(r.get('leaveApplySn',''))+' '+str(r.get('leaveNa',''))+'｜'+str(r.get('beginDate',''))+'（'+str(r.get('totalSect',''))+'節）｜'+str(r.get('applyStatusNa','')))
        return dict(text='\n'.join(lines),leaves=lst,stat=stat if not err2 else None)
    return dict(error='unknown_sis_kind',text='請指定要查詢的項目：registration（註冊狀況）、courses（開課／通識）、leave（請假紀錄與統計）。')


LEAVE_DRAFT_NOTE='⚠️ 只存成請假草稿，還沒正式送出：請到輔大學生資訊平台確認內容與勾選課程後再按送出。'


def leave_draft(config,workflow,*,leave_type='病假',begin_date='',end_date='',begin_section='',end_section='',reason='',paths=None,roots=(),run=None):
    """Create a leave draft in FJU SIS (never confirms/submits automatically)."""
    sys.path.insert(0,str(Path(__file__).parent))
    import vision
    if not (begin_date and begin_section and end_section):
        return dict(error='missing_fields',text='建立請假草稿需要：開始日期（YYYY-MM-DD）、開始節次、結束節次。')
    end_date=end_date or begin_date
    clean_paths=[p for p in (paths or []) if p]
    bad=[p for p in clean_paths if not vision.allowed(p,roots) and not allowed_file(p,roots)]
    if bad:
        return dict(error='file_not_allowed',text='只能附上本人剛傳給機器人的證明照片或檔案：'+'、'.join(Path(p).name for p in bad))
    args=['leave','draft','--type',str(leave_type or '病假'),
          '--begin-date',str(begin_date),'--end-date',str(end_date),
          '--begin-section',str(begin_section),'--end-section',str(end_section),'--json']
    if reason:args+=['--reason',str(reason)]
    for p in clean_paths:args+=['--file',str(p)]
    payload,err=_run_sis(config,args,run=run)
    if err:return err
    lines=[
        '✅ 已建立請假草稿 #'+str(payload.get('leaveApplySn',''))+'（'+str(payload.get('leave_type',leave_type))+'，'+str(begin_date)+' 第 '+str(begin_section)+'–'+str(end_section)+' 節）',
        '已勾選課程：'+str(payload.get('selected_courses',0))+' 堂｜佐證附件：'+str(payload.get('attachments',0))+' 份',
        LEAVE_DRAFT_NOTE,
    ]
    if payload.get('portal_url'):lines.append(payload['portal_url'])
    return dict(text='\n'.join(lines),draft=payload,submitted=False)


GMAIL_DRAFT_NOTE='⚠️ 已存入你的 Gmail 草稿匣，還沒寄出：請打開 Gmail「草稿」確認收件人、內文與附件後再自行按傳送。'


def _pack_attachment_batches(paths,max_bytes=18*1024*1024):
    if not paths:return [[]]
    batches,current,total=[],[],0
    for p in paths:
        size=Path(p).stat().st_size
        if size>max_bytes:return None
        if current and total+size>max_bytes:
            batches.append(current);current,total=[p],size
        else:current.append(p);total+=size
    if current:batches.append(current)
    return batches


def mail_draft(config,workflow,*,to='',subject='',body='',paths=None,roots=(),run=None):
    """Create real draft(s) in Gmail Drafts (never sends). Automatically splits attachments if >18 MB."""
    import subprocess
    mailbox=str(config.get('mailbox') or '').strip()
    py=str(config.get('mail_python') or '').strip()
    if not mailbox or not py or 'path/to' in mailbox:
        return dict(error='mail_not_configured',text='尚未設定本機 Gmail 連接器（config.json 的 mailbox / mail_python），無法寫入 Gmail 草稿匣。')
    to=str(to or '').strip()
    if not re.fullmatch(r'[^@\s<>]+@[^@\s<>]+',to):
        return dict(error='invalid_recipient',text='請提供有效的收件人 Email 地址，再存入 Gmail 草稿匣。')
    state_root=Path(config.get('state_dir') or workflow.root)
    allowed_roots=list(roots)+[state_root/'transcripts',state_root/'enrollment',state_root/'attachments']
    clean_paths=[p for p in (paths or []) if p]
    bad=[p for p in clean_paths if not allowed_file(p,allowed_roots)]
    if bad:
        return dict(error='file_not_allowed',text='只能附上本機學業檔案（成績單、在學證明、課程附件或剛傳的檔案）：'+'、'.join(Path(p).name for p in bad))
    merged_note=''
    if len(clean_paths)>1 and all(Path(p).suffix.lower()=='.pdf' for p in clean_paths) and sum(Path(p).stat().st_size for p in clean_paths)>18*1024*1024:
        import mail_draft as md
        orig_mb=round(sum(Path(p).stat().st_size for p in clean_paths)/(1024*1024),1)
        mname=('歷年數位成績單_合併' if any('成績單' in Path(p).name for p in clean_paths) else '合併附件_')+str(len(clean_paths))+'份.pdf'
        target=state_root/'transcripts'/mname
        merged=md.merge_pdfs([Path(p) for p in clean_paths],target)
        if not merged and run is None:
            # Hermes venv may lack PyMuPDF while mail_python has it; delegate merge to mail_python.
            mcode=('import pathlib, sys; sys.path.insert(0, sys.argv[1]); import mail_draft as md; '
                   'out=md.merge_pdfs([pathlib.Path(x) for x in sys.argv[3:]], pathlib.Path(sys.argv[2])); '
                   'print(str(out) if out else "")')
            try:
                done=subprocess.run([py,'-X','utf8','-c',mcode,str(Path(__file__).parent),str(target)]+clean_paths,
                    capture_output=True,timeout=60,env=cli_env(),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                if done.returncode==0 and target.is_file() and target.stat().st_size>0:merged=target
            except Exception:pass
        if merged and merged.stat().st_size<=18*1024*1024:
            new_mb=round(merged.stat().st_size/(1024*1024),1)
            merged_note='（已將 '+str(len(clean_paths))+' 份 PDF 無損合併壓縮為 1 份：'+str(orig_mb)+' MB → '+str(new_mb)+' MB）'
            clean_paths=[str(merged)]
    batches=_pack_attachment_batches(clean_paths)
    if batches is None:
        return dict(error='single_file_too_large',text='單一附件超過 18 MB（編碼後超過 Gmail 25 MB 上限），無法存入 Gmail 草稿。')
    script=Path(__file__).with_name('mail_draft.py')
    runner=run or (lambda payload:subprocess.run([py,'-X','utf8',str(script)],input=json.dumps(payload,ensure_ascii=False).encode('utf-8'),
        capture_output=True,timeout=180,env=cli_env(),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)))
    created=[]
    for idx,batch in enumerate(batches,1):
        sub=(subject or '學業信件')+(f' ({idx}/{len(batches)})' if len(batches)>1 else '')
        req={'mailbox':mailbox,'to':to,'subject':sub,'body':body or '','attachments':batch}
        try:
            done=runner(req)
            out=done.stdout.decode('utf-8','replace') if isinstance(done.stdout,bytes) else str(done.stdout or '')
            res=json.loads(out.strip().splitlines()[-1])
        except Exception as exc:
            res={'ok':False,'error':type(exc).__name__,'message':str(exc)[:180]}
        if not res.get('ok'):
            return dict(error=res.get('error') or 'draft_failed',text='❌ 寫入 Gmail 草稿匣失敗：'+str(res.get('message') or res.get('error')),created=created)
        created.append(res)
    lines=['✅ 已實際存入你的 Gmail 草稿匣（共 '+str(len(created))+' 封草稿）']
    if merged_note:lines.append(merged_note)
    elif len(created)>1:
        lines.append('（因附件總大小超過 Gmail 單封 25 MB 上限，已自動拆成 '+str(len(created))+' 封草稿）')
    for c in created:
        names='、'.join(a['name'] for a in (c.get('attachments') or [])) or '無附件'
        lines.append('• 收件人：'+c['to']+'｜主旨：'+c['subject']+'｜附件：'+names)
    lines.append(GMAIL_DRAFT_NOTE)
    return dict(text='\n'.join(lines),drafts=created,sent=False)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser()
    parser.add_argument('--config');parser.add_argument('--days',type=int,default=14);parser.add_argument('--json',action='store_true')
    args=parser.parse_args()
    sys.path.insert(0,str(Path(__file__).parent))
    from academic import load
    config,workflow=load(args.config)
    result=build(config,workflow,days=args.days)
    print(json.dumps(result,ensure_ascii=False,indent=1) if args.json else result['text'])


if __name__=='__main__':main()
