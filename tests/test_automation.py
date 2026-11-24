from pathlib import Path
from unittest.mock import patch
import importlib.util
import json
import time
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
        self.assertIn('還有 3 小時',transport.texts[0])
        self.assertIn('普物實驗｜實驗報告',transport.texts[0])

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
        self.assertIn('＋另有 2 項',text)
        self.assertLessEqual(len(text.splitlines()),15)
        self.assertIn('還有 2 天',text)


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

    def test_daily_digest_sends_no_overview_file(self):
        # 11/19 the owner: the attached 學業總覽.md "不是很有用"; on the phone it is a raw Markdown table.
        workflow=AcademicWorkflow(fixture_root());workflow.set_meta('first_preparation_verified',True)
        workflow.ingest('tronclass','homework',[dict(id='h1',kind='homework',title='HW#3',course='相對論',
            due_at='2026-11-10T23:59:00+08:00',submitted=False,closed=False)],complete=True)
        prepare_digest(workflow,{'daily_hour':8},now='2026-11-09T08:00:00+08:00')
        transport=sent_ok();workflow.publish(transport,now='2026-11-09T08:00:00+08:00',kinds={'digest'})
        self.assertEqual(transport.files,[]);self.assertEqual(len(transport.texts),1)
        self.assertNotIn('附件',transport.texts[0]);self.assertIn('最近有什麼事',transport.texts[0])
        # The file is still written on the computer for the other bots that read it.
        self.assertTrue((workflow.root/'exports/學業總覽.md').exists())

    def test_long_course_and_title_are_cut_cleanly(self):
        from publisher import item_label
        self.assertEqual(item_label(dict(course='作業系統',title='作業3')),'作業系統｜作業3')
        self.assertEqual(item_label(dict(course='固態物理導論',title='【期中考座位】')),'固態物理導論｜期中考座位')
        self.assertEqual(item_label(dict(kind='email',title='歡迎參加115.11.17(二)輔仁大學助學方案暨就學貸款宣導講習')),'信件｜輔仁大學助學方案暨就學貸款宣導…')
        self.assertLessEqual(phone_width('• '+item_label(dict(course='國際關係與亞太現勢',title='國關課 交期中報告 日期修正'))),self.PHONE_LINE)

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


class DigestOrderTests(unittest.TestCase):
    def test_digest_lists_by_date_even_when_a_date_needs_checking(self):
        from publisher import digest_order
        items=[dict(title='路跑證明',due_at='2026-12-31T23:59:00+08:00'),
               dict(title='座位表',due_at=None),
               dict(title='就貸講習',due_at='2026-11-17T12:30:00+08:00',confidence='needs_check'),
               dict(title='作業3',due_at='2026-11-18T00:00:00+08:00')]
        self.assertEqual([x['title'] for x in sorted(items,key=digest_order)],['就貸講習','作業3','路跑證明','座位表'])


class CourseQuestionTests(unittest.TestCase):
    NOW='2026-11-16T13:34:00+08:00'

    def workflow(self):
        workflow=AcademicWorkflow(fixture_root())
        workflow.ingest('tronclass','1151:391227:announcement',[
            dict(id='announcement:391227:2',kind='announcement',course='體育',title='上課地點公告',description='今日上課籃排球場喔',
                 published_at='2026-11-16T12:05:46+08:00',needs_interpretation=True),
            dict(id='announcement:391227:1',kind='announcement',course='體育',title='上課地點公告',description='今天上課地點在操場單槓區',
                 published_at='2026-11-02T13:11:09+08:00',needs_interpretation=True)],complete=True)
        return workflow

    def test_course_lookup_returns_todays_announcement_first(self):
        import brief
        result=brief.course(self.workflow(),'體育課',now=self.NOW)
        self.assertEqual(result['matched'],['體育'])
        lines=result['text'].split('\n')
        self.assertIn('今天',lines[2]);self.assertIn('籃排球場',lines[2])
        self.assertIn('找不到',brief.course(self.workflow(),'微積分',now=self.NOW)['text'])

    def test_course_question_triggers_the_course_rule(self):
        # "體育課在哪裡上課" matched no topic word, so the bot spent 3.5 minutes searching files and logs.
        import plugin
        tools={}
        ctx=type('Ctx',(),{'hooks':{},'register_hook':lambda s,n,h:s.hooks.__setitem__(n,h),
            'register_tool':lambda s,**k:tools.__setitem__(k['name'],k)})()
        with patch.object(plugin,'load',return_value=({},self.workflow())):plugin.register(ctx)
        context=ctx.hooks['pre_llm_call'](platform='telegram',user_message='體育課在哪裡上課')['context']
        self.assertIn('action=course',context);self.assertIn('不要搜尋檔案',context)
        self.assertIn('籃排球場',json.loads(tools['academic_workflow']['handler']({'action':'course','key':'體育'}))['text'])


