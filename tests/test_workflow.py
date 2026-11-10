from pathlib import Path
import unittest
from uuid import uuid4

from workflow import AcademicWorkflow,DeliveryRetry


class Transport:
    def __init__(self):self.texts=[];self.files=[];self.retry_file=True
    def send_text(self,text):self.texts.append(text);return {'message_id':'text-1'}
    def send_artifact(self,path,caption):
        self.files.append(str(path))
        if self.retry_file:
            self.retry_file=False
            raise DeliveryRetry(1)
        return {'message_id':'file-1'}


class NativeTasks:
    def __init__(self):self.tasks={};self.created=0;self.crash_after_create=False
    def find(self,key):return next((dict(v) for v in self.tasks.values() if v['idempotency_key']==key),None)
    def create_preparation(self,item,workspace,key):
        self.created+=1
        task=dict(id='t_'+str(self.created),status='ready',idempotency_key=key,workspace=str(workspace))
        self.tasks[task['id']]=task
        if self.crash_after_create:
            self.crash_after_create=False
            raise TimeoutError('process lost response after native commit')
        return task['id']
    def get(self,task_id):return self.tasks.get(task_id)
    def retire_preparation(self,task_id):
        task=self.tasks[task_id]
        if task['status'] in {'ready','review'}:task['status']='blocked';return True
        return False
    def refresh_preparation(self,item,workspace,key,task_id):
        task=self.tasks[task_id]
        if task['status']=='running':return False
        task.update(status='ready',workspace=str(workspace),idempotency_key=key)
        return True
    def finish_review(self,task_id,*,accepted,summary,artifacts=(),repair=False):
        task=self.tasks[task_id]
        task['status']='done' if accepted else 'ready' if repair else 'blocked'
        return True


def fixture_root():
    p=Path(__file__).parent/'fixtures'/uuid4().hex
    p.mkdir(parents=True)
    return p


