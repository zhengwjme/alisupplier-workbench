"""基于本地 SQLite 事实数据的外贸运营、选品（产品+地域）与客服统计分析模块。"""
import csv
import io
import json
import re
import time
from datetime import datetime


STAGES = ('新询盘', '需求确认', '已报价', '寄样中', '谈判逼单', '已成交', '冷淡')

# ISO2 国家代码 -> (中文名, 大区)
COUNTRY_META = {
    'US': ('美国', '北美'), 'CA': ('加拿大', '北美'), 'MX': ('墨西哥', '拉美'),
    'UK': ('英国', '欧洲'), 'GB': ('英国', '欧洲'), 'DE': ('德国', '欧洲'), 'FR': ('法国', '欧洲'),
    'IT': ('意大利', '欧洲'), 'ES': ('西班牙', '欧洲'), 'NL': ('荷兰', '欧洲'), 'PL': ('波兰', '欧洲'),
    'RU': ('俄罗斯', '欧洲'), 'UA': ('乌克兰', '欧洲'), 'TR': ('土耳其', '中东/欧亚'),
    'AE': ('阿联酋', '中东'), 'SA': ('沙特', '中东'), 'IQ': ('伊拉克', '中东'), 'IR': ('伊朗', '中东'),
    'KW': ('科威特', '中东'), 'QA': ('卡塔尔', '中东'), 'OM': ('阿曼', '中东'), 'JO': ('约旦', '中东'),
    'LB': ('黎巴嫩', '中东'), 'IL': ('以色列', '中东'), 'PS': ('巴勒斯坦', '中东'), 'EG': ('埃及', '中东/非洲'),
    'IN': ('印度', '南亚'), 'PK': ('巴基斯坦', '南亚'), 'BD': ('孟加拉', '南亚'), 'LK': ('斯里兰卡', '南亚'),
    'NP': ('尼泊尔', '南亚'),
    'GH': ('加纳', '非洲'), 'NG': ('尼日利亚', '非洲'), 'KE': ('肯尼亚', '非洲'), 'ZA': ('南非', '非洲'),
    'TZ': ('坦桑尼亚', '非洲'), 'UG': ('乌干达', '非洲'), 'ET': ('埃塞俄比亚', '非洲'), 'CM': ('喀麦隆', '非洲'),
    'BF': ('布基纳法索', '非洲'), 'CI': ('科特迪瓦', '非洲'), 'SN': ('塞内加尔', '非洲'), 'MA': ('摩洛哥', '非洲'),
    'DZ': ('阿尔及利亚', '非洲'), 'TN': ('突尼斯', '非洲'), 'AO': ('安哥拉', '非洲'), 'CD': ('刚果(金)', '非洲'),
    'BR': ('巴西', '拉美'), 'AR': ('阿根廷', '拉美'), 'CO': ('哥伦比亚', '拉美'), 'CL': ('智利', '拉美'),
    'PE': ('秘鲁', '拉美'), 'BO': ('玻利维亚', '拉美'), 'EC': ('厄瓜多尔', '拉美'), 'VE': ('委内瑞拉', '拉美'),
    'AU': ('澳大利亚', '大洋洲'), 'NZ': ('新西兰', '大洋洲'),
    'VN': ('越南', '东南亚'), 'TH': ('泰国', '东南亚'), 'ID': ('印尼', '东南亚'), 'MY': ('马来西亚', '东南亚'),
    'PH': ('菲律宾', '东南亚'), 'SG': ('新加坡', '东南亚'), 'MM': ('缅甸', '东南亚'), 'KH': ('柬埔寨', '东南亚'),
    'JP': ('日本', '东亚'), 'KR': ('韩国', '东亚'), 'CN': ('中国', '东亚'), 'HK': ('中国香港', '东亚'),
}

RE_FLAG_CODE = re.compile(r'flags/[^/]+/assets/([a-z]{2})\.png', re.I)
STOP_WORDS = {
    'for', 'and', 'with', 'the', 'new', 'hot', 'sale', 'high', 'quality', 'wholesale',
    'factory', 'custom', 'price', 'cheap', 'supply', 'supplies', 'design', 'style',
    'direct', 'china', 'stock', 'free', 'sample', 'oem', 'odm', 'premium', 'best',
    '100%', '100', '2024', '2025', '2026', 'top', 'grade', 'bulk', 'ready', 'ship',
    'mh', 'alibaba', 'product', 'detail', 'html', 'from', 'good', 'use', 'used',
}


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


def _norm_country(raw):
    if not raw:
        return ''
    s = str(raw).strip()
    m = RE_FLAG_CODE.search(s)
    if m:
        return m.group(1).upper()
    if len(s) == 2 and s.isalpha():
        return s.upper()
    upper = s.upper()
    for code, (zh, _) in COUNTRY_META.items():
        if upper == code or s == zh:
            return code
    return s[:20]


