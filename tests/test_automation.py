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
        self.assertIn('（3小時）',transport.texts[0])

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
        self.assertIn('（2天）',text)


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


def phone_width(line):
    # Chinese and full-width characters take a whole cell on the phone; digits and latin about half.
    return sum(1 if ord(c)>=0x2E80 else 0.55 for c in line)


class PhoneDisplayTests(unittest.TestCase):
    # The owner reads every bot on an iPhone; tables and code blocks wrap into an unreadable block.
    PHONE_LINE=21 # characters per line in the owner's Telegram screenshot

    def test_digest_has_no_tables_and_every_line_fits_two_phone_lines(self):
        workflow=AcademicWorkflow(fixture_root());workflow.set_meta('first_preparation_verified',True)
        items=[dict(id='h'+str(n),kind='homework',title='非常長的作業標題用來測試手機寬度第'+str(n)+'份',course='國際關係與亞太局勢',
            due_at='2026-11-1'+str(n)+'T23:59:00+08:00',submitted=False,closed=False) for n in range(1,8)]
        workflow.ingest('tronclass','homework',items,complete=True)
        prepare_digest(workflow,{'daily_hour':8},now='2026-11-09T08:00:00+08:00')
        transport=sent_ok();workflow.publish(transport,now='2026-11-09T08:00:00+08:00',kinds={'digest'})
        text=transport.texts[0]
        for bad in ('```','│','|---'):self.assertNotIn(bad,text)
        for line in text.splitlines():self.assertLessEqual(phone_width(line),self.PHONE_LINE,line)

    def test_long_course_and_title_are_cut_cleanly(self):
        from publisher import phone_name
        self.assertEqual(phone_name('作業系統','作業3'),'作業系統 作業3')
        self.assertEqual(phone_name('量子物理(一)','請各位同學先閱讀這篇長文'),'請各位同學先閱讀…')
        self.assertEqual(phone_name('固態物理導論','【期中考座位】'),'期中考座位')

    def test_telegram_academic_question_gets_phone_format_rule(self):
        import plugin
        ctx=type('Ctx',(),{'hooks':{},'register_hook':lambda s,n,h:s.hooks.__setitem__(n,h),'register_tool':lambda s,**k:None})()
        workflow=AcademicWorkflow(fixture_root())
        with patch.object(plugin,'load',return_value=({},workflow)):plugin.register(ctx)
        hook=ctx.hooks['pre_llm_call']
        self.assertIn('不要用表格',hook(platform='telegram',user_message='最近有什麼事情')['context'])
        self.assertNotIn('不要用表格',hook(platform='cli',user_message='最近有什麼事情')['context'])

    def test_doctor_flags_personality_files_that_ask_for_tables(self):
        import academic
        root=fixture_root()
        old='- Telegram 表格以三個反引號包住等寬純文字，使用 │ 分欄'
        for name,soul,bot in [('a',old,True),('b','- Telegram 手機排版：不要用表格、│ 分欄或三個反引號的程式碼區塊',True),('no-bot',old,False)]:
            (root/'profiles'/name).mkdir(parents=True);(root/'profiles'/name/'SOUL.md').write_text(soul,encoding='utf-8')
            if bot:(root/'profiles'/name/'.env').write_text('TELEGRAM_BOT_TOKEN=x\n',encoding='utf-8')
        self.assertEqual(academic.table_instructions(root),['profiles/a/SOUL.md'])

    def test_doctor_flags_schedule_prompts_and_skills_that_ask_for_tables(self):
        import academic,json as js
        root=fixture_root();bot=root/'profiles/mail'
        (bot/'skills/email/x').mkdir(parents=True);(bot/'cron').mkdir()
        (bot/'.env').write_text('TELEGRAM_BOT_TOKEN=x\n',encoding='utf-8')
        (bot/'SOUL.md').write_text('- 手機排版：不要用表格、│ 分欄',encoding='utf-8')
        (bot/'skills/email/x/SKILL.md').write_text('摘要用一張小表呈現',encoding='utf-8')
        # The real Mail Assistant prompt that produced a wrapped code-block table on the phone.
        table='最多五項，用三個反引號包住的小表，欄位為「信件／信箱 │ 需處理 │ 期限 │ 草稿狀態」'
        jobs=[dict(id='on',enabled=True,prompt=table),dict(id='off',enabled=False,prompt=table),
              dict(id='ok',enabled=True,prompt='訊息在手機上看：不要用表格、│ 分欄或三個反引號。'),
              dict(id='ok2',enabled=True,prompt='手機排版（不用表格、條列、少字）；不用表格或程式碼區塊。')]
        (bot/'cron/jobs.json').write_text(js.dumps({'jobs':jobs},ensure_ascii=False),encoding='utf-8-sig')
        self.assertEqual(academic.table_instructions(root),['profiles/mail/skills/email/x/SKILL.md','profiles/mail/cron/on'])


