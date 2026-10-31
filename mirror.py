"""把工作区当前内容整体镜像到远端。

本地 git 对象在多轮 reset 后不可靠,所以不引用本地 sha,直接按文件内容重建 blob。
用法: python mirror.py <远端head_sha> <提交信息>
"""

import base64
import json
import os
import subprocess
import sys

REPO = "Daiwangyang/tvbox-probe"
SKIP_DIRS = {".git", ".jarcache", "__pycache__", "node_modules", ".workbuddy"}
SKIP_FILES = {"serve.log"}
SKIP_EXT = {".log", ".pyc", ".zip"}


def gh(*args: str, inp: str | None = None):
    cmd = ["gh", "api", *args] + (["--input", "-"] if inp is not None else [])
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", input=inp)
    if r.returncode:
        sys.stderr.write(f"FAIL {args[0]}\n{r.stderr[:300]}\n")
        raise SystemExit(1)
    return r.stdout


remote_head, msg = sys.argv[1], sys.argv[2]

entries = []
for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for fn in files:
        p = os.path.relpath(os.path.join(root, fn), ".").replace("\\", "/")
        if fn in SKIP_FILES or os.path.splitext(fn)[1] in SKIP_EXT:
            continue
        raw = open(p, "rb").read()
        sha = json.loads(gh(
            f"repos/{REPO}/git/blobs", "--method", "POST",
            inp=json.dumps({"content": base64.b64encode(raw).decode(), "encoding": "base64"}),
        ))["sha"]
        entries.append({"path": p, "mode": "100644", "type": "blob", "sha": sha})

print(f"共 {len(entries)} 个文件")
tree = json.loads(gh(
    f"repos/{REPO}/git/trees", "--method", "POST",
    inp=json.dumps({"tree": entries}),
))["sha"]
commit = json.loads(gh(
    f"repos/{REPO}/git/commits", "--method", "POST",
    inp=json.dumps({"message": msg, "tree": tree, "parents": [remote_head]}),
))["sha"]
gh(
    f"repos/{REPO}/git/refs/heads/main", "--method", "PATCH",
    inp=json.dumps({"sha": commit, "force": False}),
)
print(f"pushed {commit[:8]} tree={tree[:8]}")
