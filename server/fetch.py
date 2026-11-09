"""Safe URL downloads (SSRF guard, size/time limits) with Google Drive support.

Drive handling follows the ZooKeeper drive_public_resolver: normalize to uc?export=download,
follow the virus-scan confirm page (uuid / confirm / download_warning cookie), turn Drive's
HTML error pages into readable errors, take the filename from Content-Disposition, and never
send HEAD to Drive (it counts toward the download quota).
"""
import asyncio
import html as htmllib
import ipaddress
import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

import httpx

from upscale_core import Canceled

from .config import get_settings

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv", ".mpeg", ".mpg", ".3gp"}
MAX_REDIRECTS = 8
CHUNK = 1 << 20


class FetchError(Exception):
    """Permanent: bad URL, blocked host, too large, Drive denied. Do not retry."""


class TransientFetchError(Exception):
    """Network hiccup / 5xx / 429. Worth retrying."""


@dataclass
class Fetched:
    path: Path
    size: int
    filename: str | None
    content_type: str | None


# ---------- SSRF guard ----------

def _ip_allowed(ip):
    ip = ipaddress.ip_address(ip)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def check_url(url):
    """Reject non-http(s) URLs and hosts that resolve to private/loopback/link-local addresses."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise FetchError("only http(s) URLs are allowed")
    if u.username or u.password:
        raise FetchError("credentials in URL are not allowed")
    host = u.hostname
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80),
                                                              type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError(f"cannot resolve host {host}") from e
    addrs = {i[4][0] for i in infos}
    if get_settings().allow_private_urls:  # dev/testing only
        return
    if not addrs or not all(_ip_allowed(a) for a in addrs):
        raise FetchError(f"host {host} resolves to a non-public address")


# ---------- Google Drive ----------

_DRIVE_HOSTS = ("drive.google.com", "docs.google.com", "drive.usercontent.google.com")


def drive_file_id(url):
    u = urlparse(url)
    if not u.hostname or not u.hostname.endswith(_DRIVE_HOSTS):
        return None
    m = re.search(r"/file/d/([\w-]{10,})", u.path)
    if m:
        return m.group(1)
    if "/folders/" in u.path:
        return None
    ids = parse_qs(u.query).get("id")
    return ids[0] if ids else None


def drive_folder_id(url):
    u = urlparse(url)
    if not u.hostname or not u.hostname.endswith(_DRIVE_HOSTS):
        return None
    m = re.search(r"/folders/([\w-]{10,})", u.path)
    if m:
        return m.group(1)
    if u.path.startswith("/embeddedfolderview"):
        ids = parse_qs(u.query).get("id")
        return ids[0] if ids else None
    return None


def drive_download_url(file_id):
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def pick_confirm_url(page, base_url, cookie_confirm=None):
    """Find the real download URL on Drive's "can't scan for viruses" page."""
    page = htmllib.unescape(page)
    cands = []
    # <form id="download-form" action="https://drive.usercontent.google.com/download"> + hidden inputs
    for fm in re.finditer(r'<form[^>]*action="([^"]+)"[^>]*>(.*?)</form>', page, re.S | re.I):
        params = dict(re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', fm.group(2), re.I))
        if params.get("id"):
            cands.append(urljoin(base_url, fm.group(1)) + "?" + urlencode(params))
    for href in re.findall(r'href="([^"]+)"', page):
        if "export=download" in href or "confirm=" in href or "/download?" in href:
            cands.append(urljoin(base_url, href))
    if cookie_confirm:
        q = parse_qs(urlparse(base_url).query)
        if q.get("id"):
            cands.append(f"https://drive.google.com/uc?export=download&confirm={cookie_confirm}&id={q['id'][0]}")

    def score(c):
        q = parse_qs(urlparse(c).query)
        return (2 * ("drive.usercontent.google.com" in c) + 2 * ("uuid" in q)
                + ("confirm" in q) + ("id" in q))

    cands = [c for c in dict.fromkeys(cands) if parse_qs(urlparse(c).query).get("id")]
    return max(cands, key=score) if cands else None


def drive_html_error(page, status):
    low = page.lower()
    if "quota" in low or "too many users have viewed or downloaded" in low:
        return "Google Drive download quota exceeded for this file; try again later or copy the file"
    if "accounts.google.com" in low and ("sign in" in low or "servicelogin" in low):
        return "Google Drive file is not public (sign-in required); share it as 'Anyone with the link'"
    if status == 404 or "file does not exist" in low or "not found" in low:
        return "Google Drive file not found"
    if "access denied" in low or "you need access" in low or status == 403:
        return "Google Drive access denied; share it as 'Anyone with the link'"
    return "Google Drive returned an HTML page instead of the file"


def filename_from_cd(cd):
    if not cd:
        return None
    m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", cd, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
    return m.group(1).strip() if m else None


async def list_drive_folder(client, folder_id, max_depth=4, max_folders=250, concurrency=4):
    """List video files in a public Drive folder (recursive). Returns [{id, name, url}]."""
    seen, files = set(), []
    sem = asyncio.Semaphore(concurrency)

    async def walk(fid, depth):
        if fid in seen or len(seen) >= max_folders:
            return
        seen.add(fid)
        async with sem:
            r = await client.get(f"https://drive.google.com/embeddedfolderview?id={fid}", headers=HEADERS,
                                 follow_redirects=True)
        if r.status_code != 200 or "flip-entry" not in r.text:
            if depth == 0:
                raise FetchError(drive_html_error(r.text, r.status_code) if r.status_code != 200
                                 else "Google Drive folder is empty or not public")
            return
        subs = []
        for block in r.text.split('class="flip-entry"')[1:]:
            m_id = re.search(r'id="entry-([\w-]+)"', block)
            m_href = re.search(r'href="([^"]+)"', block)
            m_title = re.search(r'class="flip-entry-title">([^<]*)<', block)
            if not (m_id and m_href):
                continue
            href = htmllib.unescape(m_href.group(1))
            name = htmllib.unescape(m_title.group(1)) if m_title else m_id.group(1)
            if "/folders/" in href:
                subs.append(m_id.group(1))
            elif "/file/d/" in href and Path(name).suffix.lower() in VIDEO_EXT:
                files.append({"id": m_id.group(1), "name": name,
                              "url": f"https://drive.google.com/file/d/{m_id.group(1)}/view"})
        if depth < max_depth:
            await asyncio.gather(*(walk(s, depth + 1) for s in subs))

    await walk(folder_id, 0)
    return files


# ---------- download ----------

async def _open(client, url, stack):
    """GET with manual redirects so every hop passes the SSRF check."""
    for _ in range(MAX_REDIRECTS):
        await check_url(url)
        req = client.build_request("GET", url, headers=HEADERS)
        resp = await client.send(req, stream=True)
        if resp.is_redirect:
            loc = resp.headers.get("location")
            await resp.aclose()
            if not loc:
                raise FetchError("redirect without location")
            url = urljoin(str(resp.url), loc)
            continue
        stack.append(resp)
        return resp
    raise FetchError("too many redirects")


def _raise_for_status(resp):
    s = resp.status_code
    if s == 429 or s >= 500:
        raise TransientFetchError(f"source returned HTTP {s}")
    if s >= 400:
        raise FetchError(f"source returned HTTP {s}")


async def _stream_to(resp, dest, max_bytes, cancel, deadline):
    cl = resp.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > max_bytes:
        raise FetchError(f"input is larger than {max_bytes // 2**20} MB")
    size = 0
    with open(dest, "wb") as f:
        async for chunk in resp.aiter_bytes(CHUNK):
            size += len(chunk)
            if size > max_bytes:
                raise FetchError(f"input is larger than {max_bytes // 2**20} MB")
            if cancel is not None and cancel.is_set():
                raise Canceled()
            if time.monotonic() > deadline:
                raise TransientFetchError("download timed out")
            f.write(chunk)
    return size


async def download(url, dest, max_bytes, timeout=300, cancel=None, client=None):
    """Download url (plain or Google Drive file link) to dest. Returns Fetched."""
    if drive_folder_id(url):
        raise FetchError("this is a Google Drive folder; submit it to /v1/batches")
    fid = drive_file_id(url)
    if fid:
        url = drive_download_url(fid)
    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(30, read=60), follow_redirects=False)
    deadline = time.monotonic() + timeout
    stack = []
    try:
        resp = await _open(client, url, stack)
        if fid and "text/html" in resp.headers.get("content-type", ""):
            page = (await resp.aread())[:2_000_000].decode("utf-8", "replace")
            nxt = pick_confirm_url(page, str(resp.url), client.cookies.get("download_warning")
                                   or next((v for k, v in client.cookies.items() if k.startswith("download_warning")), None))
            if not nxt:
                raise FetchError(drive_html_error(page, resp.status_code))
            resp = await _open(client, nxt, stack)
            if "text/html" in resp.headers.get("content-type", ""):
                page = (await resp.aread())[:2_000_000].decode("utf-8", "replace")
                raise FetchError(drive_html_error(page, resp.status_code))
        _raise_for_status(resp)
        size = await _stream_to(resp, dest, max_bytes, cancel, deadline)
        name = filename_from_cd(resp.headers.get("content-disposition")) or Path(urlparse(str(resp.url)).path).name
        return Fetched(Path(dest), size, name or None, resp.headers.get("content-type"))
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
        raise TransientFetchError(f"download failed: {e.__class__.__name__}") from e
    finally:
        for r in stack:
            await r.aclose()
        if own:
            await client.aclose()