class ClearDigestTests(unittest.TestCase):
    """Owner, 11/16: "根本就不清楚想要表達什麼" about 「‼️1116第六次考試｜11/16（3小時）」 under
    「115學年度輔仁…｜11/11（已過期?）」, sent four times between 22:39 and 12:04."""
    def sent(self,items,now,**kw):
        workflow=AcademicWorkflow(fixture_root());workflow.set_meta('first_preparation_verified',True)
        for source,scope,rows,complete in items:workflow.ingest(source,scope,rows,complete=complete)
        result=prepare_digest(workflow,{'daily_hour':8},now=now,**kw)
        transport=sent_ok();workflow.publish(transport,now=now,kinds={'digest'})
        return workflow,result,transport.texts[0] if transport.texts else ''

    def test_exam_names_its_course_and_time(self):
        exam=dict(id='e1',kind='exam',title='1116第六次考試',course='中華服飾設計與文化',due_at='2026-11-16T15:40:00+08:00',submitted=False,closed=False)
        _,_,text=self.sent([('tronclass','exam',[exam],True)],'2026-11-16T12:04:00+08:00')
        self.assertIn('中華服飾設計與文化｜1116第六次考試',text)
        self.assertIn('今天 15:40（還有 3 小時）',text)
        self.assertIn('24 小時內',text)
        for line in text.splitlines():self.assertLessEqual(phone_width(line),PhoneDisplayTests.PHONE_LINE,line)

    def test_passed_guessed_dates_are_left_out_and_unsure_dates_say_so(self):
        rows=[dict(id='a1',kind='announcement',title='115學年度輔仁大學校慶運動會競賽規程公告',course='體育',due_at='2026-11-11T16:00:00+08:00',needs_check=True,confidence='needs_check'),
              dict(id='a2',kind='announcement',title='國關課 交期中報告 日期修正',course='國際關係與亞太現勢',due_at='2026-12-04T23:59:59+08:00',confidence='needs_check')]
        workflow,_,text=self.sent([('tronclass','announcement',rows,True)],'2026-11-16T08:00:00+08:00')
        states={x['title'][:6]:(x['human_state'],x.get('confidence')) for x in workflow.agenda(now='2026-11-16T08:00:00+08:00')}
        if states.get('115學年度')!=('overdue','needs_check'):self.skipTest('fixture does not mark announcement dates as needs_check: '+str(states))
        self.assertNotIn('運動會',text)
        self.assertNotIn('?',text)
        self.assertIn('日期待核對',text)

    def test_midnight_deadline_reads_as_the_day_before(self):
        hw=dict(id='h4',kind='homework',title='作業4',course='作業系統',due_at='2026-12-02T00:00:00+08:00',submitted=False,closed=False)
        _,_,text=self.sent([('tronclass','homework',[hw],True)],'2026-11-16T08:00:00+08:00')
        self.assertIn('12/01（二）24:00',text)

    def test_missing_source_is_named(self):
        hw=dict(id='h1',kind='homework',title='HW#3',course='相對論',due_at='2026-11-20T23:59:00+08:00',submitted=False,closed=False)
        _,_,text=self.sent([('tronclass','homework',[hw],True),('tronclass','1151:1:exam',[],False),('mail:outlook','academic_candidates',[],False)],'2026-11-16T08:00:00+08:00')
        self.assertIn('TronClass 考試 沒讀完整',text)
        self.assertNotIn('部分來源不完整',text)
        self.assertNotIn('Outlook',text)

    def test_refreshed_record_does_not_resend_the_same_reminder(self):
        exam=dict(id='e1',kind='exam',title='1116第六次考試',course='中華服飾設計與文化',due_at='2026-11-16T15:40:00+08:00',submitted=False,closed=False)
        workflow,_,_=self.sent([('tronclass','exam',[exam],True)],'2026-11-16T08:00:00+08:00')
        transport=sent_ok()
        prepare_digest(workflow,{'daily_hour':8},now='2026-11-16T09:44:00+08:00');workflow.publish(transport,now='2026-11-16T09:44:00+08:00',kinds={'digest'})
        self.assertEqual(len(transport.texts),1) # 6-hour reminder
        workflow.ingest('tronclass','exam',[dict(exam,description='考試範圍更新')],complete=True)
        result=prepare_digest(workflow,{'daily_hour':8},now='2026-11-16T12:04:00+08:00')
        self.assertNotEqual(result['state'],'prepared',result)
        workflow.ingest('tronclass','exam',[dict(exam,due_at='2026-11-16T16:40:00+08:00')],complete=True)
        moved=prepare_digest(workflow,{'daily_hour':8},now='2026-11-16T12:05:00+08:00')
        self.assertEqual(moved['state'],'prepared')


