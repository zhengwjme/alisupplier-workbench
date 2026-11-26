#!/usr/bin/env python3
"""AliSupplier IM -> local SQLite -> local web UI.

Passive capture: tails the application's own app.log and parses message
records. No injection, no app restart, nothing to install beyond Python.
"""
import argparse
import hashlib
import hmac
import html as htmlmod
import json
import os
from contextlib import contextmanager
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import queue
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from ai_gateway import AIError, PROTOCOLS, request_model
from analytics import compute_analytics, customer_products, export_customers_csv
from workbench import Workbench, parse_summary
from workbench_media import card_payload

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(HERE, 'ui')


def now():
    return int(time.time())


def app_support_dir():
    override = os.environ.get('HOOK_APP_SUPPORT_DIR')
    if override:
        return override
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Application Support/AliSupplier')
    base = os.environ.get('APPDATA') or os.path.expanduser('~')
    return os.path.join(base, 'AliSupplier')


def log_candidates():
    d = app_support_dir()
    cands = [os.path.join(d, 'app.log')]
    if os.name == 'nt':
        cands.append(os.path.join(os.environ.get('LOCALAPPDATA') or d, 'AliSupplier', 'app.log'))
        cands.append(os.path.join(os.path.expanduser('~'), 'AppData', 'Roaming', 'AliSupplier', 'app.log'))
    else:
        cands.append(os.path.expanduser('~/.AliSupplier/app.log'))
    return cands


def find_log(explicit=None):
    if explicit and os.path.isfile(explicit):
        return explicit
    env = os.environ.get('HOOK_LOG')
    if env and os.path.isfile(env):
        return env
    for c in log_candidates():
        if os.path.isfile(c):
            return c
    return None


def default_db():
    if sys.platform == 'darwin':
        base = os.path.expanduser('~/Library/Application Support/AliSupplierHook')
    elif os.name == 'nt':
        base = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'AliSupplierHook')
    else:
        base = os.path.expanduser('~/.local/share/AliSupplierHook')
    return os.path.join(base, 'messages.db')


