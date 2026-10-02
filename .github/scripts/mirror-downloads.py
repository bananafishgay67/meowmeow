"""Copy verified public archives into GitHub Releases; never execute them."""
import hashlib, http.client, json, os, pathlib, re, tempfile, time
from http.cookiejar import CookieJar
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse, urljoin
from urllib.request import Request, urlopen, build_opener, HTTPCookieProcessor

REPO = os.environ['GITHUB_REPOSITORY']
TOKEN = os.environ['GH_TOKEN']
API = 'https://api.github.com/repos/' + REPO
TAG = 'satvrn-downloads'
HEADERS = {'Authorization': 'Bearer ' + TOKEN, 'Accept': 'application/vnd.github+json',
           'User-Agent': 'SATVRN-archive-migration', 'X-GitHub-Api-Version': '2022-11-28'}

def api(path, data=None):
    request = Request(API + path, data=json.dumps(data).encode() if data is not None else None,
                      headers={**HEADERS, 'Content-Type': 'application/json'})
    with urlopen(request, timeout=120) as response:
        return json.load(response)

try:
    release = api('/releases/tags/' + TAG)
except HTTPError as error:
    if error.code != 404:
        raise
    release = api('/releases', {'tag_name': TAG, 'target_commitish': 'main',
        'name': 'SATVRN downloads', 'body': 'Verified archives for SATVRN Vault. Categories appear in asset labels.',
        'draft': False, 'prerelease': False, 'make_latest': 'false'})

existing = {}
for page in range(1, 20):
    assets = api(f"/releases/{release['id']}/assets?per_page=100&page={page}")
    existing.update({asset['name']: asset for asset in assets})
    if len(assets) < 100:
        break

class DownloadLink(HTMLParser):
    url = None
    continue_url = None
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and attrs.get('id') == 'downloadButton':
            self.url = attrs.get('href')
        if tag == 'a' and attrs.get('id') == 'continue-btn':
            self.continue_url = attrs.get('href')

public_download = build_opener(HTTPCookieProcessor(CookieJar()))

def download(item, target):
    source = urlparse(item['url'])
    if source.scheme != 'https' or source.hostname != 'www.mediafire.com':
        raise ValueError('Unapproved archive source')
    page_url = item['url']
    for repair_attempt in range(3):
        with public_download.open(Request(page_url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=120) as page:
            parser = DownloadLink()
            parser.feed(page.read().decode('utf-8', errors='replace'))
        if parser.url or not parser.continue_url:
            break
        page_url = urljoin(item['url'], parser.continue_url)
        next_url = urlparse(page_url)
        if next_url.scheme != 'https' or next_url.hostname != 'www.mediafire.com':
            raise ValueError('Unexpected download refresh destination')
        time.sleep(5)
    host = urlparse(parser.url or '')
    if host.scheme != 'https' or not (host.hostname or '').endswith('.mediafire.com'):
        raise HTTPError(item['url'], 503, 'Missing public download link', None, None)
    checksum = hashlib.sha256() if len(item['sha256']) == 64 else hashlib.md5()
    sha256 = hashlib.sha256()
    count = 0
    with public_download.open(parser.url, timeout=120) as response, target.open('wb') as output:
        while chunk := response.read(1024 * 1024):
            count += len(chunk)
            if count > item['size']:
                raise ValueError('Unexpected download size')
            checksum.update(chunk)
            sha256.update(chunk)
            output.write(chunk)
    if count != item['size'] or checksum.hexdigest() != item['sha256']:
        raise ValueError('Archive failed integrity verification')
    return sha256.hexdigest()

def upload(item, name, target, digest):
    parsed = urlparse(release['upload_url'].split('{')[0])
    if parsed.hostname != 'uploads.github.com':
        raise ValueError('Unapproved upload destination')
    connection = http.client.HTTPSConnection(parsed.hostname, timeout=120)
    endpoint = parsed.path + '?' + urlencode({'name': name, 'label': item['rel']})
    connection.putrequest('POST', endpoint)
    for key, value in {**HEADERS, 'Content-Type': 'application/octet-stream',
                       'Content-Length': str(item['size'])}.items():
        connection.putheader(key, value)
    connection.endheaders()
    with target.open('rb') as source:
        while chunk := source.read(1024 * 1024):
            connection.send(chunk)
    response = connection.getresponse()
    result = json.loads(response.read())
    connection.close()
    if response.status != 201 or result.get('size') != item['size'] or result.get('digest') != 'sha256:' + digest:
        raise ValueError('GitHub upload verification failed, status ' + str(response.status))
    return result

items = json.loads(pathlib.Path('.github/mirror-input.json').read_text())
results = []
with tempfile.TemporaryDirectory() as temporary:
    for index, item in enumerate(items, 1):
        if item['size'] >= 2 * 1024 ** 3:
            raise ValueError('Archive exceeds GitHub Release asset limit')
        basename = pathlib.PurePosixPath(item['rel']).name
        name = hashlib.sha256(item['rel'].encode()).hexdigest()[:12] + '--' + re.sub(r'[^a-zA-Z0-9._-]+', '_', basename)
        old = existing.get(name)
        if old and old.get('state') == 'uploaded' and old['size'] == item['size'] and len(item['sha256']) == 64 and old.get('digest') == 'sha256:' + item['sha256']:
            result = old
        else:
            if old:
                raise ValueError('Existing asset differs; refusing overwrite')
            target = pathlib.Path(temporary) / 'archive.bin'
            for attempt in range(3):
                try:
                    digest = download(item, target)
                    break
                except (HTTPError, TimeoutError) as error:
                    if attempt == 2:
                        raise
                    time.sleep(5)
            result = upload(item, name, target, digest)
            target.unlink()
            time.sleep(1)
        results.append({'rel': item['rel'], 'size': result['size'], 'sha256': result['digest'].split(':')[1],
                        'url': result['browser_download_url']})
        print(f"Verified {index}/{len(items)}: {item['rel']}", flush=True)
pathlib.Path('mirror-verification.json').write_text(json.dumps(results, indent=2))
print('All requested GitHub Release copies verified.', flush=True)