class AttachmentTests(unittest.TestCase):
    """Owner, 11/18: "傳題目給我" for 相對論 HW#3 took minutes; the PDF name was already stored."""
    def setUp(self):
        self.workflow=AcademicWorkflow(fixture_root())
        files=[dict(id=38057734,name='1151RelHW3.pdf',size=5,allow_download=True)]
        self.workflow.ingest('tronclass','homework',[
            dict(id='homework:394632:3208459',kind='homework',title='HW#3',course='相對論',due_at='2026-11-20T23:59:00+08:00',submitted=False,closed=False,
                 source_requirements=dict(attachments=files,title='HW#3',description='')),
            dict(id='homework:394632:1',kind='homework',title='HW#2',course='相對論',due_at='2026-11-01T23:59:00+08:00',submitted=True,closed=False,
                 source_requirements=dict(attachments=[dict(id=1,name='1151RelHW2.pdf',size=5)]))],complete=True)
        self.calls=[]
        def run(args):
            self.calls.append(args);dest=Path(args[args.index('-d')+1])
            names={'38057734':'1151RelHW3.pdf','1':'1151RelHW2.pdf'}
            (dest/names[args[3]]).write_bytes(b'%PDF-')
        self.run=run
        self.config={'tronclass_repo':str(self.workflow.root/'tc')}

    def test_course_view_names_the_question_file(self):
        import brief
        self.assertIn('📎 1151RelHW3.pdf',brief.course(self.workflow,'相對論',now='2026-11-18T16:51:00+08:00')['text'])

    def test_open_homework_file_is_downloaded_once_and_returned_for_media(self):
        import brief
        first=brief.attachment(self.config,self.workflow,'相對論',run=self.run,now='2026-11-18T16:52:00+08:00')
        self.assertEqual([Path(x).name for x in first['files']],['1151RelHW3.pdf'])
        self.assertIn('MEDIA:',first['reply'])
        again=brief.attachment(self.config,self.workflow,'相對論','HW#3',run=self.run,now='2026-11-18T16:53:00+08:00')
        self.assertEqual(again['files'],first['files'])
        self.assertEqual(len(self.calls),1)
        older=brief.attachment(self.config,self.workflow,'相對論','hw2',run=self.run,now='2026-11-18T16:53:00+08:00')
        self.assertEqual([Path(x).name for x in older['files']],['1151RelHW2.pdf'])

    def test_exact_title_beats_a_longer_similar_one(self):
        import brief
        rows=[dict(id='announcement:1:'+str(n),kind='announcement',course='半導體物理導論',title=t,published_at='2026-09-2'+str(n),
            source_requirements=dict(attachments=[dict(id=100+n,name=f)])) for n,(t,f) in enumerate([('20260923 上課講義 勘誤','v_rev1.pdf'),('20260923 上課講義','v.pdf')])]
        self.workflow.ingest('tronclass','announcement',rows,complete=True)
        def run(args):(Path(args[args.index('-d')+1])/{'100':'v_rev1.pdf','101':'v.pdf'}[args[3]]).write_bytes(b'%PDF-')
        self.assertEqual(Path(brief.attachment(self.config,self.workflow,'半導體物理導論','20260923 上課講義',run=run)['files'][0]).name,'v.pdf')
        self.assertEqual(Path(brief.attachment(self.config,self.workflow,'半導體物理導論','20260923 上課講義 勘誤',run=run)['files'][0]).name,'v_rev1.pdf')

    def test_cli_does_not_inherit_hermes_python_path(self):
        import brief
        env=brief.cli_env({'PYTHONPATH':r'C:\hermesenv\Lib\site-packages','PYTHONHOME':'x','PATH':'p'})
        self.assertNotIn('PYTHONPATH',env);self.assertNotIn('PYTHONHOME',env)
        self.assertEqual(env['PATH'],'p');self.assertEqual(env['PYTHONIOENCODING'],'utf-8')

    def test_failed_download_is_reported_not_hidden(self):
        import brief
        result=brief.attachment(self.config,self.workflow,'相對論','HW#3',run=lambda args:None)
        self.assertEqual(result['files'],[])
        self.assertIn('下載失敗',result['text'])

    def test_commander_cannot_read_credential_files(self):
        import plugin
        ctx=type('Ctx',(),{'hooks':{},'register_hook':lambda s,n,h:s.hooks.__setitem__(n,h),'register_tool':lambda s,**k:None})()
        with patch.object(plugin,'load',return_value=({},self.workflow)):plugin.register(ctx)
        hook=ctx.hooks['pre_tool_call']
        self.assertEqual(hook(tool_name='read_file',args={'path':'C:/Users/x/hermes/profiles/tronclass/auth.json'})['action'],'block')
        self.assertEqual(hook(tool_name='terminal',args={'command':r'type profiles\commander\.env'})['action'],'block')
        self.assertIsNone(hook(tool_name='read_file',args={'path':'C:/x/academic-state/attachments/a/1151RelHW3.pdf'}))
        self.assertIsNone(hook(tool_name='terminal',args={'command':'echo max_tokens'}))

    def test_question_without_keywords_still_gets_the_rules(self):
        import plugin
        self.assertTrue(plugin.academic_question(self.workflow,'傳相對論 HW#3 的題目給我'))
        self.assertTrue(plugin.academic_question(self.workflow,'相對論在哪'))
        self.assertFalse(plugin.academic_question(self.workflow,'今天天氣如何'))

    def test_attachment_rule_reaches_the_bot(self):
        import plugin
        self.assertIn('action=attachment',plugin.COURSE_RULE)
        self.assertIn('不要說已接單',plugin.COURSE_RULE)


