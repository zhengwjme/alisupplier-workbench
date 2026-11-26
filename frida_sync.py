#!/usr/bin/env python3
"""将阿里卖家客户端的点击买家与客户资料实时同步到 AliSupplier 工作台。"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

EVT_PREFIX = '__ALISUPPLIER_EVT__'
HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_JS = os.path.join(HERE, 'frida_agent.js')


def default_runtime_file():
    if sys.platform == 'darwin':
        base = os.path.expanduser('~/Library/Application Support/AliSupplierHook')
    elif os.name == 'nt':
        base = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'AliSupplierHook')
    else:
        base = os.path.expanduser('~/.local/share/AliSupplierHook')
    return os.path.join(base, 'runtime.json')


def find_frida_bin():
    exe = 'frida.exe' if os.name == 'nt' else 'frida'
    for candidate in (shutil.which('frida'), os.path.expanduser(os.path.join('~', '.local', 'bin', exe))):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def find_target_pid():
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


def load_workbench_endpoint(runtime_file, explicit_port):
    if os.path.isfile(runtime_file):
        try:
            with open(runtime_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            port = int(data.get('port') or explicit_port or 8770)
            return port, data.get('token')
        except Exception:
            pass
    return int(explicit_port or 8770), None


def post_active_chat(runtime_file, explicit_port, payload):
    port, token = load_workbench_endpoint(runtime_file, explicit_port)
    url = 'http://127.0.0.1:%d/api/workbench/active-chat' % port
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['X-AliSupplier-Desktop'] = token
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers=headers,
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        print('[sync] HTTP %s: %s' % (e.code, e.read().decode('utf-8', 'replace')), file=sys.stderr)
    except Exception as e:
        print('[sync] 推送失败（请确认工作台服务已启动）: %s' % e, file=sys.stderr)
    return None


def main():
    ap = argparse.ArgumentParser(description='Frida 联动：阿里卖家点击买家 -> 工作台打开聊天详情')
    ap.add_argument('--port', type=int, default=int(os.environ.get('HOOK_PORT', '8770')))
    ap.add_argument('--runtime', default=default_runtime_file())
    ap.add_argument('--pid', type=int, default=None, help='手动指定阿里卖家 PID')
    args = ap.parse_args()

    frida_bin = find_frida_bin()
    if not frida_bin:
        raise SystemExit('未找到 frida CLI，请确认已安装 ~/.local/bin/frida')
    if not os.path.isfile(AGENT_JS):
        raise SystemExit('缺少脚本: %s' % AGENT_JS)

    print('[sync] 使用 Frida: %s' % frida_bin)
    while True:
        pid = args.pid or find_target_pid()
        if not pid:
            print('[sync] 等待「阿里卖家」启动...')
            time.sleep(2)
            continue

        print('[sync] 正在附加到阿里卖家 (PID %d)...' % pid)
        cmd = [frida_bin, '-p', str(pid), '-l', AGENT_JS]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        try:
            for line in proc.stdout:
                line = line.rstrip('\r\n')
                idx = line.find(EVT_PREFIX)
                if idx == -1:
                    continue
                try:
                    evt = json.loads(line[idx + len(EVT_PREFIX):])
                except Exception:
                    continue
                if evt.get('event') == 'ready':
                    print('[sync] 已就绪，请在「阿里卖家」点击任意买家进行联动测试（Ctrl+C 退出）。')
                elif evt.get('event') == 'active_chat':
                    name = evt.get('display_name') or evt.get('uid') or evt.get('cid')
                    print('[sync] 点击买家: %s (%s)' % (name, evt.get('cid')))
                    res = post_active_chat(args.runtime, args.port, evt)
                    if res and res.get('ok'):
                        print('[sync] 已通知工作台打开详情: %s' % res.get('peer'))
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait(timeout=3)
            print('\n[sync] 已退出。')
            return
        code = proc.wait()
        if args.pid:
            raise SystemExit(code)
        print('[sync] 连接已断开 (code=%s)，2 秒后重试...' % code)
        time.sleep(2)


if __name__ == '__main__':
    main()