# ---------------------------------------------------------------- storage
SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  cid TEXT,
  mid TEXT,
  direction TEXT,
  sender TEXT,
  receiver TEXT,
  content TEXT,
  content_type TEXT,
  msg_time INTEGER,
  captured_at INTEGER,
  source TEXT,
  table_name TEXT,
  raw TEXT,
  kind TEXT,
  media TEXT,
  payload TEXT,
  client_id TEXT,
  UNIQUE(user_id, mid)
);
CREATE INDEX IF NOT EXISTS ix_msg_user_cid ON messages(user_id, cid, msg_time);
CREATE INDEX IF NOT EXISTS ix_msg_time ON messages(msg_time);
CREATE TABLE IF NOT EXISTS raw_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER, kind TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS customers(
  user_id TEXT NOT NULL,
  cid TEXT NOT NULL,
  uid TEXT,
  display_name TEXT,
  avatar_url TEXT,
  extra TEXT,
  updated_at INTEGER,
  PRIMARY KEY(user_id, cid)
);
"""


class Store(object):
    def __init__(self, path):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.path = path
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.executescript(SCHEMA)
        have = {r[1] for r in self.db.execute('PRAGMA table_info(messages)').fetchall()}
        for col in ('kind', 'media', 'payload', 'client_id'):
            if col not in have:
                self.db.execute('ALTER TABLE messages ADD COLUMN %s TEXT' % col)
        if 'wb_passive' not in have:
            self.db.execute('ALTER TABLE messages ADD COLUMN wb_passive INTEGER NOT NULL DEFAULT 0')
        have_cust = {r[1] for r in self.db.execute('PRAGMA table_info(customers)').fetchall()}
        for col in ('stage', 'tags', 'note'):
            if col not in have_cust:
                self.db.execute('ALTER TABLE customers ADD COLUMN %s TEXT' % col)
        self.db.commit()

    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql, args=()):
        with self.lock, self.db:
            self.db.execute(sql, args)

    def add_message(self, m, passive=False):
        cols = ['user_id', 'cid', 'mid', 'direction', 'sender', 'receiver', 'content',
                'content_type', 'msg_time', 'captured_at', 'source', 'table_name', 'raw',
                'kind', 'media', 'payload', 'client_id', 'wb_passive']
        sql = 'INSERT OR IGNORE INTO messages(%s) VALUES(%s)' % (','.join(cols), ','.join('?' * len(cols)))
        with self.lock, self.db:
            cur = self.db.execute(sql, [m.get(c) for c in cols[:-1]] + [int(passive)])
            if cur.rowcount:
                return True
            row = self.db.execute('SELECT * FROM messages WHERE user_id=? AND mid=?',
                                  (m.get('user_id'), m.get('mid'))).fetchone()
            if not row or (row['cid'] and m.get('cid') and row['cid'] != m['cid']):
                return False
            updates = {c: m[c] for c in ('cid', 'media', 'client_id', 'kind', 'payload')
                       if m.get(c) and not row[c]}
            if m.get('kind') == 'card' and m.get('payload') and row['payload'] != m['payload']:
                updates.update({c: m[c] for c in ('kind', 'content_type', 'payload', 'content', 'raw') if m.get(c)})
                if m.get('media'):
                    updates['media'] = m['media']
            elif is_placeholder(row['content']) and not is_placeholder(m.get('content')):
                updates.update({c: m[c] for c in ('content', 'content_type', 'sender', 'receiver',
                                                  'raw', 'msg_time', 'kind') if m.get(c)})
            updates = {c: value for c, value in updates.items() if row[c] != value}
            if updates:
                sets = ', '.join(c + '=?' for c in updates)
                self.db.execute('UPDATE messages SET ' + sets + ' WHERE id=?', list(updates.values()) + [row['id']])
            return False

    def add_raw(self, kind, payload):
        with self.lock, self.db:
            self.db.execute('INSERT INTO raw_events(ts,kind,payload) VALUES(?,?,?)', (now(), kind, payload[:4000]))
            self.db.execute('DELETE FROM raw_events WHERE id <= (SELECT MAX(id)-3000 FROM raw_events)')

    def upsert_customer(self, user_id, cid, uid=None, display_name=None, avatar_url=None, extra=None):
        if not user_id or not cid:
            return
        extra_json = json.dumps(extra, ensure_ascii=False) if isinstance(extra, dict) else (extra or None)
        with self.lock, self.db:
            self.db.execute(
                '''INSERT INTO customers(user_id, cid, uid, display_name, avatar_url, extra, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, cid) DO UPDATE SET
                     uid=COALESCE(NULLIF(excluded.uid,''), customers.uid),
                     display_name=COALESCE(NULLIF(excluded.display_name,''), customers.display_name),
                     avatar_url=COALESCE(NULLIF(excluded.avatar_url,''), customers.avatar_url),
                     extra=COALESCE(NULLIF(excluded.extra,''), customers.extra),
                     updated_at=excluded.updated_at''',
                (user_id, cid, uid or None, display_name or None, avatar_url or None, extra_json, now()))

    def get_customer(self, user_id, cid):
        rows = self.q('SELECT * FROM customers WHERE user_id=? AND cid=?', (user_id, cid))
        if not rows:
            return None
        d = dict(rows[0])
        for field in ('extra', 'tags'):
            if d.get(field) and isinstance(d[field], str):
                try:
                    d[field] = json.loads(d[field])
                except Exception:
                    pass
        if not isinstance(d.get('tags'), list):
            d['tags'] = []
        return d

    def update_customer_meta(self, user_id, cid, stage=None, tags=None, note=None):
        if not user_id or not cid:
            return
        tags_list = [str(t).strip()[:30] for t in (tags or []) if str(t).strip()][:12] if isinstance(tags, list) else None
        tags_json = json.dumps(tags_list, ensure_ascii=False) if tags_list is not None else None
        stage_val = str(stage).strip()[:30] if stage is not None else None
        note_val = str(note).strip()[:1000] if note is not None else None
        with self.lock, self.db:
            self.db.execute(
                '''INSERT INTO customers(user_id, cid, stage, tags, note, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, cid) DO UPDATE SET
                     stage=CASE WHEN ? IS NOT NULL THEN ? ELSE customers.stage END,
                     tags=CASE WHEN ? IS NOT NULL THEN ? ELSE customers.tags END,
                     note=CASE WHEN ? IS NOT NULL THEN ? ELSE customers.note END,
                     updated_at=excluded.updated_at''',
                (user_id, cid, stage_val, tags_json, note_val, now(),
                 stage_val, stage_val, tags_json, tags_json, note_val, note_val))

    def customer_names(self):
        rows = self.q("SELECT user_id, cid, display_name, stage, tags FROM customers")
        out = {}
        for r in rows:
            tags = []
            if r['tags']:
                try:
                    parsed = json.loads(r['tags'])
                    if isinstance(parsed, list):
                        tags = parsed
                except Exception:
                    pass
            out[(r['user_id'], r['cid'])] = {
                'display_name': r['display_name'] or '',
                'stage': r['stage'] or '',
                'tags': tags,
            }
        return out


# ---------------------------------------------------------------- parsing
RE_UID = re.compile(r'LL_USERID:(\d+)')
RE_CONV = re.compile(r'convCode:\s*(\S+)')
RE_LOG_TS = re.compile(r'\[(\d{2})(\d{2})/(\d{2})(\d{2})(\d{2})\.\d+:')
RE_CCODE = re.compile(r'ccode=([^,\s]+)')
RE_FILEPATH = re.compile(r'filepath=(.+?)(?=,\s*[A-Za-z_]+=|$|\])')
RE_MSGCODE = re.compile(r'msgCode:\s*([^\s:]+):(\d+)')
RE_RECV_CID = re.compile(r'cid:([^\s,]+)')
RE_RECV_UID = re.compile(r'dmsg\.sender\.uid=(\d+)')
RE_IMG_URL = re.compile(r'(?:thumbUrl|url)=([^,\s]+)')
MSG_TYPES = {101: 'text', 10010: 'system', 102: 'image', 103: 'file', 104: 'audio', 105: 'video', 106: 'card'}


def is_placeholder(content):
    if not content:
        return True
    c = str(content).strip()
    return len(c) <= 6 and c.startswith('[') and c.endswith(']')


def log_time(line):
    m = RE_LOG_TS.search(line)
    if not m:
        return None
    mo, da, hh, mi, ss = (int(x) for x in m.groups())
    try:
        return int(time.mktime((time.localtime().tm_year, mo, da, hh, mi, ss, 0, 0, -1)))
    except Exception:
        return None


def balanced_json(s):
    depth = 0
    in_str = False
    esc = False
    start = None
    for i, ch in enumerate(s):
        if start is None:
            if ch == '{':
                start = i
                depth = 1
            continue
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


def peer_of(cid, user_id):
    if not cid:
        return None
    me = (user_id or '').split('@')[0]
    head = cid.split('#', 1)[0]
    left, sep, right = head.partition('-')
    if not sep:
        return head or None
    if left == me:
        return right or None
    if right == me:
        return left or None
    return right or left or None


def base_msg(uid, cid, mid, direction, sender, receiver, content, ctype, ts, kind, line, payload=None, media=None, client_id=None):
    domain = 'icbu'
    if cid and '@' in cid:
        domain = cid.rsplit('@', 1)[1].strip()
    return {
        'user_id': '%s@%s' % (uid, domain), 'cid': cid, 'mid': str(mid), 'direction': direction,
        'sender': sender or None, 'receiver': receiver, 'content': content,
        'content_type': ctype, 'msg_time': ts, 'captured_at': now(), 'source': 'log',
        'table_name': kind, 'raw': line[-6000:], 'kind': kind, 'media': media,
        'payload': payload, 'client_id': client_id,
    }


def parse_text(line):
    if 'convLastMsgChange' not in line or 'now:' not in line:
        return None
    blob = balanced_json(line.split('now:', 1)[1])
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except Exception:
        return None
    conversation = data.get('conversationContent') if isinstance(data, dict) else None
    lm = conversation.get('lastMessageSummary') if isinstance(conversation, dict) else None
    if not isinstance(lm, dict):
        return None
    try:
        mt = int(lm.get('msgType'))
    except (TypeError, ValueError):
        mt = None
    content = lm.get('content')
    if not content and mt in (102, 103, 104, 105, 106):
        content = {102: '[图片]', 103: '[文件]', 104: '[语音]', 105: '[视频]', 106: '[卡片]'}[mt]
    code = lm.get('code')
    if not isinstance(code, dict):
        return None
    mid = code.get('messageId') or code.get('clientId')
    uid_m = RE_UID.search(line)
    if not content or not mid or not uid_m:
        return None
    uid = uid_m.group(1)
    conv = RE_CONV.search(line)
    cid = conv.group(1).rstrip('.,') if conv else None
    actor = lm.get('sender')
    sender = actor.get('targetId') if isinstance(actor, dict) else None
    direction = ('out' if str(sender) == uid else 'in') if sender else 'unknown'
    other = peer_of(cid, uid + '@icbu')
    try:
        ts = int(lm.get('sendTime')) // 1000
    except Exception:
        ts = log_time(line)
    kind = MSG_TYPES.get(mt, 'text')
    media = None
    if mt == 102:
        if isinstance(content, str) and content.startswith('http'):
            media = content
    return base_msg(uid, cid, mid, direction, str(sender) if sender else None,
                    other if direction == 'out' else uid, content if isinstance(content, str)
                    else json.dumps(content, ensure_ascii=False),
                    MSG_TYPES.get(mt, str(mt)), ts, kind, line, media=media, client_id=code.get('clientId'))


def parse_image(line):
    if 'msgview_update' not in line or 'image!' not in line:
        return None
    path = RE_FILEPATH.search(line)
    ccode = RE_CCODE.search(line)
    uid_m = RE_UID.search(line)
    if not path or not ccode:
        return None
    uid = uid_m.group(1) if uid_m else None
    if uid is None:
        return {'kind': 'image_partial', 'media': path.group(1), 'cid': ccode.group(1),
                'ts': log_time(line)}
    cid = ccode.group(1)
    mid = 'img:' + hashlib.sha256((cid + '\0' + path.group(1)).encode('utf-8', 'replace')).hexdigest()
    url = RE_IMG_URL.search(line)
    msg = base_msg(uid, cid, mid, 'unknown', None, peer_of(cid, uid + '@icbu'),
                   '[图片]', 'image', log_time(line), 'image', line, media=path.group(1),
                   payload=json.dumps({'url': url.group(1) if url else None}))
    msg['source'] = 'log-image-linked'
    return msg


def parse_link(line):
    if 'businessName:MessageReceive' not in line:
        return None
    m = RE_MSGCODE.search(line)
    cid_m = RE_RECV_CID.search(line)
    if not m or not cid_m:
        return None
    uid_m = RE_UID.search(line)
    if not uid_m:
        return None
    return {'mid': m.group(1), 'client_id': m.group(2), 'cid': cid_m.group(1),
            'uid': uid_m.group(1), 'ts': log_time(line)}


def parse_events(line):
    out = []
    msg = parse_text(line)
    if msg:
        out.append(('message', msg))
    img = parse_image(line)
    if img:
        if img.get('kind') == 'image_partial':
            out.append(('image_partial', img))
        else:
            out.append(('message', img))
    link = parse_link(line)
    if link:
        out.append(('link', link))
    return out


def media_dir():
    return os.path.join(app_support_dir(), 'msg_image')



# ---------------------------------------------------------------- hub
class Hub(object):
    def __init__(self, store):
        self.store = store
        self.subs = set()
        self.sub_lock = threading.Lock()
        self.status = {
            'state': 'starting', 'log': None, 'log_size': 0, 'offset': 0,
            'started_at': now(), 'attached_at': None, 'last_event_at': None,
            'counters': {'lines': 0, 'parsed': 0, 'messages_in': 0, 'messages_out': 0,
                         'skipped': 0, 'raw_events': 0},
            'hooks': [], 'dbs': [], 'strategies': [], 'last_error': None, 'hint': None,
        }
        self.events = []
        self.ev_lock = threading.Lock()
        self.cards = {}
        self.links = {}
        self.card_lock = threading.Lock()
        try:
            row = store.q("SELECT SUM(direction='in') i, SUM(direction='out') o, COUNT(*) n FROM messages")[0]
            self.status['counters']['messages_in'] = row['i'] or 0
            self.status['counters']['messages_out'] = row['o'] or 0
        except Exception:
            pass

    def publish(self, obj):
        data = json.dumps(obj, ensure_ascii=False)
        with self.sub_lock:
            dead = []
            for q in self.subs:
                try:
                    q.put_nowait(data)
                except Exception:
                    dead.append(q)
            for q in dead:
                self.subs.discard(q)

    def publish_status(self):
        self.publish({'kind': 'status', 'status': self.status})

    def set_status(self, **kw):
        self.status.update(kw)
        self.publish_status()

    def on_link(self, link):
        key = (link['uid'], str(link['client_id']))
        with self.card_lock:
            old = self.links.get(key)
            if old and old.get('cid') != link.get('cid'):
                self.links[key] = None
                return
            if key in self.links and old is None:
                return
            if old:
                timestamps = [item['ts'] for item in (old, link) if item.get('ts')]
                link = dict(link, ts=min(timestamps) if timestamps else None,
                            direction=link.get('direction', old.get('direction', 'in')),
                            passive=old.get('passive', False) and link.get('passive', False))
            self.links[key] = link
        self.flush_card(key)

    def on_card(self, uid, client_id, card):
        key = (uid, client_id)
        with self.card_lock:
            self.cards[key] = card
        self.flush_card(key)

    def flush_card(self, key):
        with self.card_lock:
            link = self.links.get(key)
            card = self.cards.get(key)
        if not link or not card:
            return
        data = card_payload(card['data'])
        if not data:
            return
        products = data.get('products') or []
        p0 = products[0] if isinstance(products, list) and products and isinstance(products[0], dict) else {}
        name = htmlmod.unescape(str(p0.get('productName') or data.get('title') or '卡片消息'))
        img = p0.get('productImage') or data.get('productImage')
        uid, client_id = key
        direction = link.get('direction', 'in')
        peer = peer_of(link['cid'], uid + '@icbu')
        msg = base_msg(uid, link['cid'], link['mid'], direction, uid if direction == 'out' else peer,
                       peer if direction == 'out' else uid, name[:600], 'card', link.get('ts') or card.get('time'),
                       'card', card['data'][:6000], payload=card['data'], media=img, client_id=client_id)
        if self.store.add_message(msg, passive=link.get('passive', False)):
            key_name = 'messages_out' if direction == 'out' else 'messages_in'
            self.status['counters'][key_name] += 1
            self.status['last_event_at'] = now()
            self.status['log_user'] = msg['user_id']
            self.publish({'kind': 'message', 'message': msg})
            self.publish_status()
        else:
            self.publish({'kind': 'workbench', 'user': msg['user_id'], 'cid': msg['cid']})

    def note_event(self, kind, payload, raw=None):
        with self.ev_lock:
            self.events.append({'ts': now(), 'kind': kind, 'payload': payload})
            if len(self.events) > 300:
                del self.events[:-300]
        self.store.add_raw(kind, raw if raw else json.dumps(payload, ensure_ascii=False))
        self.status['counters']['raw_events'] += 1


# ---------------------------------------------------------------- watcher
class LogWatcher(threading.Thread):
    def __init__(self, hub, path, preferred_path=None):
        threading.Thread.__init__(self, daemon=True)
        self.hub = hub
        self.path = path
        self.preferred_path = preferred_path
        self.stop_flag = threading.Event()
        self.rescan_flag = threading.Event()
        self.importing = not hub.store.q("SELECT 1 FROM meta WHERE k='wb_first_scan'")
        self.initial_size = None

    def run(self):
        hub = self.hub
        pos = 0
        ino = None
        buf = b''
        if self.path:
            hub.set_status(state='tailing', log=self.path, offset=0, hint=None, attached_at=now())
        else:
            hub.set_status(state='no_log', hint='暂未找到阿里卖家日志，系统会继续查找。')
        while not self.stop_flag.is_set():
            if not self.path:
                self.path = find_log(self.preferred_path)
                if not self.path:
                    self.stop_flag.wait(3)
                    continue
                hub.set_status(state='tailing', log=self.path, offset=0, hint=None, attached_at=now())
            if self.rescan_flag.is_set():
                self.rescan_flag.clear()
                pos = 0
                buf = b''
                ino = None
                hub.set_status(offset=0)
            try:
                st = os.stat(self.path)
            except OSError as e:
                hub.set_status(state='no_log', hint='找不到应用日志：%s（打开阿里卖家后再试）' % self.path,
                               last_error=str(e))
                time.sleep(3)
                continue
            if self.initial_size is None:
                self.initial_size = st.st_size
            if ino != st.st_ino or st.st_size < pos:
                pos = 0
                buf = b''
                ino = st.st_ino
            hub.status['log_size'] = st.st_size
            if st.st_size > pos:
                try:
                    with open(self.path, 'rb') as f:
                        f.seek(pos)
                        chunk = f.read(min(st.st_size - pos, 4 * 1024 * 1024))
                        pos = f.tell()
                except OSError as e:
                    hub.set_status(last_error=str(e))
                    time.sleep(2)
                    continue
                hub.status['offset'] = pos
                buf += chunk
                if b'\n' in chunk or buf:
                    lines = buf.split(b'\n')
                    buf = lines.pop()
                    for raw in lines:
                        self.handle_raw(raw)
                if hub.status['state'] != 'tailing':
                    hub.set_status(state='tailing', hint=None)
            if self.importing and pos >= self.initial_size:
                hub.store.x("INSERT OR IGNORE INTO meta(k,v) VALUES('wb_first_scan','done')")
                self.importing = False
            time.sleep(0.6)
        hub.set_status(state='stopped', hint='已停止。')

    def handle_raw(self, raw):
        hub = self.hub
        hub.status['counters']['lines'] += 1
        line = raw.decode('utf-8', 'replace')
        events = parse_events(line)
        if not events:
            if 'convLastMsgChange' in line or 'msgview_update' in line:
                hub.status['counters']['skipped'] += 1
                if hub.status['counters']['skipped'] < 200:
                    hub.note_event('unparsed', {'len': len(line)}, raw=line[-1200:])
            return
        for etype, payload in events:
            timestamp = payload.get('msg_time') or payload.get('ts')
            passive = (payload.get('kind') == 'system' or
                       (self.importing and (not timestamp or timestamp < now() - 15 * 60)))
            if etype == 'link':
                hub.on_link(dict(payload, passive=passive))
                continue
            if etype == 'image_partial':
                continue
            hub.status['counters']['parsed'] += 1
            if hub.store.add_message(payload, passive=passive):
                c = hub.status['counters']
                if payload['direction'] == 'out':
                    c['messages_out'] += 1
                else:
                    c['messages_in'] += 1
                hub.status['last_event_at'] = now()
                hub.status['log_user'] = payload['user_id']
                hub.publish({'kind': 'message', 'message': payload})
            if payload.get('client_id'):
                hub.on_link({'mid': payload['mid'], 'client_id': payload['client_id'],
                             'cid': payload['cid'], 'uid': payload['user_id'].split('@')[0],
                             'ts': payload.get('msg_time'), 'direction': payload['direction'], 'passive': passive})
        hub.publish_status()


class CardWatcher(threading.Thread):
    def __init__(self, hub, db_path, uid):
        threading.Thread.__init__(self, daemon=True)
        self.hub = hub
        self.db_path = db_path
        self.uid = uid
        self.stop_flag = threading.Event()

    def read_cards(self):
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix='dxcard-')
        try:
            for suffix in ('', '-wal', '-shm'):
                src = self.db_path + suffix
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(tmp, os.path.basename(src)))
            db = sqlite3.connect(os.path.join(tmp, os.path.basename(self.db_path)))
            try:
                rows = db.execute('SELECT id, templatedata, time FROM dxcardinfo').fetchall()
            finally:
                db.close()
            out = {}
            for cid, data, ts in rows:
                if not cid or not str(cid).startswith('zh_'):
                    continue
                out[str(cid)[3:]] = {'id': cid, 'data': data or '', 'time': ts}
            return out
        except Exception as e:
            self.hub.set_status(last_error='card db: %s' % e)
            return {}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def run(self):
        seen = {}
        while not self.stop_flag.is_set():
            cards = self.read_cards()
            for client_id, card in cards.items():
                if card == seen.get(client_id):
                    continue
                seen[client_id] = card
                self.hub.on_card(self.uid, client_id, card)
            time.sleep(2)


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    hub = None
    store = None
    watcher = None
    workbench = None
    frida = None
    desktop_token = None

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _local_request(self):
        port = self.server.server_port
        hosts = {'localhost:%d' % port, '127.0.0.1:%d' % port}
        if port == 80:
            hosts.update(('localhost', '127.0.0.1'))
        host = self.headers.get('Host')
        origin = self.headers.get('Origin')
        if host not in hosts or (origin and origin != 'http://' + host):
            return False
        if self.desktop_token:
            provided = self.headers.get('X-AliSupplier-Desktop', '').encode('utf-8')
            return hmac.compare_digest(provided, self.desktop_token)
        return True

    def do_GET(self):
        if not self._local_request():
            return self._json({'error': '只能从本机工作台访问'}, 403)
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        p = u.path
        try:
            if p in ('/', '/index.html'):
                return self._file('workbench.html', 'text/html; charset=utf-8')
            if p == '/logs':
                return self._file('index.html', 'text/html; charset=utf-8')
            if p == '/workbench.js':
                return self._file('workbench.js', 'application/javascript; charset=utf-8')
            if p == '/workbench.css':
                return self._file('workbench.css', 'text/css; charset=utf-8')
            if p == '/hero-trade.jpg':
                return self._file('hero-trade.jpg', 'image/jpeg')
            if p.startswith('/api/workbench/'):
                return self._workbench_get(p, qs)
            if p == '/app.js':
                return self._file('app.js', 'application/javascript; charset=utf-8')
            if p == '/style.css':
                return self._file('style.css', 'text/css; charset=utf-8')
            if p == '/api/status':
                st = dict(self.hub.status)
                st['db'] = self.store.path
                st['frida'] = Handler.frida.snapshot() if Handler.frida else {'available': False, 'enabled': False, 'state': 'disabled'}
                return self._json(st)
            if p == '/api/users':
                return self._json([dict(r) for r in self.store.q(
                    'SELECT user_id, COUNT(*) n, MAX(msg_time) t FROM messages GROUP BY user_id')])
            if p == '/api/conversations':
                user = (qs.get('user') or [None])[0]
                rows = self.store.q(
                    '''SELECT user_id, cid, COUNT(*) n, MAX(msg_time) t, MAX(id) last_id
                       FROM messages WHERE (? IS NULL OR user_id=?)
                       GROUP BY user_id, cid ORDER BY t DESC LIMIT 300''', (user, user))
                out = []
                for r in rows:
                    d = dict(r)
                    d['peer'] = peer_of(d.get('cid'), d.get('user_id'))
                    last = self.store.q(
                        'SELECT content, direction, sender, content_type FROM messages WHERE id=?',
                        (r['last_id'],))
                    if last:
                        d['last'] = last[0]['content']
                        d['last_direction'] = last[0]['direction']
                        d['last_sender'] = last[0]['sender']
                        d['last_type'] = last[0]['content_type']
                    out.append(d)
                return self._json(out)
            if p == '/api/messages':
                user = (qs.get('user') or [None])[0]
                cid = (qs.get('cid') or [None])[0]
                lim = min(int((qs.get('limit') or ['300'])[0]), 2000)
                rows = self.store.q(
                    '''SELECT * FROM messages
                       WHERE (? IS NULL OR user_id=?) AND (? IS NULL OR IFNULL(cid,'')=?)
                       ORDER BY IFNULL(msg_time, captured_at) ASC, id ASC LIMIT ?''',
                    (user, user, cid, cid, lim))
                return self._json([dict(r) for r in rows])
            if p == '/api/events':
                with self.hub.ev_lock:
                    evs = self.hub.events[-int((qs.get('limit') or ['80'])[0]):]
                return self._json(evs[::-1])
            if p == '/api/stream':
                return self._stream()
            if p == '/api/img':
                return self._img((qs.get('u') or [''])[0])
            if p == '/media':
                return self._media((qs.get('f') or [''])[0])
            if p == '/api/health':
                return self._json({'ok': True, 'time': now()})
        except Exception as e:
            return self._json({'error': str(e)}, 500)
        return self._send(404, 'not found', 'text/plain')

    def do_POST(self):
        if not self._local_request() or self.headers.get('Sec-Fetch-Site') == 'cross-site':
            return self._json({'error': '只能从本机工作台访问'}, 403)
        u = urlparse(self.path)
        if u.path.startswith('/api/workbench/'):
            try:
                if self.headers.get_content_type() != 'application/json':
                    return self._json({'error': '需要 JSON 请求'}, 415)
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 256 * 1024:
                    return self._json({'error': '请求大小不符合限制'}, 400)
                values = json.loads(self.rfile.read(length))
                if not isinstance(values, dict):
                    return self._json({'error': '请求数据无效'}, 400)
                return self._workbench_post(u.path, values)
            except (ValueError, TypeError, AttributeError, KeyError) as error:
                return self._json({'error': str(error)}, 400)
            except AIError as error:
                return self._json({'error': str(error)}, 502)
        if u.path == '/api/rescan':
            if Handler.watcher:
                Handler.watcher.rescan_flag.set()
            return self._json({'ok': True})
        return self._send(404, 'not found', 'text/plain')

    def _workbench_get(self, path, query):
        wb = self.workbench

        def value(name, default=''):
            return (query.get(name) or [default])[0]

        if path == '/api/workbench/settings':
            return self._json({'settings': wb.settings.snapshot(public=True), 'protocols': PROTOCOLS,
                               'usage': wb.usage(), 'pending': wb.queue_count(), 'ocr_available': bool(wb.media.ocr)})
        if path == '/api/workbench/list':
            offset = max(0, int(value('offset', '0')))
            count = max(1, min(500, int(value('count', '100'))))
            rows = wb.listing(value('user'), value('q')[:200], count + 1, offset, value('filter'))
            cmap = self.store.customer_names()
            for row in rows:
                info = cmap.get((row['user_id'], row['cid'])) or {}
                row['peer'] = info.get('display_name') or peer_of(row['cid'], row['user_id'])
                if info.get('stage'):
                    row['stage'] = info['stage']
                if info.get('tags'):
                    row['tags'] = info['tags']
            return self._json({'rows': rows[:count], 'more': len(rows) > count})
        if path == '/api/workbench/detail':
            user, cid = value('user'), value('cid')
            res = wb.detail(user, cid, int(value('before', '0')))
            res['customer'] = self.store.get_customer(user, cid)
            stats = self.store.q(
                'SELECT COUNT(*) total_messages, MIN(COALESCE(msg_time, captured_at)) first_at, MAX(COALESCE(msg_time, captured_at)) last_at FROM messages WHERE user_id=? AND cid=?',
                (user, cid))
            res['stats'] = dict(stats[0]) if stats else {'total_messages': 0, 'first_at': None, 'last_at': None}
            res['products'] = customer_products(self.store, user, cid)
            return self._json(res)
        if path == '/api/workbench/message':
            return self._json(wb.evidence(value('user'), value('cid'), int(value('id', '0'))))
        if path == '/api/workbench/image':
            rows = self.store.q("SELECT media FROM messages WHERE id=? AND (kind='image' OR content_type='image')",
                                (int(value('id', '0')),))
            image = wb.media.read(rows[0]['media']) if rows and rows[0]['media'] else None
            if image:
                return self._send(200, image[1], image[0])
            return self._send(404, '图片不可用', 'text/plain; charset=utf-8')
        if path == '/api/workbench/analytics':
            days = int(value('days', '30') or '30')
            return self._json(compute_analytics(self.store, value('user'), days))
        if path == '/api/workbench/export.csv':
            csv_bytes = export_customers_csv(self.store, value('user'))
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename="alisupplier-customers.csv"')
            self.send_header('Content-Length', str(len(csv_bytes)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                self.wfile.write(csv_bytes)
            except Exception:
                pass
            return
        return self._json({'error': '接口不存在'}, 404)

    def _workbench_post(self, path, values):
        wb = self.workbench
        if path == '/api/workbench/settings':
            old = wb.settings.snapshot()
            wb.settings.save(values)
            wb.resume_after_settings(old, wb.settings.snapshot())
            self.hub.publish({'kind': 'workbench'})
            return self._json({'settings': wb.settings.snapshot(public=True)})
        if path == '/api/workbench/test':
            saved = {p['id']: p for p in wb.settings.snapshot()['profiles']}
            profile = wb.settings.profile(values.get('profile') or {}, saved)
            if values.get('image'):
                import base64
                image = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/6X8VYsAAAAASUVORK5CYII=')
                request_model(profile, '简短回答。', '请描述测试图片。', 256, image=('image/png', image))
            else:
                result, _ = request_model(profile, '按要求只返回 JSON。',
                    '测试，请返回 {"changes":"测试","summary":"测试","next_step":"","review":"","references":[]}。',
                    512)
                parse_summary(result, set())
            return self._json({'ok': True})
        if path == '/api/workbench/frida':
            if not Handler.frida:
                raise ValueError('Frida 联动模块未初始化')
            enabled = bool(values.get('enabled'))
            Handler.frida.set_enabled(enabled)
            return self._json({'ok': True, 'frida': Handler.frida.snapshot()})
        user = values.get('user') or values.get('user_id')
        cid = values.get('cid')
        if not isinstance(user, str) or not isinstance(cid, str) or not user or not cid:
            raise ValueError('请选择会话')
        if path == '/api/workbench/active-chat':
            display_name = (values.get('display_name') or '').strip() or None
            uid = (values.get('uid') or '').strip() or None
            avatar_url = (values.get('avatar_url') or '').strip() or None
            extra = values.get('extra') if isinstance(values.get('extra'), dict) else None
            self.store.upsert_customer(user, cid, uid=uid, display_name=display_name,
                                       avatar_url=avatar_url, extra=extra)
            customer = self.store.get_customer(user, cid)
            peer = (customer and customer.get('display_name')) or display_name or peer_of(cid, user)
            self.hub.publish({'kind': 'open_chat', 'user_id': user, 'cid': cid,
                              'peer': peer, 'customer': customer})
            return self._json({'ok': True, 'user_id': user, 'cid': cid, 'peer': peer, 'customer': customer})
        if path == '/api/workbench/run':
            if not wb.settings.snapshot()['text_profile']:
                raise ValueError('请先在设置里选择文字模型')
            queued = wb.manual(user, cid)
            self.hub.publish({'kind': 'workbench', 'user': user, 'cid': cid})
            return self._json({'queued': queued})
        if path == '/api/workbench/mark':
            wb.mark(user, cid, int(values['report']), values.get('field'))
            return self._json({'ok': True})
        if path == '/api/workbench/image-note':
            wb.image_note(user, cid, int(values['message_id']), str(values.get('note') or ''))
            self.hub.publish({'kind': 'workbench', 'user': user, 'cid': cid})
            return self._json({'ok': True})
        if path == '/api/workbench/customer-note':
            stage = values.get('stage') if 'stage' in values else None
            tags = values.get('tags') if 'tags' in values else None
            note = values.get('note') if 'note' in values else None
            self.store.update_customer_meta(user, cid, stage=stage, tags=tags, note=note)
            customer = self.store.get_customer(user, cid)
            self.hub.publish({'kind': 'workbench', 'user': user, 'cid': cid})
            return self._json({'ok': True, 'customer': customer})
        if path == '/api/workbench/draft-reply':
            snap = wb.settings.snapshot()
            profile_id = snap.get('text_profile')
            profiles = {p['id']: p for p in snap.get('profiles') or []}
            profile = profiles.get(profile_id)
            if not profile:
                raise ValueError('请先在「AI 与设置」中添加并选择文字模型')
            intent = str(values.get('intent') or '').strip()[:500]
            det = wb.detail(user, cid, 0)
            customer = self.store.get_customer(user, cid) or {}
            msgs = det.get('messages') or []
            recent = msgs[-18:]
            lines = []
            for m in recent:
                who = '买家' if m.get('direction') == 'in' else ('我方' if m.get('direction') == 'out' else '消息')
                text = (m.get('content') or '').strip()
                if m.get('card') and isinstance(m['card'], dict):
                    card_str = json.dumps(m['card'], ensure_ascii=False)[:240]
                    text = (text + ' ' if text else '') + '[商品/订单卡片: ' + card_str + ']'
                if text:
                    lines.append('%s: %s' % (who, text[:300]))
            latest_report = (det.get('reports') or [None])[0]
            ctx = []
            if customer.get('display_name'):
                ctx.append('客户名称: ' + customer['display_name'])
            if customer.get('stage'):
                ctx.append('客户阶段: ' + customer['stage'])
            if customer.get('note'):
                ctx.append('业务员备注: ' + customer['note'])
            if latest_report and latest_report.get('summary'):
                ctx.append('历史概要: ' + latest_report['summary'])
            if lines:
                ctx.append('最近对话:\n' + '\n'.join(lines))
            else:
                ctx.append('最近对话: （暂无历史消息，属于新对话）')
            if intent:
                ctx.append('业务员本次想表达的中文意图: ' + intent)
            else:
                ctx.append('业务员未指定具体意图，请根据最新对话自然推进成交（如解答疑问、确认规格/MOQ/交期/样卡或礼貌跟进）。')
            sys_prompt = (
                '你是资深阿里国际站外贸业务专家。请根据客户上下文，帮业务员生成 2 条可直接发给买家的英文回复草稿'
                '（若买家使用非英语如西班牙语，也可用买家语言回复并在 zh 中注明）。\n'
                '严格只输出一个 JSON 对象，不要包含 Markdown 代码块或多余文字，格式为：\n'
                '{"drafts":[{"title":"专业推进版","en":"...","zh":"中文含义简述"},{"title":"简短直接版","en":"...","zh":"中文含义简述"}]}'
            )
            raw_text, _ = request_model(profile, sys_prompt, '\n\n'.join(ctx), 900)
            cleaned = raw_text.strip()
            if cleaned.startswith('```'):
                cleaned = re.sub(r'^```[a-zA-Z]*\s*', '', cleaned)
                cleaned = re.sub(r'\s*```$', '', cleaned)
            try:
                parsed = json.loads(cleaned)
                drafts = parsed.get('drafts') if isinstance(parsed, dict) else None
            except Exception:
                m_json = re.search(r'\{[\s\S]*\}', cleaned)
                drafts = json.loads(m_json.group(0)).get('drafts') if m_json else None
            if not isinstance(drafts, list) or not drafts:
                drafts = [{'title': '建议回复', 'en': raw_text.strip(), 'zh': ''}]
            return self._json({'ok': True, 'drafts': drafts[:3]})
        return self._json({'error': '接口不存在'}, 404)

    def _img(self, url):
        import urllib.request
        from urllib.parse import urlparse as _up
        u = _up(url)
        host = (u.hostname or '').lower()
        allowed = any(host == d or host.endswith('.' + d) for d in
                      ('alicdn.com', 'alibaba.com', 'alibabausercontent.com', 'aliimg.com'))
        if u.scheme not in ('http', 'https') or not allowed:
            return self._send(403, 'blocked', 'text/plain')
        cache_dir = os.path.join(os.path.expanduser('~'), 'Library', 'Caches', 'AliSupplierHook', 'img') \
            if sys.platform == 'darwin' else os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'AliSupplierHook', 'img')
        os.makedirs(cache_dir, exist_ok=True)
        ext = os.path.splitext(u.path)[1].lower()[:5]
        if ext not in ('.jpg', '.jpeg', '.png', '.gif', '.webp'):
            ext = '.jpg'
        name = hashlib.md5(url.encode('utf-8')).hexdigest() + ext
        path = os.path.join(cache_dir, name)
        if not os.path.isfile(path):
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://message.alibaba.com/'})
                with urllib.request.urlopen(req, timeout=12) as r:
                    data = r.read(8 * 1024 * 1024)
                    ctype = r.headers.get('Content-Type', 'image/jpeg')
                if not ctype.startswith('image/'):
                    return self._send(502, 'not image', 'text/plain')
                with open(path, 'wb') as f:
                    f.write(data)
            except Exception as e:
                return self._send(502, 'fetch failed: %s' % e, 'text/plain')
        ext = os.path.splitext(path)[1].lower()
        ctype = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
                 '.gif': 'image/gif', '.webp': 'image/webp'}.get(ext, 'image/jpeg')
        with open(path, 'rb') as f:
            self._send(200, f.read(), ctype)

    def _media(self, path):
        base = os.path.realpath(os.path.join(app_support_dir(), 'msg_image'))
        full = os.path.realpath(path or '')
        if not path or not (full == base or full.startswith(base + os.sep)) or not os.path.isfile(full):
            return self._send(404, 'not found', 'text/plain')
        ext = os.path.splitext(full)[1].lower()
        ctype = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
                 '.gif': 'image/gif', '.webp': 'image/webp'}.get(ext, 'application/octet-stream')
        with open(full, 'rb') as f:
            self._send(200, f.read(), ctype)

    def _file(self, name, ctype):
        path = os.path.join(UI_DIR, name)
        if not os.path.isfile(path):
            return self._send(404, 'missing %s' % name, 'text/plain')
        with open(path, 'rb') as f:
            self._send(200, f.read(), ctype)

    def _stream(self):
        q = queue.Queue(maxsize=500)
        with self.hub.sub_lock:
            self.hub.subs.add(q)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        try:
            self.wfile.write(b'retry: 2000\n\n')
            self.wfile.flush()
            q.put_nowait(json.dumps({'kind': 'status', 'status': self.hub.status}, ensure_ascii=False))
            while True:
                try:
                    data = q.get(timeout=15)
                    self.wfile.write(('data: %s\n\n' % data).encode('utf-8'))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b': ping\n\n')
                    self.wfile.flush()
        except Exception:
            pass
        finally:
            with self.hub.sub_lock:
                self.hub.subs.discard(q)


class FridaBridge(object):
    """在工作台进程内管理 Frida 联动监听，将阿里卖家点击事件实时推送到前端。"""
    EVT_PREFIX = '__ALISUPPLIER_EVT__'

    def __init__(self, store, hub):
        self.store = store
        self.hub = hub
        self.lock = threading.Lock()
        self.enabled = False
        self.state = 'disabled'
        self.pid = None
        self.error = ''
        self.proc = None
        self.stop_flag = threading.Event()
        self.thread = None
        self.agent_path = os.path.join(HERE, 'frida_agent.js')

    def _find_frida(self):
        exe = 'frida.exe' if os.name == 'nt' else 'frida'
        cands = [
            os.path.join(HERE, exe),
            shutil.which('frida'),
            os.path.expanduser(os.path.join('~', '.local', 'bin', exe)),
        ]
        if os.name == 'nt':
            import glob
            for base in (os.environ.get('LOCALAPPDATA'), os.environ.get('APPDATA'), os.path.expanduser('~')):
                if base:
                    cands.extend(glob.glob(os.path.join(base, 'Programs', 'Python', 'Python*', 'Scripts', 'frida.exe')))
                    cands.extend(glob.glob(os.path.join(base, 'Python', 'Python*', 'Scripts', 'frida.exe')))
        else:
            cands.extend(['/opt/homebrew/bin/frida', '/usr/local/bin/frida'])
        for cand in cands:
            if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
        return None

    def _find_target_pid(self):
        if os.name == 'nt':
            import csv
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            for name in ('AliSupplier.exe', 'AliWorkbench.exe'):
                try:
                    out = subprocess.check_output(
                        ['tasklist', '/FI', 'IMAGENAME eq ' + name, '/FO', 'CSV', '/NH'],
                        text=True, creationflags=flags, stderr=subprocess.DEVNULL
                    ).strip()
                    for row in csv.reader(out.splitlines()):
                        if len(row) >= 2 and row[0].lower() == name.lower() and row[1].isdigit():
                            return int(row[1])
                except Exception:
                    pass
            return None
        for name in ('阿里卖家', 'AliSupplier'):
            try:
                out = subprocess.check_output(['pgrep', '-x', name], text=True).strip()
                if out:
                    return int(out.splitlines()[0])
            except Exception:
                pass
        return None

    def snapshot(self):
        with self.lock:
            if sys.platform not in ('darwin', 'win32'):
                return {
                    'available': False,
                    'enabled': False,
                    'state': 'disabled',
                    'pid': None,
                    'error': '',
                    'reason': '实时联动当前仅支持 macOS 与 Windows',
                }
            avail = bool(self._find_frida() and os.path.isfile(self.agent_path))
            return {
                'available': avail,
                'enabled': self.enabled,
                'state': self.state,
                'pid': self.pid,
                'error': self.error,
                'reason': '' if avail else '未检测到 frida CLI（请先 pip install frida-tools）',
            }

    def _set_state(self, state, pid=None, error=''):
        with self.lock:
            self.state = state
            self.pid = pid
            self.error = error
        self.hub.publish({'kind': 'frida_status', 'frida': self.snapshot()})

    def set_enabled(self, enabled, persist=True):
        with self.lock:
            if persist:
                try:
                    self.store.x("INSERT OR REPLACE INTO meta(k,v) VALUES('frida_enabled',?)", ('1' if enabled else '0',))
                except Exception:
                    pass
            if enabled == self.enabled:
                return
            self.enabled = bool(enabled)
            if not self.enabled:
                self.stop_flag.set()
                proc = self.proc
                self.proc = None
                if proc and proc.poll() is None:
                    proc.terminate()
                self.state = 'disabled'
                self.pid = None
                self.error = ''
                self.hub.publish({'kind': 'frida_status', 'frida': {
                    'available': bool(self._find_frida() and os.path.isfile(self.agent_path)),
                    'enabled': False, 'state': 'disabled', 'pid': None, 'error': ''
                }})
                return
            self.stop_flag.clear()
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def _run(self):
        frida_bin = self._find_frida()
        if not frida_bin or not os.path.isfile(self.agent_path):
            with self.lock:
                self.enabled = False
            self._set_state('error', None, '未找到 frida CLI 或 frida_agent.js')
            return
        while not self.stop_flag.is_set():
            pid = self._find_target_pid()
            if not pid:
                self._set_state('waiting', None, '')
                self.stop_flag.wait(2.0)
                continue
            self._set_state('attaching', pid, '')
            try:
                popen_kwargs = dict(
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding='utf-8', errors='replace', bufsize=1,
                )
                if os.name == 'nt':
                    popen_kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                proc = subprocess.Popen([frida_bin, '-p', str(pid), '-l', self.agent_path], **popen_kwargs)
            except Exception as exc:
                self._set_state('error', pid, str(exc))
                self.stop_flag.wait(3.0)
                continue
            with self.lock:
                if not self.enabled or self.stop_flag.is_set():
                    proc.terminate()
                    break
                self.proc = proc
            for line in proc.stdout:
                if self.stop_flag.is_set():
                    break
                idx = line.find(self.EVT_PREFIX)
                if idx == -1:
                    continue
                try:
                    evt = json.loads(line[idx + len(self.EVT_PREFIX):].strip())
                except Exception:
                    continue
                if evt.get('event') == 'ready':
                    self._set_state('attached', pid, '')
                elif evt.get('event') == 'active_chat':
                    user_id = evt.get('user_id')
                    cid = evt.get('cid')
                    if user_id and cid:
                        display_name = (evt.get('display_name') or '').strip() or None
                        uid = (evt.get('uid') or '').strip() or None
                        avatar_url = (evt.get('avatar_url') or '').strip() or None
                        extra = evt.get('extra') if isinstance(evt.get('extra'), dict) else None
                        self.store.upsert_customer(user_id, cid, uid=uid, display_name=display_name,
                                                   avatar_url=avatar_url, extra=extra)
                        customer = self.store.get_customer(user_id, cid)
                        peer = (customer and customer.get('display_name')) or display_name or peer_of(cid, user_id)
                        self.hub.publish({'kind': 'open_chat', 'user_id': user_id, 'cid': cid,
                                          'peer': peer, 'customer': customer})
            proc.wait()
            with self.lock:
                if self.proc is proc:
                    self.proc = None
            if not self.stop_flag.is_set():
                self._set_state('waiting', None, '')
                self.stop_flag.wait(2.0)

    def stop(self):
        self.set_enabled(False)


@contextmanager
def own_database(path):
    """Prevent two services from claiming the same durable queue."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path + '.service.lock', 'a+b') as lock:
        try:
            if os.name == 'nt':
                import msvcrt
                if not lock.tell():
                    lock.write(b'0')
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit('此数据库已由另一个工作台占用，请先停止旧服务再启动。') from None
        yield