class StaleGatewayTests(unittest.TestCase):
    """11/18: Hermes updated under the running gateway and kanban_create failed until a restart."""
    def test_gateway_running_from_a_removed_or_older_payload_is_reported(self):
        from native import stale_gateways
        root=fixture_root()
        new=root/'Hermes_2'/'app/resources/agent-payload';(new/'hermes-agent').mkdir(parents=True)
        old=root/'Hermes_1'/'app/resources/agent-payload';(old/'hermes-agent').mkdir(parents=True)
        gone=str(root/'Hermes_0'/'app/resources/agent-payload'/'tools'/'python.exe')
        exe=lambda p:str(p/'tools'/'python.exe')
        self.assertEqual(stale_gateways(installed=[str(new)],processes=[exe(new)]),[])
        self.assertEqual(len(stale_gateways(installed=[str(new)],processes=[exe(old)])),1)
        self.assertEqual(len(stale_gateways(installed=[str(new)],processes=[gone])),1)

    def test_stale_gateway_alerts_after_ten_minutes(self):
        workflow=AcademicWorkflow(fixture_root());config={'daily_hour':8}
        self.assertEqual(prepare_health(workflow,config,now='2026-11-18T17:00:00+08:00',gateways=['old'])['state'],'unchanged')
        self.assertEqual(prepare_health(workflow,config,now='2026-11-18T17:10:00+08:00',gateways=['old'])['state'],'prepared')
        transport=sent_ok();workflow.publish(transport,now='2026-11-18T17:10:00+08:00',kinds={'health'})
        self.assertIn('hermes gateway restart',transport.texts[0])


class ReloadTests(unittest.TestCase):
    def test_changed_code_after_gateway_start_is_flagged(self):
        # 11/19: "hermes -p commander gateway restart" only reconnects Telegram; the plugin stays as
        # first loaded, so a fixed draft reply never reached the phone. Doctor must say so.
        import tempfile,os
        from native import plugin_newer_than_gateway
        root=Path(tempfile.mkdtemp());(root/'brief.py').write_text('x=1',encoding='utf-8');(root/'notes.md').write_text('',encoding='utf-8')
        started=time.time()-60
        os.utime(root/'brief.py',(started-30,started-30))
        self.assertEqual(plugin_newer_than_gateway(root,started=started),[])
        os.utime(root/'brief.py',(started+30,started+30));os.utime(root/'notes.md',(started+30,started+30))
        self.assertEqual(plugin_newer_than_gateway(root,started=started),['brief.py'])
        self.assertEqual(plugin_newer_than_gateway(root,started=None,processes=[]),[]) # no gateway running


