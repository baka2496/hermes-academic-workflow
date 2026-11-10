"""One commander-only publisher with separate text/document provider receipts."""
from datetime import datetime,timedelta
from pathlib import Path
import hashlib
import json
import urllib.request
import urllib.error
from uuid import uuid4
from workflow import iso,digest,encoded,DeliveryRetry
from native import commander_scope
from operations import export_agenda


class TelegramTransport:
    def __init__(self,config):self.config=config

    def credentials(self):
        with commander_scope(self.config):
            from agent.secret_scope import get_secret
            import hermes_yaml as yaml
            token=get_secret('TELEGRAM_BOT_TOKEN','')
            cfg=yaml.safe_load((Path(self.config['hermes_root'])/'profiles/commander/config.yaml').read_text(encoding='utf-8'))
            chat=str(cfg['platforms']['telegram']['home_channel']['chat_id'])
        if not token or not chat.isdigit():raise ValueError('Commander destination unavailable')
        return token,chat

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
            raise ValueError('Telegram rejected delivery') from None
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
        result=self.request('sendMessage',json.dumps({'chat_id':chat,'text':text,'disable_web_page_preview':True},ensure_ascii=False).encode('utf-8'))
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


def prepare_digest(workflow,config,*,native=None,now=None,force=False):
    clock=datetime.fromisoformat(iso(now))
    hour=int(config.get('daily_hour',8))
    current={x['key']:x for x in workflow.agenda(now=now)}
    risk=[];within_six=False
    for item in current.values():
        if item['human_complete'] or item['human_state'] in {'past_exam','closed','overdue'} or not item.get('due_at') or item.get('confidence')=='needs_check':continue
        hours=(datetime.fromisoformat(item['due_at'])-clock).total_seconds()/3600
        band=6 if 0<hours<=6 else 24 if 0<hours<=24 else None
        if band:risk.append(digest([item['key'],item['revision'],band]));within_six=within_six or band==6
    fresh_risk=[x for x in risk if not workflow.get_meta('alert:'+x)]
    urgent=bool(fresh_risk) and (clock.hour>=hour or within_six)
    if not force and clock.hour<hour and not urgent:return {'state':'waiting_daily_window'}
    pending=next((x for x in workflow.publications() if x['payload'].get('type')=='digest' and x['state']=='pending'),None)
    if pending:return {'state':'awaiting_delivery','key':pending['key']}
    day=clock.date().isoformat()
    slot=day
    existing=workflow.get_meta('digest_slot:'+slot)
    if existing and urgent:slot='urgent:'+digest(fresh_risk)
    elif existing:return {'state':'slot_already_prepared','key':existing}
    existing=workflow.get_meta('digest_slot:'+slot)
    if existing:return {'state':'slot_already_prepared','key':existing}
    known_packages=[x for x in workflow.publications() if x['payload'].get('type','package')=='package' and current.get(x['item_key'],{}).get('revision')==x['revision']]
    packages=[x for x in known_packages if x['state']=='pending']
    source_health=[(s['source'],s['scope'],bool(s['complete']),s['errors']) for s in workflow.sources()]
    parked=[x['item_key'] for x in workflow.preparations() if current.get(x['item_key'],{}).get('revision')==x['revision'] and (x['error']=='verification_parked' or native and x['task_id'] and (native.get(x['task_id']) or {}).get('status')=='blocked')]
    fingerprint=digest(dict(agenda=[(x['key'],x['revision'],x['human_state']) for x in current.values()],packages=[x['key'] for x in known_packages],risk=risk,source_health=source_health,parked=parked))
    if fingerprint in {workflow.get_meta('last_digest_fingerprint'),workflow.get_meta('last_attempted_fingerprint')}:return {'state':'no_material_change'}
    if not known_packages and not workflow.get_meta('first_preparation_verified'):return {'state':'waiting_first_verified_preparation'}
    overview=export_agenda(workflow,now=now,native=native)
    folder=workflow.root/'deliveries'/day/fingerprint[:16];folder.mkdir(parents=True,exist_ok=True)
    durable=folder/'學業總覽.md'
    try:
        with durable.open('xb') as handle:handle.write(overview.read_bytes())
    except FileExistsError:
        pass
    artifacts=[dict(path=str(durable),sha256=hashlib.sha256(durable.read_bytes()).hexdigest())]
    for package in packages:
        artifacts.extend(package['payload']['artifacts'])
    artifacts=list({f['sha256']:f for f in artifacts}.values())
    active=[x for x in current.values() if not x['human_complete'] and x['human_state'] not in {'past_exam','closed'} and not (x['human_state']=='overdue' and x.get('due_at') and datetime.fromisoformat(x['due_at'])<clock-timedelta(days=7))]
    active.sort(key=lambda x:(x.get('confidence')=='needs_check',x.get('due_at') or '9999'))
    lines=['學業整理已更新。']
    for item in active[:2]:
        due=datetime.fromisoformat(item['due_at']).strftime('%m/%d %H:%M') if item.get('due_at') else '期限待確認'
        lines.append('• '+item.get('course','')+'｜'+item['title']+'：'+due+('，公告日期待核對' if item.get('confidence')=='needs_check' else ''))
    if packages:
        lines.append('已附 '+str(len(packages))+' 份驗證過的準備材料；本人作答／實作與提交仍待完成。')
    else:lines.append('完整待辦與查詢限制見附件；準備工作在背景繼續。')
    partial=[x for x in workflow.sources() if not x['complete']]
    if partial:lines.append('部分考試／信箱資料涵蓋不完整，附件已標示；不把未查到當作沒有。')
    if parked:lines.append('有 '+str(len(parked))+' 項背景準備已達修復上限，現有材料與限制保留在總覽。')
    key='digest:'+slot+':'+fingerprint
    payload=dict(type='digest',title='學業總覽與學習準備',message='\n'.join(lines),artifacts=artifacts,
        fingerprint=fingerprint,package_keys=[x['key'] for x in packages],source_versions={k:v['revision'] for k,v in current.items()},slot=slot,alert_keys=risk)
    with workflow.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        # Unique slot metadata and durable outbox are committed together.
        row=db.execute('SELECT value FROM metadata WHERE key=?',('digest_slot:'+slot,)).fetchone()
        if row and json.loads(row['value']):return {'state':'slot_already_prepared','key':json.loads(row['value'])}
        db.execute('INSERT OR IGNORE INTO outbox VALUES (?,?,?,?,?,?,?)',(key,'',fingerprint,encoded(payload),'pending',clock.isoformat(),None))
        db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('digest_slot:'+slot,encoded(key)))
        for package in packages:db.execute("UPDATE outbox SET state='bundled' WHERE key=?",(package['key'],))
    return {'state':'prepared','key':key,'packages':len(packages),'artifacts':len(artifacts)}
