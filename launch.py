#!/usr/bin/python3
"""启动本地服务并在外部 Chrome 打开。不会终止其他端口进程。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:18765'

def healthy():
    try:
        with urllib.request.urlopen(URL + '/api/state', timeout=1) as response:
            return json.load(response).get('app_name') == '郑店长房产选题助手'
    except Exception:
        return False

def main():
    os.chdir(ROOT)
    if not healthy():
        (ROOT / 'data').mkdir(exist_ok=True)
        with (ROOT / 'data' / 'server.log').open('ab') as log:
            child = subprocess.Popen(['/usr/bin/python3', str(ROOT / 'app.py'), 'serve'], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        for _ in range(40):
            if healthy():
                break
            if child.poll() is not None:
                raise SystemExit('启动失败，请查看程序 data/server.log；没有关闭其他程序。')
            time.sleep(0.25)
        else:
            raise SystemExit('服务启动超时，请查看 data/server.log。')
    subprocess.run(['/usr/bin/open', '-a', 'Google Chrome', URL], check=True)

if __name__ == '__main__':
    main()