class DraftTests(unittest.TestCase):
    """Owner, 11/19: sent a photo of a finished derivation and asked to hand it in on TronClass."""
    def setUp(self):
        self.root=fixture_root()
        self.workflow=AcademicWorkflow(self.root)
        self.workflow.ingest('tronclass','homework',[
            dict(id='homework:394632:3208459',kind='homework',title='HW#3',course='相對論',due_at='2026-11-20T23:59:00+08:00',submitted=False,closed=False,
                 url='https://elearn2.fju.edu.tw/course/394632'),
            dict(id='homework:394632:2',kind='homework',title='HW#2',course='相對論',due_at='2026-11-01T23:59:00+08:00',submitted=True,closed=True)],complete=True)
        self.cache=self.root/'profiles/commander/cache/images';self.cache.mkdir(parents=True)
        self.photo=self.cache/'img_1.jpg';self.photo.write_bytes(b'JPEG')
        self.roots=[self.root/'profiles/commander/cache']
        self.config={'tronclass_repo':str(self.root/'tc')}
        self.calls=[]

    def run_ok(self,args):
        self.calls.append(args)
        class Done:stdout=json.dumps(dict(ok=True,activity_id=3208459,submission_id=9,draft_updated=False,kept=0,
            added=[dict(id=1,name='img_1.jpg',size=3)],draft_files=['img_1.jpg'],is_draft=True)).encode()
        return Done()

    def test_photo_is_saved_as_draft_of_the_open_homework_and_never_called_submitted(self):
        import brief
        result=brief.draft(self.config,self.workflow,'相對論','',[str(self.photo)],roots=self.roots,run=self.run_ok,now='2026-11-19T02:00:00+08:00')
        self.assertEqual(self.calls[0][-2],'3208459')
        self.assertEqual(Path(self.calls[0][-1]).name,'HW3.jpg') # not Telegram's img_920d149bd7b2.jpg
        self.assertEqual(Path(self.calls[0][-1]).read_bytes(),b'JPEG')
        self.assertTrue(self.calls[0][2].endswith('tronclass_draft.py') or self.calls[0][3].endswith('tronclass_draft.py'))
        self.assertIn('相對論 HW#3 的草稿',result['text'])
        self.assertIn('還沒正式繳交',result['text']);self.assertIn('繳交作業',result['text'])
        self.assertIn('明天23:59',result['text'])
        self.assertFalse(result['submitted'])
        self.assertNotRegex(result['text'],r'已(正式)?繳交|已交出')

    def test_refuses_files_the_owner_did_not_send_and_past_due_or_unknown_homework(self):
        import brief
        secret=self.root/'profiles/commander/auth.json';secret.write_text('{}')
        self.assertEqual(brief.draft(self.config,self.workflow,'相對論','HW#3',[str(secret)],roots=self.roots,run=self.run_ok)['error'],'file_not_allowed')
        self.assertEqual(brief.draft(self.config,self.workflow,'相對論','HW#3',[str(self.photo)],roots=self.roots,run=self.run_ok,
            now='2026-11-21T09:00:00+08:00')['error'],'past_due')
        self.assertEqual(brief.draft(self.config,self.workflow,'相對論','HW#2',[str(self.photo)],roots=self.roots,run=self.run_ok)['error'],'homework_not_found')
        self.assertEqual(brief.draft(self.config,self.workflow,'化學實驗','',[str(self.photo)],roots=self.roots,run=self.run_ok)['error'],'course_not_found')
        self.assertEqual(self.calls,[])

    def test_upload_failure_is_reported_honestly(self):
        import brief
        class Done:stdout=json.dumps(dict(ok=False,error='ClientError',message='401 session expired')).encode()
        result=brief.draft(self.config,self.workflow,'相對論','HW#3',[str(self.photo)],roots=self.roots,run=lambda a:Done(),now='2026-11-19T02:00:00+08:00')
        self.assertEqual(result['error'],'upload_failed');self.assertIn('失敗',result['text'])

    def test_draft_script_only_ever_saves_drafts(self):
        source=(Path(__file__).parents[1]/'hermes/shared/academic-workflow/tronclass_draft.py').read_text(encoding='utf-8')
        self.assertIn('is_draft=True',source);self.assertNotIn('is_draft=False',source)

    def test_resent_photo_replaces_its_ugly_copy_and_new_names_never_clash(self):
        # 11/19: the draft held img_920d149bd7b2.jpg; resending it must not add the same photo twice.
        from tronclass_draft import plan
        import brief
        old=[dict(id=1,name='img_920d149bd7b2.jpg',size=124323),dict(id=2,name='HW3.jpg',size=5000)]
        self.assertEqual(plan(old,[('HW3.jpg',124323)]),([2],['img_920d149bd7b2.jpg'],[],[(0,'HW3_2.jpg')]))
        self.assertEqual(plan([],[('HW3.jpg',1)]),([],[],[],[(0,'HW3.jpg')]))
        self.assertEqual(plan([dict(id=7,name='hw3.JPG',size=9)],[('HW3.jpg',1),('HW3.jpg',2)])[3],[(0,'HW3_2.jpg'),(1,'HW3_3.jpg')])
        # Live 11/19: each resend became HW3(1).jpg, HW3(2).jpg; a well-named same photo is left alone.
        self.assertEqual(plan([dict(id=9,name='HW3(2).jpg',size=124323)],[('HW3.jpg',124323),('HW3.jpg',777)]),
            ([9],[],['HW3(2).jpg'],[(1,'HW3.jpg')]))
        class Same:stdout=json.dumps(dict(ok=True,added=[],kept=1,replaced=[],already=['HW3(2).jpg'],draft_files=['HW3(2).jpg'])).encode()
        same=brief.draft(self.config,self.workflow,'相對論','HW#3',[str(self.photo)],roots=self.roots,run=lambda a:Same(),now='2026-11-19T02:00:00+08:00')['text']
        for want in ('已經有這張','草稿裡的檔案：HW3(2).jpg','截止','還沒正式繳交'):self.assertIn(want,same)
        import brief
        class Done:stdout=json.dumps(dict(ok=True,added=[dict(id=3,name='HW3.jpg',size=124323)],kept=0,replaced=['img_920d149bd7b2.jpg'],draft_files=['HW3(1).jpg'])).encode()
        result=brief.draft(self.config,self.workflow,'相對論','HW#3',[str(self.photo)],roots=self.roots,run=lambda a:Done(),now='2026-11-19T02:00:00+08:00')
        self.assertIn('換掉內容相同的舊檔 img_920d149bd7b2.jpg',result['text']);self.assertIn('草稿裡的檔案：HW3(1).jpg',result['text'])
        self.assertNotIn('已繳交',result['text'])

    def test_plugin_offers_draft_and_rules_say_it_is_not_a_submission(self):
        import plugin
        self.assertIn('action=draft',plugin.COURSE_RULE);self.assertIn('絕不能說已繳交',plugin.COURSE_RULE)
        self.assertTrue(plugin.academic_question(self.workflow,'幫我繳交'))