class PreparationOffTests(unittest.TestCase):
    def test_digest_and_overview_never_mention_materials_when_off(self):
        workflow,native,_=prepared()
        with workflow.connection() as db:db.execute('DELETE FROM outbox');db.execute("UPDATE work_links SET error='verification_parked'")
        workflow.delete_meta('first_preparation_verified')
        config={'daily_hour':8,'preparation_enabled':False}
        result=prepare_digest(workflow,config,native=native,now='2026-11-09T08:00:00+08:00')
        self.assertEqual(result['state'],'prepared')
        transport=sent_ok();workflow.publish(transport,now='2026-11-09T08:00:00+08:00',kinds={'digest'})
        self.assertNotIn('卡住',transport.texts[0]);self.assertNotIn('材料',transport.texts[0])
        overview=(workflow.root/'exports/學業總覽.md').read_text(encoding='utf-8')
        self.assertNotIn('已準備的材料',overview);self.assertNotIn('準備材料',overview)
        self.assertIn('完成作答／實作並提交',overview)

    def test_tick_opens_no_cards_when_off(self):
        import academic,contextlib,types
        workflow=AcademicWorkflow(fixture_root())
        workflow.ingest('tronclass','homework',[dict(id='h1',kind='homework',title='習題',due_at='2026-12-01T23:59:00+08:00',submitted=False)],complete=True)
        workflow.set_meta('last_collection_at','2099-01-01T00:00:00+08:00')
        native=NativeTasks()
        estop=types.ModuleType('agent.estop');estop.is_engaged=lambda:False
        with patch.dict(sys.modules,{'agent':types.ModuleType('agent'),'agent.estop':estop}), \
             patch.object(academic,'commander_scope',lambda c:contextlib.nullcontext()), \
             patch.object(academic,'NativeTasks',lambda c:native),patch.object(academic,'Model',lambda c:None), \
             patch.object(academic,'interpret_pending',lambda w,m:{'processed':0}):
            result=academic.tick({'preparation_enabled':False},workflow)
            self.assertEqual((result['dispatched'],native.created),([],0))
            academic.tick({},workflow)
        self.assertEqual(native.created,1)


class ModelChainTests(unittest.TestCase):
    profile={'model':{'default':'claude-sonnet-5-5-medium','provider':'antigravity-oauth','base_url':'agy://local'},
        'fallback_model':[{'provider':'antigravity-oauth','model':'gemini-3.8-flash-medium','base_url':'agy://local'},
                          {'provider':'antigravity-oauth','model':'gemini-4-argon-medium','base_url':'agy://local'}]}

    def test_verification_tries_its_own_provider_then_the_commander_chain(self):
        from native import model_chain
        chain=model_chain({'verification_model':'gpt-6.1-sol','verification_provider':'openai-codex'},self.profile,verification=True)
        self.assertEqual([(x['provider'],x['model']) for x in chain],[('openai-codex','gpt-6.1-sol'),
            ('antigravity-oauth','claude-sonnet-5-5-medium'),('antigravity-oauth','gemini-3.8-flash-medium'),('antigravity-oauth','gemini-4-argon-medium')])
        self.assertIsNone(chain[0]['base_url'])
        self.assertEqual(len(model_chain({'verification_model':'gpt-6.1-sol'},self.profile)),3)

    def test_exhausted_quota_moves_to_the_next_model(self):
        from native import model_chain,first_answer
        tried=[]
        def call(entry):
            tried.append(entry['model'])
            if entry['model']!='gemini-3.8-flash-medium':raise RuntimeError('Individual quota reached')
            return 'answer'
        self.assertEqual(first_answer(model_chain({},self.profile),call),'answer')
        self.assertEqual(tried,['claude-sonnet-5-5-medium','gemini-3.8-flash-medium'])
        with self.assertRaises(RuntimeError) as caught:first_answer(model_chain({},self.profile),lambda e:1/0)
        self.assertIn('gemini-4-argon-medium',str(caught.exception))


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


