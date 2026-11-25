"""One commander-only publisher with separate text/document provider receipts."""
from datetime import datetime,timedelta
from pathlib import Path
import hashlib
import json
import socket
import urllib.request
import urllib.error
from uuid import uuid4
from workflow import iso,digest,encoded,DeliveryRetry
from native import commander_scope
from operations import export_agenda


class TelegramTransport:
    def __init__(self,config):self.config=config;self._credentials=None

    def credentials(self):
        # One tick sends a text and several documents; read secrets and config once.
        if self._credentials:return self._credentials
        with commander_scope(self.config):
            from agent.secret_scope import get_secret
            import hermes_yaml as yaml
            token=get_secret('TELEGRAM_BOT_TOKEN','')
            cfg=yaml.safe_load((Path(self.config['hermes_root'])/'profiles/commander/config.yaml').read_text(encoding='utf-8'))
            chat=str(((cfg.get('platforms') or {}).get('telegram') or {}).get('home_channel',{}).get('chat_id',''))
        if not token or not chat.isdigit():raise ValueError('Commander destination unavailable')
        self._credentials=(token,chat)
        return self._credentials

    def request(self,method,data,content_type='application/json'):
        token,_=self.credentials()
        req=urllib.request.Request('https://api.telegram.org/bot'+token+'/'+method,data=data,
            headers={'Content-Type':content_type},method='POST')
        try:
            with urllib.request.urlopen(req,timeout=25) as response:result=json.load(response)
        except urllib.error.HTTPError as exc:
            try:failure=json.loads(exc.read())
            except Exception:failure={}
            if exc.code==429:
                raise DeliveryRetry(failure.get('parameters',{}).get('retry_after',30)) from None
            if exc.code>=500:raise TimeoutError('Telegram response outcome unknown') from None
            # Telegram's description never contains the token; keep it so failures can be diagnosed.
            raise ValueError('Telegram rejected delivery: HTTP '+str(exc.code)+' '+str(failure.get('description',''))[:150]) from None
        except urllib.error.URLError as exc:
            # DNS failure or a refused connection never reached Telegram: safe to retry.
            if isinstance(exc.reason,(socket.gaierror,ConnectionRefusedError)):raise DeliveryRetry(60) from None
            raise TimeoutError('Telegram response outcome unknown') from None
        if result.get('ok') is not True:raise ValueError('Telegram did not confirm request')
        return result['result']

    def probe(self):
        _,chat=self.credentials()
        bot=self.request('getMe',b'{}')
        destination=self.request('getChat',json.dumps({'chat_id':chat}).encode('utf-8'))
        if str(destination.get('id'))!=chat or destination.get('type')!='private':raise ValueError('Commander destination is not the configured private chat')
        return {'bot_id':bot['id'],'bot_name':bot.get('first_name'),'configured_private_chat_verified':True}

    def send_text(self,text):
        _,chat=self.credentials()
        result=self.request('sendMessage',json.dumps({'chat_id':chat,'text':text[:4000],'disable_web_page_preview':True},ensure_ascii=False).encode('utf-8'))
        return {'message_id':str(result['message_id'])}

    def send_artifact(self,path,caption):
        _,chat=self.credentials();boundary='Academic'+uuid4().hex
        chunks=[]
        for key,value in [('chat_id',chat),('caption',caption[:900])]:
            chunks.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+key+'"\r\n\r\n'+value+'\r\n').encode('utf-8'))
        name=Path(path).name.replace('"','').replace('\r','').replace('\n','')
        chunks.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="document"; filename="'+name+'"\r\nContent-Type: application/octet-stream\r\n\r\n').encode('utf-8'))
        chunks.append(Path(path).read_bytes());chunks.append(('\r\n--'+boundary+'--\r\n').encode('ascii'))
        result=self.request('sendDocument',b''.join(chunks),'multipart/form-data; boundary='+boundary)
        if not result.get('document'):raise TimeoutError('Provider did not confirm document receipt')
        return {'message_id':str(result['message_id'])}


