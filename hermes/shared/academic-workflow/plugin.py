"""Native tool bridge to the single academic journal."""
from pathlib import Path
import json
import re
import os
import time
from collections import OrderedDict
from threading import RLock
from importlib import import_module
from workflow import digest
from academic import load,status,submit
from native import NativeTasks


ACADEMIC_TOPIC=r'學業|作業|考試|複習|讀書|課|教室|考場|公告|老師|待辦|最近有什麼|有什麼事|重要的事|期限|截止|題目|講義|附件|考卷|HW|hw|Quiz|quiz'
# "傳相對論 HW#3 的題目給我" matched none of these, so the bot never heard of action=attachment; a
# course name alone is also an academic question.


def academic_question(workflow,text):
    if re.search(ACADEMIC_TOPIC,text):return True
    import brief
    names={brief.bare(x.get('course')) for x in workflow.inbox_items() if x.get('course')}
    return any(len(n)>=2 and n in text.lower() for n in names)
# "體育課在哪裡上課" matched none of the old words, so the bot searched files and logs for 3.5 minutes
# although the announcement was already stored; action=course answers it from local data.
COURSE_RULE=('問某門課的上課地點、教室、公告、老師說了什麼、這門課有什麼作業時，用 action=course、key=課名，'
             '照它的資料回答；不要搜尋檔案、讀 log 或其他機器人的資料庫。查不到就說未確認。'
             '要題目、講義、附件或檔案時用 action=attachment、key=課名、text=作業或公告名稱，'
             '照它回的 files 每個檔案一行寫 MEDIA:路徑傳給本人，直接傳檔，不要先讀檔或轉文字（題目常是掃描檔）；工具回錯誤就照實說失敗，不要說已接單或另派別人。')
# "傳題目給我": kanban_create failed, the bot still answered "已接單" and then searched files for
# minutes, reading another profile's auth.json. Commander never needs credential files.
SECRET_PATH=re.compile(r'auth\.json|\.env\b|credentials|secrets?\.(json|ya?ml|txt)|\.session\b|cookies?\.(json|txt|sqlite)',re.I)
# The owner reads Telegram on a phone: tables and code blocks wrap into an unreadable block there.
PHONE_FORMAT=('\n手機排版：不要用表格、│ 分欄或三個反引號；一句結論後每項一行「項目｜日期」，下一步最多 4 字，'
              '整行 18 字內避免折行，最多五項；action=brief 的清單例外，照貼全文不刪項。')
# "最近有什麼事" was answered by picking five items from action=status, which dropped the travel
# section and the undated/later counts; brief.build is the one complete list for those questions.
BRIEF_RULE=('問近況（最近／目前有什麼事、重要的事、待辦）時用 action=brief：第一行一句結論，接著逐行貼上它的 text，'
            '不可刪項、不可改日期或狀態、不補清單外事項；有「⚠️紀錄矛盾」要照實請本人確認。問單一事項細節才用 action=status。')


def current_tool_identity():
    try:
        context=import_module('tools.approval_context')
        getter=getattr(context,'get_current_observability_context',None)
        if getter:
            value=getter()
            if isinstance(value,dict):return value.get('session_id',''),value.get('turn_id','')
            return getattr(value,'session_id',''),getattr(value,'turn_id','')
        # This Hermes release binds these ContextVars but has no public getter.
        return context._approval_session_id.get(),context._approval_turn_id.get()
    except Exception:return '',''


def current_worker_task():
    try:
        from gateway.session_context import get_session_env
        return get_session_env('HERMES_KANBAN_TASK','')
    except ImportError:return os.environ.get('HERMES_KANBAN_TASK','')


