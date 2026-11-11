from pathlib import Path
from unittest.mock import patch
import importlib.util
import json
import socket
import sys
import unittest
import urllib.error

from workflow import AcademicWorkflow,DeliveryRetry
from publisher import prepare_digest,prepare_health,scheduler_outcome,TelegramTransport
from operations import interpret_pending
from test_workflow import NativeTasks,Transport,fixture_root
from test_publication import prepared


class Rejected(Transport):
    def send_text(self,text):raise ValueError('recipient rejected')


def sent_ok():
    transport=Transport();transport.retry_file=False
    return transport


class DeliveryRecoveryTests(unittest.TestCase):
    def test_undelivered_digest_is_retried_the_same_day_after_backoff(self):
        workflow,native,_=prepared()
        prepare_digest(workflow,{'daily_hour':8},native=native,now='2026-11-09T08:00:00+08:00')
        workflow.publish(Rejected(),now='2026-11-09T08:00:00+08:00',kinds={'digest'})
        waiting=prepare_digest(workflow,{'daily_hour':8},native=native,now='2026-11-09T08:10:00+08:00')
        self.assertEqual(waiting['state'],'delivery_backoff')
        retried=prepare_digest(workflow,{'daily_hour':8},native=native,now='2026-11-09T08:31:00+08:00')
        self.assertEqual(retried['state'],'prepared')
        self.assertEqual(retried['packages'],1)
        transport=sent_ok()
        workflow.publish(transport,now='2026-11-09T08:31:00+08:00',kinds={'digest'})
        self.assertEqual(len(transport.texts),1)
        self.assertTrue(any('學習材料.md' in x for x in transport.files))
        self.assertIsNone(workflow.get_meta('digest_backoff'))

    def test_retry_gets_a_fresh_key_even_without_backoff_state(self):
        workflow,native,_=prepared()
        prepare_digest(workflow,{'daily_hour':8},native=native,now='2026-11-09T08:00:00+08:00')
        workflow.publish(Rejected(),now='2026-11-09T08:00:00+08:00',kinds={'digest'})
        workflow.delete_meta('digest_backoff')
        result=prepare_digest(workflow,{'daily_hour':8},native=native,now='2026-11-09T08:05:00+08:00')
        self.assertTrue(result['key'].endswith(':retry1'))
        transport=sent_ok()
        workflow.publish(transport,now='2026-11-09T08:05:00+08:00',kinds={'digest'})
        self.assertEqual(len(transport.texts),1)

    def test_backoff_grows_and_is_capped(self):
        workflow,native,_=prepared()
        clock='2026-11-09T08:00:00+08:00'
        for _ in range(6):
            prepare_digest(workflow,{'daily_hour':8},native=native,now=clock,force=True)
            workflow.publish(Rejected(),now=clock,kinds={'digest'})
        backoff=workflow.get_meta('digest_backoff')
        self.assertEqual(backoff['failures'],6)
        self.assertEqual(backoff['until'],'2026-11-09T14:00:00+08:00')

    def test_dns_failure_is_a_definite_retry_not_an_unknown_outcome(self):
        transport=TelegramTransport({});transport._credentials=('token','42')
        with patch('urllib.request.urlopen',side_effect=urllib.error.URLError(socket.gaierror(11001,'getaddrinfo failed'))):
            with self.assertRaises(DeliveryRetry):transport.send_text('hi')
        with patch('urllib.request.urlopen',side_effect=urllib.error.URLError(TimeoutError('read timed out'))):
            with self.assertRaises(TimeoutError):transport.send_text('hi')


