"""Fixed-origin mandatory updates. No disk-based permission or offline override."""
import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import tempfile
import threading
import time
from urllib.parse import quote, urlsplit
import urllib.error
import urllib.request

from app_version import VERSION

REPOSITORY = 'PoriyaVali/UFI-Fix'
RELEASES_URL = 'https://github.com/' + REPOSITORY + '/releases'
API_URL = 'https://api.github.com/repos/' + REPOSITORY + '/releases'
LEASE_SECONDS = 300
RETRY_SECONDS = 10
MAX_METADATA = 2 * 1024 * 1024
MAX_INSTALLER = 256 * 1024 * 1024
DOWNLOAD_HOSTS = frozenset(('github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'))


class UpdateError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'[vV]?(?:0|[1-9][0-9]{0,4})(?:\.(?:0|[1-9][0-9]{0,4})){0,3}', value):
        raise UpdateError('invalid_release', 'Published stable releases must use numeric version tags such as v2.3.0.')
    parts = tuple(int(part) for part in value.lstrip('vV').split('.'))
    if any(part > 65535 for part in parts):
        raise UpdateError('invalid_release', 'Release version is outside the Windows version range.')
    return parts + (0,) * (4 - len(parts))


def valid_download_url(url):
    if not isinstance(url, str) or len(url) > 16384:
        return False
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and parsed.hostname in DOWNLOAD_HOSTS
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and not parsed.fragment)
    except ValueError:
        return False


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, downloads):
        super().__init__()
        self.downloads = downloads

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.downloads or not valid_download_url(newurl):
            raise UpdateError('untrusted_update_url', 'Update connection redirected outside the approved HTTPS origins.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _tls_context():
    if os.name != 'nt':
        return ssl.create_default_context()
    # Enumerate the Windows trust store explicitly, rather than accepting an
    # SSL_CERT_FILE/SSL_CERT_DIR supplied in the launcher's environment.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    for data, encoding, trust in ssl.enum_certificates('ROOT'):
        if encoding == 'x509_asn' and (trust is True or '1.3.6.1.5.5.7.3.1' in trust):
            context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(data))
    return context


def _opener(downloads=False):
    # Explicit TLS certificate/hostname validation and no environment proxy.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                      urllib.request.HTTPSHandler(context=_tls_context()),
                                      _Redirects(downloads))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Repeated JSON property')
        result[key] = value
    return result


def fetch_release_page(page):
    url = API_URL + '?per_page=100&page=' + str(page)
    request = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github+json', 'User-Agent': 'UFI-Fix/' + VERSION,
        'Cache-Control': 'no-cache, no-store', 'Pragma': 'no-cache'})
    try:
        with _opener().open(request, timeout=8) as response:
            if response.status != 200 or response.geturl() != url:
                raise UpdateError('update_check_failed', 'GitHub did not return an authoritative release response.')
            data = response.read(MAX_METADATA + 1)
            if len(data) > MAX_METADATA:
                raise UpdateError('invalid_release', 'Release metadata exceeds its size limit.')
            result = json.loads(data.decode('utf-8'), object_pairs_hook=_unique_object)
            if not isinstance(result, list) or len(result) > 100:
                raise ValueError('Invalid release page')
            return result
    except UpdateError:
        raise
    except (OSError, ValueError, UnicodeError, urllib.error.URLError):
        raise UpdateError('update_check_failed', 'Cannot verify releases online. New operations remain locked; retry the GitHub connection.') from None


