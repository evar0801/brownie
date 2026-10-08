"""git 統合の入口 wt.py の呼び口のスタブ（試験用・トークン0）。本物（~/.claude/tools/wt.py）の代わりに NIGHT_WT_PY で差し込む。

    python tests/wt_stub.py finish <project> <branch> [--gate "<cmd>"] [--gate-timeout 1800] [--keep] [--discard] [--session ID]

呼び口の約束（DECISIONS [E-045]）のうち、ブラウニーから見える所を真似る:
  ①作業コピーに未コミットがあれば 20 ②ブランチが HANDOFF §0 を書いていれば 20
  ③作業コピーの中で本流を取り込む（衝突なら取り込みを取り消して 10。本体・ブランチ・作業コピーは残す）
  ⑥--gate を作業コピーで実行（環境変数 WT_PROJECT・WT_WORKTREE・WT_BRANCH・WT_BASE）。失敗・時間切れは 20
  ⑧本体は早送りだけ ⑨作業コピーを外しブランチを消す（--keep なら残す）
  ④-NEW の振り直し・⑤§0 への差し込み・⑦本体の使用中の判定はしない（台本で 30 と renumbered・shifted を出せる）。
標準出力の最後の1行は `WT-REPORT ` + ASCII の JSON。終了コードも同じ code。

台本（試験が決める）: 環境変数 WT_STUB_SCRIPT に JSON ファイル。キーはブランチ名に含まれる文字列、値は
  "0"（本来の流れ）/ "20" / "2" / "10"（本当の衝突が無くても 10）/ "hang"（眠り続ける）
  / "30"（①〜⑥を済ませてから 30。本物と同じく関門は毎回流れる）/ "30*"（いつも 30）/ "30,0"（1回目 30、2回目から本来の流れ）
  / {"codes": "0", "renumbered": [...], "shifted": [...], "reason": "…", "human": {"at": 1, "files": {"shared.txt": "human\n"}}}
  human は「at 回目の呼び出しの最初に、人が本体でそのファイルを書き換えてコミットした」を真似る（衝突の試験用。
  ブラウニーの作業セッションの最中に本体を書き換えると、ブラウニーは「作業コピーの外への変更」として保留にするため）。
呼び出しの記録: WT_STUB_LOG（jsonl。1回の呼び出しで start と end の2行。pid・code・関門を流したか）。
"""
import argparse, json, os, re, subprocess, sys, time

sys.stdout.reconfigure(encoding="utf-8")


def git(args, cwd):
    return subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def note(row):
    path = os.environ.get("WT_STUB_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(row, pid=os.getpid(), at=time.time()), ensure_ascii=False) + "\n")


def kill_tree(p):
    if p.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        try:
            p.kill()
        except OSError:
            pass


ap = argparse.ArgumentParser(prog="wt_stub.py")
sub = ap.add_subparsers(dest="cmd", required=True)
f = sub.add_parser("finish", help="ブランチを本流へ入れる（スタブ）")
f.add_argument("project")
f.add_argument("branch")
f.add_argument("--gate", default=None, help="作業コピーで流す検査のコマンド")
f.add_argument("--gate-timeout", type=int, default=1800, help="検査の持ち時間（秒）")
f.add_argument("--keep", action="store_true")
f.add_argument("--records-later", action="store_true", help="受け取るだけ（本体の使用中の判定を真似ないので、記録だけ後から入れる動きも真似ない）")
f.add_argument("--discard", action="store_true")
f.add_argument("--session", default=None)
f.add_argument("--prefix", default="claude")
a = ap.parse_args()

project, branch = os.path.abspath(a.project), a.branch
state = {"gate_ran": False}


def report(code, status, reason="", **kw):
    head = git(["rev-parse", "HEAD"], project).stdout.strip()
    d = {"code": code, "status": status, "project": project, "branch": branch, "head": head,
         "renumbered": [], "shifted": [], "files": [], "reason": reason}
    d.update(kw)
    note({"event": "end", "branch": branch, "code": code, "status": status, "gate_ran": state["gate_ran"], "reason": reason})
    print("WT-REPORT " + json.dumps(d, ensure_ascii=True), flush=True)
    sys.exit(code)


note({"event": "start", "branch": branch, "project": project, "gate": a.gate})

# 台本
spec, extra = "0", {}
script = os.environ.get("WT_STUB_SCRIPT")
if script and os.path.isfile(script):
    for key, val in json.load(open(script, encoding="utf-8")).items():
        if key in branch:
            if isinstance(val, dict):
                spec = str(val.get("codes", "0"))
                extra = {k: v for k, v in val.items() if k in ("renumbered", "shifted", "reason", "human")}
            else:
                spec = str(val)
            break
n = 0
if script:  # 何回目の呼び出しかを台本の隣に数える（台本が無ければ数えない）
    counter = os.path.join(os.path.dirname(os.path.abspath(script)), "wt_stub_" + re.sub(r"[^A-Za-z0-9_-]", "-", branch) + ".count")
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(n + 1))
if spec.endswith("*"):
    code_now = spec[:-1]
