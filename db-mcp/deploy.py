"""Provision restricted credentials, verify them, deploy, and smoke test.

Run from Windows with the existing authenticated Turso CLI in WSL.
Never prints credentials. Generated credentials.json is gitignored.
"""
import argparse
import json
import pathlib
import secrets
import subprocess
import time
import urllib.request
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parent


def command(args, data=None):
    result = subprocess.run(args, input=data, capture_output=True, text=True, encoding='utf-8', cwd=ROOT)
    if result.returncode:
        # Token mint errors do not include token values. Do not dump subprocess output.
        raise RuntimeError(f"Command failed: {args[0]} {args[1]}. Check CLI login and permissions.")
    return result.stdout.strip()


def database(secret, sql):
    url = secret['TURSO_URL'].replace('libsql://', 'https://').rstrip('/') + '/v2/pipeline'
    req = urllib.request.Request(url, method='POST', headers={
        'Authorization': 'Bearer ' + secret['TURSO_READ_TOKEN'], 'Content-Type': 'application/json'
    }, data=json.dumps({'requests': [{'type': 'execute', 'stmt': {'sql': sql, 'want_rows': True}}, {'type': 'close'}]}).encode())
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)['results'][0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env-file', required=True, type=pathlib.Path)
    parser.add_argument('--database', required=True, help='Steam Turso database name, not the Epic database')
    args = parser.parse_args()
    source = {}
    for line in args.env_file.read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            source[key.strip()] = value.strip().strip('\"\'')
    credential_file = ROOT / 'credentials.json'
    if credential_file.exists():
        secret = json.loads(credential_file.read_text())
        if secret['TURSO_URL'] != source['TURSO_DATABASE_URL']:
            raise RuntimeError('Saved credentials target a different database.')
    else:
        token = command(['wsl', '-e', '/home/meshak/.turso/turso', 'db', 'tokens', 'create', args.database,
                         '--read-only', '-p', 'scrape_tracker:data_read', '-p', 'newly_added:data_read',
                         '-p', 'known_comingsoon:data_read'])
        if len(token.split('.')) != 3:
            raise RuntimeError('Turso did not return a token.')
        secret = {'TURSO_URL': source['TURSO_DATABASE_URL'], 'TURSO_READ_TOKEN': token,
                  'MCP_AUTH_TOKEN': secrets.token_urlsafe(32)}
        credential_file.write_text(json.dumps(secret, indent=2), encoding='utf-8')
    for table in ['scrape_tracker', 'newly_added', 'known_comingsoon']:
        if database(secret, f'SELECT * FROM {table} LIMIT 0')['type'] == 'error':
            raise RuntimeError(f'Read permission failed for {table}')
    if database(secret, 'PRAGMA table_info(scrape_tracker)')['type'] == 'error':
        raise RuntimeError('Schema inspection is denied by Turso. Adjust schema implementation before deploying.')
    for sql in ['SELECT * FROM email_verification_cache LIMIT 0',
                'UPDATE scrape_tracker SET game_name=game_name WHERE 0']:
        result = database(secret, sql)
        if result['type'] != 'error' or not any(word in json.dumps(result).lower() for word in ['auth', 'permission', 'prohibit', 'forbidden']):
            raise RuntimeError('Credential isolation was not proved. Deployment stopped.')
    print('Database read access and denied write/unapproved-table access verified.')
    wrangler = str(ROOT / 'node_modules' / '.bin' / 'wrangler.cmd')
    command([wrangler, 'secret', 'bulk'], json.dumps(secret))
    output = command([wrangler, 'deploy'])
    endpoint = None
    for line in output.splitlines():
        if '.workers.dev' in line:
            endpoint = line.strip() + '/mcp'
    if not endpoint:
        raise RuntimeError('Deployment returned no Worker URL. Inspect Wrangler deployment status.')
    for attempt in range(3):
        try:
            print(command(['node', 'smoke.mjs', endpoint]))
            break
        except RuntimeError:
            if attempt == 2:
                raise
            time.sleep(3)  # Allow the new Worker deployment to propagate.
    (ROOT / 'mcp-client.json').write_text(json.dumps({'mcpServers': {'sre-database': {
        'url': endpoint, 'headers': {'Authorization': 'Bearer ' + secret['MCP_AUTH_TOKEN']}
    }}}, indent=2), encoding='utf-8')
    print(endpoint)
    print('Bearer credential saved locally in db-mcp/credentials.json, never in git.')


if __name__ == '__main__':
    main()