def latest_release(fetch=fetch_release_page):
    releases = []
    for page in range(1, 11):
        rows = fetch(page)
        if not isinstance(rows, list) or len(rows) > 100:
            raise UpdateError('invalid_release', 'Invalid GitHub release listing.')
        for row in rows:
            if not isinstance(row, dict) or type(row.get('draft')) is not bool or type(row.get('prerelease')) is not bool:
                raise UpdateError('invalid_release', 'Release publication state is missing.')
            if row['draft'] or row['prerelease']:
                continue
            tag = row.get('tag_name')
            numeric = version_tuple(tag)
            expected_url = RELEASES_URL + '/tag/' + quote(tag, safe='')
            if row.get('html_url') != expected_url or type(row.get('id')) is not int or row['id'] <= 0:
                raise UpdateError('invalid_release', 'Release identity does not match the fixed project.')
            try:
                published = datetime.fromisoformat(row['published_at'].replace('Z', '+00:00'))
                if published.tzinfo is None:
                    raise ValueError('No release timezone')
            except (KeyError, AttributeError, TypeError, ValueError):
                raise UpdateError('invalid_release', 'Stable release is not published.') from None
            releases.append((numeric, row))
        if len(rows) < 100:
            break
    else:
        raise UpdateError('invalid_release', 'Release pagination exceeds its verification limit.')
    if not releases:
        raise UpdateError('no_published_release', 'No stable published release can be verified; new operations remain locked.')
    numeric, release = max(releases, key=lambda item: item[0])
    return numeric, copy.deepcopy(release)


def installer_asset(release):
    tag = release['tag_name']
    plain = tag.lstrip('vV')
    assets = release.get('assets')
    if not isinstance(assets, list):
        raise UpdateError('update_asset_missing', 'The new release has no complete installer asset.')
    names = ('UFI-Fix-v' + plain + '-Setup.exe', 'UFI-Fix-Setup.exe')
    for name in names:
        found = [asset for asset in assets if isinstance(asset, dict) and asset.get('name') == name]
        if not found:
            continue
        if len(found) != 1:
            raise UpdateError('invalid_update_asset', 'Installer asset is ambiguous.')
        asset = found[0]
        expected = 'https://github.com/' + REPOSITORY + '/releases/download/' + quote(tag, safe='') + '/' + name
        if (asset.get('state') != 'uploaded' or asset.get('browser_download_url') != expected
                or type(asset.get('size')) is not int or not 2 < asset['size'] <= MAX_INSTALLER
                or not isinstance(asset.get('digest'), str)
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', asset['digest'])):
            raise UpdateError('invalid_update_asset', 'Installer needs its official HTTPS URL, size and GitHub SHA-256 digest.')
        return copy.deepcopy(asset)
    raise UpdateError('update_asset_missing', 'Publish the complete UFI-Fix installer before users can update.')


class ReleaseGate:
    def __init__(self, current=VERSION, fetch=fetch_release_page, clock=time.monotonic):
        self.current = current
        self.numeric = version_tuple(current)
        self.fetch, self.clock = fetch, clock
        self.lock = threading.RLock()
        self._status = None
        self._release = None
        self._highest = None
        self._deadline = 0
        self._last_tick = None

    def _base(self):
        return {'current_version': self.current, 'latest_version': None,
                'operations_allowed': False, 'update_available': False,
                'code': 'update_check_required', 'message': 'Online release verification is required.',
                'releases_url': RELEASES_URL, 'verification_lease_seconds': LEASE_SECONDS,
                'offline_override': False}

    def peek(self):
        with self.lock:
            now = self.clock()
            if self._status is not None and self._last_tick <= now < self._deadline:
                return copy.deepcopy(self._status)
            result = self._base()
            if self._release:
                result['latest_version'] = self._release['tag_name'].lstrip('vV')
            return result

    def status(self, force=False):
        with self.lock:
            now = self.clock()
            if not force and self._status is not None and self._last_tick <= now < self._deadline:
                return copy.deepcopy(self._status)
            result = self._base()
            try:
                numeric, release = latest_release(self.fetch)
                if self._highest is not None and numeric < self._highest:
                    raise UpdateError('release_rollback', 'Release history moved backwards; verification is locked until the latest release is restored.')
                self._highest, self._release = numeric, release
                newer = numeric > self.numeric
                result.update(latest_version=release['tag_name'].lstrip('vV'),
                              operations_allowed=not newer, update_available=newer,
                              code='update_required' if newer else 'current',
                              message=('A newer UFI-Fix release is required. Update before starting new operations.'
                                       if newer else 'Online release verification passed.'),
                              release_url=release['html_url'])
                lifetime = LEASE_SECONDS
            except UpdateError as error:
                result.update(code=error.code, message=str(error))
                if self._release:
                    result['latest_version'] = self._release['tag_name'].lstrip('vV')
                lifetime = RETRY_SECONDS
            self._last_tick = self.clock()
            self._deadline = self._last_tick + lifetime
            self._status = result
            return copy.deepcopy(result)

    def require_current(self):
        state = self.status()
        if not state['operations_allowed']:
            raise UpdateError(state['code'], state['message'] + ' ' + RELEASES_URL)

    def update_target(self, force=False):
        with self.lock:
            state = self.status(force)
            if state['code'] != 'update_required':
                raise UpdateError(state['code'], state['message'])
            return copy.deepcopy(self._release), installer_asset(self._release)


GATE = ReleaseGate()


def secure_update_directory():
    if os.name != 'nt':
        raise UpdateError('windows_required', 'Installer updates require Windows.')
    import ctypes
    folder = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x26, None, 0, folder) != 0:
        raise UpdateError('update_storage_unavailable', 'Windows Program Files could not be resolved.')
    root = Path(folder.value)
    directory = root / 'UFI-Fix' / 'updates'
    for path in (root / 'UFI-Fix', directory):
        path.mkdir(exist_ok=True)
        if path.is_symlink() or bool(path.stat(follow_symlinks=False).st_file_attributes & 0x400):
            raise UpdateError('update_storage_unavailable', 'Update staging must be a regular protected directory.')
    return directory


