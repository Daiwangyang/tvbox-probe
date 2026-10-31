"""经 GitHub Trees API 整体推送本地 HEAD 的树,绕开连不上的 git 传输通道。

用法: python push_tree.py <远端head_sha> <提交信息>
"""

import json
import subprocess
import sys

REPO = "Daiwangyang/tvbox-probe"


def gh(*args: str):
    r = subprocess.run(["gh", "api", *args], capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        sys.stderr.write(f"FAIL {args[0]}\n{r.stderr[:300]}\n")
        raise SystemExit(1)
    return r.stdout


remote_head, msg = sys.argv[1], sys.argv[2]
local_tree = subprocess.run(
    ["git", "rev-parse", "HEAD^{tree}"], capture_output=True, text=True
).stdout.strip()

c = json.loads(gh(
    f"repos/{REPO}/git/commits", "--method", "POST",
    "-f", f"message={msg}", "-f", f"tree={local_tree}", "-f", f"parents[]={remote_head}",
))["sha"]
gh(f"repos/{REPO}/git/refs/heads/main", "--method", "PATCH", "-f", f"sha={c}")
print(f"pushed {c[:8]} tree={local_tree[:8]}")
