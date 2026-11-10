from pathlib import Path
from datetime import datetime
import json
import unittest
from workflow import AcademicWorkflow,encoded,DeliveryRetry
from publisher import prepare_digest
from test_workflow import NativeTasks,Transport,fixture_root


def prepared():
    workflow=AcademicWorkflow(fixture_root());native=NativeTasks()
    source=dict(id='h1',kind='homework',title='力學習題',course='普通物理',description='練習牛頓第二定律',due_at='2026-11-15T23:59:00+08:00',submitted=False,closed=False)
    workflow.ingest('tronclass','homework',[source],complete=True)
    workflow.dispatch(native,now='2026-11-09T08:00:00+08:00')
    task=native.get('t_1');task['status']='review';workspace=Path(task['workspace'])
    item=json.loads((workspace/'source.json').read_text(encoding='utf-8'))
    doc=workspace/'學習材料.md';doc.write_text('# 牛頓第二定律\n'+('合力等於質量乘以加速度。先畫受力圖再選座標，單位為 N、kg、m/s²。這是自擬練習，不冒充原題；本人尚未作答或提交。\n'*3),encoding='utf-8')
    (workspace/'manifest.json').write_text(json.dumps(dict(item_key=item['key'],revision=item['revision'],artifacts=[str(doc)],summary='已整理力學練習方法。'),ensure_ascii=False),encoding='utf-8')
    workflow.reconcile(native,lambda *_:dict(accepted=True,readiness='limited',summary='自擬練習材料已準備，原題仍待核對。',limitations=['非原題答案']),now='2026-11-09T08:00:00+08:00')
    return workflow,native,source


