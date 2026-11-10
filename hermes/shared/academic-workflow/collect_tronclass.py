"""Read-only collector executed by the existing authenticated TronClass interpreter."""
import asyncio
from contextlib import redirect_stdout
from datetime import datetime,timezone,timedelta
import html
import json
import re
import sys

TZ=timezone(timedelta(hours=8))


def current_semester(now):
    if now.month>=8:return f'{now.year-1911}1'
    return f'{now.year-1912}{1 if now.month==1 else 2}'


def text(value):
    if not isinstance(value,str):return ''
    return re.sub(r'\s+',' ',html.unescape(re.sub(r'<[^>]+>',' ',value))).strip()


def instant(value):
    if value is None or value=='':return None
    try:
        if isinstance(value,(int,float)):
            stamp=float(value)/(1000 if float(value)>10**11 else 1)
            return datetime.fromtimestamp(stamp,timezone.utc).astimezone(TZ).isoformat()
        dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return dt.astimezone(TZ).isoformat() if dt.tzinfo else None
    except (ValueError,OverflowError,OSError):return None


async def paged(http,path,array_key,params=None):
    accumulated={};errors=[];complete=False;last_page=None
    for page in range(1,21):
        response=await asyncio.wait_for(http.get_json(path,params={**(params or {}),'page':page,'page_size':100}),timeout=15)
        if not isinstance(response,dict) or not isinstance(response.get(array_key),list):
            raise ValueError('unexpected_collection_shape')
        raw=response[array_key]
        ids=[str(x['id']) for x in raw if isinstance(x,dict) and x.get('id') is not None]
        if len(ids)!=len(raw):raise ValueError('collection_item_missing_id')
        new={str(x['id']):x for x in raw if str(x['id']) not in accumulated}
        accumulated.update(new)
        total=response.get('total');pages=response.get('pages');reported=response.get('page')
        if isinstance(total,int) and total>=len(accumulated) and len(accumulated)==total:
            complete=True;break
        if isinstance(pages,int) and isinstance(reported,int) and reported==page and page>=pages:
            complete=True;break
        if not new or (page>1 and reported is not None and reported==last_page):
            errors.append('pagination_not_advancing');break
        if not isinstance(total,int) and not isinstance(pages,int):
            errors.append('coverage_metadata_unavailable');break
        last_page=reported
    else:errors.append('page_budget_reached')
    return list(accumulated.values()),complete,errors


async def collect(semester):
    from fju_tronclass.cli._helpers import build_client
    from fju_tronclass.config import get_settings
    snapshots=[];catalog=[]
    base=get_settings().tronclass_base_url.rstrip('/')
    async with build_client() as client:
        courses=await asyncio.wait_for(client.get_my_courses(),timeout=30)
        catalog=[c.model_dump(mode='json') for c in courses if str(c.semester).replace('-','')==semester.replace('-','')]
        if not catalog:raise ValueError('no_current_semester_courses')
        snapshots.append(dict(source='tronclass',scope='courses:'+semester,items=[],complete=True,errors=[]))
        for course in catalog:
            cid=str(course['id']);name=course.get('name','未命名課程')
            for kind,path,key in [
                ('homework',f'/api/courses/{cid}/homework-activities','homework_activities'),
                ('exam',f'/api/courses/{cid}/exams','exams'),
                ('announcement','/api/course-bulletins','bulletins')]:
                scope=semester+':'+cid+':'+kind
                try:
                    raws,complete,errors=await paged(client._http,path,key,{'course_id':cid} if kind=='announcement' else None)
                    items=[]
                    for raw in raws:
                        if kind=='announcement' and str(raw.get('course_id'))!=cid:continue
                        identifier=str(raw['id'])
                        detail={}
                        if kind=='homework' and not raw.get('submitted') and not raw.get('is_closed'):
                            try:detail=await asyncio.wait_for(client.get_activity(int(identifier)),timeout=15)
                            except Exception as exc:errors.append('detail_'+type(exc).__name__);complete=False
                        merged={**raw,**detail}
                        data=merged.get('data') if isinstance(merged.get('data'),dict) else {}
                        content=text(merged.get('description') or merged.get('content') or merged.get('body') or data.get('description') or '')
                        # A bulletin is evidence, not automatically a new assignment.
                        item=dict(id=f'{kind}:{cid}:{identifier}',kind=kind,course=name,course_id=cid,
                            title=text(raw.get('title') or raw.get('name')) or '未命名項目',description=content,
                            due_at=instant(raw.get('deadline') or raw.get('end_time')) if kind=='homework' else instant(raw.get('start_time')) if kind=='exam' else None,
                            start_at=instant(raw.get('start_time')),submitted=raw.get('submitted') if isinstance(raw.get('submitted'),bool) else None,
                            closed=raw.get('is_closed') if isinstance(raw.get('is_closed'),bool) else None,
                            published_at=instant(raw.get('created_at') or raw.get('publish_time')),
                            url=f'{base}/course/{cid}',
                            source_ref=dict(course_id=cid,item_id=identifier,kind=kind),
                            source_requirements=dict(title=raw.get('title'),description=content,
                                attachments=merged.get('uploads',[]),other_resources=data.get('other_resources',[])),
                            needs_interpretation=kind=='announcement')
                        items.append(item)
                    snapshots.append(dict(source='tronclass',scope=scope,items=items,complete=complete,errors=errors))
                except Exception as exc:
                    snapshots.append(dict(source='tronclass',scope=scope,items=[],complete=False,errors=[type(exc).__name__]))
    return dict(observed_at=datetime.now(TZ).isoformat(),semester=semester,courses=catalog,snapshots=snapshots)


def main():
    sys.stdout.reconfigure(encoding='utf-8');sys.stderr.reconfigure(encoding='utf-8')
    now=datetime.now(TZ)
    semester=sys.argv[1] if len(sys.argv)>1 else current_semester(now)
    try:
        with redirect_stdout(sys.stderr):data=asyncio.run(collect(semester))
    except BaseException as exc:
        data=dict(observed_at=now.isoformat(),semester=semester,courses=[],snapshots=[dict(source='tronclass',scope='courses:'+semester,items=[],complete=False,errors=[type(exc).__name__])])
    print(json.dumps(data,ensure_ascii=False,default=str))


if __name__=='__main__':main()
