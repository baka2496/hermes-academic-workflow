"""Everything the owner has going on, built straight from the source records.

Bots answer "目前有什麼事情" by relaying this output instead of picking items from memory, so an
answer can never silently drop an item or change a date (a model with less reasoning dropped
pending items; a model with more reasoning dropped others). Output is phone-formatted: one item
per line, no tables.

    python brief.py [--config config.json] [--days 14] [--json]
"""
from datetime import datetime,timedelta
from pathlib import Path
import argparse
import json
import re
import sys

WEEKDAY='一二三四五六日'
DONE_TRIP={'已完成','已取消','已結束','已放棄'}


def when(due,clock):
    day=due.strftime('%m/%d')
    if due.date()==clock.date():return '今天'+due.strftime('%H:%M')
    if due.date()==(clock+timedelta(days=1)).date():return '明天'+due.strftime('%H:%M')
    return day+'（'+WEEKDAY[due.weekday()]+'）'


def short(text,width=12):
    text=re.sub(r'[【】\[\]「」"“”]','',text or '').strip()
    return text if len(text)<=width else text[:width-1]+'…'


def academic(workflow,clock,days):
    shown,later,undated,overdue=[],0,0,[]
    for item in workflow.agenda(now=clock.isoformat()):
        if item.get('human_state')!='open':
            if item.get('human_state')=='overdue' and item.get('due_at'):overdue.append(item)
            continue
        if not item.get('due_at'):undated+=1;continue
        due=datetime.fromisoformat(item['due_at'])
        if due-clock>timedelta(days=days):later+=1;continue
        course=re.sub(r'[（(][^）)]*[）)]?$','',item.get('course') or '').strip()
        title=re.sub(r'[【】\[\]「」"“”]','',item.get('title','')).replace(course,'').strip() if course else item.get('title','')
        # Same rule as the overview table: a date read out of an announcement still needs checking.
        shown.append(dict(due=due,name=short((course+' ' if course else '')+title,13),when=when(due,clock),
            tentative=item.get('confidence')=='needs_check' or bool(item.get('deadline_conflicts'))))
    return sorted(shown,key=lambda x:x['due']),later,undated


def dates_in(text):
    return set(re.findall(r'20\d\d-\d\d-\d\d',text or ''))


def trips(path,clock):
    try:records=json.loads(Path(path).read_text(encoding='utf-8')).get('opportunities',[])
    except (OSError,ValueError):return None
    result=[]
    for record in records:
        decision=record.get('user_decision')
        decision=decision.get('status') if isinstance(decision,dict) else decision
        if decision!='pending' and record.get('status')!='已選定':continue
        if record.get('status') in DONE_TRIP:continue
        start,end=record.get('outbound_date'),record.get('return_date')
        if end and end<clock.date().isoformat():continue
        span=(start[5:].replace('-','/')+('–'+end[5:].replace('-','/') if end and end!=start else '')) if start else '日期未定'
        problems=[]
        # A calendar hold or venue that does not match the trip's own dates means the record contradicts
        # itself (e.g. an overnight trip with a one-day hold): say so instead of picking one version.
        hold=(record.get('calendar_hold_check') or {}).get('details','')
        if start and dates_in(hold) and not dates_in(hold)&{start,end}:problems.append('日期')
        elif start and end and dates_in(hold) and start!=end and dates_in(hold)=={end}:problems.append('天數')
        venue=(record.get('venue_research') or {}).get('venue','')
        place=re.match(r'([^\s（(]{2})',venue)
        if place and place.group(1) not in (record.get('destination','')+record.get('title','')):problems.append('地點')
        status=record.get('status','')
        label={'不符合':'不符合條件，待你決定'}.get(status,status)
        title=re.sub(r'（.*?）|規劃|\(.*?\)','',record.get('title','')).strip()
        result.append(dict(id=record.get('id'),name=short(title,12),when=span,status=label,problems=problems))
    return result


