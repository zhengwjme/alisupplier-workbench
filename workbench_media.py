"""Extract card fields and analyze safely associated images on demand."""
import hashlib
import html
import json
import os
import shutil
import subprocess
import time
from urllib.parse import urlsplit
from urllib.request import Request

from ai_gateway import AIError, OPENER, request_model


MAX_IMAGE = 6 * 1024 * 1024
CDN_HOSTS = ('alicdn.com', 'alibaba.com', 'alibabausercontent.com', 'aliimg.com')
IMAGE_PROMPT = '只描述图中可见内容，保留可读的型号、尺寸、数字和文字。不确定或模糊请标明，不推断客户已确认。用中文简短说明。'


def card_payload(value):
    try:
        doc = json.loads(value or '{}')
        if isinstance(doc, dict):
            return doc
    except (TypeError, ValueError):
        pass
    return {}


def limited(value, length=700):
    return html.unescape(str(value or ''))[:length]


def fields(doc, names):
    return {label: limited(doc[key]) for key, label in names if doc.get(key) not in (None, '')}


def extract_card(payload):
    card = card_payload(payload)
    data = fields(card, (('inquiryID', '询盘ID'), ('title', '卡片标题'), ('content', '客户留言'),
                         ('price', '卡片标价'), ('moq', '起订量'), ('quantity', '采购数量')))
    products = card.get('products')
    if isinstance(products, list):
        data['商品'] = [fields(product, (('productId', '商品ID'), ('productName', '商品名'),
                                          ('productModel', '型号'), ('productUnitPrice', '卡片标价'),
                                          ('productUnit', '单位'), ('productCurrency', '币种'),
                                          ('productMOQ', '起订量'), ('quantity', '采购数量')))
                      for product in products if isinstance(product, dict)]
    attrs = card.get('attributes')
    if isinstance(attrs, list):
        data['属性'] = [fields(attr, (('attrName', '名称'), ('attrValue', '内容')))
                      for attr in attrs if isinstance(attr, dict)]
    return data


