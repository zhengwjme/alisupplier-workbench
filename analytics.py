"""基于本地 SQLite 事实数据的外贸运营与客服统计分析模块。"""
import csv
import io
import json
import time
from datetime import datetime


STAGES = ('新询盘', '需求确认', '已报价', '寄样中', '谈判逼单', '已成交', '冷淡')


def _fmt_ts(ts):
    if not ts:
        return ''
    try:
        return datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return ''


def _peer_fallback(cid, user_id):
    if not cid:
        return '未知客户'
    head = cid.split('#', 1)[0]
    parts = [p for p in head.split('-') if p]
    uid = (user_id or '').split('@', 1)[0]
    others = [p for p in parts if p != uid]
    return others[0] if others else (parts[0] if parts else cid)


def _extract_products(payload_str):
    if not payload_str:
        return []
    try:
        doc = json.loads(payload_str)
    except Exception:
        return []
    if not isinstance(doc, dict):
        return []
    out = []
    if isinstance(doc.get('products'), list) and doc['products']:
        for pr in doc['products']:
            if not isinstance(pr, dict):
                continue
            title = (pr.get('productName') or pr.get('title') or '').strip()
            pid = str(pr.get('productId') or '').strip()
            price = str(pr.get('productUnitPrice') or '').strip()
            if title or pid:
                out.append({'key': pid or title, 'title': title or pid, 'price': price})
    elif doc.get('productAction') or doc.get('moq'):
        pa = doc.get('productAction') if isinstance(doc.get('productAction'), dict) else {}
        ti = pa.get('traceInfo') if isinstance(pa.get('traceInfo'), dict) else {}
        ap = pa.get('actionParams') if isinstance(pa.get('actionParams'), dict) else {}
        pid = str(ti.get('productId') or doc.get('productId') or '').strip()
        url_tail = str(ap.get('url') or '').split('/')[-1].replace('.html', '').replace('-', ' ').strip()
        title = (doc.get('title') or doc.get('subject') or url_tail).strip()
        price = str(doc.get('price') or '').strip()
        if title or pid:
            out.append({'key': pid or title, 'title': title or pid, 'price': price})
    return out