def build(config,workflow,*,now=None,days=14):
    clock=datetime.fromisoformat(now) if now else datetime.now().astimezone()
    items,later,undated=academic(workflow,clock,days)
    lines=['📚 學業（'+str(days)+' 天內）']
    lines+=['• '+x['name']+'｜'+x['when']+('＊' if x['tentative'] else '') for x in items] or ['• 沒有']
    extra=[]
    if later:extra.append(str(later)+' 項較晚')
    if undated:extra.append(str(undated)+' 項日期待確認')
    if extra:lines.append('＋另有 '+'、'.join(extra))
    if any(x['tentative'] for x in items):lines.append('＊日期由公告推導，待核對')
    trip_path=config.get('trip_opportunities') or str(Path(config.get('hermes_root',''))/'shared/travel-workflow/opportunities.json')
    found=trips(trip_path,clock)
    lines.append('🧳 旅遊')
    if found is None:lines.append('• 旅遊紀錄讀不到（未確認）')
    else:
        for x in found:
            lines.append('• '+x['name']+'｜'+x['when'])
            lines.append('  '+x['status']+('｜⚠️紀錄矛盾：'+'、'.join(x['problems']) if x['problems'] else ''))
        if not found:lines.append('• 沒有進行中的旅遊')
    return dict(text='\n'.join(lines),academic=[{k:v for k,v in x.items() if k!='due'} for x in items],
        academic_later=later,academic_undated=undated,trips=found,generated_at=clock.isoformat(timespec='minutes'))


def bare(name):
    return re.sub(r'[（(][^）)]*[）)]?$','',name or '').strip().lower()


def course(workflow,name,*,now=None,announcements=3):
    """One course's latest announcements and open deadlines, from the locally collected TronClass data.
    "體育課在哪裡上課" took 3.5 minutes of searching files and logs; the answer was already stored here."""
    clock=datetime.fromisoformat(now) if now else datetime.now().astimezone()
    wanted=bare(re.sub(r'課$','',(name or '').strip()))
    items=workflow.inbox_items()
    names=sorted({x.get('course') for x in items if x.get('course')})
    matched=[n for n in names if wanted and (wanted in bare(n) or bare(n) in wanted)]
    if not matched:
        return dict(text='找不到「'+(name or '')+'」這門課。目前的課：'+'、'.join(names),matched=[])
    lines=[]
    checked=max((s['observed_at'] for s in workflow.sources() if s['source']=='tronclass'),default='')
    for title in matched:
        lines.append('🏫 '+title+('（資料 '+checked[11:16]+' 更新）' if checked else ''))
        posts=sorted((x for x in items if x.get('course')==title and x.get('kind')=='announcement' and not x.get('closed')),
            key=lambda x:x.get('published_at') or '',reverse=True)[:announcements]
        lines.append('📢 最新公告' if posts else '📢 沒有公告')
        for post in posts:
            day=datetime.fromisoformat(post['published_at']) if post.get('published_at') else None
            stamp='今天' if day and day.date()==clock.date() else day.strftime('%m/%d') if day else '日期未知'
            lines.append('• '+stamp+' '+short(post.get('title'),14)+'：'+re.sub(r'\s+',' ',post.get('description') or '').strip()[:120])
        open_items=[x for x in workflow.agenda(now=clock.isoformat()) if x.get('course')==title and x.get('human_state')=='open']
        if open_items:
            lines.append('📝 待辦')
            for item in sorted(open_items,key=lambda x:x.get('due_at') or '9999')[:5]:
                due=datetime.fromisoformat(item['due_at']) if item.get('due_at') else None
                lines.append('• '+short(item.get('title'),14)+'｜'+(when(due,clock) if due else '日期待確認'))
    return dict(text='\n'.join(lines),matched=matched,checked_at=checked)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser()
    parser.add_argument('--config');parser.add_argument('--days',type=int,default=14);parser.add_argument('--json',action='store_true')
    args=parser.parse_args()
    sys.path.insert(0,str(Path(__file__).parent))
    from academic import load
    config,workflow=load(args.config)
    result=build(config,workflow,days=args.days)
    print(json.dumps(result,ensure_ascii=False,indent=1) if args.json else result['text'])


if __name__=='__main__':main()