class DigestContentTests(unittest.TestCase):
    def test_materials_verified_after_daily_digest_are_sent_the_same_day(self):
        workflow=AcademicWorkflow(fixture_root());native=NativeTasks()
        source=dict(id='h1',kind='homework',title='力學習題',course='普通物理',description='練習牛頓第二定律',due_at='2026-11-15T23:59:00+08:00',submitted=False,closed=False)
        workflow.ingest('tronclass','homework',[source],complete=True)
        workflow.dispatch(native,now='2026-11-09T07:00:00+08:00')
        workflow.set_meta('first_preparation_verified',True)
        prepare_digest(workflow,{'daily_hour':8},native=native,now='2026-11-09T08:00:00+08:00')
        transport=sent_ok()
        workflow.publish(transport,now='2026-11-09T08:00:00+08:00',kinds={'digest'})
        self.assertEqual(len(transport.texts),1)
        task=native.get('t_1');task['status']='review';workspace=Path(task['workspace'])
        item=json.loads((workspace/'source.json').read_text(encoding='utf-8'))
        doc=workspace/'學習材料.md';doc.write_text('# 牛頓第二定律\n'+('合力等於質量乘以加速度，先畫受力圖再選座標。\n'*10),encoding='utf-8')
        (workspace/'manifest.json').write_text(json.dumps(dict(item_key=item['key'],revision=item['revision'],artifacts=[str(doc)],summary='練習方法'),ensure_ascii=False),encoding='utf-8')
        workflow.reconcile(native,lambda *_:dict(accepted=True,readiness='limited',summary='力學練習已準備。',limitations=[]),now='2026-11-09T10:00:00+08:00')
        late=prepare_digest(workflow,{'daily_hour':8},native=native,now='2026-11-09T23:00:00+08:00')
        self.assertEqual(late['state'],'slot_already_prepared')
        result=prepare_digest(workflow,{'daily_hour':8},native=native,now='2026-11-09T10:00:00+08:00')
        self.assertEqual(result['state'],'prepared')
        self.assertIn(':materials:',result['key'])
        workflow.publish(transport,now='2026-11-09T10:00:00+08:00',kinds={'digest'})
        self.assertIn('新材料',transport.texts[-1])
        self.assertIn('附 1 份',transport.texts[-1])
        self.assertTrue(any('學習材料.md' in x for x in transport.files))

    def test_urgent_deadline_is_not_held_back_before_first_verified_preparation(self):
        workflow=AcademicWorkflow(fixture_root())
        workflow.ingest('tronclass','homework',[dict(id='h9',kind='homework',title='實驗報告',course='普物實驗',due_at='2026-11-09T12:00:00+08:00',submitted=False,closed=False)],complete=True)
        result=prepare_digest(workflow,{'daily_hour':8},now='2026-11-09T09:00:00+08:00')
        self.assertEqual(result['state'],'prepared')
        transport=sent_ok()
        workflow.publish(transport,now='2026-11-09T09:00:00+08:00',kinds={'digest'})
        self.assertIn('剩 3 小時',transport.texts[0])

    def test_digest_lists_more_items_with_countdown(self):
        workflow=AcademicWorkflow(fixture_root())
        workflow.set_meta('first_preparation_verified',True)
        items=[dict(id='h'+str(n),kind='homework',title='習題'+str(n),course='微積分',due_at='2026-11-1'+str(n)+'T23:59:00+08:00',submitted=False,closed=False) for n in range(1,8)]
        workflow.ingest('tronclass','homework',items,complete=True)
        prepare_digest(workflow,{'daily_hour':8},now='2026-11-09T08:00:00+08:00')
        transport=sent_ok()
        workflow.publish(transport,now='2026-11-09T08:00:00+08:00',kinds={'digest'})
        text=transport.texts[0]
        self.assertEqual(text.count('習題'),5)
        self.assertIn('＋2 項',text)
        self.assertLessEqual(len(text.splitlines()),8)
        self.assertIn('剩 2 天',text)


