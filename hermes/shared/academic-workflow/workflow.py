"""Durable academic source inbox, native-task coordination and publication journal."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import sqlite3
import time
from uuid import uuid4

TAIPEI=timezone(timedelta(hours=8))


class DeliveryRetry(Exception):
    def __init__(self,delay):self.delay=max(1,int(delay));super().__init__('Definite rejection; retry permitted')


def iso(value=None):
    if value is None:return datetime.now(TAIPEI).isoformat()
    parsed=datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value
    if parsed.tzinfo is None:raise ValueError('An explicit timezone is required')
    return parsed.astimezone(TAIPEI).isoformat()


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


class AcademicWorkflow:
    def __init__(self,root):
        self.root=Path(root).resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.db_path=self.root/'academic.db'
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS source_checks(
                  source TEXT,scope TEXT,observed_at TEXT,complete INTEGER,errors TEXT,
                  PRIMARY KEY(source,scope));
                CREATE TABLE IF NOT EXISTS items(
                  key TEXT PRIMARY KEY,source TEXT,scope TEXT,revision TEXT,payload TEXT,observed_at TEXT);
                CREATE TABLE IF NOT EXISTS versions(
                  item_key TEXT,revision TEXT,payload TEXT,observed_at TEXT,
                  PRIMARY KEY(item_key,revision));
                CREATE TABLE IF NOT EXISTS annotations(
                  item_key TEXT,source_revision TEXT,payload TEXT,
                  PRIMARY KEY(item_key,source_revision));
                CREATE TABLE IF NOT EXISTS work_links(
                  item_key TEXT,revision TEXT,task_id TEXT,attempts INTEGER DEFAULT 0,
                  verified TEXT,retry_at TEXT,error TEXT,
                  PRIMARY KEY(item_key,revision));
                CREATE TABLE IF NOT EXISTS outbox(
                  key TEXT PRIMARY KEY,item_key TEXT,revision TEXT,payload TEXT,state TEXT,
                  created_at TEXT,error TEXT);
                CREATE TABLE IF NOT EXISTS receipts(
                  outbox_key TEXT,part TEXT,state TEXT,receipt TEXT,updated_at TEXT,
                  PRIMARY KEY(outbox_key,part));
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
            ''')

    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.db_path,timeout=15)
        db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def ingest(self,source,scope,items,*,complete,observed_at=None,errors=()):
        observed_at=iso(observed_at)
        normalized=[]
        for raw in items:
            if not raw.get('id') or raw.get('kind') not in {'homework','exam','announcement','email','manual'}:
                raise ValueError('Source item requires stable id and supported kind')
            item=dict(raw)
            if item.get('due_at'):item['due_at']=iso(item['due_at'])
            if item.get('start_at'):item['start_at']=iso(item['start_at'])
            normalized.append(item)
        changed=0
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for item in normalized:
                key=source+'|'+str(item['id'])
                revision=digest(item)
                old=db.execute('SELECT revision FROM items WHERE key=?',(key,)).fetchone()
                changed+=int(old is None or old['revision']!=revision)
                db.execute('INSERT OR IGNORE INTO versions VALUES (?,?,?,?)',(key,revision,encoded(item),observed_at))
                db.execute('INSERT INTO items VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET scope=excluded.scope,revision=excluded.revision,payload=excluded.payload,observed_at=excluded.observed_at',
                    (key,source,scope,revision,encoded(item),observed_at))
            db.execute('INSERT INTO source_checks VALUES (?,?,?,?,?) ON CONFLICT(source,scope) DO UPDATE SET observed_at=excluded.observed_at,complete=excluded.complete,errors=excluded.errors',
                (source,scope,observed_at,int(complete),encoded(list(errors))))
        return {'observed':len(normalized),'changed':changed,'complete':bool(complete)}

    def sources(self):
        with self.connection() as db:
            return [dict(row,errors=json.loads(row['errors'])) for row in db.execute('SELECT * FROM source_checks ORDER BY source,scope')]

    def get_meta(self,key,default=None):
        with self.connection() as db:
            row=db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
            return json.loads(row['value']) if row else default

    def set_meta(self,key,value):
        with self.connection() as db:db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',(key,encoded(value)))

    def inbox_items(self):
        with self.connection() as db:
            rows=db.execute('SELECT i.*,a.payload AS annotation FROM items i LEFT JOIN annotations a ON a.item_key=i.key AND a.source_revision=i.revision').fetchall()
        return [dict(json.loads(r['payload']),key=r['key'],source_revision=r['revision'],
                     annotation=json.loads(r['annotation']) if r['annotation'] else None,observed_at=r['observed_at']) for r in rows]

    def annotate(self,key,revision,annotation):
        if type(annotation.get('actionable')) is not bool:raise ValueError('Actionability must be explicit')
        with self.connection() as db:
            row=db.execute('SELECT revision FROM items WHERE key=?',(key,)).fetchone()
            if not row or row['revision']!=revision:return False
            if annotation.get('alias_of'):
                target=db.execute('SELECT payload FROM items WHERE key=?',(annotation['alias_of'],)).fetchone()
                if not target or json.loads(target['payload'])['kind'] not in {'homework','exam'}:raise ValueError('Alias target must be an existing academic activity')
            db.execute('INSERT OR IGNORE INTO annotations VALUES (?,?,?)',(key,revision,encoded(annotation)))
        return True

    def agenda(self,*,now=None):
        clock=datetime.fromisoformat(iso(now))
        inbox=self.inbox_items()
        result=[]
        for raw in inbox:
            item={k:v for k,v in raw.items() if k not in {'annotation','observed_at'}}
            annotation=raw.get('annotation') or {}
            if annotation.get('alias_of'):continue
            if item.get('needs_interpretation') and annotation.get('actionable') is not True:continue
            item.update(annotation)
            related=[x for x in inbox if (x.get('annotation') or {}).get('alias_of')==item['key']]
            conflicts=sorted({x['annotation']['due_at'] for x in related if x['annotation'].get('due_at') and x['annotation']['due_at']!=item.get('due_at')})
            revision=digest([item['source_revision'],annotation,conflicts])
            if conflicts:item['deadline_conflicts']=conflicts
            due=datetime.fromisoformat(item['due_at']) if item.get('due_at') else None
            completed=item.get('submitted') is True
            closed=item.get('closed') is True
            state='completed' if completed else 'closed' if closed else 'overdue' if due and due<clock else 'open'
            next_action='來源已確認提交' if completed else '確認課程是否仍接受提交；準備補救詢問草稿' if closed else '先閱讀準備材料，再完成本人作答／實作'
            if item['kind']=='exam' and not completed:
                if due and due<clock:state='past_exam';next_action='考試時間已過，核對本人實際參加狀態'
                else:next_action='使用複習材料練習；本人參加考試'
            if item.get('action'):next_action=item['action']
            result.append(dict(item,revision=revision,observed_at=raw['observed_at'],related_sources=[x['key'] for x in related],
                human_complete=completed,human_state=state,next_action=next_action,
                start_by=(due-timedelta(days=2)).isoformat() if due and not completed else None,
                planning_status='provisional; timetable/calendar availability not yet verified'))
        result.sort(key=lambda x:(x['human_complete'],x.get('due_at') or '9999',x.get('course',''),x['title']))
        return result

    def preparations(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM work_links ORDER BY item_key,revision')]

    @contextmanager
    def lease(self,name,seconds=120):
        token=uuid4().hex
        acquired=False
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT value FROM metadata WHERE key=?',('lease:'+name,)).fetchone()
            if not row or json.loads(row['value'])['expires']<time.time():
                db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('lease:'+name,encoded(dict(token=token,expires=time.time()+seconds))))
                acquired=True
        try:yield acquired
        finally:
            if acquired:
                with self.connection() as db:
                    row=db.execute('SELECT value FROM metadata WHERE key=?',('lease:'+name,)).fetchone()
                    if row and json.loads(row['value'])['token']==token:
                        db.execute('UPDATE metadata SET value=? WHERE key=?',(encoded(dict(token=token,expires=0)),'lease:'+name))

    def dispatch(self,native,*,now=None,limit=2):
        clock=datetime.fromisoformat(iso(now));created=[]
        with self.lease('dispatch') as acquired:
            if not acquired:return []
            links=self.preparations()
            # Reconcile native commits lost before local mapping, including superseded source versions.
            for link in links:
                if link['task_id']:continue
                found=native.find('academic:prepare:'+digest([link['item_key'],link['revision']]))
                if found:
                    link['task_id']=found['id']
                    with self.connection() as db:db.execute('UPDATE work_links SET task_id=? WHERE item_key=? AND revision=?',(found['id'],link['item_key'],link['revision']))
            agenda=self.agenda(now=now)
            current_items={x['key']:x for x in agenda}
            for link in links:
                item=current_items.get(link['item_key'])
                if link['task_id'] and (not item or item['human_complete'] or item['human_state'] in {'closed','past_exam'}):
                    native.retire_preparation(link['task_id'])
            active_ids={x['task_id'] for x in links if x['task_id'] and (native.get(x['task_id']) or {}).get('status') in {'ready','running','review','todo'}}
            for item in agenda:
                current_link=next((x for x in links if x['item_key']==item['key'] and x['revision']==item['revision'] and x['task_id']),None)
                if current_link or item['human_complete'] or item['human_state'] in {'closed','past_exam'}:continue
                older=next((x for x in reversed(links) if x['item_key']==item['key'] and x['task_id']),None)
                if not older:continue
                task=native.get(older['task_id']) or {}
                if older['task_id'] not in active_ids and len(active_ids)>=limit:continue
                if task.get('status')=='running' and task.get('assignee')=='academic-validator':
                    native.finish_review(older['task_id'],accepted=False,summary='來源已更新，保留舊產物，改準備新版本。',repair=False)
                workspace=self.source_workspace(item)
                key='academic:prepare:'+digest([item['key'],item['revision']])
                with self.connection() as db:db.execute('INSERT OR IGNORE INTO work_links(item_key,revision) VALUES (?,?)',(item['key'],item['revision']))
                if native.refresh_preparation(item,workspace,key,older['task_id']):
                    with self.connection() as db:db.execute('UPDATE work_links SET task_id=?,error=NULL,retry_at=NULL WHERE item_key=? AND revision=?',(older['task_id'],item['key'],item['revision']))
                    created.append(older['task_id'])
                    active_ids.add(older['task_id'])
            links=self.preparations()
            active=sum(1 for task_id in {x['task_id'] for x in links if x['task_id']} if (native.get(task_id) or {}).get('status') in {'ready','running','review','todo'})
            for item in agenda:
                if active>=limit:break
                if item['human_complete'] or item['human_state']=='closed':continue
                if item['kind'] in {'announcement','email'} and item.get('actionable') is not True:continue
                due=datetime.fromisoformat(item['due_at']) if item.get('due_at') else None
                if due and due<clock-timedelta(days=7):continue
                if item['kind']=='exam' and (not due or due<=clock):continue
                with self.connection() as db:
                    link=db.execute('SELECT * FROM work_links WHERE item_key=? AND revision=?',(item['key'],item['revision'])).fetchone()
                    if link and link['task_id']:continue
                    if any(x['item_key']==item['key'] and x['revision']!=item['revision'] and x['task_id'] for x in links):continue
                    if link and link['retry_at'] and datetime.fromisoformat(link['retry_at'])>clock:continue
                    db.execute('INSERT OR IGNORE INTO work_links(item_key,revision) VALUES (?,?)',(item['key'],item['revision']))
                key='academic:prepare:'+digest([item['key'],item['revision']])
                workspace=self.source_workspace(item)
                try:
                    found=native.find(key)
                    task_id=found['id'] if found else native.create_preparation(item,workspace,key)
                    with self.connection() as db:
                        db.execute('UPDATE work_links SET task_id=?,error=NULL WHERE item_key=? AND revision=?',(task_id,item['key'],item['revision']))
                    created.append(task_id);active+=1
                except Exception as exc:
                    with self.connection() as db:
                        db.execute('UPDATE work_links SET error=?,retry_at=? WHERE item_key=? AND revision=?',(type(exc).__name__,(clock+timedelta(minutes=5)).isoformat(),item['key'],item['revision']))
        return created

    def source_workspace(self,item):
        workspace=self.root/'workspaces'/digest(item['key'])[:16]/item['revision'][:16]
        workspace.mkdir(parents=True,exist_ok=True)
        input_path=workspace/'source.json'
        if not input_path.exists():input_path.write_text(json.dumps(item,ensure_ascii=False,indent=2),encoding='utf-8')
        return workspace

    def recover_workers(self,native,*,now=None):
        current={x['key']:x['revision'] for x in self.agenda(now=now)}
        return [dict(task_id=x['task_id'],**native.recover_worker(x['task_id'],now=datetime.fromisoformat(iso(now)).timestamp()))
                for x in self.preparations() if x['task_id'] and current.get(x['item_key'])==x['revision'] and (native.get(x['task_id']) or {}).get('status')=='blocked']

    def publications(self):
        with self.connection() as db:
            return [dict(row,payload=json.loads(row['payload'])) for row in db.execute('SELECT * FROM outbox ORDER BY created_at,key')]

    def read_package(self,item,task):
        workspace=Path(task['workspace_path'] if 'workspace_path' in task else task['workspace']).resolve()
        manifest_path=workspace/'manifest.json'
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('item_key')!=item['key'] or manifest.get('revision')!=item['revision']:
            raise ValueError('artifact_source_revision_mismatch')
        artifacts=manifest.get('artifacts')
        if not isinstance(artifacts,list) or not artifacts:raise ValueError('no_artifacts')
        files=[]
        for value in artifacts:
            path=Path(value).resolve()
            if not path.is_relative_to(workspace) or not path.is_file():raise ValueError('artifact_outside_workspace_or_missing')
            if path.suffix.lower() not in {'.md','.txt','.csv','.ics'}:raise ValueError('unsupported_artifact_use_inspectable_markdown')
            data=path.read_bytes()
            if len(data)<128 or len(data)>20*1024*1024:raise ValueError('artifact_empty_or_oversized')
            if len(data.decode('utf-8'))>50000:
                raise ValueError('artifact_exceeds_complete_content_verification_budget')
            files.append(dict(path=str(path),sha256=hashlib.sha256(data).hexdigest(),
                text=data.decode('utf-8')))
        if not any(f['path'].lower().endswith('.md') for f in files):raise ValueError('study_markdown_required')
        return manifest,files

    def reconcile(self,native,verifier,*,now=None):
        clock=datetime.fromisoformat(iso(now));results=[]
        current={x['key']:x for x in self.agenda(now=now)}
        with self.lease('reconcile',seconds=1800) as acquired:
            if not acquired:return []
            for link in self.preparations():
                item=current.get(link['item_key'])
                if not item or item['revision']!=link['revision'] or not link['task_id']:continue
                if link['error']=='verification_parked':continue
                if link['retry_at'] and datetime.fromisoformat(link['retry_at'])>clock:continue
                task=native.get(link['task_id'])
                if not task or not (task['status'] in {'review','done'} or task['status']=='running' and task.get('assignee')=='academic-validator'):continue
                key='package:'+digest([item['key'],item['revision']])
                with self.connection() as db:
                    existing=db.execute('SELECT state FROM outbox WHERE key=?',(key,)).fetchone()
                    if existing and existing['state']!='superseded':continue
                stage='artifact'
                try:
                    manifest,files=self.read_package(item,task)
                    hashes={f['path']:f['sha256'] for f in files}
                    cached=json.loads(link['verified']) if link['verified'] else {}
                    stage='verification'
                    verdict=cached if cached.get('_artifact_hashes')==hashes else verifier(item,files)
                    if not isinstance(verdict,dict) or type(verdict.get('accepted')) is not bool:raise RuntimeError('invalid_verification_verdict')
                    stage='native'
                    if not verdict['accepted']:
                        repair=link['attempts']<1 and task['status']=='review'
                        reason=str(verdict.get('reason') or '內容驗證未通過；請依來源補正可用學習內容。')
                        if not native.finish_review(link['task_id'],accepted=False,summary=reason,repair=repair):continue
                        with self.connection() as db:
                            db.execute('UPDATE work_links SET attempts=attempts+1,verified=NULL,error=?,retry_at=NULL WHERE item_key=? AND revision=?',('content_review_failed',item['key'],item['revision']))
                        results.append(dict(task_id=link['task_id'],state='repair' if repair else 'blocked'))
                        continue
                    # Persist the verdict before the native transition; a crash after completion can recover it.
                    verdict=dict(verdict,_artifact_hashes=hashes)
                    with self.connection() as db:
                        db.execute('UPDATE work_links SET verified=?,error=NULL WHERE item_key=? AND revision=?',(encoded(verdict),item['key'],item['revision']))
                    summary=str(verdict.get('summary') or manifest.get('summary') or '學習準備材料已完成驗證。')
                    if task['status']!='done' and not native.finish_review(link['task_id'],accepted=True,summary=summary,artifacts=[f['path'] for f in files]):continue
                    frozen=[]
                    for file in files:
                        data=Path(file['path']).read_bytes()
                        if hashlib.sha256(data).hexdigest()!=file['sha256']:raise RuntimeError('artifact_changed_during_verification')
                        directory=self.root/'verified-artifacts'/file['sha256'];directory.mkdir(parents=True,exist_ok=True)
                        target=directory/Path(file['path']).name
                        try:
                            with target.open('xb') as handle:handle.write(data)
                        except FileExistsError:
                            if hashlib.sha256(target.read_bytes()).hexdigest()!=file['sha256']:raise RuntimeError('frozen_artifact_hash_mismatch')
                        frozen.append(dict(path=str(target),sha256=file['sha256']))
                    payload=dict(title=item['title'],course=item.get('course',''),summary=summary,
                        human_action=item['next_action'],due_at=item.get('due_at'),readiness=verdict.get('readiness','limited'),
                        limitations=verdict.get('limitations',[]),artifacts=frozen,
                        source_url=item.get('url'),source_item_id=item['id'])
                    with self.connection() as db:
                        db.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,state='pending',created_at=excluded.created_at,error=NULL WHERE outbox.state='superseded'",(key,item['key'],item['revision'],encoded(payload),'pending',clock.isoformat(),None))
                        db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('first_preparation_verified',encoded(True)))
                    results.append(dict(task_id=link['task_id'],state='verified'))
                except Exception as exc:
                    count=link['attempts']+1
                    if stage=='artifact' and task['status']=='review':
                        native.finish_review(link['task_id'],accepted=False,summary='產物檢查失敗：'+type(exc).__name__+'。請核對來源版本、manifest 及實際產物。',repair=count<=1)
                        retry=None
                    else:
                        retry_key='verify_errors:'+item['key']+':'+item['revision']
                        failures=self.get_meta(retry_key,0)+1
                        self.set_meta(retry_key,failures)
                        retry=(clock+timedelta(minutes=15 if failures==1 else 60)).isoformat()
                        if failures>=3:retry=None
                        count=link['attempts'] # model/service failures do not consume the one content repair
                    with self.connection() as db:
                        error='verification_parked' if stage!='artifact' and failures>=3 else type(exc).__name__
                        db.execute('UPDATE work_links SET attempts=?,error=?,retry_at=? WHERE item_key=? AND revision=?',(count,error,retry,item['key'],item['revision']))
                    results.append(dict(task_id=link['task_id'],state='parked' if error=='verification_parked' else 'waiting',error=type(exc).__name__))
        return results

    def delivery_receipts(self):
        with self.connection() as db:
            return [dict(row,receipt=json.loads(row['receipt']) if row['receipt'] else None) for row in db.execute('SELECT * FROM receipts ORDER BY outbox_key,part')]

    def publish(self,transport,*,now=None,limit=1,kinds=None):
        clock=datetime.fromisoformat(iso(now));results=[]
        current={x['key']:x['revision'] for x in self.agenda(now=now)}
        with self.lease('publish',seconds=1800) as acquired:
            if not acquired:return []
            for publication in self.publications():
                if len(results)>=limit:break
                payload=publication['payload']
                if publication['state'] in {'sent','superseded','failed','bundled','partial-unknown'}:continue
                if kinds is not None and payload.get('type','package') not in kinds:continue
                if publication['item_key'] and current.get(publication['item_key'])!=publication['revision']:
                    with self.connection() as db:db.execute("UPDATE outbox SET state='superseded' WHERE key=?",(publication['key'],))
                    continue
                if payload.get('source_versions') is not None and current!=payload['source_versions']:
                    with self.connection() as db:
                        db.execute("UPDATE outbox SET state='superseded' WHERE key=?",(publication['key'],))
                        for child in payload.get('package_keys',[]):
                            row=db.execute('SELECT payload FROM outbox WHERE key=?',(child,)).fetchone()
                            files=json.loads(row['payload']).get('artifacts',[]) if row else []
                            confirmed=files and all(db.execute("SELECT 1 FROM receipts WHERE part=? AND state='sent'",('artifact:'+f['sha256'],)).fetchone() for f in files)
                            db.execute("UPDATE outbox SET state=? WHERE key=? AND state='bundled'",('sent' if confirmed else 'pending',child))
                        slot=payload.get('slot')
                        if slot:db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('digest_slot:'+slot,encoded(None)))
                    continue
                key=publication['key']
                content=payload.get('message') or (payload.get('course','')+'｜'+payload['title']+'\n'+payload['summary']+'\n本人下一步：'+payload['human_action']+(('\n期限：'+payload['due_at']) if payload.get('due_at') else '')+'\n這是學習準備材料，尚不代表本人已完成或提交。')
                parts=[('text',None)]+[('artifact:'+f['sha256'],f) for f in payload.get('artifacts',[])]
                for part,file in parts:
                    if payload.get('source_versions') is not None and {x['key']:x['revision'] for x in self.agenda(now=now)}!=payload['source_versions']:break
                    with self.connection() as db:
                        old=db.execute('SELECT * FROM receipts WHERE outbox_key=? AND part=?',(key,part)).fetchone()
                        if not old and file:
                            prior=db.execute("SELECT * FROM receipts WHERE part=? AND outbox_key!=? ORDER BY CASE state WHEN 'sent' THEN 0 WHEN 'unknown' THEN 1 WHEN 'sending' THEN 2 ELSE 3 END,updated_at DESC LIMIT 1",(part,key)).fetchone()
                            if prior:
                                data=json.loads(prior['receipt'] or '{}');data['inherited_from']=prior['outbox_key']
                                inherited='unknown' if prior['state']=='sending' else prior['state']
                                db.execute('INSERT OR IGNORE INTO receipts VALUES (?,?,?,?,?)',(key,part,inherited,encoded(data),clock.isoformat()))
                                old=db.execute('SELECT * FROM receipts WHERE outbox_key=? AND part=?',(key,part)).fetchone()
                        previous=json.loads(old['receipt']) if old and old['receipt'] else {}
                        if old and old['state']=='sent':continue
                        if old and old['state'] in {'unknown','failed'}:break
                        if old and old['state']=='sending':
                            db.execute("UPDATE receipts SET state='unknown',updated_at=? WHERE outbox_key=? AND part=?",(clock.isoformat(),key,part))
                            break
                        if previous.get('retry_at') and datetime.fromisoformat(previous['retry_at'])>clock:break
                        attempts=previous.get('attempts',0)+1
                        db.execute('INSERT OR REPLACE INTO receipts VALUES (?,?,?,?,?)',(key,part,'sending',encoded({'attempts':attempts}),clock.isoformat()))
                    try:
                        if file:
                            path=Path(file['path'])
                            if hashlib.sha256(path.read_bytes()).hexdigest()!=file['sha256']:raise ValueError('artifact_changed_after_verification')
                            receipt=transport.send_artifact(path,payload.get('title','學業準備'))
                        else:receipt=transport.send_text(content)
                        if not receipt.get('message_id'):raise TimeoutError('No confirmed provider receipt')
                        state='sent';receipt=dict(message_id=str(receipt['message_id']),attempts=attempts)
                    except DeliveryRetry as exc:
                        state='retry' if attempts<3 else 'failed'
                        receipt=dict(attempts=attempts,retry_at=(clock+timedelta(seconds=exc.delay)).isoformat(),error_type=type(exc).__name__)
                    except ValueError as exc:
                        state='failed';receipt=dict(attempts=attempts,error_type=type(exc).__name__)
                    except Exception as exc:
                        state='unknown';receipt=dict(attempts=attempts,error_type=type(exc).__name__)
                    with self.connection() as db:
                        db.execute('UPDATE receipts SET state=?,receipt=?,updated_at=? WHERE outbox_key=? AND part=?',(state,encoded(receipt),clock.isoformat(),key,part))
                    if state!='sent':break
                with self.connection() as db:
                    statuses={r['part']:r['state'] for r in db.execute('SELECT part,state FROM receipts WHERE outbox_key=?',(key,))}
                    state='sent' if all(statuses.get(p)=='sent' for p,_ in parts) else 'partial-unknown' if 'unknown' in statuses.values() else 'failed' if 'failed' in statuses.values() else 'pending'
                    db.execute('UPDATE outbox SET state=? WHERE key=?',(state,key))
                    if state in {'sent','failed','partial-unknown'}:
                        for alert in payload.get('alert_keys',[]):db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('alert:'+alert,encoded(state)))
                    if state=='sent' and payload.get('type')=='digest':
                        db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('last_digest_fingerprint',encoded(payload['fingerprint'])))
                        for child in payload.get('package_keys',[]):db.execute("UPDATE outbox SET state='sent' WHERE key=?",(child,))
                    elif state in {'failed','partial-unknown'} and payload.get('type')=='digest':
                        db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('last_attempted_fingerprint',encoded(payload['fingerprint'])))
                        for child in payload.get('package_keys',[]):db.execute('UPDATE outbox SET state=? WHERE key=? AND state=\'bundled\'',(state,child))
                results.append(dict(key=key,state=state))
        return results


if __name__=='__main__':
    raise SystemExit('Use academic.py for the supported command interface.')
