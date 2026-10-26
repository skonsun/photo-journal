import os, json, time, hashlib, hmac, secrets, threading, mimetypes, io, tempfile, queue
from datetime import datetime, timezone, timedelta
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from PIL import Image, ImageOps
APP_DIR = Path(__file__).resolve().parent
ROOT = Path(os.environ.get('PHOTO_ROOT', '/photos')).resolve()
DATA = Path(os.environ.get('DATA_ROOT', '/data'))
DATA.mkdir(parents=True, exist_ok=True)
CACHE = DATA / 'cache'
CACHE.mkdir(exist_ok=True)
DB = DATA / 'galleries.json'
SECRET = os.environ.get('ADMIN_PASSWORD', '')
if len(SECRET) < 12 or SECRET.startswith('CHANGE_ME'):
    raise SystemExit('Set ADMIN_PASSWORD to a unique password of at least 12 characters.')
PORT = int(os.environ.get('PORT', '8789'))
SCAN_INTERVAL = max(5, float(os.environ.get('SCAN_INTERVAL', '15')))
ALLOW_ORIGINAL = os.environ.get('ALLOW_ORIGINAL', 'true').lower() in ('true', '1', 'yes')
EXT = {'.jpg', '.jpeg', '.png', '.webp', '.avif', '.heic', '.heif', '.tif', '.tiff', '.bmp'}
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass
lock = threading.RLock()
catalog = {}
scan_times = {}
date_cache = {}
try:
    date_cache = json.loads((DATA / 'dates.json').read_text())
except (FileNotFoundError, ValueError):
    pass
warm_queue = queue.Queue(maxsize=256)
warm_pending = set()
image_slots = threading.BoundedSemaphore(2)
PHOTO_TZ = timezone(timedelta(hours=float(os.environ.get('PHOTO_UTC_OFFSET_HOURS', '8'))))

def capture_date(p, version):
    key = str(p) + ':' + version
    if key in date_cache:
        return date_cache[key]
    stamp = p.stat().st_mtime
    label = ''
    source = 'modified'
    try:
        with Image.open(p) as im:
            exif = im.getexif()
            try:
                details = exif.get_ifd(34665)
            except Exception:
                details = {}
            value = details.get(36867) or exif.get(36867) or details.get(36868) or exif.get(36868) or exif.get(306)
            if isinstance(value, bytes):
                value = value.decode(errors='replace')
            if value:
                dt = datetime.strptime(str(value).strip().strip('\x00')[:19], '%Y:%m:%d %H:%M:%S')
                zone = details.get(36881) or exif.get(36881)
                tz = PHOTO_TZ
                if zone and len(str(zone)) == 6:
                    z = str(zone)
                    delta = timedelta(hours=int(z[1:3]), minutes=int(z[4:6]))
                    tz = timezone(delta if z[0] == '+' else -delta)
                stamp = dt.replace(tzinfo=tz).timestamp()
                label = dt.strftime('%Y-%m-%d %H:%M:%S')
                source = 'exif'
    except Exception:
        pass
    if not label:
        label = datetime.fromtimestamp(stamp, PHOTO_TZ).strftime('%Y-%m-%d %H:%M:%S')
    result = (stamp, label, source)
    date_cache[key] = result
    if len(date_cache) > 100000:
        date_cache.clear()
    return result

def read_db():
    with lock:
        try:
            return json.loads(DB.read_text())
        except FileNotFoundError:
            return {}

def write_db(db):
    with lock:
        tmp = DATA / 'galleries.tmp'
        tmp.write_text(json.dumps(db, ensure_ascii=False))
        os.replace(tmp, DB)

def safe_path(rel):
    p = (ROOT / rel).resolve()
    if not p.is_relative_to(ROOT):
        raise ValueError('目录超出照片库范围')
    return p

