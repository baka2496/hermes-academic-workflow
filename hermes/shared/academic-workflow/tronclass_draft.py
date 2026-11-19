"""Save files as a TronClass homework draft, keeping files already in the draft.

Runs with the FJU-TronClass-MCP virtualenv (it holds the login and HTTP client):

    <tronclass venv python> tronclass_draft.py <repo> <activity id> <file> [<file> ...]

Prints one JSON line. Only ever saves a draft (is_draft=True): the owner presses 「繳交作業」 on the
TronClass page. The CLI's own `homework upload` takes one file and replaces the draft's files, so a
second photo of the same homework would silently drop the first; this keeps them.
"""
from pathlib import Path
import asyncio
import json
import re
import sys


TELEGRAM_NAME=re.compile(r'^img_[0-9a-f]{6,}\.\w+$',re.I)


def plan(existing,files):
    """Which draft files to keep, which new files to upload, and their names.

    11/19: the draft held the photo as img_920d149bd7b2.jpg (Telegram's name). Sending it again must
    not add the same picture twice. A draft file of exactly the same size is that same photo:
    - still under Telegram's random name: replace it with the copy named after the homework;
    - already well named: leave it and upload nothing (each new upload stays in the owner's TronClass
      storage, and TronClass then renames the next one HW3(1).jpg, HW3(2).jpg …).
    A different second photo gets HW3_2.jpg instead of a second HW3.jpg.

    existing: [dict(id,name,size)]; files: [(name,size)].
    Returns (kept ids, replaced names, already-there names, [(index,name) to upload])."""
    replaced=[];already=[];uploads=[]
    for i,(name,size) in enumerate(files):
        same=next((u for u in existing if u.get('size')==size and u not in replaced),None)
        if same and not TELEGRAM_NAME.match(str(same.get('name') or '')):already.append(same.get('name'));continue
        if same:replaced.append(same)
        uploads.append((i,name))
    kept=[u for u in existing if u not in replaced]
    taken={str(u.get('name') or '').lower() for u in kept}
    named=[]
    for i,name in uploads:
        stem,dot,ext=name.rpartition('.')
        if not dot:stem,ext=name,''
        candidate,n=name,1
        while candidate.lower() in taken:
            n+=1;candidate=stem+'_'+str(n)+('.'+ext if ext else '')
        taken.add(candidate.lower());named.append((i,candidate))
    return [int(u['id']) for u in kept],[u.get('name') for u in replaced],already,named


async def main(repo,activity_id,paths):
    sys.path.insert(0,str(Path(repo)/'src'))
    from fju_tronclass.cli._helpers import build_client
    from fju_tronclass.services.homework import find_draft_submission,guess_content_type,wait_upload_ready
    async with build_client() as client:
        profile=await client.get_profile()
        draft=await find_draft_submission(client,activity_id,profile.id)
        existing=[u for u in (draft or {}).get('uploads') or [] if isinstance(u,dict) and u.get('id')]
        paths=[Path(x).resolve() for x in paths]
        kept,replaced,already,uploads=plan(existing,[(x.name,x.stat().st_size) for x in paths])
        if not uploads:
            names=[u.get('name') for u in existing]
            return dict(ok=True,activity_id=activity_id,submission_id=(draft or {}).get('id'),draft_updated=False,
                kept=len(kept),replaced=[],already=already,added=[],draft_files=names,is_draft=True)
        added=[]
        for index,name in uploads:
            path=paths[index]
            created=await client.create_upload(name,path.stat().st_size)
            upload_id=int(created['id'])
            await client.put_upload_file(str(created['upload_url']),path,content_type=guess_content_type(path))
            await wait_upload_ready(client,upload_id,interval=2.0,timeout=90.0)
            added.append(dict(id=upload_id,name=name,size=path.stat().st_size))
        saved=await client.save_homework_submission(activity_id,comment=(draft or {}).get('comment') or '',
            upload_ids=kept+[x['id'] for x in added],is_draft=True,submission_id=(draft or {}).get('id'))
        after=await find_draft_submission(client,activity_id,profile.id)
        names=[u.get('name') for u in (after or {}).get('uploads') or [] if isinstance(u,dict)]
        return dict(ok=True,activity_id=activity_id,submission_id=(after or {}).get('id') or ((saved or {}).get('submission') or {}).get('id'),
            draft_updated=draft is not None,kept=len(kept),replaced=replaced,already=already,added=added,draft_files=names,is_draft=True)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try:print(json.dumps(asyncio.run(main(sys.argv[1],int(sys.argv[2]),sys.argv[3:])),ensure_ascii=False))
    except Exception as exc:print(json.dumps(dict(ok=False,error=type(exc).__name__,message=str(exc)[:300]),ensure_ascii=False))