class AcademicWorkflowTests(unittest.TestCase):
    def test_source_submission_releases_obsolete_preparation_capacity(self):
        workflow=AcademicWorkflow(fixture_root());native=NativeTasks()
        first=dict(id='h1',kind='homework',title='習題一',submitted=False)
        workflow.ingest('tronclass','hw',[first],complete=True);workflow.dispatch(native,limit=1)
        native.get('t_1')['status']='review';first['submitted']=True
        workflow.ingest('tronclass','hw',[first,dict(id='h2',kind='homework',title='習題二',submitted=False)],complete=True)
        workflow.dispatch(native,limit=1)
        self.assertEqual(native.get('t_1')['status'],'blocked')
        self.assertEqual(native.created,2)

    def test_source_revision_reuses_card_and_waits_for_running_worker(self):
        workflow=AcademicWorkflow(fixture_root());native=NativeTasks()
        item=dict(id='h1',kind='homework',title='習題',course='物理',due_at='2026-11-15T12:00:00+08:00',submitted=False)
        workflow.ingest('tronclass','hw',[item],complete=True)
        workflow.dispatch(native,now='2026-11-09T08:00:00+08:00',limit=1)
        native.get('t_1')['status']='running'
        item['due_at']='2026-11-16T12:00:00+08:00'
        workflow.ingest('tronclass','hw',[item],complete=True)
        workflow.dispatch(native,now='2026-11-09T08:05:00+08:00',limit=1)
        self.assertEqual(native.created,1)
        native.get('t_1')['status']='review'
        workflow.dispatch(native,now='2026-11-09T08:10:00+08:00',limit=1)
        self.assertEqual(native.created,1)
        self.assertEqual(len(workflow.preparations()),2)
        self.assertTrue(all(x['task_id']=='t_1' for x in workflow.preparations()))
        source=__import__('json').loads((Path(native.get('t_1')['workspace'])/'source.json').read_text(encoding='utf-8'))
        self.assertEqual(source['due_at'],item['due_at'])

    def test_concurrent_dispatchers_create_one_native_task(self):
        from concurrent.futures import ThreadPoolExecutor
        root=fixture_root();workflow=AcademicWorkflow(root);native=NativeTasks()
        workflow.ingest('tronclass','course:1',[dict(id='h1',kind='homework',title='習題',course='物理',submitted=False)],complete=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _:AcademicWorkflow(root).dispatch(native),range(4)))
        self.assertEqual(native.created,1)
        self.assertEqual(len(workflow.preparations()),1)

    def test_academic_notice_alias_does_not_create_second_assignment(self):
        workflow=AcademicWorkflow(fixture_root())
        workflow.ingest('tronclass','hw',[dict(id='h1',kind='homework',title='作業3',course='作業系統',course_id='42',submitted=False)],complete=True)
        original_revision=workflow.agenda()[0]['revision']
        workflow.ingest('gmail','mail',[dict(id='m1',kind='email',title='作業系統 作業3',description='作業3繳交提醒',needs_interpretation=True)],complete=False)
        messages=workflow.inbox_items()
        mail=next(x for x in messages if x['id']=='m1')
        workflow.annotate(mail['key'],mail['source_revision'],dict(actionable=True,action='完成作業3',alias_of='tronclass|h1'))
        self.assertEqual(len(workflow.agenda()),1)
        self.assertEqual(workflow.agenda()[0]['related_sources'],['gmail|m1'])
        self.assertEqual(workflow.agenda()[0]['revision'],original_revision)

    def test_verified_package_is_queued_once_without_marking_student_complete(self):
        import json
        root=fixture_root();workflow=AcademicWorkflow(root);native=NativeTasks()
        item=dict(id='hw:9:2',kind='homework',course='普通物理',title='運動分析',description='由位置資料計算速度與加速度。',due_at='2026-11-13T23:59:00+08:00',submitted=False,closed=False)
        workflow.ingest('tronclass','course:9',[item],complete=True)
        workflow.dispatch(native,now='2026-11-09T09:00:00+08:00')
        task=native.tasks['t_1'];task['status']='review'
        workspace=Path(task['workspace']);source=json.loads((workspace/'source.json').read_text(encoding='utf-8'))
        artifact=workspace/'學習準備.md'
        artifact.write_text('''# 運動分析準備\n來源：課程作業 hw:9:2。\n以中央差分估算速度 v[i]=(x[i+1]-x[i-1])/(2Δt)，再用中央差分估算加速度；先確認位置為 m、時間為 s。資料端點需用前向／後向差分。比較不同取樣間隔的誤差，並用等加速度模型檢查。本人需匯入實際位置資料、核對圖表與撰寫觀察。這是準備材料，不代表已完成實驗或提交。''',encoding='utf-8')
        (workspace/'manifest.json').write_text(json.dumps(dict(item_key=source['key'],revision=source['revision'],summary='已準備中央差分方法、單位檢查與本人操作步驟。',artifacts=[str(artifact)]),ensure_ascii=False),encoding='utf-8')
        verifier=lambda source,files:dict(accepted=True,readiness='prepared',summary='已備妥運動分析方法與操作步驟。',limitations=[])
        workflow.reconcile(native,verifier)
        workflow.reconcile(native,verifier)
        self.assertEqual(len(workflow.publications()),1)
        self.assertEqual(native.get('t_1')['status'],'done')
        self.assertFalse(workflow.agenda()[0]['human_complete'])
        transport=Transport()
        workflow.publish(transport,now='2026-11-09T09:00:00+08:00')
        workflow.publish(transport,now='2026-11-09T09:00:02+08:00')
        self.assertEqual(len(transport.texts),1)
        self.assertEqual(len(transport.files),2)
        self.assertEqual(workflow.publications()[0]['state'],'sent')

    def test_restart_after_native_create_recovers_existing_task(self):
        root=fixture_root();workflow=AcademicWorkflow(root);native=NativeTasks()
        item=dict(id='hw:3:1',kind='homework',course='電路學',title='放大器分析',description='分析增益',due_at='2026-11-15T23:59:00+08:00',submitted=False,closed=False)
        workflow.ingest('tronclass','course:3',[item],complete=True)
        native.crash_after_create=True
        workflow.dispatch(native,now='2026-11-09T09:00:00+08:00')
        resumed=AcademicWorkflow(root)
        resumed.dispatch(native,now='2026-11-09T09:01:00+08:00')
        self.assertEqual(native.created,1)
        self.assertEqual(len(resumed.preparations()),1)
        self.assertEqual(resumed.preparations()[0]['task_id'],'t_1')

    def test_partial_source_failure_never_erases_work_or_marks_it_complete(self):
        workflow=AcademicWorkflow(fixture_root())
        item=dict(id='hw:1:9',kind='homework',course='實驗',title='報告',submitted=False,closed=False)
        workflow.ingest('tronclass','course:1',[item],complete=True)
        workflow.ingest('tronclass','course:1',[],complete=False,errors=['source_timeout'])
        agenda=workflow.agenda()
        self.assertEqual(len(agenda),1)
        self.assertFalse(agenda[0]['human_complete'])
        self.assertEqual(workflow.sources()[0]['complete'],0)

    def test_repeated_source_and_changed_deadline_update_one_agenda_item(self):
        workflow=AcademicWorkflow(fixture_root())
        item=dict(id='hw:42:7',kind='homework',course='電磁學',title='習題二',
                  description='完成第 2、4 題並附推導。',due_at='2026-11-12T23:59:00+08:00',
                  submitted=False,closed=False,url='https://example.edu/courses/42')
        workflow.ingest('tronclass','1151:42:homework',[item],complete=True,observed_at='2026-11-08T20:00:00+08:00')
        workflow.ingest('tronclass','1151:42:homework',[item],complete=True,observed_at='2026-11-08T20:30:00+08:00')
        first=workflow.agenda(now='2026-11-08T21:00:00+08:00')
        self.assertEqual(len(first),1)
        item['due_at']='2026-11-14T23:59:00+08:00'
        workflow.ingest('tronclass','1151:42:homework',[item],complete=True,observed_at='2026-11-08T21:10:00+08:00')
        changed=workflow.agenda(now='2026-11-08T21:15:00+08:00')
        self.assertEqual(len(changed),1)
        self.assertEqual(changed[0]['key'],first[0]['key'])
        self.assertNotEqual(changed[0]['revision'],first[0]['revision'])
        self.assertEqual(changed[0]['due_at'],'2026-11-14T23:59:00+08:00')
        self.assertFalse(changed[0]['human_complete'])


if __name__=='__main__':unittest.main()
