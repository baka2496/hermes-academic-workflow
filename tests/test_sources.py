from datetime import datetime
import unittest
from collect_tronclass import paged,current_semester
from operations import interpret_pending,import_mail,run_json
from workflow import AcademicWorkflow
from test_workflow import fixture_root


class SourceTests(unittest.IsolatedAsyncioTestCase):
    def test_timed_out_source_does_not_leave_an_orphan_python_worker(self):
        import sys,time,psutil
        root=fixture_root();pid_file=root/'source-child.pid'
        code='import subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);Path('+repr(str(pid_file))+').write_text(str(p.pid));time.sleep(60)'
        with self.assertRaises(TimeoutError):run_json([sys.executable,'-c',code],timeout=1)
        child=int(pid_file.read_text())
        deadline=time.monotonic()+3
        while psutil.pid_exists(child) and time.monotonic()<deadline:time.sleep(0.05)
        self.assertFalse(psutil.pid_exists(child))

    def test_tronclass_candidate_survives_mail_import(self):
        workflow=AcademicWorkflow(fixture_root())
        import_mail(workflow,{'gmail':{'messages':[{'id':'123','subject':'TronClass','from':'noreply@example.com','body':'New item available'}],'truncated':False}})
        self.assertEqual(len(workflow.inbox_items()),1)
        self.assertEqual(workflow.inbox_items()[0]['title'],'TronClass')

    async def test_repeated_school_page_is_partial_and_retains_first_page(self):
        class HTTP:
            async def get_json(self,*args,**kwargs):return {'exams':[{'id':1}],'total':2,'page':1}
        items,complete,errors=await paged(HTTP(),'/exams','exams')
        self.assertEqual(items,[{'id':1}]);self.assertFalse(complete)
        self.assertIn('pagination_not_advancing',errors)

    async def test_empty_school_page_without_total_is_not_proof_of_completeness(self):
        class HTTP:
            async def get_json(self,*args,**kwargs):return {'exams':[]}
        _,complete,_=await paged(HTTP(),'/exams','exams')
        self.assertFalse(complete)

    def test_january_belongs_to_previous_first_semester(self):
        self.assertEqual(current_semester(datetime(2027,1,10)),'1151')
        self.assertEqual(current_semester(datetime(2027,2,10)),'1152')

    def test_extraction_failure_stays_parked_after_three_attempts(self):
        workflow=AcademicWorkflow(fixture_root())
        workflow.ingest('user','academic',[dict(id='u1',kind='manual',title='考試',needs_interpretation=True)],complete=True)
        class Broken:
            count=0
            def json(self,*args):self.count+=1;raise RuntimeError('unavailable')
        model=Broken()
        for at in ['2026-11-09T08:00:00+08:00','2026-11-09T09:00:00+08:00','2026-11-09T11:00:00+08:00','2026-11-10T08:00:00+08:00']:
            result=interpret_pending(workflow,model,now=at)
        self.assertEqual(model.count,3)
        self.assertEqual(result['remaining'],1)
        self.assertEqual(workflow.agenda(),[])


if __name__=='__main__':unittest.main()