def countdown(due,clock):
    hours=(due-clock).total_seconds()/3600
    if hours<0:return '已過期'
    if hours<24:return str(max(1,int(hours)))+'小時'
    return str(int(hours//24))+'天'


PHONE_LINE=21 # characters per line in the owner's iPhone Telegram


WEEKDAY='一二三四五六日'
KIND={'exam':'考試','homework':'作業','announcement':'公告','email':'信件','manual':'你記的'}
SCOPE={'exam':'考試','homework':'作業','announcement':'公告','collector':'課程','courses':'課程'}


def width(text):
    # Chinese and full-width characters take a whole cell on the phone; digits and latin about half.
    return sum(1 if ord(c)>=0x2E80 else 0.55 for c in text)


def fit(text,limit):
    if width(text)<=limit:return text
    while text and width(text)+1>limit:text=text[:-1]
    return text.rstrip()+'…'


def item_label(item):
    """Course and title together: "1116第六次考試" alone did not say which class had the exam."""
    import re
    title=re.sub(r'[【】\[\]「」"“”]','',item.get('title','')).strip()
    # Leading greetings and a date already shown on the next line only push the subject off screen.
    title=re.sub(r'^(歡迎參加|關於|公告[:：]?)','',title).strip() or title
    title=re.sub(r'^\d{2,4}[./]\d{1,2}[./]\d{1,2}\s*([（(].[）)])?','',title).strip() or title
    course=re.sub(r'[（(][^）)]*[）)]?$','',item.get('course') or '').strip()
    if course:title=title.replace(course,'').strip() or title
    head=course or KIND.get(item.get('kind'),'')
    # 11/25: no "…" — a cut title ("國關課 交期中報…") hid what the item was; Telegram wraps it.
    return (head+'｜' if head else '')+title


def due_line(item,clock):
    due=datetime.fromisoformat(item['due_at'])
    # TronClass writes "end of 12/01" as 12/02 00:00, which reads like a day later; show 12/01 24:00.
    clock_text='24:00' if due.strftime('%H:%M')=='00:00' else due.strftime('%H:%M')
    if clock_text=='24:00':due-=timedelta(minutes=1)
    day='今天' if due.date()==clock.date() else '明天' if due.date()==(clock+timedelta(days=1)).date() else due.strftime('%m/%d')+'（'+WEEKDAY[due.weekday()]+'）'
    day+=('' if day.endswith('）') else ' ')+clock_text
    if item.get('confidence')=='needs_check':return day+'（日期待核對）'
    return day+'（還有 '+countdown(due,clock).replace('小時',' 小時').replace('天',' 天')+'）'


def missing_sources(workflow):
    """Name what was not read instead of "部分來源不完整"; Outlook is knowingly not connected."""
    gaps=[]
    for source in workflow.sources():
        if source['complete'] or source['source']=='mail:outlook':continue
        name='TronClass '+SCOPE.get(source['scope'].split(':')[-1],'資料') if source['source']=='tronclass' else 'Gmail 信件' if source['source']=='mail:gmail' else None
        if name and name not in gaps:gaps.append(name)
    return gaps


def digest_message(active,clock,*,header,shown,urgent_keys=()):
    """Phone message: what is due soon, each item as 「課程｜標題」 plus its time on the next line."""
    soon=[x for x in active if x.get('due_at') and x.get('confidence')!='needs_check'
        and 0<(datetime.fromisoformat(x['due_at'])-clock).total_seconds()<=86400 or x['key'] in urgent_keys]
    late=[x for x in active if x.get('due_at') and datetime.fromisoformat(x['due_at'])<=clock and x not in soon]
    rest=[x for x in active if x not in soon and x not in late]
    lines=[header]
    if late:
        lines.append('⛔ 已過期、還沒完成')
        for item in late:lines+=['• '+item_label(item),'  '+datetime.fromisoformat(item['due_at']).strftime('%m/%d %H:%M')+' 截止']
    if soon:
        lines.append('⏰ 24 小時內')
        for item in soon:lines+=['• '+item_label(item),'  '+due_line(item,clock)]
    upcoming=[x for x in rest if x.get('due_at')][:max(0,shown-len(soon))]
    if upcoming:
        lines.append('📅 接下來')
        for item in upcoming:lines+=['• '+item_label(item),'  '+due_line(item,clock)]
    if not soon and not upcoming:lines.append('• 目前沒有有日期的待辦')
    left=len(rest)-len(upcoming)
    if left:lines.append('＋另有 '+str(left)+' 項（較晚或沒日期）')
    return lines


def active_window(config,clock):
    return int(config.get('daily_hour',8))<=clock.hour<int(config.get('followup_until_hour',22))


def digest_order(item):
    # Strictly by date, undated last: ranking announcement-derived dates last hid a registration due
    # in 3 days behind items due in 47. Those dates still show a "?" so the owner knows to check them.
    return (not item.get('due_at'),item.get('due_at') or '')


def prepare_digest(workflow,config,*,native=None,now=None,force=False):
    clock=datetime.fromisoformat(iso(now))
    hour=int(config.get('daily_hour',8))
    current={x['key']:x for x in workflow.agenda(now=now)}
    risk=[];risk_items={};within_six=False
    for item in current.values():
        if item['human_complete'] or item['human_state'] in {'past_exam','closed','overdue'} or not item.get('due_at') or item.get('confidence')=='needs_check':continue
        hours=(datetime.fromisoformat(item['due_at'])-clock).total_seconds()/3600
        band=6 if 0<hours<=6 else 24 if 0<hours<=24 else None
        # Keyed on the deadline, not the revision: a refreshed TronClass record re-sent the same exam
        # reminder at 09:44 and 12:04. A moved deadline is still a new reminder.
        if band:risk.append(digest([item['key'],item['due_at'],band]));risk_items[risk[-1]]=item['key'];within_six=within_six or band==6
    fresh_risk=[x for x in risk if not workflow.get_meta('alert:'+x)]
    urgent=bool(fresh_risk) and (clock.hour>=hour or within_six)
    if not force and clock.hour<hour and not urgent:return {'state':'waiting_daily_window'}
    backoff=workflow.get_meta('digest_backoff') or {}
    if not force and backoff.get('until') and datetime.fromisoformat(backoff['until'])>clock:
        return {'state':'delivery_backoff','until':backoff['until']}
    pending=next((x for x in workflow.publications() if x['payload'].get('type')=='digest' and x['state']=='pending'),None)
    if pending:return {'state':'awaiting_delivery','key':pending['key']}
    known_packages=[x for x in workflow.publications() if x['payload'].get('type','package')=='package' and current.get(x['item_key'],{}).get('revision')==x['revision']]
    packages=[x for x in known_packages if x['state']=='pending']
    day=clock.date().isoformat()
    slot=day
    followup=False
    existing=workflow.get_meta('digest_slot:'+slot)
    if existing and urgent:slot='urgent:'+digest(fresh_risk)
    elif existing and packages and active_window(config,clock) and workflow.get_meta('followups:'+day,0)<int(config.get('max_followups_per_day',2)):
        # Materials verified after the daily digest go out the same day instead of waiting a night.
        slot='materials:'+digest(sorted(x['key'] for x in packages));followup=True
    elif existing:return {'state':'slot_already_prepared','key':existing}
    existing=workflow.get_meta('digest_slot:'+slot)
    if existing:return {'state':'slot_already_prepared','key':existing}
    source_health=[(s['source'],s['scope'],bool(s['complete']),s['errors']) for s in workflow.sources()]
    preparation=config.get('preparation_enabled',True) is not False
    parked=[] if not preparation else [x['item_key'] for x in workflow.preparations() if current.get(x['item_key'],{}).get('revision')==x['revision'] and (x['error']=='verification_parked' or native and x['task_id'] and (native.get(x['task_id']) or {}).get('status')=='blocked')]
    fingerprint=digest(dict(agenda=[(x['key'],x['revision'],x['human_state']) for x in current.values()],packages=[x['key'] for x in known_packages],risk=risk,source_health=source_health,parked=parked))
    # Undelivered verified materials are always news, even if the agenda itself did not change.
    if not packages and fingerprint in {workflow.get_meta('last_digest_fingerprint'),workflow.get_meta('last_attempted_fingerprint')}:return {'state':'no_material_change'}
    # Until one preparation is verified the digest waits, but near deadlines are never held back.
    if preparation and not known_packages and not urgent and not workflow.get_meta('first_preparation_verified'):return {'state':'waiting_first_verified_preparation'}
    overview=export_agenda(workflow,now=now,native=native,preparation=preparation)
    folder=workflow.root/'deliveries'/day/fingerprint[:16];folder.mkdir(parents=True,exist_ok=True)
    durable=folder/'學業總覽.md'
    try:
        with durable.open('xb') as handle:handle.write(overview.read_bytes())
    except FileExistsError:
        pass
    # 11/19 the owner: the attached 學業總覽.md "不是很有用". On the phone it opens as a raw Markdown
    # table and repeats the message above it, so it stays on the computer and only real materials go out.
    artifacts=[]
    for package in packages:
        artifacts.extend(package['payload']['artifacts'])
    artifacts=list({f['sha256']:f for f in artifacts}.values())
    # A date guessed from an announcement that has already passed ("運動會規程 11/11 已過期?") is not
    # something to act on; it stays in the attached overview only.
    active=[x for x in current.values() if not x['human_complete'] and x['human_state'] not in {'past_exam','closed'}
        and not (x['human_state']=='overdue' and (x.get('confidence')=='needs_check' or x.get('due_at') and datetime.fromisoformat(x['due_at'])<clock-timedelta(days=7)))]
    active.sort(key=digest_order)
    shown=int(config.get('digest_items',5))
    urgent_keys={risk_items[x] for x in fresh_risk} if slot.startswith('urgent:') else set()
    header='📚 新材料' if followup else '⚠️ 期限提醒' if urgent_keys else '📚 今日學業'
    lines=digest_message(active,clock,header=header,shown=shown,urgent_keys=urgent_keys)
    if packages:lines.append('📎 附 '+str(len(packages))+' 份準備材料')
    gaps=missing_sources(workflow)
    if gaps:lines.append('ℹ️ '+'、'.join(gaps)+' 沒讀完整，可能漏列')
    if parked:lines.append('ℹ️ '+str(len(parked))+' 項準備材料卡住')
    lines.append('要看全部，傳「最近有什麼事」給我')
    key='digest:'+slot+':'+fingerprint
    # An undelivered row with the same slot and content stays as history; a retry needs its own key.
    earlier=sum(1 for x in workflow.publications() if x['key']==key or x['key'].startswith(key+':retry'))
    if earlier:key+=':retry'+str(earlier)
    payload=dict(type='digest',title='學業總覽與學習準備',message='\n'.join(lines),artifacts=artifacts,
        fingerprint=fingerprint,package_keys=[x['key'] for x in packages],source_versions={k:v['revision'] for k,v in current.items()},slot=slot,alert_keys=risk)
    with workflow.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        # Unique slot metadata and durable outbox are committed together.
        row=db.execute('SELECT value FROM metadata WHERE key=?',('digest_slot:'+slot,)).fetchone()
        if row and json.loads(row['value']):return {'state':'slot_already_prepared','key':json.loads(row['value'])}
        db.execute('INSERT OR IGNORE INTO outbox VALUES (?,?,?,?,?,?,?)',(key,'',fingerprint,encoded(payload),'pending',clock.isoformat(),None))
        db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('digest_slot:'+slot,encoded(key)))
        if followup:
            row=db.execute('SELECT value FROM metadata WHERE key=?',('followups:'+day,)).fetchone()
            db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('followups:'+day,encoded((json.loads(row['value']) if row else 0)+1)))
        for package in packages:db.execute("UPDATE outbox SET state='bundled' WHERE key=?",(package['key'],))
    return {'state':'prepared','key':key,'packages':len(packages),'artifacts':len(artifacts)}


