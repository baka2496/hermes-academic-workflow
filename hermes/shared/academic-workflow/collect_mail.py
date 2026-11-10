"""Read-only, bounded deeper academic scan using existing local Gmail authorization."""
from datetime import datetime,timedelta,timezone
import importlib.util
import json
import re
import sys
import time

PROGRESS={'stage':'load_existing_authorization','started':time.monotonic()}


def collect(path,header_budget=1000,body_budget=40,course_names=()):
    spec=importlib.util.spec_from_file_location('academic_existing_mailbox',path)
    mailbox=importlib.util.module_from_spec(spec);spec.loader.exec_module(mailbox)
    errors=[];messages=[]
    with mailbox.gmail() as (client,_):
        PROGRESS['stage']='select_readonly_all_mail'
        mailbox.imap_result(client.select(mailbox.folder(client,'All'),readonly=True))
        cutoff=(datetime.now(timezone.utc)-timedelta(days=30)).strftime('%d-%b-%Y')
        PROGRESS['stage']='search_last_30_days'
        identifiers=mailbox.search(client,'SINCE',cutoff)
        selected=identifiers[-header_budget:]
        candidates=[]
        for offset in range(0,len(selected),100):
            PROGRESS.update(stage='read_message_headers',batch=offset//100+1,selected=len(selected))
            rows=mailbox.gmail_fetch(client,selected[offset:offset+100],'BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)]')
            for row in rows:
                preview=mailbox.describe_gmail(row,12000)
                text=preview.get('from','')+' '+preview.get('subject','')+' '+preview.get('body','')
                if re.search(r'fju\.edu\.tw|TronClass|學校|系辦|助教|繳交|作業|期中|期末|課程|實驗報告|專題',text,re.I) or any(name and name in text for name in course_names):
                    candidates.append(row)
        # Body-only academic mail is a fixed scan limitation, reported in coverage rather than as a
        # failure; otherwise Gmail could never be marked complete and every digest would warn.
        body_reads=0
        for row in reversed(candidates):
            value=mailbox.describe_gmail(row,12000)
            if body_reads<body_budget:
                PROGRESS['stage']='read_academic_candidate_body'
                try:
                    value=mailbox.describe_gmail(mailbox.gmail_message(client,row['id']),12000)
                    body_reads+=1
                    if len(value.get('body',''))>=12000:errors.append('message_body_excerpt')
                except Exception as exc:
                    errors.append('candidate_body_'+type(exc).__name__)
                    # Preserve every candidate header and end body reads on a broken socket.
                    body_reads=body_budget
            else:errors.append('candidate_body_budget_reached')
            messages.append(value)
        return {'gmail':{'messages':messages,'truncated':len(identifiers)>header_budget or len(candidates)>body_budget,
                         'errors':sorted(set(errors)),'coverage':{'folder':'All Mail','days':30,'matched_headers':len(identifiers),'inspected_headers':len(selected),'academic_candidates':len(candidates),'read_messages':len(messages),'scan':'sender_subject_and_course_names_then_candidate_body','body_prefix_bytes':36000,'header_filter_only':True,'body_only_mail_may_be_missed':True}},
                'outlook':{'error':'not_authorized_or_unavailable','messages':[]}}


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        names=json.loads(__import__('pathlib').Path(sys.argv[2]).read_text(encoding='utf-8')) if len(sys.argv)>2 else []
        result=collect(sys.argv[1],course_names=names)
    except Exception as exc:result={'gmail':{'error':type(exc).__name__,'messages':[],'diagnostic':dict(stage=PROGRESS['stage'],elapsed_seconds=round(time.monotonic()-PROGRESS['started'],1),batch=PROGRESS.get('batch'))},'outlook':{'error':'not_authorized_or_unavailable','messages':[]}}
    print(json.dumps(result,ensure_ascii=False))