def _country_label(code):
    if not code:
        return ('未知地域', '未识别')
    meta = COUNTRY_META.get(code.upper())
    if meta:
        return (f'{meta[0]} ({code.upper()})', meta[1])
    return (code, '其他地区')


def extract_products(payload_str):
    return _extract_products(payload_str)


def customer_products(store, user, cid):
    rows = store.q(
        "SELECT payload FROM messages WHERE user_id=? AND cid=? AND kind='card' AND payload IS NOT NULL ORDER BY id DESC",
        (user, cid)
    )
    items = {}
    for r in rows:
        for pr in _extract_products(r['payload']):
            entry = items.setdefault(pr['key'], {
                'id': pr['id'], 'title': pr['title'], 'price': pr['price'], 'moq': pr['moq'], 'count': 0
            })
            entry['count'] += 1
            if not entry['price'] and pr['price']:
                entry['price'] = pr['price']
            if not entry['moq'] and pr['moq']:
                entry['moq'] = pr['moq']
    return sorted(items.values(), key=lambda x: -x['count'])[:12]


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
            moq = str(pr.get('productMOQ') or pr.get('moq') or '').strip()
            if title or pid:
                out.append({'key': pid or title, 'id': pid, 'title': title or pid, 'price': price, 'moq': moq})
    elif doc.get('productAction') or doc.get('moq'):
        pa = doc.get('productAction') if isinstance(doc.get('productAction'), dict) else {}
        ti = pa.get('traceInfo') if isinstance(pa.get('traceInfo'), dict) else {}
        ap = pa.get('actionParams') if isinstance(pa.get('actionParams'), dict) else {}
        pid = str(ti.get('productId') or doc.get('productId') or '').strip()
        url_tail = str(ap.get('url') or '').split('/')[-1].replace('.html', '').replace('-', ' ').strip()
        title = (doc.get('title') or doc.get('subject') or url_tail).strip()
        price = str(doc.get('price') or doc.get('displayPrice') or '').strip()
        moq = str(doc.get('moq') or '').strip()
        if title or pid:
            out.append({'key': pid or title, 'id': pid, 'title': title or pid, 'price': price, 'moq': moq})
    return out


def _extract_card_countries(payload_str, cid, uid_to_country, cid_to_country):
    """从卡片 JSON 中提取真实国家代码：支持单客户卡片 countryFlag 及沉默提醒卡片 items[].buyerAliId+country。"""
    if not payload_str:
        return
    try:
        doc = json.loads(payload_str)
    except Exception:
        return
    if not isinstance(doc, dict):
        return
    # 1. 客户卡片本身的 countryFlag / country
    for k in ('countryFlag', 'country', 'buyerCountry', 'countryCode'):
        val = doc.get(k)
        if isinstance(val, str) and val.strip():
            code = _norm_country(val)
            if code and cid and cid not in cid_to_country:
                cid_to_country[cid] = code
    # 2. 阿里卖家系统推送的买家列表卡片（含 buyerAliId + country 国旗）
    items = doc.get('items')
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            code = _norm_country(it.get('country') or it.get('countryFlag') or '')
            if not code:
                continue
            fa = it.get('followNowAction') if isinstance(it.get('followNowAction'), dict) else {}
            ap = fa.get('actionParams') if isinstance(fa.get('actionParams'), dict) else {}
            buyer_id = str(ap.get('buyerAliId') or it.get('buyerAliId') or '').strip()
            if buyer_id:
                uid_to_country[buyer_id] = code