BENIGN_MAIL_ERRORS=('candidate_body_','message_body_excerpt','scan_budget_reached')
HINTS={
    'tronclass':'TronClass 讀取失敗；登入可能過期，請在 FJU-TronClass-MCP 重新登入後等待下一輪讀取。',
    'gmail':'Gmail 讀取失敗；請檢查本機 Gmail 授權與 mail_python 環境。',
    'interpretation':'公告／信件解析服務連續失敗；請檢查 commander 的模型授權或額度，24 小時後會自動重試。',
    'verification':'產物驗證服務連續失敗；請檢查 commander 的模型授權或額度，24 小時後會自動重試。',
    'worker':'背景準備卡因授權或非暫時性錯誤停住；請檢查 hermes-agent 的模型授權，再於看板解除封鎖。',
    'gateway':'Hermes 已自動更新，但機器人還在跑舊版，派工等功能會出錯；請在電腦執行 hermes gateway restart。',
}
# A stale gateway breaks features immediately; do not wait the two hours used for flaky sources.
FAST_ALERT={'gateway':10}


def health_problems(workflow,*,recovery=(),preparation=True,gateways=()):
    problems={}
    if gateways:problems['gateway']=list(gateways)
    for source in workflow.sources():
        if source['source']=='tronclass' and source['scope']=='collector' and not source['complete']:
            problems['tronclass']=source['errors']
        if source['source']=='mail:gmail':
            hard=[e for e in source['errors'] if not str(e).startswith(BENIGN_MAIL_ERRORS)]
            if hard:problems['gmail']=hard
    current={x['key']:x['revision'] for x in workflow.agenda()}
    interpret=[k for k,v in workflow.meta_items('interpret:').items() if isinstance(v,dict) and v.get('parked')]
    if interpret:problems['interpretation']=[str(len(interpret))+' items']
    verification=[x for x in workflow.preparations() if preparation and x['error']=='verification_parked' and current.get(x['item_key'])==x['revision']]
    if verification:problems['verification']=[str(len(verification))+' items']
    stuck=[x['task_id'] for x in recovery if x.get('state') in {'not_retryable','parked'}]
    if stuck:problems['worker']=stuck
    return problems