class DigestTests(unittest.TestCase):
    def test_verifier_service_budget_survives_coordinator_restart(self):
        workflow,native,_=prepared()
        with workflow.connection() as db:db.execute('DELETE FROM outbox');db.execute('UPDATE work_links SET verified=NULL')
        native.get('t_1')['status']='review';calls=[]
        def unavailable(*args):calls.append(1);raise RuntimeError('service unavailable')
        for clock in ['2026-11-09T09:00:00+08:00','2026-11-09T10:00:00+08:00','2026-11-09T12:00:00+08:00','2026-11-10T09:00:00+08:00']:
            AcademicWorkflow(workflow.root).reconcile(native,unavailable,now=clock)
        self.assertEqual(len(calls),3)
        self.assertEqual(workflow.preparations()[0]['attempts'],0)
        self.assertEqual(workflow.preparations()[0]['error'],'verification_parked')

    def test_confirmed_attachment_is_not_resent_after_partial_digest_becomes_stale(self):
        workflow,native,_=prepared()
        package=workflow.publications()[0]
        second=workflow.root/'second.md';second.write_text('第二份材料\n'*100,encoding='utf-8')
        import hashlib
        package['payload']['artifacts'].append(dict(path=str(second),sha256=hashlib.sha256(second.read_bytes()).hexdigest()))
        with workflow.connection() as db:db.execute('UPDATE outbox SET payload=? WHERE key=?',(encoded(package['payload']),package['key']))
        class Partial(Transport):
            def send_artifact(self,path,caption):
                self.files.append(str(path))
                if Path(path)==second:raise DeliveryRetry(1)
                return {'message_id':'confirmed-'+str(len(self.files))}
        transport=Partial()
        prepare_digest(workflow,{'daily_hour':8},native=native,now='2026-11-09T08:00:00+08:00')
        workflow.publish(transport,now='2026-11-09T08:00:00+08:00',kinds={'digest'})
        workflow.ingest('user','manual',[dict(id='u1',kind='manual',title='專題')],complete=True)
        workflow.publish(transport,now='2026-11-09T08:01:00+08:00',kinds={'digest'})
        prepare_digest(workflow,{'daily_hour':8},native=native,now='2026-11-09T08:02:00+08:00')
        workflow.publish(transport,now='2026-11-09T08:02:00+08:00',kinds={'digest'})
        self.assertEqual(sum('學習材料.md' in x for x in transport.files),1)

    def test_urgent_deadline_uses_bounded_bands_instead_of_every_tick(self):
        workflow,native,source=prepared();transport=Transport();transport.retry_file=False
        source['due_at']='2026-11-09T12:00:00+08:00'
        workflow.ingest('tronclass','homework',[source],complete=True)
        # Existing verified preparation is from an earlier revision; urgent deadlines still
        # belong to the source agenda, while stale preparation must never be attached.
        workflow.set_meta('first_preparation_verified',True)
        result=prepare_digest(workflow,{'daily_hour':8},native=native,now='2026-11-09T06:30:00+08:00')
        self.assertEqual(result['state'],'prepared')
        workflow.publish(transport,now='2026-11-09T06:30:00+08:00',kinds={'digest'})
        for clock in ['2026-11-09T06:35:00+08:00','2026-11-09T07:30:00+08:00','2026-11-09T08:00:00+08:00']:
            prepare_digest(workflow,{'daily_hour':8},native=native,now=clock)
            workflow.publish(transport,now=clock,kinds={'digest'})
        self.assertEqual(len(transport.texts),1)

    def test_unrelated_source_change_keeps_verified_attachment_available(self):
        workflow,native,_=prepared();transport=Transport();transport.retry_file=False
        prepare_digest(workflow,{'daily_hour':8},native=native,now='2026-11-09T08:00:00+08:00')
        workflow.ingest('user','manual',[dict(id='m1',kind='manual',title='專題查詢',submitted=False)],complete=True)
        workflow.publish(transport,now='2026-11-09T08:01:00+08:00',kinds={'digest'})
        result=prepare_digest(workflow,{'daily_hour':8},native=native,now='2026-11-09T08:02:00+08:00')
        self.assertEqual(result['packages'],1)
        workflow.publish(transport,now='2026-11-09T08:02:00+08:00',kinds={'digest'})
        self.assertTrue(any('學習材料.md' in x for x in transport.files))

    def test_terminal_delivery_failure_cannot_starve_next_day(self):
        workflow,native,_=prepared()
        class Rejected(Transport):
            def send_text(self,text):raise ValueError('recipient rejected')
        prepare_digest(workflow,{'daily_hour':8},native=native,now='2026-11-09T08:00:00+08:00')
        workflow.publish(Rejected(),now='2026-11-09T08:00:00+08:00',kinds={'digest'})
        self.assertEqual(next(x for x in workflow.publications() if x['payload'].get('type')=='digest')['state'],'failed')
        workflow.ingest('user','manual',[dict(id='m1',kind='manual',title='專題查詢')],complete=True)
        prepare_digest(workflow,{'daily_hour':8},native=native,now='2026-11-10T08:00:00+08:00')
        transport=Transport();transport.retry_file=False
        workflow.publish(transport,now='2026-11-10T08:00:00+08:00',kinds={'digest'})
        self.assertEqual(len(transport.texts),1)

    def test_uncertain_text_send_is_not_repeated_or_followed_by_orphan_files(self):
        workflow,native,_=prepared()
        class Uncertain(Transport):
            def send_text(self,text):self.texts.append(text);raise TimeoutError('lost provider response')
        transport=Uncertain()
        prepare_digest(workflow,{'daily_hour':8},native=native,now='2026-11-09T08:00:00+08:00')
        workflow.publish(transport,now='2026-11-09T08:00:00+08:00',kinds={'digest'})
        AcademicWorkflow(workflow.root).publish(transport,now='2026-11-09T08:05:00+08:00',kinds={'digest'})
        self.assertEqual(len(transport.texts),1)
        self.assertEqual(transport.files,[])

    def test_changed_bytes_do_not_inherit_cached_acceptance(self):
        workflow,native,_=prepared()
        # Reproduce a crash after persisting acceptance but before queuing publication.
        with workflow.connection() as db:db.execute('DELETE FROM outbox')
        task=native.get('t_1');task['status']='review'
        doc=Path(task['workspace'])/'學習材料.md';doc.write_text('# changed\n'+('incorrect unverified answer '*30),encoding='utf-8')
        verdicts=[]
        def reject(*args):verdicts.append(args);return dict(accepted=False,reason='內容變更，需重新驗證')
        workflow.reconcile(native,reject,now='2026-11-09T09:00:00+08:00')
        self.assertEqual(len(verdicts),1)
        self.assertEqual(workflow.publications(),[])

    def test_malformed_verifier_reply_does_not_consume_worker_repair(self):
        workflow,native,_=prepared()
        with workflow.connection() as db:
            db.execute('DELETE FROM outbox');db.execute('UPDATE work_links SET verified=NULL')
        native.get('t_1')['status']='review'
        workflow.reconcile(native,lambda *_:json.loads('bad json'),now='2026-11-09T09:00:00+08:00')
        self.assertEqual(workflow.preparations()[0]['attempts'],0)
        self.assertEqual(native.get('t_1')['status'],'review')

    def test_sent_package_does_not_make_next_day_look_like_new_information(self):
        workflow,native,_=prepared();transport=Transport();transport.retry_file=False
        prepare_digest(workflow,{'daily_hour':8},native=native,now='2026-11-09T08:00:00+08:00')
        workflow.publish(transport,now='2026-11-09T08:00:00+08:00',kinds={'digest'})
        result=prepare_digest(workflow,{'daily_hour':8},native=native,now='2026-11-10T08:00:00+08:00')
        self.assertEqual(result['state'],'no_material_change')
        self.assertEqual(len(transport.texts),1)

    def test_changed_source_cannot_publish_an_old_digest(self):
        workflow,native,source=prepared();transport=Transport();transport.retry_file=False
        prepare_digest(workflow,{'daily_hour':8},native=native,now='2026-11-09T08:00:00+08:00')
        source['due_at']='2026-11-16T23:59:00+08:00'
        workflow.ingest('tronclass','homework',[source],complete=True)
        workflow.publish(transport,now='2026-11-09T08:01:00+08:00',kinds={'digest'})
        self.assertEqual(transport.texts,[])


if __name__=='__main__':unittest.main()