class BriefTests(unittest.TestCase):
    """"目前有什麼事情" answers come from brief.py, so it must list every pending item with its real date."""
    NOW='2026-11-13T16:00:00+08:00'

    def build(self,agenda,trips):
        import brief,tempfile,json as js
        folder=Path(tempfile.mkdtemp());path=folder/'opportunities.json'
        path.write_text(js.dumps({'opportunities':trips},ensure_ascii=False),encoding='utf-8')
        workflow=type('W',(),{'agenda':lambda self,now=None:agenda})()
        return brief.build({'trip_opportunities':str(path)},workflow,now=self.NOW)

    def test_lists_every_item_inside_the_window_with_its_date(self):
        agenda=[dict(course='相對論',title='請假和補考',due_at='2026-11-13T16:40:00+08:00',human_state='open',confidence='needs_check'),
                dict(course='',title='助學講習',due_at='2026-11-17T12:30:00+08:00',human_state='open'),
                dict(course='作業系統',title='作業3',due_at='2026-11-18T00:00:00+08:00',human_state='open'),
                dict(course='國關',title='期中報告',due_at='2026-12-04T23:59:00+08:00',human_state='open'),
                dict(course='量子',title='讀長文',human_state='open'),
                dict(course='服飾',title='第五次考試',due_at='2026-11-09T15:40:00+08:00',human_state='past_exam')]
        text=self.build(agenda,[])['text']
        for expected in ('今天16:40＊','＊日期由公告推導，待核對','11/17（二）','11/18（三）','另有 1 項較晚、1 項日期待確認'):self.assertIn(expected,text)
        self.assertNotIn('第五次考試',text)
        self.assertNotIn('|',text)  # phone format: no table

    def test_trips_waiting_for_a_decision_are_never_dropped(self):
        trips=[dict(id='a',title='阿里山兩天一夜看日出（雙人）',status='不符合',user_decision={'status':'pending'},outbound_date='2026-12-19',return_date='2026-12-20'),
               dict(id='b',title='大阪機票',status='不符合',user_decision={'status':'declined'},outbound_date='2026-12-27',return_date='2027-01-12'),
               dict(id='c',title='舊行程',status='已選定',outbound_date='2026-10-01',return_date='2026-10-02')]
        result=self.build([],trips)
        self.assertEqual([x['id'] for x in result['trips']],['a'])
        self.assertIn('12/19–12/20',result['text']);self.assertIn('待你決定',result['text'])

    def test_a_record_that_contradicts_itself_is_flagged_not_resolved(self):
        # The real 礁溪 record: an overnight 12/06–12/07 plan with a one-day 12/07 hold at a 烏來 venue.
        trip=dict(id='x',title='宜蘭礁溪兩天一夜溫泉規劃',destination='台灣宜蘭礁溪',status='已選定',user_decision='pending',
                  outbound_date='2026-12-06',return_date='2026-12-07',
                  calendar_hold_check={'details':'2026-12-07T09:00–19:00+08:00，busy_status=tentative'},
                  venue_research={'venue':'烏來湯布苑溫泉館（新北市烏來區）'})
        text=self.build([],[trip])['text']
        self.assertIn('12/06–12/07',text);self.assertIn('⚠️紀錄矛盾：天數、地點',text)

    def test_missing_trip_file_is_reported_as_unconfirmed(self):
        import brief
        workflow=type('W',(),{'agenda':lambda self,now=None:[]})()
        text=brief.build({'trip_opportunities':'Z:/nope.json'},workflow,now=self.NOW)['text']
        self.assertIn('未確認',text)


