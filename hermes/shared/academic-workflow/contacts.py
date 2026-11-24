"""Proactive contact email lookup from local academic DB, Gmail history, and public FJU pages."""
from __future__ import annotations

import html
import json
from pathlib import Path
import re
import ssl
import subprocess
import time
import urllib.parse
import urllib.request

EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
MAIL_INTENT = re.compile(
    r'(?:寄信|寫信|發信|回信|傳信|mail|email|草稿)\s*給\s*([^，。！？?!\n]{2,28})'
    r'|(?:查(?:一下|詢)?|找)\s*([^，。！？?!\n]{2,24}?)\s*的?\s*(?:email|e-mail|信箱|郵件)',
    re.I,
)
TRAILING_STOP = re.compile(r'(?:嗎|呢|吧|好嗎|可以嗎|的信箱|的email|的郵件|老師|教授|副教授|助理教授|助教|系主任|主任|先生|小姐).*$', re.I)
ORG_PREFIX = re.compile(r'^((?:輔仁大學|輔大)?[\u4e00-\u9fff]{1,6}(?:系|所|學院|中心|處|室|組))\s*')
GENERIC_EMAILS = re.compile(r'^(?:noreply|no-reply|info|service|webmaster|admin|contact|office|im|mis|csie|ee)@', re.I)

_CACHE: dict[str, tuple[float, dict]] = {}


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if hasattr(ssl, 'VERIFY_X509_STRICT'):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def extract_recipient_query(text: str) -> dict | None:
    if not text or not isinstance(text, str):
        return None
    m = MAIL_INTENT.search(text)
    if not m:
        return None
    raw = (m.group(1) or m.group(2) or '').strip()
    raw = re.split(r'[，。！？?!\n]|告訴|說明|問|請教|關於|附上', raw)[0].strip()
    if not raw or EMAIL_RE.search(raw):
        return None
    cleaned = TRAILING_STOP.sub('', raw).strip()
    org = ''
    om = ORG_PREFIX.match(cleaned)
    if om:
        org = om.group(1).strip()
        name = cleaned[om.end():].strip()
    else:
        stripped = re.sub(r'^(?:輔仁大學|輔大)', '', cleaned).strip()
        # Handle "輔大物理邱紹斌" (department without trailing 系/所 + 3-char Chinese name)
        if re.fullmatch(r'[\u4e00-\u9fff]{5,7}', stripped):
            org = stripped[:-3]
            name = stripped[-3:]
        else:
            name = stripped
    name = re.sub(r'^(?:輔仁大學|輔大)', '', name).strip()
    if len(name) < 2 or len(name) > 12:
        return None
    return {'raw': raw, 'name': name, 'org': org}


def _deobfuscate_emails(raw_html: str) -> list[str]:
    text = html.unescape(raw_html or '')
    text = re.sub(r'\s*(?:\[at\]|\(at\)|＠|\[＠\])\s*', '@', text, flags=re.I)
    text = re.sub(r'\s*(?:\[dot\]|\(dot\))\s*', '.', text, flags=re.I)
    found = EMAIL_RE.findall(text)
    out = []
    for addr in found:
        low = addr.lower()
        if low.endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg', '.css', '.js')):
            continue
        if low not in out:
            out.append(low)
    return out


def _search_local_db(workflow, name: str) -> list[dict]:
    if not workflow:
        return []
    hits = []
    seen = set()
    try:
        items = workflow.inbox_items()
    except Exception:
        return []
    for item in items:
        blob = ' '.join(str(item.get(k) or '') for k in ('title', 'description', 'course'))
        if name not in blob:
            continue
        for addr in _deobfuscate_emails(blob):
            if addr not in seen and not GENERIC_EMAILS.match(addr):
                seen.add(addr)
                hits.append({'email': addr, 'source': '本機學業紀錄（' + str(item.get('source') or 'local') + '）'})
    return hits


