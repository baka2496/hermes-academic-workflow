"""Live regression test: ask 認真用功居 the questions the owner really asks, from the owner's own
Telegram account, and check every answer against academic.db.

Written after 11/18, when "傳題目給我" ran for five minutes: the earlier manual tests only asked
questions with keywords and never asked for a file, a follow-up without keywords, or a case where the
tool has nothing to give. Each case here checks the answer against the stored source data, the time
taken, and that the bot only used academic_workflow (no file searching, no hand-off to other bots).

    <telethon python> tests/live/telegram_smoke.py [--out test-screenshots] [--only N,N]

Needs the Telethon session at C:\\Users\\shiro\\tools\\telegram-mcp-private\\academic-test and
TELEGRAM_API_ID / TELEGRAM_API_HASH in the Windows user environment. Sends real messages.
"""
from datetime import datetime,timezone
from pathlib import Path
import argparse
import asyncio
import html
import json
import re
import sqlite3
import subprocess
import sys
import time

BOT=9196578787
HERMES=Path(r'C:\Users\shiro\AppData\Local\hermes')
STATE=HERMES/'shared/academic-state/academic.db'
CHAT=HERMES/'profiles/commander/state.db'
SESSION=r'C:\Users\shiro\tools\telegram-mcp-private\academic-test'
# Read-only helpers; speed is checked by the time limit instead.
ALLOWED_TOOLS={'academic_workflow','tool_search','tool_describe','tool_call','memory','session_search','agent_preferences'}
HANDOFF=re.compile(r'已接單|派專員|派人|稍後(傳|回覆|提供)|正在(前往|取得|處理)')
LIMIT=40 # seconds; the PDF took 11–15 s on 11/18, the five-minute failure is what this guards


def source_items():
    db=sqlite3.connect(STATE)
    rows=[json.loads(p) for (p,) in db.execute("select payload from items where source='tronclass'")]
    return rows


def files(item):
    return [x for x in ((item.get('source_requirements') or {}).get('attachments') or []) if x.get('name') and not x.get('deleted')]


def cases(now):
    items=source_items()
    courses=sorted({x['course'] for x in items if x.get('course')})
    rel=[x for x in items if x.get('course')=='相對論' and x.get('kind')=='homework' and not x.get('submitted')
         and x.get('due_at') and x['due_at']>=now.isoformat()]
    rel.sort(key=lambda x:x['due_at'])
    with_file=[x for x in rel if files(x)]
    seat=next((x for x in items if x.get('kind')=='announcement' and '座位' in (x.get('title') or '') and files(x)),None)
    bare=next((c for c in courses if not any(files(x) for x in items if x.get('course')==c)),None)
    fake='化學實驗'
    assert all(fake not in c for c in courses),'pick another missing course name'
    result=[dict(name='開新對話',send='/new',expect='text',check=lambda r:None)]
    def mentions(*words):
        def check(r):
            missing=[w for w in words if w not in r['text']]
            return '回答少了：'+'、'.join(missing) if missing else None
        return check
    if rel:
        due=datetime.fromisoformat(rel[0]['due_at']).strftime('%m/%d').lstrip('0').replace('/0','/')
        result.append(dict(name='列出某課作業',send='給我相對論的作業',expect='text',check=mentions(rel[0]['title'],due)))
    if with_file:
        f=files(with_file[0])[0]
        result.append(dict(name='沒有關鍵字的追問要檔案',send='傳題目給我',expect='document',file=f))
    if seat:
        f=files(seat)[0]
        result.append(dict(name='要公告附件',send='傳'+seat['course']+'的期中考座位表給我',expect='document',file=f,
            any_of=[x['name'] for x in files(seat)]))
    # A file never downloaded before: a cached copy hid that every download from the gateway failed.
    cache=HERMES/'shared/academic-state/attachments'
    fresh=next((x for x in items if x.get('kind')=='announcement' and files(x) and len(files(x))==1
        and re.search(r'講義|ch\d',x.get('title') or '',re.I)
        and not (cache/re.sub(r'[^0-9A-Za-z_-]','_',x.get('id') or '')).is_dir()),None)
    if fresh:
        result.append(dict(name='下載沒抓過的檔案',send='傳'+fresh['course']+'「'+fresh['title']+'」的檔案給我',expect='document',file=files(fresh)[0]))
    if bare:
        def no_file(r):
            if r['documents']:return '這門課沒有附件，卻傳了檔案：'+', '.join(d['name'] for d in r['documents'])
            if not re.search(r'沒有|找不到|未確認|無',r['text']):return '沒有說明找不到附件'
        result.append(dict(name='沒有附件時照實說',send='傳'+bare+'的題目給我',expect='text',check=no_file))
    def unknown(r):
        if not re.search(r'找不到|沒有|未確認|不在',r['text']):return '沒有說找不到這門課'
    result.append(dict(name='不存在的課',send=fake+'有什麼作業',expect='text',check=unknown))
    result.append(dict(name='近況清單',send='最近有什麼事',expect='text',check=mentions('📚')))
    return result


