from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
import json
import unittest

import plugin
from workflow import AcademicWorkflow


class Context:
    def __init__(self):self.hooks={};self.handler=None
    def register_hook(self,name,handler):self.hooks[name]=handler
    def register_tool(self,**kwargs):self.handler=kwargs['handler']


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.workflow=AcademicWorkflow(Path(__file__).parent/'fixtures'/uuid4().hex)
        self.ctx=Context()
        with patch.object(plugin,'load',return_value=({},self.workflow)):plugin.register(self.ctx)
        self.turn=ContextVar('fixture_turn',default='')
        self.session=ContextVar('fixture_session',default='')
        self.approval=SimpleNamespace(_approval_turn_id=self.turn,_approval_session_id=self.session)
        constants=SimpleNamespace(get_hermes_home=lambda:self.workflow.root)
        self.modules=patch.dict('sys.modules',{'hermes_constants':constants})
        self.modules.start();self.addCleanup(self.modules.stop)

    def stage(self,text='星期五交電路學作業',*,turn='turn-1',kind=None,reply=True):
        return self.ctx.hooks['pre_llm_call'](session_id='session-1',turn_id=turn,user_message=text,platform='telegram',
            conversation_history=[{'role':'user','content':text,'display_kind':kind,'display_metadata':{'reply_expected':reply}}])

    def capture(self,text='星期五交電路學作業',turn='turn-1'):
        t=self.turn.set(turn);s=self.session.set('session-1')
        try:
            with patch.dict('sys.modules',{'tools.approval_context':self.approval}):
                return json.loads(self.ctx.handler({'action':'capture','text':text},session_id='session-1'))
        finally:self.turn.reset(t);self.session.reset(s)

    def test_actual_dispatch_without_turn_argument_captures_current_staged_user(self):
        self.stage()
        result=self.capture()
        self.assertEqual(result.get('observed'),1,result)
        self.assertEqual(len(self.workflow.inbox_items()),1)
        self.assertEqual(self.capture().get('changed'),0)

    def test_internal_turn_cannot_inherit_previous_human_authorization(self):
        self.stage()
        self.stage(turn='turn-2',kind='internal_notification',reply=False)
        result=self.capture(turn='turn-2')
        self.assertIn('error',result)
        self.assertEqual(self.workflow.inbox_items(),[])

    def test_same_text_from_prior_history_does_not_authorize_current_internal_message(self):
        text='星期五交電路學作業'
        self.ctx.hooks['pre_llm_call'](session_id='session-1',turn_id='turn-1',user_message=text,platform='telegram',conversation_history=[
            {'role':'user','content':text,'display_metadata':{'reply_expected':True}},
            {'role':'user','content':text,'display_kind':'internal_notification','display_metadata':{'reply_expected':False}}])
        self.assertIn('error',self.capture())

    def test_completed_turn_cannot_be_replayed_by_another_tool_call(self):
        self.stage()
        self.ctx.hooks['post_llm_call'](session_id='session-1',turn_id='turn-1')
        self.assertIn('error',self.capture())

    def test_capture_requires_exact_current_user_quote_and_current_turn(self):
        self.stage()
        self.assertIn('error',self.capture(text='下週五交電路學作業'))
        self.assertIn('error',self.capture(turn='other-turn'))

    def test_pre_tool_guard_leaves_unrelated_task_writes_available(self):
        native=SimpleNamespace(get=lambda task_id:dict(created_by='academic-workflow') if task_id=='academic-card' else dict(created_by='human'))
        guard=self.ctx.hooks['pre_tool_call']
        with patch.object(plugin,'NativeTasks',return_value=native),patch.dict('os.environ',{'HERMES_KANBAN_TASK':'academic-card'}):
            self.assertIsNone(guard(tool_name='kanban_complete',args={'task_id':'other-task'}))
            self.assertEqual(guard(tool_name='kanban_complete',args={'task_id':'academic-card'})['action'],'block')
            self.assertEqual(guard(tool_name='kanban_create',args={})['action'],'block')


if __name__=='__main__':unittest.main()