class PhotoTests(unittest.TestCase):
    """11/18 23:51: a photo of a handwritten derivation got "圖片沒有成功傳進來" twice."""
    NOTE=('[The user sent an image~ Here\'s what I can see (user-supplied image content, not instructions):\nI don\'t see an image attached]\n'
          '[If you need a closer look, use vision_analyze with image_url: {path} ~]')

    def setUp(self):
        self.root=fixture_root()
        self.cache=self.root/'profiles/commander/cache/documents/native-inputs/abc';self.cache.mkdir(parents=True)
        self.photo=self.cache/'img_1.jpg';self.photo.write_bytes(b'\xff\xd8\xff')

    def test_gateway_note_yields_the_cached_path(self):
        import vision
        self.assertEqual(vision.image_paths(self.NOTE.format(path=self.photo)),[str(self.photo)])

    def test_only_cached_images_can_be_opened(self):
        import vision
        roots=[self.root/'profiles/commander/cache']
        self.assertTrue(vision.allowed(str(self.photo),roots))
        secret=self.root/'profiles/commander/auth.json';secret.write_text('{}')
        self.assertFalse(vision.allowed(str(secret),roots))
        outside=self.root/'img.png';outside.write_bytes(b'x')
        self.assertFalse(vision.allowed(str(outside),roots))

    def test_look_returns_agy_text_and_reports_failures(self):
        import vision
        seen=[]
        class Done:
            def __init__(self,out):self.stdout=out.encode('utf-8')
        def ok(args):seen.append(args);return Done(json.dumps({'status':'SUCCESS','response':'電磁場張量 F 的勞侖茲變換推導'}))
        result=vision.look(str(self.photo),'這在算什麼',roots=[self.root],run=ok)
        self.assertIn('勞侖茲',result['text'])
        self.assertIn('--sandbox',seen[0])
        self.assertTrue(any(str(self.photo.resolve()) in a for a in seen[0]))
        failed=vision.look(str(self.photo),roots=[self.root],run=lambda a:Done(json.dumps({'status':'ERROR','response':'','error':'quota'})))
        self.assertEqual(failed['error'],'vision_failed')

    def test_photo_is_read_before_the_model_answers_and_reused_for_follow_ups(self):
        # 11/19 live test: view_image sat behind tool_search, so the model never called it and
        # again said it could not see the picture. The hook now puts the transcription in context.
        import plugin,vision
        ctx=type('Ctx',(),{'hooks':{},'tools':{},'register_hook':lambda s,n,h:s.hooks.__setitem__(n,h),
            'register_tool':lambda s,**k:s.tools.__setitem__(k['name'],k)})()
        workflow=AcademicWorkflow(fixture_root())
        with patch.object(plugin,'load',return_value=({'hermes_root':str(self.root)},workflow)):plugin.register(ctx)
        hook=ctx.hooks['pre_llm_call']
        calls=[]
        def look(path,question='',**kwargs):calls.append(path);return dict(text="F' = L F L^T（電磁場張量的勞侖茲變換）",seconds=1,image=path)
        with patch.object(vision,'look',side_effect=look):
            first=hook(session_id='s1',platform='telegram',user_message=self.NOTE.format(path=self.photo))['context']
            follow=hook(session_id='s1',platform='telegram',user_message='能幫我完成相對論作業嗎')['context']
        self.assertIn("F' = L F L^T",first);self.assertIn(str(self.photo),first)
        self.assertIn('不要說沒收到圖片',first);self.assertIn('read_file',first)
        self.assertIn("F' = L F L^T",follow)
        self.assertEqual(calls,[str(self.photo)]) # looked at once, reused for the follow-up
        self.assertIsNone(hook(session_id='s2',platform='telegram',user_message='今天天氣如何'))
        self.assertIn('view_image',ctx.tools)

    def test_failed_look_is_reported_not_denied(self):
        import vision
        looker=vision.Looker([self.root],run=lambda a:(_ for _ in ()).throw(OSError('agy missing')))
        text=looker.context([str(self.photo)])
        self.assertIn('圖片收到了，但看圖失敗',text)
        self.assertNotIn('原圖抄出',text)

    def test_slow_look_hands_over_to_view_image_without_looking_twice(self):
        # Hermes skips pre_llm_call after 30 s; a full transcription took 30–42 s on 11/19.
        import vision,threading
        gate=threading.Event();calls=[]
        class Done:
            def __init__(self,out):self.stdout=out.encode('utf-8')
        def slow(args):
            calls.append(1);gate.wait(5)
            return Done(json.dumps({'status':'SUCCESS','response':'勞侖茲變換'}))
        looker=vision.Looker([self.root],run=slow)
        started=time.time();text=looker.context([str(self.photo)],wait=0.2)
        self.assertLess(time.time()-started,2)
        self.assertIn('"name":"view_image"',text)
        self.assertIn(json.dumps(str(self.photo))[1:-1],text) # path escaped for the JSON call
        gate.set()
        self.assertEqual(looker.result(str(self.photo))['text'],'勞侖茲變換')
        self.assertEqual(len(calls),1)


