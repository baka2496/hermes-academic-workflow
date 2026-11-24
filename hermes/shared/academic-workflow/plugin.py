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


ACADEMIC_TOPIC=r'學業|作業|考試|複習|讀書|課|教室|考場|公告|老師|待辦|最近有什麼|有什麼事|重要的事|期限|截止|題目|講義|附件|考卷|HW|hw|Quiz|quiz|繳交|上傳|草稿|成績單|歷年成績|數位成績單|學期成績|排名|在學證明|註冊|請假|假單|病假|事假|公假|喪假|生理假|缺曠|通識|開課'
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
             '照它回的 files 每個檔案一行寫 MEDIA:路徑傳給本人，直接傳檔，不要先讀檔或轉文字（題目常是掃描檔）；工具回錯誤就照實說失敗，不要說已接單或另派別人。'
             # 11/18 smoke test: a pre-send agent_preferences review on every lookup made one answer take 60 s.
             # 11/19: the owner sent a photo of a finished derivation and wanted it handed in.
             '本人要把照片或檔案交到作業（交、繳交、上傳、存草稿）時用 action=draft、key=課名、text=作業名稱、files=[圖片路徑]，'
             '照它回的 text 回覆；它只存草稿，一定要講「還沒正式繳交，請到 TronClass 按繳交作業」，絕不能說已繳交。'
             '要歷年成績單或學期數位成績單時用 action=transcript（text 可填指定學期或「不含排名」，不填則下載全部學期），'
             '要數位在學證明時用 action=enrollment（text 可填「英文」或學期），照它回的 files 每個檔案一行寫 MEDIA:路徑傳給本人；失敗就照實回報。'
             '查當學期註冊狀況、全校開課／通識課程或請假紀錄統計時用 action=sis（key=registration|courses|leave，text=關鍵字）。'
             '要建立請假草稿時用 action=leave_draft（帶 begin_date、end_date、begin_section、end_section、leave_type、text=事由、files=證明圖片路徑），'
             '它只存請假草稿，絕不自動送出，回覆一定要講「還沒正式送出，請到學生資訊平台確認後送出」。'
             '使用者要把信件或成績單／附件「存到 Gmail 草稿」時，一定要呼叫 action=mail_draft（帶 to=收件人Email、subject=主旨、text=內文、files=附件路徑）真正寫入 Gmail 草稿匣，'
             'mail_draft 會自動無損合併多個大 PDF，直接把 transcript 回傳的檔案路徑放進 files 即可，絕對不要開 terminal 自己合併 PDF 或自己上網爬 Email；'
             '絕不能只在聊天室打一段「【信件草稿】」就說已存到草稿；存完提醒本人打開 Gmail 草稿匣確認後再自行寄出。'
             'brief、course、attachment、transcript、enrollment、sis 這類查詢直接回覆，不需要 agent_preferences review 或 tool_describe。')
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
    import vision
    # Photos are cached under the profile; the owner often sends the photo, then the question.
    image_roots=[Path(config.get('hermes_root',''))/'profiles/commander/cache'] if config.get('hermes_root') else []
    recent_images={};looker=vision.Looker(image_roots)
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
        context=''
        with lock:
            found=vision.image_paths(str(user_message))
            if found:recent_images[session_id]=(found,time.monotonic())
            elif session_id in recent_images and time.monotonic()-recent_images[session_id][1]<=1800:found=recent_images[session_id][0]
        # 11/19 live test: plugin tools are deferred behind tool_search, so the model never saw
        # view_image, tried read_file on the JPEG and again said it had no way to see pictures.
        # Looking at the photo here does not depend on the model finding the tool.
        if found:context+=looker.context(found,str(user_message))
        try:
            import contacts
            contact_ctx=contacts.lookup_context(config,workflow,str(user_message))
            if contact_ctx:context+=contact_ctx
        except Exception:pass
        if academic_question(workflow,str(user_message)):
            agenda=workflow.agenda()
            context+='學業總待辦由 academic_workflow 工具查詢。先查同一份紀錄再回答，不重建來源已有的任務；準備產物與本人完成分開。最新來源紀錄有 '+str(len(agenda))+' 項。'+BRIEF_RULE+COURSE_RULE
            if getattr(platform,'value',platform)=='telegram':context+=PHONE_FORMAT
        return {'context':context} if context else None
    def view_image(args,**kwargs):
        try:return json.dumps(looker.result(args.get('path',''),args.get('question',''))
            or {'error':'timeout','message':'看圖逾時，請稍後再傳一次。'},ensure_ascii=False)
        except Exception as exc:return json.dumps({'error':type(exc).__name__,'message':'看圖失敗，請稍後再傳一次。'},ensure_ascii=False)
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
            elif action=='draft':
                import brief
                files=args.get('files') or []
                if isinstance(files,str):files=[files]
                result=brief.draft(config,workflow,args.get('key',''),args.get('text',''),files,roots=image_roots)
            elif action=='transcript':
                import brief
                result=brief.transcript(config,workflow,args.get('text',''))
            elif action=='enrollment':
                import brief
                result=brief.enrollment(config,workflow,args.get('text',''))
            elif action=='sis':
                import brief
                result=brief.sis_query(config,workflow,args.get('key',''),args.get('text',''))
            elif action=='contact':
                import contacts
                q=args.get('text') or args.get('key') or ''
                result=contacts.lookup(config,workflow,'查 '+str(q)+' 的 email') or {'name':q,'matches':[],'pages':[]}
            elif action=='leave_draft':
                import brief
                files=args.get('files') or []
                if isinstance(files,str):files=[files]
                result=brief.leave_draft(
                    config,workflow,
                    leave_type=args.get('leave_type') or '病假',
                    begin_date=args.get('begin_date') or '',
                    end_date=args.get('end_date') or '',
                    begin_section=args.get('begin_section') or '',
                    end_section=args.get('end_section') or '',
                    reason=args.get('text') or '',
                    paths=files,roots=image_roots)
            elif action=='mail_draft':
                import brief
                files=args.get('files') or []
                if isinstance(files,str):files=[files]
                result=brief.mail_draft(
                    config,workflow,
                    to=args.get('to') or args.get('key') or '',
                    subject=args.get('subject') or '',
                    body=args.get('text') or '',
                    paths=files,roots=image_roots)
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
        'name':'academic_workflow','description':'brief=近況完整清單（學業＋旅遊，手機排版，照貼不刪）；course=某門課（key=課名）的最新公告與待辦，含上課地點與附件檔名；attachment=下載某門課作業／公告的附件（key=課名，text=作業或公告名稱），回 files 路徑，用 MEDIA:路徑傳給本人；draft=把本人傳的照片／檔案存成 TronClass 作業草稿（key=課名，text=作業名稱，files=圖片路徑；只存草稿，本人自己按繳交）；transcript=下載官方數位成績單 PDF；enrollment=下載官方數位在學證明 PDF（text 可填英文或學期）；sis=查詢學生資訊平台（key=registration|courses|leave，text=關鍵字）；contact=查詢老師公開 Email；leave_draft=建立請假草稿（只存草稿不自動送出，可附本人傳的證明照片）；mail_draft=真正寫入 Gmail 草稿匣（帶 to、subject、text=內文、files=附件路徑；只存草稿不寄出，附件過大自動拆封）；status=學業紀錄細節；source=讀原始來源；capture=記下本輪本人學業交代；submit=提交準備產物給協調器驗證。',
        'parameters':{'type':'object','required':['action'],'properties':{
            'action':{'type':'string','enum':['brief','course','attachment','draft','transcript','enrollment','sis','contact','leave_draft','mail_draft','status','source','capture','submit']},'key':{'type':'string'},
            'files':{'type':'array','items':{'type':'string'},'description':'draft/leave_draft/mail_draft：要附加的本機檔案路徑'},
            'text':{'type':'string','description':'capture：原文；attachment：作業名；transcript/enrollment：學期或版本；sis：搜尋關鍵字；leave_draft：請假事由；mail_draft：信件內文'},
            'to':{'type':'string','description':'mail_draft：收件人 Email 地址'},
            'subject':{'type':'string','description':'mail_draft：信件主旨'},
            'leave_type':{'type':'string','description':'leave_draft：假別（預設病假，如事假、病假、公假）'},
            'begin_date':{'type':'string','description':'leave_draft：開始日期 YYYY-MM-DD'},
            'end_date':{'type':'string','description':'leave_draft：結束日期 YYYY-MM-DD（可省略同開始日期）'},
            'begin_section':{'type':'string','description':'leave_draft：開始節次（如 1 或 D1）'},
            'end_section':{'type':'string','description':'leave_draft：結束節次（如 2 或 D2）'},
            'task_id':{'type':'string'}}}},handler=handler)
    ctx.register_tool(name='view_image',toolset='skills',schema={
        'name':'view_image','description':'看本人傳來的照片或截圖：打開 Hermes 暫存的圖片檔，抄出上面的文字、算式與數據。本人傳圖時一定先用它，不要說沒收到圖片。',
        'parameters':{'type':'object','required':['path'],'properties':{
            'path':{'type':'string','description':'圖片路徑（訊息裡 image_url: 後面的路徑）'},
            'question':{'type':'string','description':'本人想從圖裡知道什麼'}}}},handler=view_image)
    ctx.register_hook('pre_llm_call',pre_llm_call)
    ctx.register_hook('pre_tool_call',pre_tool_call)
    ctx.register_hook('post_llm_call',post_llm_call)
