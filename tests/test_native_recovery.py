from datetime import datetime, timezone
from pathlib import Path
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from native import NativeTasks, bootstrap


@unittest.skipUnless(os.environ.get('ACADEMIC_HERMES_PAYLOAD'), 'Set ACADEMIC_HERMES_PAYLOAD for native Kanban tests')
class NativeRecoveryTests(unittest.TestCase):
    def test_retire_ready_or_review_task_preserves_workspace(self):
        self.assertTrue(self.native.retire_preparation(self.task_id))
        self.assertEqual(self.native.get(self.task_id)['status'],'blocked')
        self.assertTrue(self.workspace.is_dir())
        self.assertTrue(self.native.refresh_preparation(dict(self.item,revision='revision-2'),self.workspace,'academic:new-source',self.task_id))
        self.review()
        self.assertTrue(self.native.retire_preparation(self.task_id))
        self.assertEqual(self.native.get(self.task_id)['status'],'blocked')
        self.assertTrue(self.workspace.is_dir())

    def setUp(self):
        self.folder=Path(__file__).parent/'native-fixtures'/uuid4().hex
        self.folder.mkdir(parents=True)
        hermes_root = self.folder / 'hermes'
        self.enterContext(patch.dict(os.environ, {
            'HERMES_HOME': str(hermes_root),
            'HERMES_KANBAN_HOME': str(hermes_root),
            'HERMES_KANBAN_DB': '',
        }))
        self.config = {
            'hermes_root': str(hermes_root),
            'hermes_payload': os.environ['ACADEMIC_HERMES_PAYLOAD'],
            'runtime': str(Path(__file__).resolve().parents[1] / 'hermes' / 'shared' / 'academic-workflow'),
            'tronclass_repo': str(self.folder / 'tronclass'),
        }
        bootstrap(self.config)
        from hermes_cli.kanban_db import create_board
        create_board('command-center')
        self.native=NativeTasks(self.config)
        self.item=dict(key='tronclass|fixture',revision='revision-one',course='測試課程',title='測試作業')
        self.workspace=self.folder/'first';self.workspace.mkdir()
        self.task_id=self.native.create_preparation(self.item,self.workspace,'academic:fixture:'+uuid4().hex)

    def review(self):
        with self.native.connection() as db:
            from hermes_cli.kanban_db import claim_task,request_review
            task=claim_task(db,self.task_id,claimer='isolated-test')
            self.assertTrue(request_review(db,self.task_id,reviewer='academic-validator',summary='fixture material',expected_run_id=task.current_run_id))

    def test_reviewer_crash_after_claim_can_finish_same_native_run(self):
        self.review()
        with self.native.connection() as db:
            from hermes_cli.kanban_db import claim_review_task
            claimed=claim_review_task(db,self.task_id,claimer='academic-validator:2147483647')
        self.assertTrue(self.native.finish_review(self.task_id,accepted=True,summary='Verified fixture material'))
        task=self.native.get(self.task_id)
        self.assertEqual(task['status'],'done')
        with self.native.connection() as db:
            rows=db.execute('SELECT id FROM task_runs WHERE task_id=? AND outcome=?',(self.task_id,'completed')).fetchall()
        self.assertEqual([r['id'] for r in rows],[claimed.current_run_id])
        self.assertTrue(self.native.finish_review(self.task_id,accepted=True,summary='Verified fixture material'))

    def test_refresh_reuses_task_id_and_waits_for_running_worker(self):
        with self.native.connection() as db:
            from hermes_cli.kanban_db import claim_task,complete_task
            task=claim_task(db,self.task_id,claimer='isolated-test')
        next_workspace=self.folder/'second';next_workspace.mkdir()
        revised=dict(self.item,revision='revision-two')
        new_key='academic:fixture:'+uuid4().hex
        self.assertFalse(self.native.refresh_preparation(revised,next_workspace,new_key,self.task_id))
        with self.native.connection() as db:
            self.assertTrue(complete_task(db,self.task_id,summary='Old version preparation',expected_run_id=task.current_run_id))
        self.assertTrue(self.native.refresh_preparation(revised,next_workspace,new_key,self.task_id))
        refreshed=self.native.find(new_key)
        self.assertEqual(refreshed['id'],self.task_id)
        self.assertEqual(refreshed['workspace_path'],str(next_workspace))
        self.assertEqual(refreshed['status'],'ready')
        self.assertIn('revision=revision-two',refreshed['body'])

    def test_transient_worker_recovery_has_delay_and_lifetime_budget(self):
        clock=datetime(2026,11,9,8,tzinfo=timezone.utc).timestamp()
        for attempt in range(3):
            with self.native.connection() as db:
                db.execute("UPDATE tasks SET status='blocked',block_kind=NULL,last_failure_error='HTTP 429 rate limit',current_run_id=NULL WHERE id=?",(self.task_id,));db.commit()
            planned=self.native.recover_worker(self.task_id,now=clock)
            self.assertEqual(planned['state'],'waiting')
            self.assertEqual(self.native.get(self.task_id)['status'],'blocked')
            clock=planned['retry_at']+1
            recovered=self.native.recover_worker(self.task_id,now=clock)
            self.assertEqual(recovered['state'],'recovered')
            self.assertEqual(recovered['attempts'],attempt+1)
            clock+=1
        with self.native.connection() as db:
            db.execute("UPDATE tasks SET status='blocked',block_kind=NULL,last_failure_error='HTTP 429 rate limit' WHERE id=?",(self.task_id,));db.commit()
        self.assertEqual(self.native.recover_worker(self.task_id,now=clock)['state'],'parked')
        self.assertEqual(self.native.get(self.task_id)['status'],'blocked')

    def test_human_or_auth_block_is_never_automatically_unblocked(self):
        for kind,error in [('needs_input','question scope unavailable'),(None,'HTTP 403 unauthorized')]:
            with self.native.connection() as db:
                db.execute("UPDATE tasks SET status='blocked',block_kind=?,last_failure_error=? WHERE id=?",(kind,error,self.task_id));db.commit()
            self.assertEqual(self.native.recover_worker(self.task_id)['state'],'not_retryable')
            self.assertEqual(self.native.get(self.task_id)['status'],'blocked')


if __name__=='__main__':unittest.main()