def download_installer(gate=GATE, directory=None, opener=None, progress=lambda done, total: None):
    release, asset = gate.update_target(force=True)
    directory = Path(directory) if directory is not None else secure_update_directory()
    directory.mkdir(parents=True, exist_ok=True)
    digest = asset['digest'].split(':', 1)[1]
    # Unique names prevent a stale partial download from becoming an update.
    descriptor, partial = tempfile.mkstemp(prefix='ufi-update-', suffix='.partial', dir=directory)
    partial = Path(partial)
    destination = partial.with_suffix('.exe')
    try:
        request = urllib.request.Request(asset['browser_download_url'], headers={
            'User-Agent': 'UFI-Fix/' + VERSION, 'Accept': 'application/octet-stream', 'Cache-Control': 'no-cache'})
        selected_opener = opener if opener is not None else _opener(downloads=True)
        deadline = time.monotonic() + 180
        checksum, total, signature = hashlib.sha256(), 0, b''
        with os.fdopen(descriptor, 'wb') as output:
            descriptor = None
            with selected_opener.open(request, timeout=10) as response:
                if response.status != 200 or not valid_download_url(response.geturl()):
                    raise UpdateError('update_download_failed', 'Installer download did not use an approved HTTPS response.')
                while True:
                    data = response.read(1024 * 1024)
                    if not data:
                        break
                    if time.monotonic() > deadline:
                        raise UpdateError('update_download_failed', 'Installer download timed out.')
                    total += len(data)
                    if total > asset['size']:
                        raise UpdateError('update_integrity_failed', 'Installer is larger than its verified release asset.')
                    signature = (signature + data)[:2]
                    checksum.update(data)
                    output.write(data)
                    progress(total, asset['size'])
            output.flush()
            os.fsync(output.fileno())
        if total != asset['size'] or checksum.hexdigest() != digest or signature != b'MZ':
            raise UpdateError('update_integrity_failed', 'Installer size, SHA-256 or Windows executable signature did not match the release.')
        refreshed, current_asset = gate.update_target(force=True)
        if (refreshed['id'] != release['id'] or refreshed['tag_name'] != release['tag_name']
                or current_asset['browser_download_url'] != asset['browser_download_url']
                or current_asset['digest'] != asset['digest'] or current_asset['size'] != total):
            raise UpdateError('release_changed', 'A different update was published during download; retry the latest release.')
        os.replace(partial, destination)
        return {'path': str(destination), 'version': release['tag_name'].lstrip('vV'),
                'sha256': digest, 'bytes': total, 'release_id': release['id']}
    except UpdateError:
        raise
    except (OSError, ValueError, urllib.error.URLError):
        raise UpdateError('update_download_failed', 'Cannot download the verified installer; retry the official release connection.') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        partial.unlink(missing_ok=True)