class HealthTests(unittest.TestCase):
    def test_persistent_source_failure_alerts_once_then_reports_recovery(self):
        workflow=AcademicWorkflow(fixture_root());config={'daily_hour':8}
        workflow.ingest('tronclass','collector',[],complete=False,errors=['RuntimeError'])
        self.assertEqual(prepare_health(workflow,config,now='2026-11-09T09:00:00+08:00')['state'],'unchanged')
        self.assertEqual(prepare_health(workflow,config,now='2026-11-09T11:00:00+08:00')['state'],'prepared')
        transport=sent_ok()
        workflow.publish(transport,now='2026-11-09T11:00:00+08:00',kinds={'health'})
        self.assertIn('TronClass',transport.texts[0])
        self.assertEqual(prepare_health(workflow,config,now='2026-11-09T13:00:00+08:00')['state'],'unchanged')
        workflow.ingest('tronclass','collector',[],complete=True)
        self.assertEqual(prepare_health(workflow,config,now='2026-11-09T13:05:00+08:00')['state'],'prepared')
        workflow.publish(transport,now='2026-11-09T13:05:00+08:00',kinds={'health'})
        self.assertIn('恢復',transport.texts[1])
        self.assertEqual(len(transport.texts),2)

    def test_benign_mail_limits_and_outlook_do_not_alert(self):
        workflow=AcademicWorkflow(fixture_root())
        workflow.ingest('mail:gmail','academic_candidates',[],complete=False,errors=['candidate_body_budget_reached','scan_budget_reached'])
        workflow.ingest('mail:outlook','academic_candidates',[],complete=False,errors=['not_authorized_or_unavailable'])
        prepare_health(workflow,{},now='2026-11-09T09:00:00+08:00')
        self.assertEqual(prepare_health(workflow,{},now='2026-11-09T15:00:00+08:00')['problems'],[])

    def test_night_problems_wait_for_daily_window(self):
        workflow=AcademicWorkflow(fixture_root())
        workflow.ingest('mail:gmail','academic_candidates',[],complete=False,errors=['not_authorized_or_unavailable'])
        prepare_health(workflow,{},now='2026-11-09T22:00:00+08:00')
        self.assertEqual(prepare_health(workflow,{},now='2026-11-10T03:00:00+08:00')['state'],'waiting_daily_window')
        self.assertEqual(prepare_health(workflow,{},now='2026-11-10T08:00:00+08:00')['state'],'prepared')

    def test_failed_health_alert_is_not_requeued_every_tick(self):
        workflow=AcademicWorkflow(fixture_root())
        workflow.ingest('tronclass','collector',[],complete=False,errors=['RuntimeError'])
        prepare_health(workflow,{},now='2026-11-09T09:00:00+08:00')
        prepare_health(workflow,{},now='2026-11-09T11:00:00+08:00')
        workflow.publish(Rejected(),now='2026-11-09T11:00:00+08:00',kinds={'health'})
        self.assertEqual(prepare_health(workflow,{},now='2026-11-09T11:05:00+08:00')['state'],'recently_attempted')
        self.assertEqual(prepare_health(workflow,{},now='2026-11-09T17:05:00+08:00')['state'],'prepared')

    def test_scheduler_crashes_alert_once_and_recovery_is_reported(self):
        workflow=AcademicWorkflow(fixture_root());config={'delivery_enabled':True};transport=sent_ok()
        states=[scheduler_outcome(workflow,config,ok=False,error_type='KeyError',now='2026-11-09T09:0'+str(n)+':00+08:00',transport=transport)['state'] for n in range(5)]
        self.assertEqual(states,['counted','counted','alert_sent','already_alerted','already_alerted'])
        self.assertEqual(scheduler_outcome(workflow,config,ok=True,transport=transport)['state'],'recovery_sent')
        self.assertEqual(scheduler_outcome(workflow,config,ok=True,transport=transport)['state'],'ok')
        self.assertEqual(len(transport.texts),2)


