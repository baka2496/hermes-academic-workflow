"""Create a real draft (with optional attachments) in Gmail Drafts using existing mailbox.py."""
from __future__ import annotations

import email.policy
from email.message import EmailMessage
from email.utils import formatdate
import hashlib
import importlib.util
import json
import mimetypes
from pathlib import Path
import re
import sys
import time

# Gmail rejects messages over 25 MB after base64 (~33% overhead), so cap raw attachments at 18 MB.
MAX_RAW_ATTACHMENTS_BYTES = 18 * 1024 * 1024


def create_draft(mailbox_path: str, to_addr: str, subject: str, body: str, attachments: list[str] = ()) -> dict:
    to_addr = str(to_addr or '').strip()
    if not re.fullmatch(r'[^@\s<>]+@[^@\s<>]+', to_addr):
        return {'ok': False, 'error': 'invalid_recipient', 'message': '收件人 Email 格式無效：' + to_addr}
    files = [Path(p).resolve() for p in (attachments or []) if p]
    missing = [str(p) for p in files if not p.is_file()]
    if missing:
        return {'ok': False, 'error': 'missing_file', 'message': '找不到附件：' + '、'.join(Path(x).name for x in missing)}
    total_bytes = sum(p.stat().st_size for p in files)
    if total_bytes > MAX_RAW_ATTACHMENTS_BYTES:
        mb = round(total_bytes / (1024 * 1024), 1)
        return {
            'ok': False,
            'error': 'attachments_too_large',
            'total_mb': mb,
            'message': f'附件總大小 {mb} MB，經 Base64 編碼後會超過 Gmail 25 MB 單封信上限（原始檔建議 18 MB 內）；請減少附件數量或分兩封草稿。',
        }
    spec = importlib.util.spec_from_file_location('academic_existing_mailbox', mailbox_path)
    mb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mb)

    with mb.gmail() as (client, account):
        # Python/OpenSSL on Windows raises SSL: BAD_LENGTH when imaplib sends >16 MB in one write.
        orig_send = client.send
        def chunked_send(data):
            step = 256 * 1024
            if len(data) <= step:
                return orig_send(data)
            view = memoryview(data)
            for off in range(0, len(view), step):
                orig_send(view[off:off + step])
        client.send = chunked_send
        draft_folder = mb.folder(client, 'Drafts')
        draft_mid = '<academic-draft-' + hashlib.sha256(f'{to_addr}|{subject}|{time.time()}'.encode('utf-8')).hexdigest()[:24] + '@localhost>'
        msg = EmailMessage(policy=email.policy.SMTP)
        msg['From'] = account
        msg['To'] = to_addr
        msg['Subject'] = subject or '（無主旨）'
        msg['Message-ID'] = draft_mid
        msg['Date'] = formatdate(usegmt=True)
        msg.set_content(body or '')
        attached = []
        for p in files:
            ctype, _ = mimetypes.guess_type(p.name)
            maintype, subtype = (ctype.split('/', 1) if ctype and '/' in ctype else ('application', 'octet-stream'))
            msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
            attached.append({'name': p.name, 'size': p.stat().st_size})
        mb.imap_result(client.append(draft_folder, '(\\Draft)', None, msg.as_bytes()))
        mb.imap_result(client.select(draft_folder, readonly=True))
        verified = bool(mb.search(client, 'HEADER', 'MESSAGE-ID', draft_mid))
        return {
            'ok': verified,
            'verified': verified,
            'account': account,
            'to': to_addr,
            'subject': msg['Subject'],
            'message_id': draft_mid,
            'attachments': attached,
            'total_mb': round(total_bytes / (1024 * 1024), 2),
        }


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    payload = json.loads(sys.stdin.read())
    try:
        res = create_draft(
            payload['mailbox'],
            payload['to'],
            payload.get('subject', ''),
            payload.get('body', ''),
            payload.get('attachments') or [],
        )
    except Exception as exc:
        res = {'ok': False, 'error': type(exc).__name__, 'message': str(exc)[:240]}
    print(json.dumps(res, ensure_ascii=False))


if __name__ == '__main__':
    main()