def register(ctx):
    config,workflow=load(Path(__file__).with_name('config.json'))
    human_turns=OrderedDict();lock=RLock()
    def pre_llm_call(session_id='',turn_id='',user_message='',platform='',conversation_history=None,**kwargs):
        history=conversation_history or []
        current=history[-1] if history and isinstance(history[-1],dict) else {}
        metadata=current.get('display_metadata') or {}
        if not isinstance(metadata,dict):metadata={}
        human=(getattr(platform,'value',platform)=='telegram' and session_id and turn_id
            and isinstance(user_message,str) and current.get('role')=='user'
            and current.get('content')==user_message and current.get('display_kind') is None
            and metadata.get('reply_expected') is True)
        with lock:
            now=time.monotonic()
            for key in list(human_turns):
                if key[0]==session_id or now-human_turns[key][1]>21600:human_turns.pop(key,None)
            if human:human_turns[(session_id,turn_id)]=(user_message,now)
            while len(human_turns)>256:human_turns.popitem(last=False)
        if academic_question(workflow,str(user_message)):
            agenda=workflow.agenda()
            context='學業總待辦由 academic_workflow 工具查詢。先查同一份紀錄再回答，不重建來源已有的任務；準備產物與本人完成分開。最新來源紀錄有 '+str(len(agenda))+' 項。'+BRIEF_RULE+COURSE_RULE
            if getattr(platform,'value',platform)=='telegram':context+=PHONE_FORMAT
            return {'context':context}
        return None
    def handler(args,session_id='',**kwargs):
        try:
            action=args['action']
            if action=='status':result=status(config,workflow)
            elif action=='brief':
                import brief
                result={'text':brief.build(config,workflow)['text']}
            elif action=='course':
                import brief
                result=brief.course(workflow,args.get('key',''))
            elif action=='attachment':
                import brief
                result=brief.attachment(config,workflow,args.get('key',''),args.get('text',''))
            elif action=='source':
                result=next((x for x in workflow.inbox_items() if x['key']==args.get('key')),{'error':'source_not_found'})
            elif action=='submit':result=submit(config,workflow,args['task_id'])
            elif action=='capture':
                quote=args.get('text','').strip()
                active_session,turn_id=current_tool_identity()
                with lock:
                    record=human_turns.get((session_id,turn_id)) if session_id==active_session else None
                    original=record[0] if record and time.monotonic()-record[1]<=21600 else ''
                if not quote or quote not in original:raise ValueError('Capture requires verbatim current user text')
                key=digest([session_id,turn_id,quote])
                result=workflow.ingest('user',session_id,[dict(id=key,kind='manual',title=quote[:100],description=quote,
                    needs_interpretation=True,submitted=False,closed=False)],complete=True)
            else:raise ValueError('Unsupported action')
            return json.dumps(result,ensure_ascii=False,default=str)
        except Exception as exc:
            return json.dumps({'error':type(exc).__name__,'message':'學業操作未完成；保留原任務與檔案，請核對參數或來源。'},ensure_ascii=False)
    def pre_tool_call(tool_name='',args=None,**kwargs):
        args=args or {}
        if tool_name in {'read_file','search_files','terminal','execute_code'} and SECRET_PATH.search(json.dumps(args,ensure_ascii=False)):
            return {'action':'block','message':'不讀取登入、金鑰或 cookie 檔。學業檔案用 academic_workflow(action="attachment")。'}
        current=current_worker_task()
        if tool_name not in {'kanban_complete','kanban_request_review','kanban_create'} or not current:return None
        if tool_name!='kanban_create' and (args.get('task_id') or current)!=current:return None
        task=NativeTasks(config).get(current)
        if task and task['created_by']=='academic-workflow':
            if tool_name in {'kanban_complete','kanban_request_review','kanban_create'}:
                return {'action':'block','message':'本學業卡使用 manifest 交接：完成實際產物後呼叫 academic_workflow(action="submit",task_id=本卡ID)，由協調器驗證；不另建卡或重複覆核。'}
        return None
    def post_llm_call(session_id='',turn_id='',**kwargs):
        with lock:human_turns.pop((session_id,turn_id),None)
    ctx.register_tool(name='academic_workflow',toolset='skills',schema={
        'name':'academic_workflow','description':'brief=近況完整清單（學業＋旅遊，手機排版，照貼不刪）；course=某門課（key=課名）的最新公告與待辦，含上課地點與附件檔名；attachment=下載某門課作業／公告的附件（key=課名，text=作業或公告名稱），回 files 路徑，用 MEDIA:路徑傳給本人；status=學業紀錄細節；source=讀原始來源；capture=記下本輪本人學業交代；submit=提交準備產物給協調器驗證。',
        'parameters':{'type':'object','required':['action'],'properties':{
            'action':{'type':'string','enum':['brief','course','attachment','status','source','capture','submit']},'key':{'type':'string'},
            'text':{'type':'string','description':'capture：本輪本人要記下的學業事項原文；attachment：作業或公告名稱，例如 HW#3'},'task_id':{'type':'string'}}}},handler=handler)
    ctx.register_hook('pre_llm_call',pre_llm_call)
    ctx.register_hook('pre_tool_call',pre_tool_call)
    ctx.register_hook('post_llm_call',post_llm_call)
