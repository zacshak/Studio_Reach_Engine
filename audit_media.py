"""Read-only database and active-media consistency audit. Writes only a local report."""
import concurrent.futures
import collections
from datetime import datetime, timezone
import json
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
from Claude_Lead_Discovery_Engine.pipeline import email_state

BASE = 'https://sre-review.meshakzac.workers.dev'
MCP = 'https://sre-db-mcp.meshakzac.workers.dev/mcp'
ACTIVE = {'Pending', 'Writing', 'Drafted', 'Scheduled', 'Sending'}


def request(url, data=None, head=False):
    for attempt in range(3):
        req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
            method='HEAD' if head else ('POST' if data else 'GET'),
            headers={'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream', 'User-Agent': 'SRE-media-audit'})
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                if head:
                    return {'bytes': int(response.headers.get('Content-Length', '-1'))}
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024: raise ValueError('response exceeds audit memory budget')
                return json.loads(raw)
        except urllib.error.HTTPError as error:
            if error.code < 500 and error.code != 429 or attempt == 2: raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2: raise
        time.sleep(attempt + 1)


def rows():
    offset = 0
    while True:
        response = request(MCP, {'jsonrpc':'2.0', 'id':1, 'method':'tools/call', 'params':{'name':'query', 'arguments':{
            'sql':'SELECT appid, game_name, Mail_status, scrape_status, emails, Require_Socials FROM scrape_tracker ORDER BY appid',
            'page_size':500, 'offset':offset}}})['result']
        if response.get('isError'): raise RuntimeError(response['content'][0]['text'])
        page = json.loads(response['content'][0]['text'])
        yield from (dict(zip(page['columns'], row)) for row in page['rows'])
        if not page['has_more']: break
        offset = page['next_offset']


def inspect(row, index):
    issues = []
    appid = row['appid']
    if row['Mail_status'] in {'Writing','Drafted','Scheduled','Sending'}:
        if row['scrape_status'] not in {'seeded','scraped'}: issues.append('outreach_with_ineligible_scrape_status')
        if email_state(row['emails']) != 'valid': issues.append('outreach_without_syntactically_valid_email')
    if row['Mail_status'] not in ACTIVE:
        return issues
    folder = index.get(str(appid))
    if not folder: return issues + ['missing_media_index']
    prefix = BASE + '/media/' + urllib.parse.quote(folder, safe='') + '/'
    try:
        manifest = request(prefix + 'manifest.json')
        images = manifest.get('images')
        if not isinstance(images, list): return issues + ['invalid_images_manifest']
        if 'SpriteSheet_Screenshots.png' not in images: issues.append('sprite_not_in_manifest')
        if row['Mail_status'] in {'Drafted','Scheduled','Sending'} and not (manifest.get('mail') or '').strip():
            issues.append('missing_draft_content')
        for name in images:
            if not isinstance(name,str) or '/' in name or '\\' in name:
                issues.append('invalid_image_name'); continue
            try:
                info = request(prefix + urllib.parse.quote(name,safe=''), head=True)
                if info['bytes'] == 0: issues.append('empty_image:' + name)
            except urllib.error.HTTPError as error:
                issues.append(('missing_image:' if error.code == 404 else 'image_check_error:') + name)
    except urllib.error.HTTPError as error:
        issues.append('missing_manifest' if error.code == 404 else f'media_check_http_{error.code}')
    except Exception as error:
        issues.append('check_error:' + type(error).__name__)
    return issues


def main():
    records = list(rows())
    index = request(BASE + '/media/index.json')
    findings = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(inspect,row,index): row for row in records}
        for number, future in enumerate(concurrent.futures.as_completed(futures),1):
            row = futures[future]
            issues = future.result()
            if issues: findings.append({'appid':row['appid'], 'game_name':row['game_name'], 'mail_status':row['Mail_status'], 'issues':issues})
            if number % 100 == 0: print(f'Audited {number}/{len(records)} rows', flush=True)
    report = {'completed_at':datetime.now(timezone.utc).isoformat(), 'total_rows':len(records), 'states':dict(collections.Counter(r['Mail_status'] for r in records)),
        'active_rows':sum(r['Mail_status'] in ACTIVE for r in records),
        'issue_counts':dict(collections.Counter(issue.split(':')[0] for f in findings for issue in f['issues'])),
        'findings':sorted(findings,key=lambda f:f['appid'])}
    path = pathlib.Path('out/media_audit.json'); path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='findings'}), flush=True)
    print('Report:',path.resolve(),flush=True)


if __name__ == '__main__': main()
