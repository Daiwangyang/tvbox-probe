#!/usr/bin/env python3
"""丢一个 my.json 给影视仓,再把「哪个源能用」用表格看明白。

启动后在浏览器打开 http://127.0.0.1:5678/ 就是面板:
- 顶部是你唯一要填进影视仓的地址
- 下面是每个站点的延迟、状态、是否广告解析
- 点「重测」会重新跑一遍并刷新 /my.json,影视仓那边不用改动

投影仪和电脑在同一个局域网的话,把电脑 IP 加进去,投影直接用
http://<你的IP>:5678/my.json,地址永远不变。
"""
from __future__ import annotations

import html
import os
import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
MY = ROOT / "my.json"
REPORT = ROOT / "report.md"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 5678

_state = {"running": False, "last": "", "log": []}


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def run_probe() -> None:
    _state["running"] = True
    _state["log"] = []
    try:
        p = subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "probe.py")],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        for line in p.stdout:
            _state["log"].append(line.rstrip())
            _state["log"] = _state["log"][-200:]
        p.wait()
        _state["last"] = time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        _state["log"].append(f"执行失败: {e}")
    finally:
        _state["running"] = False


def sites() -> list[dict]:
    if not MY.exists():
        return []
    try:
        return json.loads(MY.read_text(encoding="utf-8")).get("sites") or []
    except Exception:
        return []


def lat() -> dict[str, int]:
    """从 report.md 恶读延迟,只为了在面板上显示。"""
    out: dict[str, int] = {}
    if not REPORT.exists():
        return out
    for ln in REPORT.read_text(encoding="utf-8").splitlines():
        if not ln.startswith("|") or "ms" not in ln:
            continue
        parts = [p.strip() for p in ln.strip("|").split("|")]
        if len(parts) < 3:
            continue
        try:
            ms = int(parts[0].replace("ms", ""))
        except ValueError:
            continue
        api = parts[2].strip("`")
        out[api] = ms
    return out


PAGE = """<!doctype html><meta charset="utf-8">
<title>影视仓配置</title>
<style>
*{box-sizing:border-box}
body{margin:0;padding:28px;font:14px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;
 color:#1b1b1b;background:#f6f5f2}
h1{font-size:17px;font-weight:500;margin:0 0 4px}
.sub{color:#6b6a66;font-size:13px;margin-bottom:20px}
.card{background:#fff;border:1px solid #e2e0da;border-radius:12px;padding:16px 18px;
 margin-bottom:16px}
.lbl{font-size:12px;color:#6b6a66;margin-bottom:6px}
.url{font:13px/1.5 ui-monospace,Consolas,monospace;background:#eef4fc;color:#0c447c;
 border:1px solid #b5d4f4;border-radius:8px;padding:10px 12px;word-break:break-all}
.btn{border:1px solid #d3d1c7;background:#fff;border-radius:8px;padding:7px 14px;
 font-size:13px;cursor:pointer}
.btn:hover{background:#f1efea}
.btn:disabled{color:#b4b2a9;cursor:default}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-weight:500;color:#6b6a66;font-size:12px;
 padding:6px 8px;border-bottom:1px solid #e2e0da}
td{padding:6px 8px;border-bottom:1px solid #f1efea}
td.api{font:12px ui-monospace,Consolas,monospace;color:#6b6a66;word-break:break-all}
.f{display:inline-block;min-width:52px;padding:1px 7px;border-radius:5px;font-size:12px}
.ok{background:#eaf3de;color:#3b6d11}
.mid{background:#faeeda;color:#854f0b}
.bad{background:#fcebeb;color:#a32d2d}
.keep{background:#f1efea;color:#6b6a66}
pre{background:#2c2c2a;color:#d3d1c7;padding:12px;border-radius:8px;font-size:12px;
 overflow:auto;max-height:220px;margin:0}
</style>
<h1>影视仓配置</h1>
<div class="sub">下面这个地址填进影视仓的「配置地址」,用一次就不用再换。</div>

<div class="card">
 <div class="lbl">局域网地址(投影仪和电脑需在同一 WiFi)</div>
 <div class="url">__LAN__</div>
 <div class="lbl" style="margin-top:12px">本机地址</div>
 <div class="url">__LOCAL__</div>
</div>

<div class="card">
 <div class="lbl">共 __N__ 个源 · 上次更新 __LAST__</div>
 <button class="btn" onclick="go()" __DIS__>__BTN__</button>
 <span id="hint" class="sub" style="margin:0 0 0 10px"></span>
</div>

<div class="card">
<table><thead><tr><th>延迟</th><th>站点</th><th>接口</th></tr></thead>
<tbody>__ROWS__</tbody></table>
</div>

<pre id="log">__LOG__</pre>

<script>
async function go(){
  document.querySelector('.btn').disabled=true;
  document.getElementById('hint').textContent='重测中,约 2-4 分钟…';
  await fetch('/refresh',{method:'POST'});
  const t=setInterval(async()=>{
    const r=await fetch('/status'); const j=await r.json();
    document.getElementById('log').textContent=j.log.join('\\n');
    if(!j.running){clearInterval(t);location.reload();}
  },2000);
}
</script>
"""


def render() -> bytes:
    rows = []
    ms_map = lat()
    for s in sites():
        ms = ms_map.get(s["api"], -1)
        if ms <= 0:
            cls, label = "keep", "保留"
        elif ms <= 800:
            cls, label = "ok", f"{ms}ms"
        elif ms <= 1500:
            cls, label = "mid", f"{ms}ms"
        else:
            cls, label = "bad", f"{ms}ms"
        rows.append(
            f'<tr><td><span class="f {cls}">{label}</span></td>'
            f'<td>{html.escape(s.get("name", ""))}</td>'
            f'<td class="api">{html.escape(s.get("api", ""))}</td></tr>'
        )
    ip = lan_ip()
    page = (
        PAGE.replace("__LAN__", f"http://{ip}:{PORT}/my.json")
        .replace("__LOCAL__", f"http://127.0.0.1:{PORT}/my.json")
        .replace("__N__", str(len(rows)))
        .replace("__LAST__", _state["last"] or "尚未运行")
        .replace("__BTN__", "重测中…" if _state["running"] else "重测全部源")
        .replace("__DIS__", "disabled" if _state["running"] else "")
        .replace("__ROWS__", "".join(rows) or '<tr><td colspan="3">还没有数据,点重测</td></tr>')
        .replace("__LOG__", html.escape("\n".join(_state["log"][-40:])))
    )
    return page.encode("utf-8")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, render(), "text/html; charset=utf-8")
        if path == "/my.json":
            if not MY.exists():
                return self._send(404, b"{}", "application/json")
            return self._send(200, MY.read_bytes(), "application/json; charset=utf-8")
        if path == "/status":
            body = json.dumps({"running": _state["running"], "log": _state["log"][-60:]}).encode()
            return self._send(200, body, "application/json")
        return self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if urlparse(self.path).path == "/refresh":
            if not _state["running"]:
                threading.Thread(target=run_probe, daemon=True).start()
            return self._send(200, b'{"ok":1}', "application/json")
        return self._send(404, b"not found", "text/plain")


if __name__ == "__main__":
    print(f"面板  http://127.0.0.1:{PORT}/")
    print(f"配置  http://{lan_ip()}:{PORT}/my.json   ← 填这个到影视仓")
    print("Ctrl+C 停止")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
