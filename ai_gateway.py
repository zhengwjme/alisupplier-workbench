"""HTTP adapters shared by summaries, image analysis and connection tests."""
import base64
import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


PROTOCOLS = {
    'chat': 'OpenAI Chat Completions 兼容',
    'responses': 'OpenAI Responses',
    'anthropic': 'Anthropic Messages',
    'gemini': 'Gemini generateContent',
}
URLS = {
    'chat': 'https://api.openai.com/v1',
    'responses': 'https://api.openai.com/v1',
    'anthropic': 'https://api.anthropic.com/v1',
    'gemini': 'https://generativelanguage.googleapis.com/v1beta',
}


class AIError(Exception):
    def __init__(self, message, retry=True):
        super().__init__(message)
        self.retry = retry


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirect())


def base_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('服务地址须为 HTTP(S) 地址，不带密钥、查询参数或片段')
    if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('外部 AI 地址需要 HTTPS')
    _ = parsed.port
    return value.rstrip('/')


def request_model(profile, instruction, prompt, max_output, timeout=None, image=None):
    protocol, model, key = (profile[k] for k in ('protocol', 'model', 'key'))
    root = profile['base_url']
    headers = {'Content-Type': 'application/json'}
    if protocol in ('chat', 'responses') and key:
        headers['Authorization'] = 'Bearer ' + key

    encoded = base64.b64encode(image[1]).decode('ascii') if image else None
    data_url = 'data:' + image[0] + ';base64,' + encoded if image else None
    if protocol == 'chat':
        path = '/chat/completions'
        content = prompt
        if image:
            content = [{'type': 'text', 'text': prompt},
                       {'type': 'image_url', 'image_url': {'url': data_url}}]
        body = {'model': model, profile.get('token_field', 'max_tokens'): max_output, 'messages': [
            {'role': 'system', 'content': instruction}, {'role': 'user', 'content': content}]}
    elif protocol == 'responses':
        path = '/responses'
        content = [{'type': 'input_text', 'text': prompt}]
        if image:
            content.append({'type': 'input_image', 'image_url': data_url, 'detail': 'auto'})
        body = {'model': model, 'instructions': instruction, 'store': False,
                'max_output_tokens': max_output, 'input': [{'role': 'user', 'content': content}]}
    elif protocol == 'anthropic':
        path = '/messages'
        headers['anthropic-version'] = '2023-06-01'
        if key:
            headers['x-api-key'] = key
        content = [{'type': 'text', 'text': prompt}]
        if image:
            content.append({'type': 'image', 'source': {
                'type': 'base64', 'media_type': image[0], 'data': encoded}})
        body = {'model': model, 'system': instruction, 'max_tokens': max_output,
                'messages': [{'role': 'user', 'content': content}]}
    else:
        path = '/models/' + quote(model[7:] if model.startswith('models/') else model, safe='') + ':generateContent'
        if key:
            headers['x-goog-api-key'] = key
        parts = [{'text': prompt}]
        if image:
            parts.append({'inlineData': {'mimeType': image[0], 'data': encoded}})
        body = {'systemInstruction': {'parts': [{'text': instruction}]},
                'contents': [{'role': 'user', 'parts': parts}],
                'generationConfig': {'maxOutputTokens': max_output}}
    endpoint = root if root.endswith(path) else root + path
    req = Request(endpoint, json.dumps(body, ensure_ascii=False).encode(), headers, method='POST')
    try:
        with OPENER.open(req, timeout=timeout or profile['timeout']) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError('AI response too large')
            data = json.loads(raw)
    except HTTPError as exc:
        status = exc.code
        exc.close()
        raise AIError('AI 服务返回 HTTP %d' % status, status in (408, 429) or status >= 500) from None
    except (URLError, TimeoutError, OSError):
        raise AIError('AI 服务连接失败或超时') from None
    except (ValueError, TypeError):
        raise AIError('AI 服务未返回有效 JSON') from None

    try:
        if protocol == 'chat':
            item = data['choices'][0]['message']['content']
            result = item if isinstance(item, str) else ''.join(p.get('text', '') for p in item)
            used = data.get('usage', {}).get('total_tokens', 0)
        elif protocol == 'responses':
            result = ''.join(p.get('text', '') for item in data.get('output', [])
                             for p in item.get('content', []) if p.get('type') == 'output_text')
            used = data.get('usage', {}).get('total_tokens', 0)
        elif protocol == 'anthropic':
            result = ''.join(p.get('text', '') for p in data.get('content', []) if p.get('type') == 'text')
            usage = data.get('usage') or {}
            used = sum(usage.get(k, 0) for k in ('input_tokens', 'output_tokens',
                                                'cache_creation_input_tokens', 'cache_read_input_tokens'))
        else:
            result = ''.join(p.get('text', '') for p in data['candidates'][0]['content']['parts']
                             if not p.get('thought'))
            used = data.get('usageMetadata', {}).get('totalTokenCount', 0)
        if not result.strip():
            raise ValueError
        return result.strip(), int(used or 0)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        raise AIError('AI 服务未返回可用内容') from None
