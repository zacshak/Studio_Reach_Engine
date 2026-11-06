"""Read-only live smoke test. MCP/media deliberately use urllib's default User-Agent."""
import base64
import json
import urllib.parse
import urllib.request

MCP = 'https://sre-mcp.zacgamedev.com/mcp'
REVIEW = 'https://sre-review.zacgamedev.com'
LEGACY = 'https://sre-review.meshakzac.workers.dev'


def fetch(url, body=None, browser=False):
    headers = {'Accept': 'application/json, text/event-stream'}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    if browser:
        headers['User-Agent'] = 'Mozilla/5.0 SRE-Dashboard-Smoke/1.0'
    with urllib.request.urlopen(urllib.request.Request(
            url, data=json.dumps(body).encode() if body is not None else None,
            headers=headers), timeout=40) as response:
        return response.read()


def rpc(method, params):
    data = json.loads(fetch(MCP, {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}))
    assert 'error' not in data, data
    result = data['result']
    assert not result.get('isError'), result
    return result


def main():
    for url in (REVIEW, LEGACY):
        assert b'Studio Reach Engine' in fetch(url, browser=True)
        print(f'PASS dashboard HTML: {url}')
    state = json.loads(fetch(REVIEW + '/api/state', browser=True))
    old = json.loads(fetch(LEGACY + '/api/state', browser=True))
    assert set(state) == set(old)
    tools = rpc('tools/list', {})['tools']
    assert {'get_game_media', 'get_game_image', 'query'} <= {t['name'] for t in tools}
    rpc('tools/call', {'name': 'list_tables', 'arguments': {}})
    print(f'PASS default Python MCP discovery ({len(tools)} tools) and list_tables')
    ids = state['approval'] + state['triage'] + state['nomail']
    assert ids, 'No reviewable rows available for the media test'
    appid = ids[0]
    result = rpc('tools/call', {'name': 'get_game_media', 'arguments': {'appid': appid}})
    content = result['content']
    image = next(c for c in content if c['type'] == 'image')
    image_bytes = base64.b64decode(image['data'], validate=True)
    assert image_bytes.startswith(b'\x89PNG\r\n\x1a\n')
    folder = urllib.parse.quote(state['index'][str(appid)], safe='')
    direct = fetch(REVIEW + f'/media/{folder}/SpriteSheet_Screenshots.png')
    assert direct == image_bytes, 'MCP and direct media bytes differ'
    print(f'PASS default Python MCP image and identical direct media: appid {appid}')


if __name__ == '__main__':
    main()