def prepare_health(workflow,config,*,now=None,recovery=(),gateways=None):
    """Queue one Telegram alert when infrastructure stays broken, and one when it recovers."""
    clock=datetime.fromisoformat(iso(now))
    threshold=timedelta(minutes=int(config.get('health_alert_after_minutes',120)))
    if gateways is None:
        from native import stale_gateways
        gateways=stale_gateways() if config.get('hermes_root') and config.get('check_gateway',True) else []
    problems=health_problems(workflow,recovery=recovery,preparation=config.get('preparation_enabled',True) is not False,gateways=gateways)
    since=workflow.meta_items('health_since:')
    for key in since:
        if key.split(':',1)[1] not in problems:workflow.delete_meta(key)
    persistent=[]
    for name in sorted(problems):
        first=since.get('health_since:'+name)
        if not first:workflow.set_meta('health_since:'+name,clock.isoformat());first=clock.isoformat()
        if clock-datetime.fromisoformat(first)>=(timedelta(minutes=FAST_ALERT[name]) if name in FAST_ALERT else threshold):persistent.append(name)
    alerted=workflow.get_meta('health_alerted',[]) or []
    if persistent==alerted:return {'state':'unchanged','problems':persistent}
    if not persistent and set(alerted)&set(problems):return {'state':'unchanged','problems':persistent}
    if not active_window(config,clock):return {'state':'waiting_daily_window','problems':persistent}
    if any(x['payload'].get('type')=='health' and x['state']=='pending' for x in workflow.publications()):return {'state':'awaiting_delivery'}
    attempt=workflow.get_meta('health_attempt') or {}
    if attempt.get('signature')==persistent and clock-datetime.fromisoformat(attempt['at'])<timedelta(hours=6):return {'state':'recently_attempted'}
    lines=['🩺 學業工作流狀態']
    for name in persistent:lines.append('• '+HINTS[name])
    resolved=[x for x in alerted if x not in persistent]
    if resolved:lines.append('已恢復：'+'、'.join({'tronclass':'TronClass','gmail':'Gmail','interpretation':'解析服務','verification':'驗證服務','worker':'背景準備','gateway':'機器人版本'}.get(x,x) for x in resolved)+'。')
    if not persistent:lines.append('目前所有來源與服務恢復正常。')
    key='health:'+digest(persistent)+':'+clock.isoformat()
    payload=dict(type='health',title='學業工作流狀態',message='\n'.join(lines),artifacts=[],signature=persistent)
    with workflow.connection() as db:
        db.execute('INSERT OR IGNORE INTO outbox VALUES (?,?,?,?,?,?,?)',(key,'','',encoded(payload),'pending',clock.isoformat(),None))
        db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('health_attempt',encoded(dict(signature=persistent,at=clock.isoformat()))))
    return {'state':'prepared','key':key,'problems':persistent}