else:
    seq = spec.split(",")
    code_now = seq[min(n, len(seq) - 1)]
human = extra.pop("human", None)
if human and not a.discard and n + 1 == int(human.get("at", 1)):
    for rel, body in human.get("files", {}).items():
        with open(os.path.join(project, rel), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        git(["add", "--", rel], project)
    git(["commit", "-qm", "human edit (stub)"], project)

if code_now == "hang":
    time.sleep(3600)
if code_now == "2":
    report(2, "abnormal", "台本: 異常（スタブ）")
if code_now == "20":
    report(20, "check_failed", extra.get("reason") or "台本: 検査で弾いた（スタブ）")
if code_now == "10":
    report(10, "conflict", "台本: 衝突（スタブ）")

# 本来の流れ
wt = None
cur = None
for ln in git(["worktree", "list", "--porcelain"], project).stdout.splitlines():
    if ln.startswith("worktree "):
        cur = os.path.normpath(ln[9:])
    elif ln == f"branch refs/heads/{branch}":
        wt = cur
base = git(["symbolic-ref", "--short", "HEAD"], project).stdout.strip()
if git(["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], project).returncode != 0:
    report(2, "abnormal", f"ブランチ {branch} が無い")
if not wt:
    report(2, "abnormal", f"ブランチ {branch} の作業コピーが無い（スタブは作業コピーが要る）")
if a.discard:
    git(["worktree", "remove", "--force", wt], project)
    report(0, "discarded", "作業コピーだけ外した")
# ①
if git(["status", "--porcelain"], wt).stdout.strip():
    report(20, "check_failed", "作業コピーに未コミットの変更がある")


# ②
def section0(rev):
    r = git(["show", f"{rev}:HANDOFF.md"], project)
    if r.returncode != 0:
        return None
    m = re.search(r"^## [^\n]*\n(.*?)(?=^## |\Z)", r.stdout, re.S | re.M)
    return m.group(0) if m else ""


s_base, s_branch = section0(base), section0(branch)
if s_base is not None and s_branch is not None and s_base != s_branch:
    mb = git(["merge-base", base, branch], project).stdout.strip()
    if section0(mb) != s_branch:  # ブランチ側が §0 を変えた（本流側だけが変えたのは取り込みで入る）
        report(20, "check_failed", "ブランチが HANDOFF §0 を書いている")
# ③
r = git(["merge", "--no-edit", base], wt)
if r.returncode != 0:
    unmerged = [x for x in git(["diff", "--name-only", "-z", "--diff-filter=U"], wt).stdout.split("\0") if x]
    git(["merge", "--abort"], wt)
    if unmerged:
        report(10, "conflict", "本当の衝突: " + ", ".join(unmerged), files=unmerged)
    report(2, "abnormal", "取り込みに失敗（衝突以外）: " + (r.stdout + r.stderr).strip()[-200:])
# ⑥
if a.gate:
    state["gate_ran"] = True
    env = dict(os.environ, WT_PROJECT=project, WT_WORKTREE=wt, WT_BRANCH=branch, WT_BASE=base)
    p = subprocess.Popen(a.gate, shell=True, cwd=wt, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        out, _ = p.communicate(timeout=a.gate_timeout)
    except subprocess.TimeoutExpired:
        kill_tree(p)
        report(20, "check_failed", f"gate が {a.gate_timeout} 秒で終わらなかった（時間切れ）")
    text = out.decode("utf-8", errors="replace")
    sys.stdout.write(text)
    if p.returncode != 0:
        tail = " / ".join([ln for ln in text.strip().splitlines() if ln.strip()][-3:])
        report(20, "check_failed", f"gate に落ちた（{p.returncode}）: {tail}")
# ⑦（台本）
if code_now == "30":
    report(30, "busy", "台本: 本体が使用中（スタブ）")
# ⑧
before = git(["rev-parse", "HEAD"], project).stdout.strip()
r = git(["merge", "--ff-only", branch], project)
if r.returncode != 0:
    report(2, "abnormal", "本体を早送りできなかった: " + (r.stdout + r.stderr).strip()[-200:])
files = [x for x in git(["diff", "--name-only", "-z", before, "HEAD"], project).stdout.split("\0") if x]
# ⑨
if not a.keep:
    git(["worktree", "remove", "--force", wt], project)
    git(["branch", "-d", branch], project)
report(0, "merged", "", files=files, **{k: v for k, v in extra.items() if k != "reason"})