def image_mime(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    return None


class Media:
    def __init__(self, workbench, root):
        self.workbench = workbench
        self.root = os.path.realpath(root)
        self.ocr = shutil.which('tesseract')

    @staticmethod
    def remote(path):
        try:
            parsed = urlsplit(path)
            host = (parsed.hostname or '').lower()
            return (parsed.scheme == 'https' and not parsed.username and not parsed.password
                    and parsed.port in (None, 443) and
                    any(host == item or host.endswith('.' + item) for item in CDN_HOSTS))
        except ValueError:
            return False

    def local(self, path):
        real = os.path.realpath(path)
        if real.startswith(self.root + os.sep) and os.path.isfile(real):
            return real
        return None

    def preview_url(self, message):
        path = message.get('media') or ''
        if (message.get('kind') == 'image' or message.get('content_type') == 'image') and path:
            if self.remote(path) or self.local(path):
                return '/api/workbench/image?id=' + str(message['id'])
        return None

    def read(self, path, timeout=5):
        try:
            if self.remote(path):
                request = Request(path, headers={'User-Agent': 'Mozilla/5.0',
                                                 'Referer': 'https://message.alibaba.com/'})
                with OPENER.open(request, timeout=timeout) as response:
                    data = response.read(MAX_IMAGE + 1)
            else:
                filename = self.local(path)
                if not filename:
                    return None
                with open(filename, 'rb') as file:
                    data = file.read(MAX_IMAGE + 1)
            mime = image_mime(data)
            if mime and len(data) <= MAX_IMAGE:
                return mime, data
        except (OSError, ValueError):
            pass
        return None

    def cached(self, key):
        rows = self.workbench.store.q('SELECT body FROM wb_media_cache WHERE cache_key=?', (key,))
        return rows[0]['body'] if rows else None

    def save(self, key, text):
        self.workbench.store.x('INSERT OR IGNORE INTO wb_media_cache(cache_key,body) VALUES(?,?)', (key, text))

    def analyze(self, message, settings, deadline):
        path = message.get('media')
        if not path:
            return None, '图片不可用或归属待核对'
        # Old versions may have attached local files to recognized message IDs
        # using only a timestamp. Keep those files available for manual review.
        if not self.remote(path) and message.get('source') != 'log-image-linked':
            return None, '图片归属待核对'
        if not settings['ocr'] and not settings['vision']:
            return None, '图片识别未启用'
        remaining = deadline - time.monotonic()
        if remaining < 2:
            return None, '图片待识别'
        image = self.read(path, timeout=min(4, remaining))
        if not image:
            return None, '原图不可用或超过大小限制'
        digest = hashlib.sha256(image[1]).hexdigest()
        extracted = None
        if settings['ocr'] and self.ocr:
            ocr_key = 'ocr:1:' + digest
            extracted = self.cached(ocr_key)
            if extracted is None:
                try:
                    remaining = deadline - time.monotonic()
                    if remaining < 1:
                        return None, '图片待识别'
                    languages = subprocess.run(
                        [self.ocr, '--list-langs'], capture_output=True, timeout=min(2, remaining),
                        check=True).stdout.decode('utf-8', 'replace').splitlines()
                    selected = '+'.join(lang for lang in ('eng', 'chi_sim', 'chi_tra') if lang in languages)
                    remaining = deadline - time.monotonic()
                    if remaining < 1:
                        return None, '图片待识别'
                    cmd = [self.ocr, 'stdin', 'stdout', '--psm', '11']
                    if selected:
                        cmd.extend(('-l', selected))
                    text = subprocess.run(cmd, input=image[1], capture_output=True,
                                          timeout=min(8, remaining), check=True)
                    extracted = text.stdout.decode('utf-8', 'replace').strip()[:1400]
                    self.save(ocr_key, extracted)
                except (OSError, subprocess.SubprocessError):
                    pass
            if extracted and sum(ch.isalnum() for ch in extracted) >= 16:
                return 'OCR：' + extracted, None

        profile = next((p for p in settings['profiles'] if p['id'] == settings['image_profile']), None)
        if settings['vision'] and profile and (settings['allow_external_images'] or
                urlsplit(profile['base_url']).hostname in ('localhost', '127.0.0.1', '::1')):
            cache_key = 'vision:1:' + hashlib.sha256(
                (digest + profile['protocol'] + profile['base_url'] + profile['model']).encode()).hexdigest()
            found = self.cached(cache_key)
            if found:
                return '看图模型：' + found, None
            remaining = deadline - time.monotonic()
            if remaining >= 2 and self.workbench.reserve_image(settings['image_budget']):
                try:
                    found, _ = request_model(profile, IMAGE_PROMPT, '请读图。', 400,
                                             timeout=min(remaining, profile['timeout']), image=image)
                    self.save(cache_key, found[:1400])
                    return '看图模型：' + found[:1400], None
                except AIError:
                    return ('OCR：' + extracted if extracted else None), '看图失败，需核对'
        if extracted:
            return 'OCR（可能不完整）：' + extracted, '识别内容较少，需核对'
        if settings['ocr'] and not self.ocr:
            return None, '本机未找到 Tesseract'
        return None, '图片需查看'


def message_input(message, media=None, settings=None, deadline=None):
    kind = message.get('kind') or message.get('content_type')
    extra = message.get('content_type')
    data = {'id': message['id'], '发言方': {'in': '客户', 'out': '我方'}.get(
        message.get('direction'), '未知'), '时间': message.get('msg_time') or message.get('captured_at')}
    if kind == 'card' or extra == 'card':
        card = extract_card(message.get('payload'))
        # Keep large cards within the request budget while retaining each product in the source card.
        data['卡片'] = limited(json.dumps(card, ensure_ascii=False, separators=(',', ':')), 2400) if card else '卡片待补全'
        if not card:
            data['attachment_pending'] = '卡片待补全'
    elif kind == 'image' or extra == 'image':
        if message.get('image_note'):
            data['图片人工核对'] = limited(message['image_note'])
        elif media:
            result, status = media.analyze(message, settings, deadline)
            if result:
                data['图片识别（线索）'] = result
            if status:
                data['attachment_pending'] = status
        else:
            data['图片'] = '图片消息'
    else:
        data['内容'] = limited(message.get('content'), 2400) or '[' + (extra or '消息') + ']'
    return data