def _search_gmail(config: dict, name: str, *, timeout: int = 6) -> list[dict]:
    mailbox = str(config.get('mailbox') or '').strip()
    py = str(config.get('mail_python') or '').strip()
    if not mailbox or not py or 'path/to' in mailbox or not Path(mailbox).is_file() or not Path(py).is_file():
        return []
    code = (
        "import importlib.util, json, re, sys\n"
        "spec=importlib.util.spec_from_file_location('mb', sys.argv[1])\n"
        "mb=importlib.util.module_from_spec(spec); spec.loader.exec_module(mb)\n"
        "name=sys.argv[2]; out=[]\n"
        "with mb.gmail() as (c,_):\n"
        "  mb.imap_result(c.select(mb.folder(c,'All'), readonly=True))\n"
        "  ids=mb.search(c, 'X-GM-RAW', f'\"{name}\"')[-15:]\n"
        "  if ids:\n"
        "    for r in mb.gmail_fetch(c, ids, 'BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT)]'):\n"
        "      d=mb.describe_gmail(r, 4000)\n"
        "      txt=f\"{d.get('from','')} {d.get('to','')} {d.get('subject','')}\"\n"
        "      for e in re.findall(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}', txt):\n"
        "        out.append({'email': e.lower(), 'source': 'Gmail 往來紀錄'})\n"
        "print(json.dumps(out, ensure_ascii=False))\n"
    )
    try:
        from brief import cli_env
        done = subprocess.run(
            [py, '-X', 'utf8', '-c', code, mailbox, name],
            capture_output=True,
            timeout=timeout,
            env=cli_env(),
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        if done.returncode != 0:
            return []
        rows = json.loads(done.stdout.decode('utf-8', 'replace').strip().splitlines()[-1])
        seen, out = set(), []
        for r in rows:
            addr = (r.get('email') or '').lower()
            if addr and addr not in seen and not GENERIC_EMAILS.match(addr):
                seen.add(addr)
                out.append(r)
        return out
    except Exception:
        return []


def _default_fetch(url: str, timeout: int = 5) -> str:
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36'},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
        return resp.read().decode('utf-8', 'ignore')


def _extract_ddg_urls(ddg_html: str) -> list[str]:
    urls = []
    for raw in re.findall(r'uddg=([^&"\']+)', ddg_html):
        u = urllib.parse.unquote(raw)
        if u.startswith(('http://', 'https://')) and u not in urls:
            urls.append(u)
    for raw in re.findall(r'href="(https?://[^"]+fju\.edu\.tw[^"]*)"', ddg_html, re.I):
        u = html.unescape(raw)
        if u not in urls:
            urls.append(u)
    return urls


def _find_name_matches(text: str, name: str) -> tuple[str, list[re.Match]]:
    exact = list(re.finditer(re.escape(name), text))
    if exact:
        return name, exact
    # Allow 1-character typo/homophone for 3-character Chinese names (same surname + 1 other char)
    if len(name) == 3 and re.fullmatch(r'[\u4e00-\u9fff]{3}', name):
        pat = re.compile(re.escape(name[0]) + r'(?:' + re.escape(name[1]) + r'[\u4e00-\u9fff]|[\u4e00-\u9fff]' + re.escape(name[2]) + r')')
        fuzzy = list(pat.finditer(text))
        if fuzzy:
            return fuzzy[0].group(0), fuzzy
    return '', []


def _search_web_fju(name: str, org: str = '', *, fetch=None, budget_seconds: float = 8.0) -> tuple[list[dict], list[dict]]:
    fetch = fetch or _default_fetch
    deadline = time.monotonic() + budget_seconds
    dept = re.sub(r'^(?:輔仁大學|輔大)', '', org or '').strip()
    queries = [f'site:fju.edu.tw {name} {dept}'.strip(), f'site:fju.edu.tw {name}']
    if dept and len(name) == 3:
        queries.append(f'site:fju.edu.tw {dept} {name[0]} {name[2]} email')
    candidate_urls: list[str] = []
    for q in queries:
        if time.monotonic() >= deadline or len(candidate_urls) >= 4:
            break
        try:
            ddg = fetch('https://html.duckduckgo.com/html/?q=' + urllib.parse.quote(q), timeout=4)
            for u in _extract_ddg_urls(ddg):
                if 'fju.edu.tw' in u.lower() and u not in candidate_urls:
                    candidate_urls.append(u)
        except Exception:
            continue
    hits = []
    pages = []
    seen = set()
    for url in candidate_urls[:4]:
        if time.monotonic() >= deadline:
            break
        try:
            page = fetch(url, timeout=4)
        except Exception:
            continue
        plain = re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', page)))
        matched_name, plain_matches = _find_name_matches(plain, name)
        if not matched_name:
            continue
        nearby = []
        snippets = []
        for match in plain_matches:
            window = plain[max(0, match.start() - 120):min(len(plain), match.end() + 400)]
            snippets.append(window)
            for addr in _deobfuscate_emails(window):
                if addr not in nearby and not GENERIC_EMAILS.match(addr):
                    nearby.append(addr)
        # Also inspect raw HTML around matched name for mailto: links hidden in attributes
        for match in re.finditer(re.escape(matched_name), page):
            raw_win = page[max(0, match.start() - 400):min(len(page), match.end() + 1200)]
            for addr in _deobfuscate_emails(raw_win):
                if addr not in nearby and not GENERIC_EMAILS.match(addr):
                    nearby.append(addr)
        chosen = nearby or [a for a in _deobfuscate_emails(page) if a.endswith('fju.edu.tw') and not GENERIC_EMAILS.match(a)]
        for addr in chosen[:2]:
            if addr not in seen:
                seen.add(addr)
                hits.append({'email': addr, 'source': url, 'matched_name': matched_name})
        ext = re.search(r'(?:分機|電話|TEL|Ext)[^0-9]{0,8}(\(?0?2\)?[-\s]?2905[-\s]?\d{4}|\d{4})', snippets[0] if snippets else '', re.I)
        office = re.search(r'(?:研究室|辦公室)[^A-Za-z0-9\u4e00-\u9fff]{0,4}([A-Za-z]{1,3}\s?\d{3,4}|[\u4e00-\u9fff]{2,6}\s?[A-Za-z]{1,3}\d{3,4})', snippets[0] if snippets else '')
        pages.append({'url': url, 'matched_name': matched_name, 'ext': ext.group(1) if ext else '', 'office': office.group(1) if office else ''})
        if hits:
            break
    return hits, pages


def lookup(config: dict, workflow, text: str, *, fetch=None, skip_gmail: bool = False) -> dict | None:
    parsed = extract_recipient_query(text)
    if not parsed:
        return None
    cache_key = parsed['name'] + '|' + parsed['org']
    cached = _CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < 600 and fetch is None:
        return cached[1]
    name, org = parsed['name'], parsed['org']
    hits = _search_local_db(workflow, name)
    pages: list[dict] = []
    if not hits and not skip_gmail:
        hits = _search_gmail(config, name)
    if not hits:
        hits, pages = _search_web_fju(name, org, fetch=fetch)
    result = {'name': name, 'org': org, 'matches': hits, 'pages': pages}
    if fetch is None:
        _CACHE[cache_key] = (time.monotonic(), result)
    return result


def lookup_context(config: dict, workflow, text: str, *, fetch=None, skip_gmail: bool = False) -> str:
    res = lookup(config, workflow, text, fetch=fetch, skip_gmail=skip_gmail)
    if not res:
        return ''
    name = res['name']
    matches = res['matches']
    pages = res.get('pages') or []
    if matches:
        lines = [f'\n【系統已自動查到「{name}」的聯絡 Email（請直接附上讓本人確認，不要再反問本人有沒有 Email）】']
        for m in matches[:3]:
            note = f"（官網姓名為「{m['matched_name']}」，請提醒本人核對）" if m.get('matched_name') and m['matched_name'] != name else ''
            lines.append(f"- {m['email']}{note}（來源：{m['source']}）")
        lines.append('提醒：寄信前仍須先確認信件主旨、內容與收件人，取得本人同意後才可送出。')
        return '\n'.join(lines) + '\n'
    if pages:
        p = pages[0]
        extra = '、'.join(x for x in [f"辦公室 {p['office']}" if p.get('office') else '', f"分機 {p['ext']}" if p.get('ext') else ''] if x)
        return (
            f'\n【系統已查過本機紀錄、Gmail 與輔大官網：找到「{name}」的師資頁（{p["url"]}）'
            + (f'，{extra}' if extra else '')
            + '，但該頁未公開 Email】\n'
            '請直接照實告訴本人：已查過輔大官網師資頁但未公開 Email（可附上查到的分機／辦公室），請本人提供 Email 與信件想寫的內容；不要再說「若沒有可由我查詢」。\n'
        )
    return (
        f'\n【系統已自動查過本機學業紀錄、Gmail 與輔大公開網頁，未查到「{name}」的 Email】\n'
        '請直接照實告知未查到，請本人提供正確 Email 或確認姓名是否有同音異字，並詢問想寫的信件內容；不要再說「若沒有可由我查詢」。\n'
    )