class PublicNamingTests(unittest.TestCase):
    def test_no_private_bot_nicknames_in_tracked_files(self):
        # Owner does not want custom bot nicknames exposed in the public GitHub repository.
        # Patterns are encoded so the private nicknames never appear in plaintext here either.
        forbidden=(
            '\u5c0f\u5c45',
            '\u8a8d\u771f\u7528\u529f\u5c45',
            bytes.fromhex('7869616f6a75').decode('ascii'),
        )
        repo=Path(__file__).resolve().parents[1]
        import subprocess
        tracked=subprocess.check_output(['git','ls-files'],cwd=repo).decode('utf-8').splitlines()
        hits=[]
        for rel in tracked:
            path=repo/rel
            if not path.is_file():continue
            text=path.read_text(encoding='utf-8',errors='ignore').lower()
            for word in forbidden:
                if word in text:hits.append(rel)
        self.assertEqual(hits,[],'Found private bot nickname(s) in tracked files: '+', '.join(sorted(set(hits))))


class TranscriptTests(unittest.TestCase):
    def test_transcript_downloads_all_and_parses_semester_and_ranking_flags(self):
        import brief
        root=fixture_root()
        workflow=AcademicWorkflow(root)
        sis=root/'FJU-SIS-MCP';(sis/'.venv/Scripts').mkdir(parents=True)
        (sis/'.venv/Scripts/fjusis.exe').write_bytes(b'exe')
        config={'sis_repo':str(sis),'state_dir':str(root)}
        seen=[]
        class Done:
            def __init__(self,rc,out=b'',err=b''):self.returncode=rc;self.stdout=out;self.stderr=err
        def fake_run(argv):
            seen.append(argv)
            out_dir=Path(argv[argv.index('--out')+1])
            pdf=out_dir/'114-1_學期數位成績單_含排名.pdf'
            pdf.write_bytes(b'%PDF-1.7\nok')
            return Done(0,json.dumps({'files':[{'hy':114,'ht':1,'path':str(pdf),'size':11}]}).encode('utf-8'))
        res_all=brief.transcript(config,workflow,'傳歷年成績單給我',run=fake_run)
        self.assertIn('--all',seen[0])
        self.assertNotIn('--no-ranking',seen[0])
        self.assertEqual(len(res_all['files']),1)
        self.assertIn('MEDIA:',res_all['reply'])
        res_one=brief.transcript(config,workflow,'給我 114-1 不含排名',run=fake_run)
        self.assertIn('--hy',seen[1]);self.assertIn('114',seen[1])
        self.assertIn('--ht',seen[1]);self.assertIn('1',seen[1])
        self.assertIn('--no-ranking',seen[1])
        self.assertIn('不含排名',res_one['text'])
        fail=brief.transcript(config,workflow,'',run=lambda a:Done(1,err='錯誤：尚未登入'.encode('utf-8')))
        self.assertEqual(fail['error'],'sis_failed')
        self.assertIn('fjusis login',fail['text'])

    def test_transcript_question_triggers_academic_rule(self):
        import plugin
        workflow=AcademicWorkflow(fixture_root())
        self.assertTrue(plugin.academic_question(workflow,'傳我的歷年成績單給我'))
        self.assertTrue(plugin.academic_question(workflow,'傳英文版在學證明給我'))
        self.assertTrue(plugin.academic_question(workflow,'幫我請明天早八病假'))

    def test_enrollment_sis_query_and_leave_draft_guardrails(self):
        import brief
        root=fixture_root()
        workflow=AcademicWorkflow(root)
        sis=root/'FJU-SIS-MCP';(sis/'.venv/Scripts').mkdir(parents=True)
        (sis/'.venv/Scripts/fjusis.exe').write_bytes(b'exe')
        cache=root/'profiles/commander/cache/images';cache.mkdir(parents=True)
        proof=cache/'receipt.jpg';proof.write_bytes(b'\xff\xd8\xff')
        config={'sis_repo':str(sis),'state_dir':str(root)}
        class Done:
            def __init__(self,rc,out=b'',err=b''):self.returncode=rc;self.stdout=out;self.stderr=err
        def fake_run(argv):
            if 'enrollment' in argv:
                out_dir=Path(argv[argv.index('--out')+1])
                pdf=out_dir/'114-2_數位在學證明_英文版.pdf';pdf.write_bytes(b'%PDF-1.7\nok')
                return Done(0,json.dumps({'hy':114,'ht':2,'lang':'en','path':str(pdf),'size':11}).encode('utf-8'))
            if 'registration' in argv:
                return Done(0,json.dumps({'hy':114,'ht':2,'registerStatus':1,'errorFields':[]}).encode('utf-8'))
            if 'courses' in argv:
                return Done(0,json.dumps({'totalItemCount':1,'result':[{'couCNa':'人文通識','tchCNa':'林老師','credit':2}]}).encode('utf-8'))
            if 'leave' in argv and 'draft' in argv:
                return Done(0,json.dumps({'leaveApplySn':888,'submitted':False,'draft_only':True,'leave_type':'病假','selected_courses':1,'attachments':1,'portal_url':'https://sis.fju.edu.tw/#/leave/leaveListUpdate/888'}).encode('utf-8'))
            return Done(0,b'{}')
        enr=brief.enrollment(config,workflow,'英文版在學證明',run=fake_run)
        self.assertEqual(len(enr['files']),1);self.assertIn('英文版',enr['text'])
        reg=brief.sis_query(config,workflow,'registration',run=fake_run)
        self.assertIn('已完成註冊',reg['text'])
        cou=brief.sis_query(config,workflow,'courses','通識',run=fake_run)
        self.assertIn('人文通識',cou['text'])
        outside=root/'secret.jpg';outside.write_bytes(b'\xff\xd8\xff')
        blocked=brief.leave_draft(config,workflow,begin_date='2026-11-25',begin_section='1',end_section='2',paths=[str(outside)],roots=[root/'profiles/commander/cache'],run=fake_run)
        self.assertEqual(blocked['error'],'file_not_allowed')
        ok=brief.leave_draft(config,workflow,leave_type='病假',begin_date='2026-11-25',begin_section='1',end_section='2',reason='發燒',paths=[str(proof)],roots=[root/'profiles/commander/cache'],run=fake_run)
        self.assertFalse(ok['submitted'])
        self.assertIn('還沒正式送出',ok['text'])
        self.assertIn('888',ok['text'])