def stop_when_parent_exits(server):
    try:
        while os.read(sys.stdin.fileno(), 4096):
            pass
    except OSError:
        pass
    server.shutdown()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=int(os.environ.get('HOOK_PORT', '8770')))
    ap.add_argument('--db', default=os.environ.get('HOOK_DB') or default_db())
    ap.add_argument('--log', default=None)
    ap.add_argument('--no-browser', action='store_true')
    ap.add_argument('--desktop', action='store_true', help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.desktop:
        token = os.environ.pop('ALISUPPLIER_DESKTOP_TOKEN', None)
        if not token:
            ap.error('桌面模式必须从客户端启动')
        Handler.desktop_token = token.encode('ascii')

    with own_database(args.db):
        serve(args)


def serve(args):
    srv = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    srv.daemon_threads = True
    path = find_log(args.log)
    store = Store(args.db)
    hub = Hub(store)
    workbench = Workbench(store, hub, media_dir())
    frida_bridge = FridaBridge(store, hub)
    Handler.hub = hub
    Handler.store = store
    Handler.workbench = workbench
    Handler.frida = frida_bridge

    print('数据库: %s' % args.db)
    print('工作台: http://127.0.0.1:%d/' % srv.server_port)
    if path:
        print('应用日志: %s' % path)
    else:
        print('暂未找到阿里卖家日志；将继续查找。也可用 --log 指定 app.log。')
    Handler.watcher = LogWatcher(hub, path, args.log)
    Handler.watcher.start()
    import glob
    cards = glob.glob(os.path.join(app_support_dir(), 'AppData', '*', 'dynamicX_cards', 'DXCardStorageNew.db'))
    for card_db in cards:
        account = os.path.basename(os.path.dirname(os.path.dirname(card_db)))
        matched = re.fullmatch(r'(\d+)(?:@icbu)?', account)
        if matched:
            CardWatcher(hub, card_db, matched.group(1)).start()
        else:
            print('跳过账号归属不明确的卡片库: %s' % account)
    workbench.start()
    saved_frida = store.q("SELECT v FROM meta WHERE k='frida_enabled'")
    if saved_frida and saved_frida[0]['v'] == '1':
        frida_bridge.set_enabled(True, persist=False)

    runtime_file = os.path.join(os.path.dirname(os.path.abspath(args.db)), 'runtime.json')
    try:
        rt_data = {'port': srv.server_port, 'pid': os.getpid()}
        if Handler.desktop_token:
            rt_data['token'] = Handler.desktop_token.decode('ascii')
        with open(runtime_file, 'w', encoding='utf-8') as f:
            json.dump(rt_data, f)
        os.chmod(runtime_file, 0o600)
    except OSError:
        runtime_file = None

    if args.desktop:
        threading.Thread(target=stop_when_parent_exits, args=(srv,), daemon=True).start()
        print('ALISUPPLIER_DESKTOP_READY ' + json.dumps({'port': srv.server_port}), flush=True)
    elif not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open('http://127.0.0.1:%d/' % srv.server_port)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if runtime_file and os.path.exists(runtime_file):
            try:
                os.remove(runtime_file)
            except OSError:
                pass
        frida_bridge.stop()
        workbench.stop.set()
        Handler.watcher.stop_flag.set()
        srv.server_close()


if __name__ == '__main__':
    main()