def limit(case):
    size=(case.get('file') or {}).get('size') or 0
    return LIMIT+int(size/1_000_000*6)


def tools_used(since):
    db=sqlite3.connect(CHAT)
    names=[]
    for (calls,) in db.execute('select tool_calls from messages where timestamp>=? and tool_calls is not null',(since,)):
        for call in json.loads(calls):
            fn=call.get('function') or {}
            names.append(fn.get('name'))
            if fn.get('name')=='tool_call':
                try:names+=[x.get('name') for x in json.loads(fn.get('arguments') or '{}').get('calls',[])]
                except ValueError:pass
    return names


async def ask(client,case):
    started=time.time()
    sent=await client.send_message(BOT,case['send'])
    replies=[];last=None
    while time.time()-started<limit(case)+60:
        await asyncio.sleep(2)
        new=[m async for m in client.iter_messages(BOT,min_id=sent.id) if not m.out]
        if len(new)!=len(replies):replies=new;last=time.time()
        has_file=any(m.file for m in replies)
        if case['expect']!='document' or has_file:
            if replies and time.time()-last>=6:break
        # Telegram uploads ~5 MB lecture notes in about 25 s after the text; give the file until its
        # limit before treating a text-only answer (e.g. an honest "下載失敗") as final.
        elif replies and time.time()-started>=limit(case) and time.time()-last>=6:break
    replies=sorted(replies,key=lambda m:m.id)
    first_useful=next((m for m in replies if (m.file if case['expect']=='document' else m.raw_text)),None)
    seconds=round((first_useful.date.timestamp() if first_useful else time.time())-sent.date.timestamp())
    return dict(sent=dict(text=case['send'],date=sent.date.astimezone().strftime('%H:%M:%S')),started=sent.date.timestamp(),
        seconds=seconds,text='\n'.join(m.raw_text for m in replies if m.raw_text),
        documents=[dict(name=m.file.name,size=m.file.size) for m in replies if m.file],
        messages=[dict(out=False,date=m.date.astimezone().strftime('%H:%M:%S'),text=m.raw_text or '',
            file=(m.file.name+'（'+str(round(m.file.size/1024))+' KB）') if m.file else None) for m in replies])


def judge(case,reply):
    problems=[]
    if case['send']=='/new':return problems
    if not reply['messages']:return ['沒有回覆']
    if reply['seconds']>limit(case):problems.append('太慢：'+str(reply['seconds'])+' 秒（上限 '+str(limit(case))+'）')
    if HANDOFF.search(reply['text']):problems.append('說要轉派或稍後再給，而不是直接回答')
    bad=[t for t in tools_used(reply['started']) if t not in ALLOWED_TOOLS]
    if bad:problems.append('用了不該用的工具：'+'、'.join(sorted(set(bad))))
    if case['expect']=='document':
        names=case.get('any_of') or [case['file']['name']]
        got=[d for d in reply['documents'] if d['name'] in names]
        if not got:problems.append('沒收到檔案 '+names[0]+'（收到：'+(', '.join(d['name'] for d in reply['documents']) or '無')+'）')
        elif case['file'].get('size') and case['file']['name']==got[0]['name'] and got[0]['size']!=case['file']['size']:
            problems.append('檔案大小不符：'+str(got[0]['size'])+' ≠ TronClass '+str(case['file']['size']))
    elif case.get('check'):
        issue=case['check'](reply)
        if issue:problems.append(issue)
    return problems