def _scan_index(gid, force=False):
    db = read_db()
    if gid not in db:
        return None
    g = db[gid]
    if not force and time.time() - scan_times.get(gid, 0) < 12:
        return catalog.get(gid, [])
    base = safe_path(g['folder'])
    result = []
    if base.is_dir():
        for parent, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = sorted((d for d in dirs if not d.startswith('.') and (not (Path(parent) / d).is_symlink())))
            for name in files:
                p = Path(parent) / name
                if p.suffix.lower() not in EXT or p.is_symlink() or name.startswith('.'):
                    continue
                try:
                    st = p.stat()
                    rel = str(p.relative_to(ROOT))
                    key = hashlib.sha256(rel.encode()).hexdigest()[:24]
                    version = hashlib.sha256(f'{st.st_mtime_ns}:{st.st_size}'.encode()).hexdigest()[:12]
                    taken, label, source = capture_date(p, version)
                    result.append({'id': key, 'name': name, 'path': rel, 'v': version, 'mtime': st.st_mtime, 'taken': taken, 'date': label, 'dateSource': source})
                except (OSError, ValueError):
                    continue
    result.sort(key=lambda x: (x['name'].lower(), x['path']))
    if g.get('sort', 'taken') == 'taken':
        result.sort(key=lambda x: (x['taken'], x['name'].lower(), x['path']))
    elif g.get('sort') == 'newest':
        result.sort(key=lambda x: x['mtime'], reverse=True)
    for item in result:
        cache = CACHE / f"{item['id']}-{item['v']}-480.jpg"
        jobkey = (item['id'], item['v'])
        if not cache.exists() and jobkey not in warm_pending:
            try:
                warm_pending.add(jobkey)
                warm_queue.put_nowait(item.copy())
            except queue.Full:
                warm_pending.discard(jobkey)
                break
    with lock:
        catalog[gid] = result
        scan_times[gid] = time.time()
        try:
            temp = DATA / 'dates.tmp'
            temp.write_text(json.dumps(date_cache))
            os.replace(temp, DATA / 'dates.json')
        except OSError:
            pass
        try:
            temp = INDEX_DIR / (gid + '.tmp')
            temp.write_text(json.dumps(result, ensure_ascii=False))
            os.replace(temp, INDEX_DIR / (gid + '.json'))
        except OSError:
            pass
    return result
INDEX_DIR = DATA / 'index'
INDEX_DIR.mkdir(exist_ok=True)
for _gid in read_db():
    try:
        catalog[_gid] = json.loads((INDEX_DIR / (_gid + '.json')).read_text())
    except (FileNotFoundError, ValueError):
        pass
scan_inflight = set()

def scan(gid, force=False):
    if gid not in read_db():
        return None
    if force:
        with lock:
            if gid in scan_inflight:
                return catalog.get(gid, [])
            scan_inflight.add(gid)
        try:
            return _scan_index(gid, True)
        finally:
            with lock:
                scan_inflight.discard(gid)
    if gid not in catalog and gid not in scan_inflight:
        threading.Thread(target=scan, args=(gid, True), daemon=True).start()
    return catalog.get(gid, [])

def scanner():
    while True:
        for gid in read_db():
            try:
                scan(gid, True)
            except Exception:
                pass
        time.sleep(SCAN_INTERVAL)
threading.Thread(target=scanner, daemon=True).start()

def public_info(gid, offset=0, limit=24, version_only=False):
    g = read_db().get(gid)
    if not g:
        return None
    items = scan(gid) or []
    clean = [{k: x[k] for k in ('id', 'name', 'v', 'date', 'dateSource')} for x in items]
    version = hashlib.sha256(json.dumps([g, clean, gid not in catalog], sort_keys=True).encode()).hexdigest()[:16]
    if version_only:
        return dict(version=version, count=len(items), indexing=gid not in catalog)
    cover_item = next((p for p in clean if p['id'] == g.get('cover')), clean[0] if clean else None)
    page = clean[offset:offset + limit]
    return dict(indexing=gid not in catalog, title=g['title'], subtitle=g.get('subtitle', ''), cover=g.get('cover', ''), coverPhoto=cover_item, allowOriginal=ALLOW_ORIGINAL, count=len(items), photos=page, version=version, offset=offset, nextOffset=offset + len(page), hasMore=offset + len(page) < len(items))

def _make_image(p, size):
    with Image.open(p) as original:
        im = ImageOps.exif_transpose(original)
        im.thumbnail((size, size))
        if im.mode not in ('RGB', 'L'):
            if 'A' in im.getbands():
                bg = Image.new('RGB', im.size, '#ffffff')
                bg.paste(im, mask=im.getchannel('A'))
                im = bg
            else:
                im = im.convert('RGB')
        if im.mode == 'L':
            im = im.convert('RGB')
        out = io.BytesIO()
        im.save(out, 'JPEG', quality=86, optimize=True)
        return out.getvalue()

