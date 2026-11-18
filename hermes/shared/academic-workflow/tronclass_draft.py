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
import sys


async def main(repo,activity_id,paths):
    sys.path.insert(0,str(Path(repo)/'src'))
    from fju_tronclass.cli._helpers import build_client
    from fju_tronclass.services.homework import find_draft_submission,guess_content_type,wait_upload_ready
    async with build_client() as client:
        profile=await client.get_profile()
        draft=await find_draft_submission(client,activity_id,profile.id)
        kept=[int(u['id']) for u in (draft or {}).get('uploads') or [] if isinstance(u,dict) and u.get('id')]
        added=[]
        for path in paths:
            path=Path(path).resolve()
            created=await client.create_upload(path.name,path.stat().st_size)
            upload_id=int(created['id'])
            await client.put_upload_file(str(created['upload_url']),path,content_type=guess_content_type(path))
            await wait_upload_ready(client,upload_id,interval=2.0,timeout=90.0)
            added.append(dict(id=upload_id,name=path.name,size=path.stat().st_size))
        saved=await client.save_homework_submission(activity_id,comment=(draft or {}).get('comment') or '',
            upload_ids=kept+[x['id'] for x in added],is_draft=True,submission_id=(draft or {}).get('id'))
        after=await find_draft_submission(client,activity_id,profile.id)
        names=[u.get('name') for u in (after or {}).get('uploads') or [] if isinstance(u,dict)]
        return dict(ok=True,activity_id=activity_id,submission_id=(after or {}).get('id') or ((saved or {}).get('submission') or {}).get('id'),
            draft_updated=draft is not None,kept=len(kept),added=added,draft_files=names,is_draft=True)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try:print(json.dumps(asyncio.run(main(sys.argv[1],int(sys.argv[2]),sys.argv[3:])),ensure_ascii=False))
    except Exception as exc:print(json.dumps(dict(ok=False,error=type(exc).__name__,message=str(exc)[:300]),ensure_ascii=False))