def compute_analytics(store, user='', days=30):
    now_ts = int(time.time())
    cutoff = now_ts - int(days) * 86400 if days and int(days) > 0 else 0

    rows = store.q(
        """SELECT id, user_id, cid, direction, content, kind, payload,
                  COALESCE(msg_time, captured_at) AS ts
           FROM messages
           WHERE cid IS NOT NULL AND cid<>'' AND (?='' OR user_id=?)
           ORDER BY user_id, cid, ts ASC, id ASC""",
        (user or '', user or '')
    )
    cust_rows = store.q(
        """SELECT user_id, cid, uid, display_name, stage, tags, note, updated_at
           FROM customers WHERE (?='' OR user_id=?)""",
        (user or '', user or '')
    )
    customers = {}
    for r in cust_rows:
        tags = []
        if r['tags']:
            try:
                parsed = json.loads(r['tags'])
                if isinstance(parsed, list):
                    tags = parsed
            except Exception:
                pass
        customers[(r['user_id'], r['cid'])] = {
            'uid': r['uid'] or '',
            'display_name': r['display_name'] or '',
            'stage': r['stage'] or '',
            'tags': tags,
            'note': r['note'] or '',
            'updated_at': r['updated_at'],
        }

    threads = {}
    in_msgs = 0
    out_msgs = 0
    hours = [0] * 24
    products = {}
    accounts = {}
    response_deltas = []

    for r in rows:
        uid_key = r['user_id']
        cid = r['cid']
        ts = int(r['ts'] or 0)
        direction = r['direction'] or 'unknown'
        in_window = (cutoff == 0 or ts >= cutoff)

        key = (uid_key, cid)
        t = threads.get(key)
        if not t:
            t = {
                'user_id': uid_key, 'cid': cid,
                'last_dir': None, 'last_ts': 0, 'last_content': '',
                'first_ts': ts, 'total': 0, 'in_count': 0, 'out_count': 0,
                'pending_in_ts': None, 'active_in_window': False,
            }
            threads[key] = t
        t['total'] += 1
        if ts and (t['first_ts'] == 0 or ts < t['first_ts']):
            t['first_ts'] = ts
        if ts >= t['last_ts']:
            t['last_ts'] = ts
            t['last_dir'] = direction
            t['last_content'] = (r['content'] or '').strip()[:120]

        if in_window:
            t['active_in_window'] = True
            acc = accounts.setdefault(uid_key, {
                'user_id': uid_key, 'cids': set(), 'in_count': 0, 'out_count': 0,
                'unreplied': 0, 'deltas': []
            })
            acc['cids'].add(cid)

            if direction == 'in':
                in_msgs += 1
                t['in_count'] += 1
                acc['in_count'] += 1
                if ts > 0:
                    try:
                        hours[datetime.fromtimestamp(ts).hour] += 1
                    except Exception:
                        pass
                if t['pending_in_ts'] is None:
                    t['pending_in_ts'] = ts
            elif direction == 'out':
                out_msgs += 1
                t['out_count'] += 1
                acc['out_count'] += 1
                if t['pending_in_ts'] is not None and ts >= t['pending_in_ts']:
                    delta = ts - t['pending_in_ts']
                    # 仅统计 24 小时内的有效客服响应，避免跨周历史干扰
                    if 0 <= delta <= 86400:
                        response_deltas.append(delta)
                        acc['deltas'].append(delta)
                t['pending_in_ts'] = None

            if r['kind'] == 'card' and r['payload']:
                for pr in _extract_products(r['payload']):
                    p_entry = products.setdefault(pr['key'], {
                        'title': pr['title'], 'price': pr['price'], 'count': 0, 'cids': set()
                    })
                    p_entry['count'] += 1
                    p_entry['cids'].add(cid)
                    if not p_entry['price'] and pr['price']:
                        p_entry['price'] = pr['price']

    urgent_unreplied = []
    silent_followups = []
    stage_counts = {s: 0 for s in STAGES}
    stage_counts['未标记'] = 0

    for key, t in threads.items():
        cinfo = customers.get(key) or {}
        stage = cinfo.get('stage') or '未标记'
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        peer = cinfo.get('display_name') or _peer_fallback(t['cid'], t['user_id'])
        wait_sec = max(0, now_ts - t['last_ts']) if t['last_ts'] else 0

        if t['last_dir'] == 'in':
            if t['user_id'] in accounts:
                accounts[t['user_id']]['unreplied'] += 1
            urgent_unreplied.append({
                'user_id': t['user_id'], 'cid': t['cid'], 'peer': peer,
                'stage': cinfo.get('stage') or '',
                'wait_minutes': int(wait_sec // 60),
                'last_ts': t['last_ts'], 'last_content': t['last_content'],
            })
        elif t['last_dir'] == 'out' and wait_sec >= 86400 and stage not in ('已成交', '冷淡'):
            silent_followups.append({
                'user_id': t['user_id'], 'cid': t['cid'], 'peer': peer,
                'stage': cinfo.get('stage') or '',
                'wait_hours': round(wait_sec / 3600.0, 1),
                'last_ts': t['last_ts'], 'last_content': t['last_content'],
            })

    urgent_unreplied.sort(key=lambda x: -x['last_ts'])
    silent_followups.sort(key=lambda x: -x['wait_hours'])

    avg_resp_min = round((sum(response_deltas) / len(response_deltas)) / 60.0, 1) if response_deltas else None
    within_1h = round(100.0 * sum(1 for d in response_deltas if d <= 3600) / len(response_deltas), 1) if response_deltas else None

    top_products = [
        {'title': v['title'], 'price': v['price'], 'buyers': len(v['cids']), 'mentions': v['count']}
        for v in sorted(products.values(), key=lambda x: (-len(x['cids']), -x['count']))[:10]
    ]

    account_list = []
    for a in accounts.values():
        avg_m = round((sum(a['deltas']) / len(a['deltas'])) / 60.0, 1) if a['deltas'] else None
        account_list.append({
            'user_id': a['user_id'],
            'customers': len(a['cids']),
            'in_messages': a['in_count'],
            'out_messages': a['out_count'],
            'unreplied': a['unreplied'],
            'avg_response_minutes': avg_m,
        })
    account_list.sort(key=lambda x: -x['customers'])

    return {
        'days': int(days),
        'generated_at': now_ts,
        'kpi': {
            'total_customers': len(threads),
            'active_customers': sum(1 for t in threads.values() if t['active_in_window']),
            'in_messages': in_msgs,
            'out_messages': out_msgs,
            'unreplied_count': len(urgent_unreplied),
            'silent_followup_count': len(silent_followups),
            'avg_response_minutes': avg_resp_min,
            'within_1h_rate': within_1h,
            'response_samples': len(response_deltas),
        },
        'stages': [{'stage': k, 'count': v} for k, v in stage_counts.items() if v > 0 or k in STAGES],
        'hours': hours,
        'top_products': top_products,
        'accounts': account_list,
        'urgent_unreplied': urgent_unreplied[:12],
        'silent_followups': silent_followups[:12],
    }


def export_customers_csv(store, user=''):
    now_ts = int(time.time())
    threads = store.q(
        """SELECT m.user_id, m.cid, COUNT(*) total_msgs,
                  SUM(CASE WHEN m.direction='in' THEN 1 ELSE 0 END) in_msgs,
                  SUM(CASE WHEN m.direction='out' THEN 1 ELSE 0 END) out_msgs,
                  MIN(COALESCE(m.msg_time, m.captured_at)) first_ts,
                  MAX(COALESCE(m.msg_time, m.captured_at)) last_ts,
                  (SELECT direction FROM messages m2 WHERE m2.user_id=m.user_id AND m2.cid=m.cid
                   ORDER BY COALESCE(m2.msg_time, m2.captured_at) DESC, m2.id DESC LIMIT 1) last_dir
           FROM messages m
           WHERE m.cid IS NOT NULL AND m.cid<>'' AND (?='' OR m.user_id=?)
           GROUP BY m.user_id, m.cid
           ORDER BY last_ts DESC""",
        (user or '', user or '')
    )
    cust_map = {}
    for r in store.q("SELECT * FROM customers WHERE (?='' OR user_id=?)", (user or '', user or '')):
        cust_map[(r['user_id'], r['cid'])] = dict(r)
    rep_map = {}
    for r in store.q(
        """SELECT r.user_id, r.cid, r.body FROM wb_reports r
           WHERE r.id IN (SELECT MAX(id) FROM wb_reports GROUP BY user_id, cid)"""
    ):
        try:
            rep_map[(r['user_id'], r['cid'])] = json.loads(r['body'] or '{}')
        except Exception:
            pass

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        '客户名称', '买家UID', '卖家账号', '会话CID', '意向阶段', '标签', '备注',
        '跟进状态', '累计消息', '买家消息', '我方回复', '首次互动时间', '最近消息时间',
        'AI当前概要', 'AI建议跟进'
    ])
    for t in threads:
        key = (t['user_id'], t['cid'])
        c = cust_map.get(key) or {}
        rep = rep_map.get(key) or {}
        peer = c.get('display_name') or _peer_fallback(t['cid'], t['user_id'])
        uid = c.get('uid') or _peer_fallback(t['cid'], t['user_id'])
        tags = ''
        if c.get('tags'):
            try:
                parsed = json.loads(c['tags'])
                tags = ', '.join(parsed) if isinstance(parsed, list) else str(c['tags'])
            except Exception:
                tags = str(c['tags'])
        last_dir = t['last_dir'] or ''
        wait_sec = max(0, now_ts - int(t['last_ts'] or 0))
        if last_dir == 'in':
            status = '待我回复'
        elif last_dir == 'out' and wait_sec >= 86400:
            status = '沉默待唤醒'
        else:
            status = '正常'
        writer.writerow([
            peer, uid, t['user_id'], t['cid'], c.get('stage') or '', tags, c.get('note') or '',
            status, t['total_msgs'], t['in_msgs'], t['out_msgs'],
            _fmt_ts(t['first_ts']), _fmt_ts(t['last_ts']),
            rep.get('summary') or '', rep.get('next_step') or ''
        ])
    return ('\ufeff' + buf.getvalue()).encode('utf-8')