def make_image(p, size):
    with image_slots:
        return _make_image(p, size)

def cached_image(item, size):
    cache = CACHE / f"{item['id']}-{item['v']}-{size}.jpg"
    if cache.exists():
        return cache.read_bytes()
    payload = make_image(safe_path(item['path']), size)
    fd, tmp = tempfile.mkstemp(dir=CACHE)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(payload)
        os.replace(tmp, cache)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return payload

def warm_worker():
    while True:
        item = warm_queue.get()
        try:
            cached_image(item, 480)
        except Exception:
            pass
        finally:
            warm_pending.discard((item['id'], item['v']))
            warm_queue.task_done()
threading.Thread(target=warm_worker, daemon=True).start()
login_attempts = {}

class Handler(BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass

    def reply(self, status, data, ctype='application/json; charset=utf-8', extra=None):
        if not isinstance(data, bytes):
            data = (json.dumps(data, ensure_ascii=False) if ctype.startswith('application/json') else data).encode()
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        if not extra or 'Cache-Control' not in extra:
            self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            if self.command != 'HEAD':
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def auth(self):
        cookies = dict((x.strip().split('=', 1) for x in self.headers.get('Cookie', '').split(';') if '=' in x))
        val = cookies.get('session', '')
        try:
            stamp, sig = val.split('.')
            return int(stamp) > time.time() and hmac.compare_digest(sig, hmac.new(SECRET.encode(), stamp.encode(), hashlib.sha256).hexdigest())
        except (ValueError, TypeError):
            return False

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        query = parse_qs(urlparse(self.path).query)
        try:
            if path == '/health':
                return self.reply(200, {'ok': True})
            if path in ('/', '/admin'):
                return self.reply(200, (APP_DIR / 'admin.html').read_text(), 'text/html; charset=utf-8')
            if path.startswith('/g/') and len(path.strip('/').split('/')) == 2:
                gid = path.strip('/').split('/')[1]
                if gid not in read_db():
                    return self.reply(404, {'error': '分享不存在或已取消'})
                return self.reply(200, (APP_DIR / 'gallery.html').read_text(), 'text/html; charset=utf-8')
            if path == '/api/admin/state':
                if not self.auth():
                    return self.reply(401, {'error': '请先登录'})
                db = read_db()
                return self.reply(200, {'galleries': [dict(id=k, **g, count=len(scan(k) or []), indexing=k not in catalog) for k, g in db.items()]})
            if path == '/api/admin/folders':
                if not self.auth():
                    return self.reply(401, {'error': '请先登录'})
                rel = query.get('path', [''])[0]
                base = safe_path(rel)
                dirs = sorted([{'name': p.name, 'path': str(p.relative_to(ROOT))} for p in base.iterdir() if p.is_dir() and (not p.is_symlink()) and (not p.name.startswith('.'))], key=lambda x: x['name'])
                count = sum((1 for p in base.iterdir() if p.is_file() and p.suffix.lower() in EXT and (not p.is_symlink())))
                return self.reply(200, {'path': rel, 'folders': dirs, 'count': count})
            if path.startswith('/api/gallery/'):
                offset = max(0, int(query.get('offset', ['0'])[0]))
                limit = min(100, max(1, int(query.get('limit', ['24'])[0])))
                info = public_info(path.rsplit('/', 1)[1], offset, limit, query.get('versionOnly', [''])[0] == '1')
                return self.reply(200, info) if info else self.reply(404, {'error': '分享不存在'})
            if path.startswith('/original/'):
                if not ALLOW_ORIGINAL:
                    return self.reply(403, {'error': '此实例未开放原图访问'})
                parts = path.strip('/').split('/')
                if len(parts) != 3:
                    return self.reply(404, {})
                gid, key = parts[1:]
                item = next((x for x in scan(gid) or [] if x['id'] == key), None)
                if not item:
                    return self.reply(404, {})
                p = safe_path(item['path'])
                with p.open('rb') as src:
                    self.send_response(200)
                    self.send_header('Content-Type', mimetypes.guess_type(p.name)[0] or 'application/octet-stream')
                    self.send_header('Content-Length', str(os.fstat(src.fileno()).st_size))
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.send_header('Cache-Control', 'private, max-age=31536000, immutable' if query.get('v', [''])[0] == item['v'] else 'no-store')
                    self.send_header('Content-Disposition', 'inline')
                    self.end_headers()
                    if self.command == 'HEAD':
                        return
                    try:
                        while True:
                            block = src.read(262144)
                            if not block:
                                break
                            self.wfile.write(block)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                return
            if path.startswith('/image/'):
                parts = path.strip('/').split('/')
                if len(parts) != 3:
                    return self.reply(404, {})
                gid, key = parts[1:]
                item = next((x for x in scan(gid) or [] if x['id'] == key), None)
                if not item:
                    return self.reply(404, {})
                size = 2000 if query.get('size', [''])[0] == 'large' else 1280 if query.get('size', [''])[0] == 'cover' else 480
                payload = cached_image(item, size)
                caching = 'private, max-age=31536000, immutable' if query.get('v', [''])[0] == item['v'] else 'no-store'
                return self.reply(200, payload, 'image/jpeg', {'Cache-Control': caching, 'ETag': '"' + item['v'] + '-' + str(size) + '"'})
            return self.reply(404, {'error': '页面不存在'})
        except (ValueError, FileNotFoundError, PermissionError):
            return self.reply(400, {'error': '无法访问该目录或照片'})
        except Exception:
            return self.reply(500, {'error': '照片暂不可用，请稍后重试'})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            origin = self.headers.get('Origin')
            if origin and urlparse(origin).netloc != self.headers.get('Host'):
                return self.reply(403, {'error': '来源校验失败'})
            length = int(self.headers.get('Content-Length', '0'))
            if length > 65536:
                return self.reply(413, {'error': '请求过大'})
            body = json.loads(self.rfile.read(length) or b'{}')
            if path == '/api/admin/login':
                ip = self.client_address[0]
                now = time.time()
                attempts = [t for t in login_attempts.get(ip, []) if now - t < 300]
                if len(attempts) >= 12:
                    return self.reply(429, {'error': '尝试过于频繁，请五分钟后重试'})
                if not hmac.compare_digest(str(body.get('password', '')), SECRET):
                    login_attempts[ip] = attempts + [now]
                    return self.reply(401, {'error': '管理密码不正确'})
                login_attempts.pop(ip, None)
                stamp = str(int(now + 43200))
                sig = hmac.new(SECRET.encode(), stamp.encode(), hashlib.sha256).hexdigest()
                secure = '; Secure' if self.headers.get('X-Forwarded-Proto') == 'https' else ''
                return self.reply(200, {'ok': True}, extra={'Set-Cookie': f'session={stamp}.{sig}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200{secure}'})
            if not self.auth():
                return self.reply(401, {'error': '请先登录'})
            if path == '/api/admin/logout':
                return self.reply(200, {'ok': True}, extra={'Set-Cookie': 'session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'})
            if path == '/api/admin/save':
                gid = body.get('id') or secrets.token_urlsafe(12)
                db = read_db()
                if body.get('id') and gid not in db:
                    return self.reply(404, {'error': '分享不存在'})
                folder = str(body.get('folder', ''))
                p = safe_path(folder)
                if not p.is_dir():
                    return self.reply(400, {'error': '请选择可访问的文件夹'})
                title = str(body.get('title', '')).strip()[:160]
                if not title:
                    return self.reply(400, {'error': '请输入标题'})
                g = {'title': title, 'subtitle': str(body.get('subtitle', ''))[:600], 'folder': folder, 'sort': body.get('sort', 'taken'), 'cover': str(body.get('cover', ''))[:24], 'created': db.get(gid, {}).get('created', int(time.time()))}
                with lock:
                    db[gid] = g
                    write_db(db)
                    scan_times.pop(gid, None)
                threading.Thread(target=scan, args=(gid, True), daemon=True).start()
                return self.reply(200, {'id': gid})
            if path == '/api/admin/delete':
                gid = body.get('id')
                with lock:
                    db = read_db()
                    db.pop(gid, None)
                    write_db(db)
                    catalog.pop(gid, None)
                return self.reply(200, {'ok': True})
            if path == '/api/admin/rescan':
                gid = body.get('id')
                scan(gid, True)
                return self.reply(200, {'ok': True})
            return self.reply(404, {})
        except (ValueError, KeyError, TypeError):
            return self.reply(400, {'error': '输入有误'})
        except Exception:
            return self.reply(500, {'error': '操作失败，请重试'})
if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