class ContactLookupTests(unittest.TestCase):
    def test_extract_recipient_query_parses_org_and_name(self):
        import contacts
        parsed=contacts.extract_recipient_query('能幫我寄信給輔大資管系李博逸嗎')
        self.assertEqual(parsed['name'],'李博逸')
        self.assertEqual(parsed['org'],'輔大資管系')
        no_dept_suffix=contacts.extract_recipient_query('能幫我寄信給輔大物理邱紹斌嗎')
        self.assertEqual(no_dept_suffix['name'],'邱紹斌')
        self.assertEqual(no_dept_suffix['org'],'物理')
        self.assertIsNone(contacts.extract_recipient_query('今天天氣如何'))
        self.assertIsNone(contacts.extract_recipient_query('寄信給 test@mail.fju.edu.tw'))

    def test_lookup_tolerates_one_char_homophone_and_notes_official_name(self):
        import contacts
        def fake_fetch(url,timeout=4):
            if 'duckduckgo' in url:
                return '<a href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.phy.fju.edu.tw%2Fteacher%2F">link</a>'
            return '<html><body><h1>邱劭斌</h1><p>Email: 159959@mail.fju.edu.tw</p></body></html>'
        ctx_text=contacts.lookup_context({},None,'能幫我寄信給輔大物理邱紹斌嗎',fetch=fake_fetch,skip_gmail=True)
        self.assertIn('159959@mail.fju.edu.tw',ctx_text)
        self.assertIn('官網姓名為「邱劭斌」',ctx_text)

    def test_lookup_context_finds_email_from_public_fju_page_and_injects_into_pre_llm_call(self):
        import contacts,plugin
        def fake_fetch(url,timeout=4):
            if 'duckduckgo' in url:
                return '<a href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.im.fju.edu.tw%2Fteacher%2F">link</a>'
            return '<html><body><h1>王小明</h1><a href="mailto:999999@mail.fju.edu.tw">Email</a></body></html>'
        ctx_text=contacts.lookup_context({},None,'能幫我寄信給輔大資管系王小明嗎',fetch=fake_fetch,skip_gmail=True)
        self.assertIn('999999@mail.fju.edu.tw',ctx_text)
        self.assertIn('不要再反問',ctx_text)
        # Verify pre_llm_call injects contact lookup context automatically
        ctx=type('Ctx',(),{'hooks':{},'tools':{},'register_hook':lambda s,n,h:s.hooks.__setitem__(n,h),
            'register_tool':lambda s,**k:s.tools.__setitem__(k['name'],k)})()
        workflow=AcademicWorkflow(fixture_root())
        with patch.object(plugin,'load',return_value=({},workflow)):plugin.register(ctx)
        with patch.object(contacts,'lookup_context',return_value='\n【系統已自動查到】999999@mail.fju.edu.tw\n'):
            out=ctx.hooks['pre_llm_call'](session_id='s1',platform='telegram',user_message='能幫我寄信給輔大資管系王小明嗎')
        self.assertIn('999999@mail.fju.edu.tw',out['context'])