def _tokenize_title(title):
    words = re.findall(r'[A-Za-z][A-Za-z0-9-]{2,}', title or '')
    return [w.lower() for w in words if w.lower() not in STOP_WORDS and not w.isdigit()]


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
        """SELECT user_id, cid, uid, display_name, extra, stage, tags, note, updated_at
           FROM customers WHERE (?='' OR user_id=?)""",
        (user or '', user or '')
    )
    customers = {}
    uid_to_country = {}
    cid_to_country = {}

    for r in cust_rows:
        tags = []
        extra = {}
        if r['tags']:
            try:
                parsed = json.loads(r['tags'])
                if isinstance(parsed, list):
                    tags = parsed
            except Exception:
                pass
        if r['extra']:
            try:
                parsed_ex = json.loads(r['extra'])
                if isinstance(parsed_ex, dict):
                    extra = parsed_ex
            except Exception:
                pass
        country = _norm_country(extra.get('country') or extra.get('countryCode') or extra.get('countryIcon') or '')
        if country:
            cid_to_country[r['cid']] = country
            if r['uid']:
                uid_to_country[r['uid']] = country
        customers[(r['user_id'], r['cid'])] = {
            'uid': r['uid'] or '',
            'display_name': r['display_name'] or '',
            'company': extra.get('companyName') or '',
            'country': country,
            'stage': r['stage'] or '',
            'tags': tags,
            'note': r['note'] or '',
            'updated_at': r['updated_at'],
        }

    # 第一遍：先从全部卡片里收集地域映射（确保历史上出现过的国旗信息也能关联到对应客户）
    for r in rows:
        if r['kind'] == 'card' and r['payload']:
            _extract_card_countries(r['payload'], r['cid'], uid_to_country, cid_to_country)

    threads = {}
    in_msgs = 0
    out_msgs = 0
    hours = [0] * 24
    products = {}
    accounts = {}
    response_deltas = []
    cid_products = {}

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
                    if 0 <= delta <= 86400:
                        response_deltas.append(delta)
                        acc['deltas'].append(delta)
                t['pending_in_ts'] = None

            if r['kind'] == 'card' and r['payload']:
                for pr in _extract_products(r['payload']):
                    p_entry = products.setdefault(pr['key'], {
                        'id': pr['id'], 'title': pr['title'], 'price': pr['price'], 'moq': pr['moq'],
                        'count': 0, 'cids': set(), 'countries': set()
                    })
                    p_entry['count'] += 1
                    p_entry['cids'].add(cid)
                    if not p_entry['price'] and pr['price']:
                        p_entry['price'] = pr['price']
                    if not p_entry['moq'] and pr['moq']:
                        p_entry['moq'] = pr['moq']
                    cid_products.setdefault(cid, set()).add(pr['title'])

    # 解析每个会话的最终归属国家
    def resolve_country(key_tuple):
        u_id, c_id = key_tuple
        cinfo = customers.get(key_tuple) or {}
        if cinfo.get('country'):
            return cinfo['country']
        if c_id in cid_to_country:
            return cid_to_country[c_id]
        peer_uid = cinfo.get('uid') or _peer_fallback(c_id, u_id)
        if peer_uid in uid_to_country:
            return uid_to_country[peer_uid]
        return ''

    urgent_unreplied = []
    silent_followups = []
    stage_counts = {s: 0 for s in STAGES}
    stage_counts['未标记'] = 0

    country_stats = {}
    region_stats = {}
    country_product_map = {}

    for key, t in threads.items():
        cinfo = customers.get(key) or {}
        stage = cinfo.get('stage') or '未标记'
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        peer = cinfo.get('display_name') or _peer_fallback(t['cid'], t['user_id'])
        wait_sec = max(0, now_ts - t['last_ts']) if t['last_ts'] else 0
        country_code = resolve_country(key)

        if country_code:
            cname, rname = _country_label(country_code)
            cs = country_stats.setdefault(country_code, {'code': country_code, 'name': cname, 'region': rname, 'buyers': 0, 'messages': 0})
            cs['buyers'] += 1
            cs['messages'] += t['in_count'] + t['out_count']
            region_stats[rname] = region_stats.get(rname, 0) + 1
            for ptitle in cid_products.get(t['cid'], ()):
                cp = country_product_map.setdefault(country_code, {'country': cname, 'region': rname, 'products': {}})
                cp['products'][ptitle] = cp['products'].get(ptitle, 0) + 1

        if t['last_dir'] == 'in':
            if t['user_id'] in accounts:
                accounts[t['user_id']]['unreplied'] += 1
            urgent_unreplied.append({
                'user_id': t['user_id'], 'cid': t['cid'], 'peer': peer,
                'country': _country_label(country_code)[0] if country_code else '',
                'stage': cinfo.get('stage') or '',
                'wait_minutes': int(wait_sec // 60),
                'last_ts': t['last_ts'], 'last_content': t['last_content'],
            })
        elif t['last_dir'] == 'out' and wait_sec >= 86400 and stage not in ('已成交', '冷淡'):
            silent_followups.append({
                'user_id': t['user_id'], 'cid': t['cid'], 'peer': peer,
                'country': _country_label(country_code)[0] if country_code else '',
                'stage': cinfo.get('stage') or '',
                'wait_hours': round(wait_sec / 3600.0, 1),
                'last_ts': t['last_ts'], 'last_content': t['last_content'],
            })

    # 把国家关联回产品 + 统计产品热词
    cid_to_resolved_country = {t['cid']: resolve_country(k) for k, t in threads.items()}
    keyword_counts = {}
    for p in products.values():
        for c_id in p['cids']:
            cc = cid_to_resolved_country.get(c_id)
            if cc:
                p['countries'].add(_country_label(cc)[0])
        for w in set(_tokenize_title(p['title'])):
            keyword_counts[w] = keyword_counts.get(w, 0) + len(p['cids'])

    top_keywords = [
        {'word': w, 'buyers': c}
        for w, c in sorted(keyword_counts.items(), key=lambda x: (-x[1], x[0]))[:16]
    ]

    top_products = [
        {
            'id': v['id'], 'title': v['title'], 'price': v['price'], 'moq': v['moq'],
            'buyers': len(v['cids']), 'mentions': v['count'],
            'countries': sorted(v['countries'])[:4]
        }
        for v in sorted(products.values(), key=lambda x: (-len(x['cids']), -x['count']))[:15]
    ]

    countries_ranked = sorted(country_stats.values(), key=lambda x: (-x['buyers'], -x['messages']))[:15]
    regions_ranked = [{'region': k, 'buyers': v} for k, v in sorted(region_stats.items(), key=lambda x: -x[1])]

    country_products = []
    for code, entry in sorted(country_product_map.items(), key=lambda kv: -sum(kv[1]['products'].values()))[:10]:
        top_p = sorted(entry['products'].items(), key=lambda x: -x[1])[:3]
        country_products.append({
            'country': entry['country'], 'region': entry['region'],
            'products': [{'title': t, 'buyers': n} for t, n in top_p]
        })

    urgent_unreplied.sort(key=lambda x: -x['last_ts'])
    silent_followups.sort(key=lambda x: -x['wait_hours'])

    avg_resp_min = round((sum(response_deltas) / len(response_deltas)) / 60.0, 1) if response_deltas else None
    within_1h = round(100.0 * sum(1 for d in response_deltas if d <= 3600) / len(response_deltas), 1) if response_deltas else None

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
            'identified_country_customers': sum(c['buyers'] for c in country_stats.values()),
            'unique_countries': len(country_stats),
            'unique_products': len(products),
            'in_messages': in_msgs,
            'out_messages': out_msgs,
            'unreplied_count': len(urgent_unreplied),
            'silent_followup_count': len(silent_followups),
            'avg_response_minutes': avg_resp_min,
            'within_1h_rate': within_1h,
            'response_samples': len(response_deltas),
        },
        'regions': regions_ranked,
        'countries': countries_ranked,
        'top_products': top_products,
        'top_keywords': top_keywords,
        'country_products': country_products,
        'stages': [{'stage': k, 'count': v} for k, v in stage_counts.items() if v > 0 or k in STAGES],
        'hours': hours,
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
    uid_to_country = {}
    cid_to_country = {}
    for r in store.q("SELECT * FROM customers WHERE (?='' OR user_id=?)", (user or '', user or '')):
        d = dict(r)
        extra = {}
        if d.get('extra'):
            try:
                extra = json.loads(d['extra']) or {}
            except Exception:
                pass
        d['_extra'] = extra
        cc = _norm_country(extra.get('country') or extra.get('countryCode') or extra.get('countryIcon') or '')
        if cc:
            cid_to_country[d['cid']] = cc
            if d.get('uid'):
                uid_to_country[d['uid']] = cc
        cust_map[(d['user_id'], d['cid'])] = d

    cid_prods = {}
    for r in store.q("SELECT cid, payload FROM messages WHERE kind='card' AND payload IS NOT NULL"):
        _extract_card_countries(r['payload'], r['cid'], uid_to_country, cid_to_country)
        for pr in _extract_products(r['payload']):
            cid_prods.setdefault(r['cid'], set()).add(pr['title'])

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
        '客户名称', '国家/地区', '大区', '公司名称', '买家UID', '卖家账号', '会话CID',
        '意向阶段', '标签', '备注', '咨询商品', '跟进状态',
        '累计消息', '买家消息', '我方回复', '首次互动时间', '最近消息时间',
        'AI当前概要', 'AI建议跟进'
    ])
    for t in threads:
        key = (t['user_id'], t['cid'])
        c = cust_map.get(key) or {}
        extra = c.get('_extra') or {}
        rep = rep_map.get(key) or {}
        peer = c.get('display_name') or _peer_fallback(t['cid'], t['user_id'])
        uid = c.get('uid') or _peer_fallback(t['cid'], t['user_id'])
        cc = cid_to_country.get(t['cid']) or uid_to_country.get(uid) or ''
        cname, rname = _country_label(cc) if cc else ('', '')
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
        prods_txt = ' | '.join(sorted(cid_prods.get(t['cid'], ()))[:5])
        writer.writerow([
            peer, cname, rname, extra.get('companyName') or '', uid, t['user_id'], t['cid'],
            c.get('stage') or '', tags, c.get('note') or '', prods_txt, status,
            t['total_msgs'], t['in_msgs'], t['out_msgs'],
            _fmt_ts(t['first_ts']), _fmt_ts(t['last_ts']),
            rep.get('summary') or '', rep.get('next_step') or ''
        ])
    return ('\ufeff' + buf.getvalue()).encode('utf-8')