def scheduler_outcome(workflow,config,*,ok,error_type=None,now=None,transport=None):
    """Tell the owner directly when the scheduler itself keeps crashing; the outbox may be the broken part."""
    clock=datetime.fromisoformat(iso(now))
    if ok:
        failures=workflow.get_meta('scheduler_failure_count',0)
        workflow.set_meta('scheduler_failure_count',0)
        alerted=workflow.get_meta('scheduler_alerted_at')
        if not alerted or not config.get('delivery_enabled'):return {'state':'ok'}
        try:
            (transport or TelegramTransport(config)).send_text('✅ 學業排程已恢復正常執行。')
            workflow.delete_meta('scheduler_alerted_at')
            return {'state':'recovery_sent','failures':failures}
        except Exception as exc:return {'state':'recovery_unsent','error_type':type(exc).__name__}
    failures=(workflow.get_meta('scheduler_failure_count',0) or 0)+1
    workflow.set_meta('scheduler_failure_count',failures)
    if not config.get('delivery_enabled') or failures<int(config.get('scheduler_alert_after_failures',3)):return {'state':'counted','failures':failures}
    alerted=workflow.get_meta('scheduler_alerted_at')
    if alerted and clock-datetime.fromisoformat(alerted)<timedelta(hours=12):return {'state':'already_alerted','failures':failures}
    try:
        (transport or TelegramTransport(config)).send_text('⚠️ 學業排程已連續 '+str(failures)+' 次失敗（'+str(error_type or 'Error')+'）。\n請在電腦上執行 academic.py doctor 檢查設定；問題排除前不會有新的學業整理。')
        workflow.set_meta('scheduler_alerted_at',clock.isoformat())
        return {'state':'alert_sent','failures':failures}
    except Exception as exc:return {'state':'alert_unsent','failures':failures,'error_type':type(exc).__name__}