def render(results,out):
    rows=[]
    for r in results:
        rows.append('<div class="b me">'+html.escape(r['sent']['text'])+'<span class="t">'+r['sent']['date'][:5]+'</span></div>')
        for m in r['messages']:
            body=('📄 '+html.escape(m['file'])+'\n' if m['file'] else '')+html.escape(m['text'])
            rows.append('<div class="b bot">'+body.strip()+'<span class="t">'+m['date'][:5]+'</span></div>')
        if r['case']!='開新對話':
            ok=not r['problems']
            rows.append('<div class="v '+('ok' if ok else 'bad')+'">'+('✅ ' if ok else '❌ ')+html.escape(r['case'])+'｜'+str(r['seconds'])+' 秒'
                +''.join('<br>• '+html.escape(p) for p in r['problems'])+'</div>')
    passed=sum(1 for r in results if r['case']!='開新對話' and not r['problems']);total=len(results)-1
    page=('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=390"><style>'
        'body{margin:0;width:390px;background:#c8d9b5;font:16px/1.35 -apple-system,"Segoe UI","Microsoft JhengHei",sans-serif}'
        '.h{background:#f7f7f7;padding:10px 14px;font-weight:600;border-bottom:1px solid #ccc}.h small{display:block;font-weight:400;color:#888;font-size:12px}'
        '.c{padding:8px 8px 16px;display:flex;flex-direction:column;gap:6px}'
        '.b{max-width:84%;padding:7px 10px 16px;border-radius:14px;white-space:pre-wrap;word-break:break-word;position:relative}'
        '.bot{background:#fff;align-self:flex-start}.me{background:#e1ffc7;align-self:flex-end}'
        '.t{position:absolute;right:8px;bottom:2px;font-size:11px;color:#8a8a8a}'
        '.v{align-self:center;font-size:13px;padding:4px 10px;border-radius:10px;max-width:92%}.ok{background:#dff3d8;color:#235c1b}.bad{background:#fde2e1;color:#8a1f17}'
        '</style><div class="h">認真用功居 回歸測試<small>'+datetime.now().strftime('%m/%d %H:%M')+'｜通過 '+str(passed)+'/'+str(total)
        +'｜390px 手機寬度</small></div><div class="c">'+''.join(rows)+'</div>')
    out.mkdir(parents=True,exist_ok=True)
    page_path=out/'smoke.html';page_path.write_text(page,encoding='utf-8')
    png=out.resolve()/('smoke-'+datetime.now().strftime('%Y%m%d-%H%M')+'.png')
    chrome=next((p for p in [r'C:\Program Files\Google\Chrome\Application\chrome.exe',r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'] if Path(p).exists()),None)
    if chrome:
        height=200+sum(90+40*r['text'].count('\n') for r in results)
        subprocess.run([chrome,'--headless','--disable-gpu','--hide-scrollbars','--window-size=390,'+str(height),
            '--force-device-scale-factor=2','--screenshot='+str(png),page_path.resolve().as_uri()],capture_output=True,timeout=60)
    return png if png.exists() else page_path,passed,total


async def main():
    import winreg
    from telethon import TelegramClient
    parser=argparse.ArgumentParser();parser.add_argument('--out',default='test-screenshots');parser.add_argument('--only')
    args=parser.parse_args()
    def env(name):
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,'Environment') as key:return winreg.QueryValueEx(key,name)[0]
    client=TelegramClient(SESSION,int(env('TELEGRAM_API_ID')),env('TELEGRAM_API_HASH'))
    await client.connect()
    if not await client.is_user_authorized():sys.exit('Telethon session not authorized')
    plan=cases(datetime.now(timezone.utc).astimezone())
    if args.only:plan=[plan[0]]+[plan[int(i)] for i in args.only.split(',')]
    results=[]
    for case in plan:
        reply=await ask(client,case)
        problems=judge(case,reply)
        results.append(dict(case=case['name'],seconds=reply['seconds'],problems=problems,sent=reply['sent'],messages=reply['messages'],text=reply['text']))
        print(('PASS ' if not problems else 'FAIL ')+case['name']+' '+str(reply['seconds'])+'s '+'; '.join(problems),flush=True)
    await client.disconnect()
    image,passed,total=render(results,Path(args.out))
    (Path(args.out)/'smoke-result.json').write_text(json.dumps(results,ensure_ascii=False,indent=1),encoding='utf-8')
    print('passed',passed,'/',total,'->',image)
    sys.exit(0 if passed==total else 1)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    asyncio.run(main())