class ScheduledDeliveryTests(unittest.TestCase):
    def test_doctor_reports_when_every_recent_send_failed(self):
        import academic
        workflow,native,_=prepared()
        self.assertIsNone(academic.delivery_problem(workflow,now='2026-11-09T08:00:00+08:00'))
        prepare_digest(workflow,{'daily_hour':8},native=native,now='2026-11-09T08:00:00+08:00')
        workflow.publish(Rejected(),now='2026-11-09T08:00:00+08:00',kinds={'digest'})
        problem=academic.delivery_problem(workflow,now='2026-11-09T09:00:00+08:00')
        self.assertIn('全部失敗',problem)
        self.assertIsNone(academic.delivery_problem(workflow,now='2026-11-12T09:00:00+08:00')) # older than 48h
        prepare_digest(workflow,{'daily_hour':8},native=native,now='2026-11-09T08:31:00+08:00')
        workflow.publish(sent_ok(),now='2026-11-09T08:31:00+08:00',kinds={'digest'})
        self.assertIsNone(academic.delivery_problem(workflow,now='2026-11-09T09:00:00+08:00'))

    def test_commander_scope_binds_commander_secrets_when_already_running_as_commander(self):
        # The scheduler runs the coordinator with HERMES_HOME=profiles/commander and a secret-free env;
        # delegated_profile_scope is a no-op there, which left TELEGRAM_BOT_TOKEN empty for days.
        import native,types,contextlib
        home=Path('C:/h/profiles/commander');bound=[]
        def fakes(current):
            constants=types.ModuleType('hermes_constants')
            constants.get_hermes_home=lambda:current;constants.hermes_home_key=lambda p:Path(p).as_posix().lower()
            scope=types.ModuleType('agent.secret_scope')
            scope.build_profile_secret_scope=lambda h:{'TELEGRAM_BOT_TOKEN':'t'}
            scope.set_secret_scope=lambda m,profile_home=None:bound.append((m,profile_home)) or 'token'
            scope.reset_secret_scope=lambda token:bound.append('reset')
            loader=types.ModuleType('hermes_cli.env_loader');loader.hydrate_profile_secret_sources=lambda h:{}
            routes=types.ModuleType('tools.delegate_tool_routes')
            routes.delegated_profile_scope=lambda h:(bound.append('delegated'),contextlib.nullcontext())[1]
            return {'hermes_constants':constants,'agent':types.ModuleType('agent'),'agent.secret_scope':scope,
                'hermes_cli':types.ModuleType('hermes_cli'),'hermes_cli.env_loader':loader,
                'tools':types.ModuleType('tools'),'tools.delegate_tool_routes':routes}
        config={'hermes_root':'C:/h'}
        with patch.object(native,'bootstrap',lambda c:None):
            with patch.dict(sys.modules,fakes(home)):
                with native.commander_scope(config):pass
            self.assertEqual(bound,[({'TELEGRAM_BOT_TOKEN':'t'},str(home)),'reset'])
            bound.clear()
            with patch.dict(sys.modules,fakes(Path('C:/h'))):
                with native.commander_scope(config):pass
            self.assertEqual(bound,['delegated'])


class StatusQuestionTests(unittest.TestCase):
    def test_status_questions_are_answered_from_the_complete_brief(self):
        # A Flash model answered "最近有什麼重要的事情嗎" by picking five items from action=status,
        # dropping the travel section and the later/undated counts.
        import plugin
        tools={}
        ctx=type('Ctx',(),{'hooks':{},'register_hook':lambda s,n,h:s.hooks.__setitem__(n,h),
            'register_tool':lambda s,**k:tools.__setitem__(k['name'],k)})()
        workflow=AcademicWorkflow(fixture_root())
        workflow.ingest('tronclass','homework',[dict(id='h1',kind='homework',title='作業3',course='作業系統',
            due_at='2099-01-01T23:59:00+08:00',submitted=False)],complete=True)
        with patch.object(plugin,'load',return_value=({'trip_opportunities':'Z:/nope.json'},workflow)):plugin.register(ctx)
        context=ctx.hooks['pre_llm_call'](platform='telegram',user_message='最近有什麼重要的事情嗎')['context']
        self.assertIn('action=brief',context);self.assertIn('不可刪項',context)
        tool=tools['academic_workflow']
        self.assertIn('brief',tool['schema']['parameters']['properties']['action']['enum'])
        text=json.loads(tool['handler']({'action':'brief'}))['text']
        self.assertIn('📚 學業',text);self.assertIn('🧳 旅遊',text);self.assertIn('1 項較晚',text)