class SelfHealingTests(unittest.TestCase):
    def test_parked_interpretation_retries_after_a_day(self):
        workflow=AcademicWorkflow(fixture_root())
        workflow.ingest('user','academic',[dict(id='u1',kind='manual',title='考試',needs_interpretation=True)],complete=True)
        class Broken:
            count=0
            def json(self,*args):self.count+=1;raise RuntimeError('unavailable')
        model=Broken()
        for at in ['2026-11-09T08:00:00+08:00','2026-11-09T09:00:00+08:00','2026-11-09T11:00:00+08:00','2026-11-10T11:30:00+08:00']:
            interpret_pending(workflow,model,now=at)
        self.assertEqual(model.count,4)

    def test_parked_verification_retries_after_a_day(self):
        workflow,native,_=prepared()
        with workflow.connection() as db:db.execute('DELETE FROM outbox');db.execute('UPDATE work_links SET verified=NULL')
        native.get('t_1')['status']='review'
        def unavailable(*args):raise RuntimeError('service unavailable')
        for clock in ['2026-11-09T09:00:00+08:00','2026-11-09T10:00:00+08:00','2026-11-09T12:00:00+08:00']:
            workflow.reconcile(native,unavailable,now=clock)
        self.assertEqual(workflow.preparations()[0]['error'],'verification_parked')
        accepted=lambda *_:dict(accepted=True,readiness='limited',summary='恢復後驗證通過。',limitations=[])
        workflow.reconcile(native,accepted,now='2026-11-10T12:30:00+08:00')
        self.assertIsNone(workflow.preparations()[0]['error'])
        self.assertEqual(len(workflow.publications()),1)

    def test_legacy_parked_link_gets_a_retry_time(self):
        workflow,native,_=prepared()
        with workflow.connection() as db:
            db.execute('DELETE FROM outbox');db.execute("UPDATE work_links SET verified=NULL,error='verification_parked',retry_at=NULL")
        native.get('t_1')['status']='review'
        workflow.reconcile(native,lambda *_:1/0,now='2026-11-09T09:00:00+08:00')
        self.assertEqual(workflow.preparations()[0]['retry_at'],'2026-11-10T09:00:00+08:00')


class PayloadTests(unittest.TestCase):
    def test_app_update_falls_back_to_installed_payload(self):
        import native
        root=fixture_root();new=root/'Hermes_2'/'app/resources/agent-payload';(new/'hermes-agent').mkdir(parents=True)
        stale=str(root/'Hermes_1'/'app/resources/agent-payload')
        with patch.object(native,'installed_payloads',return_value=[new]):
            self.assertEqual(native.resolve_payload(stale),new)
        current=root/'current';(current/'hermes-agent').mkdir(parents=True)
        with patch.object(native,'installed_payloads',side_effect=AssertionError('no lookup needed')):
            self.assertEqual(native.resolve_payload(str(current)),current)
        with patch.object(native,'installed_payloads',return_value=[]):
            with self.assertRaises(FileNotFoundError):native.resolve_payload(str(root/'missing'))


class InstallerTests(unittest.TestCase):
    def load(self):
        spec=importlib.util.spec_from_file_location('academic_installer',Path(__file__).parents[1]/'install.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def test_install_copies_creates_config_and_protects_local_edits(self):
        installer=self.load();home=fixture_root()
        (home/'profiles/commander').mkdir(parents=True)
        report=installer.install(home)
        self.assertTrue((home/'profiles/hermes-agent/plugins/user-academic-workflow/__init__.py').is_file())
        self.assertTrue((home/'profiles/commander/scripts/academic-coordinator.py').is_file())
        config=json.loads((home/'shared/academic-workflow/config.json').read_text(encoding='utf-8'))
        self.assertEqual(config['hermes_root'],home.as_posix())
        self.assertEqual(report['conflicts'],[])
        edited=home/'shared/academic-workflow/workflow.py';edited.write_text('# local edit\n',encoding='utf-8')
        again=installer.install(home)
        self.assertEqual(again['conflicts'],[str(edited)])
        self.assertEqual(edited.read_text(encoding='utf-8'),'# local edit\n')
        forced=installer.install(home,force=True)
        self.assertEqual(len(forced['backed_up']),1)
        self.assertNotEqual(edited.read_text(encoding='utf-8'),'# local edit\n')
        self.assertIn('commander: 缺少 cron.script_timeout_seconds: 1500',installer.yaml_gaps(home))


if __name__=='__main__':unittest.main()
