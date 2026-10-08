"""ブラウニーの合流まわり（司令塔の側）の試験。トークン0。使い捨てのリポジトリを %TEMP% の下に毎回新しく作る。

    python tests/test_merge_flow.py              # 終了コード 0＝合格、1＝不合格
    python tests/test_merge_flow.py --only 3,r1  # 一部だけ

2026-09-25 から、合流の中身（本流の取り込み・番号の振り直し・§0 への差し込み・本体の早送り）は git 統合の入口 wt.py finish（[E-045]）が持つ。
ここでは wt.py をスタブ（tests/wt_stub.py）に差し替え、ブラウニーの側に残った仕事を試す:
③ ドライバが落ちた衝突（10）→ 解消役 → 解消役の検出が未解消・印を拾い、失敗にする（本体は無傷）
④ 作業コピーの中に junction → wt.py を呼ばず、作業コピーを消さず、リンク先が無傷
⑤ 司令塔は本体に書き込む git（merge・commit・reset など）を1回も打たない（本体へ入れるのは wt.py だけ）
H1 作業コピーの .git が無い → 上へ辿った git が上のリポジトリを触らない
H3 作業役がリンク（mode 120000）をコミット → 関門が落とす（20 → 失敗）
H4 日本語・空白を含むファイル名の衝突 → 名前が割れずに解消役へ渡る
M4 関門の試験は本流を取り込んだ後の木に対して走る／解消役が tests/ を変えたら凍結で保留
M5 解消役は1票に1回（解消の後にまた衝突したら失敗）
M6 ブランチが既に別の作業コピーにある → 直接書きに落ちず、再利用する
R1 評価役が NG → 保留（本体は動かない）→ 報告の「通すなら」を流すと、評価役を飛ばして合流する（「はい」の道）
R2 評価役は1票1回（30 で再試行して関門が2回流れても、評価役は1回）
W1 WT-REPORT の renumbered・shifted が報告に出る ／ W2 WT-REPORT の行が無い → 失敗（異常）／ W3 finish --gate に対応していない wt.py を見分ける
2026-09-25 敵対レビューの差し戻し（③越えない線・②暴走）:
M5b 解消役のセッション中に本体が動いた → 保留（③3）／ s1 情報ファイルの署名（③4）／ s2 どのリポジトリでも Claude・フックの設定は保留（③5）
s3 作業コピーを作れない git リポジトリは直接書かずに失敗（③7）／ s4 審判官の追記は PENDING.md だけコミット（③10）
s5 追跡外を 排除/ へ移して wt.py --discard で外す・-d（③11）／ s6 解消で差分が変わったら評価役を1回だけ走り直す（③12）
s7 wt.py の時間切れで index.lock を報告（②4）／ s8 git のエディタ待ち・孫の握るパイプ・kill_tree の確かめ（②6・②7・②15）
s9 差し戻しは時間切れ・合流済みを再開しない・持ち時間は通算（②9・②10）／ s10 finish の回数上限・--gate-timeout の確かめ・% の拒否・
    評価役の利用上限・STOP で解消役を起こさない（②13・②18・②20・②21・②22）／ R1 ブラウニーが動いている間は --approved を拒む（③9）

wt.py 側へ移った項目（ここでは試さない。本物の wt.py の試験が持つ）:
旧① 両側が同じ番号を足した → ドライバが票側をずらす（取り込みは wt.py の③）／旧② 同じ夜に2票を続けて合流し post-merge が索引を足す
旧H2 本流が master/main 以外 ／ 旧M1 取り込みの間に本体のブランチが切り替わった ／ 旧⑤ 本体に MERGE_HEAD が生まれない（本体の早送りは wt.py の⑧）
旧⑦ 砂場での 衝突 → 解消役 → 合流 は tests/test_overnight.py（ProjB）へ移した。
各試験は「成功の印がある」かつ「失敗の印が無い」の両方で判定する。
"""
import datetime, importlib.util, json, os, re, shutil, subprocess, sys, tempfile, time

sys.stdout.reconfigure(encoding="utf-8")
NIGHT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(os.path.expanduser("~"), ".claude", "tools")
# 本物の wt.py で流すときは NIGHT_SANDBOX_WT_PY に渡す（既定はスタブ）。その隣の install_md_merge.py（同じ版のドライバ）を使う
REAL = os.environ.get("NIGHT_SANDBOX_WT_PY", "")
STUB = not REAL
INSTALL = os.path.join(os.path.dirname(os.path.abspath(REAL)), "install_md_merge.py") if REAL else ""
if not os.path.isfile(INSTALL):
    INSTALL = os.path.join(TOOLS, "install_md_merge.py")
ONLY = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else None

TMP = tempfile.mkdtemp(prefix="night_merge_flow_")
EVAR = os.path.join(TMP, "evar")
HOME = os.path.join(TMP, "home")
os.makedirs(EVAR)
os.makedirs(HOME)
SCRIPT = os.path.join(TMP, "wt_script.json")
STUB_LOG = os.path.join(TMP, "wt_stub_log.jsonl")
json.dump({}, open(SCRIPT, "w", encoding="utf-8"))
os.environ.update(NIGHT_EVAR_ROOT=EVAR, NIGHT_HOME=HOME, NIGHT_JUDGE_INTERVAL_SEC="0", PYTHONUTF8="1", FAKE_CLAUDE_SLEEP="0",
                  NIGHT_WT_PY=REAL or os.path.join(NIGHT, "tests", "wt_stub.py"), WT_STUB_SCRIPT=SCRIPT, WT_STUB_LOG=STUB_LOG,
                  NIGHT_BUSY_RETRY_SEC="0", NIGHT_GATE_TIMEOUT_SEC="120", NIGHT_WT_TIMEOUT_SEC="180",
                  NIGHT_WATCH_HOME=os.path.join(TMP, "watch_home"), NIGHT_MIN_FREE_PCT="0", NIGHT_MIN_FREE_GB="0")
for k in ("NIGHT_SELF_REPO", "WT_PROJECT", "WT_WORKTREE", "WT_BRANCH", "WT_BASE", "NIGHT_MODEL"):
    os.environ.pop(k, None)

# 偽の解消役・評価役（FAKE_RESOLVE=nothing｜add_commit｜resolve｜resolve_touch｜resolve_and_human。受け取った指示文を残す）
FAKE = os.path.join(TMP, "fake_resolver.py")
open(FAKE, "w", encoding="utf-8").write(r'''
import json, os, subprocess, sys
args = sys.argv[1:]
sid = args[args.index("--session-id") + 1]
prompt = sys.stdin.buffer.read().decode("utf-8", errors="replace")
mode = os.environ.get("FAKE_RESOLVE", "")
kind = os.environ.get("NIGHT_KIND", "")
open(os.path.join(os.environ["NIGHT_HOME"], f"{kind}_prompt_{mode}.md"), "w", encoding="utf-8").write(prompt)
g = lambda *a, cwd=None: subprocess.run(["git", "-c", "core.quotePath=false", *a], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
main = os.path.dirname(os.path.abspath(g("rev-parse", "--git-common-dir").stdout.strip()))
if kind == "review":
    if os.environ.get("FAKE_REVIEW_LIMIT"):
        print(json.dumps({"type": "result", "is_error": True, "total_cost_usd": 0, "num_turns": 1, "session_id": sid,
                          "result": "You've hit your usage limit. Your limit will reset at 5am"}))
        sys.exit(0)
    res = "NG: 偽の評価役が止めた（fake）" if os.environ.get("FAKE_REVIEW_NG") else "OK"
    print(json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0, "num_turns": 1, "session_id": sid, "result": res}))
    sys.exit(0)
if mode.startswith("resolve"):
    for f in [x for x in g("diff", "--name-only", "-z", "--diff-filter=U").stdout.split("\0") if x]:
        body = open(f, encoding="utf-8").read()
        open(f, "w", encoding="utf-8", newline="\n").write("".join(l for l in body.splitlines(True) if not l.startswith(("<<<<<<<", "=======", ">>>>>>>"))))
    if mode == "resolve_touch":
        os.makedirs("tests", exist_ok=True)
        open(os.path.join("tests", "frozen.py"), "w").write("# resolver touched the frozen tests\n")
    g("add", "-A")
    g("commit", "-q", "--no-edit")
    if mode == "resolve_and_human":
        f = os.environ["FAKE_FILE"]
        open(os.path.join(main, f), "w", encoding="utf-8", newline="\n").write("human again\n")
        g("commit", "-qam", "human again", cwd=main)
if mode == "add_commit":
    g("add", "-A")
    g("commit", "-q", "--no-edit")
print(json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0, "num_turns": 1, "session_id": sid,
                  "result": "resolved" if mode.startswith("resolve") else "unresolved: fake"}))
''')
os.environ["NIGHT_CLAUDE_BIN"] = FAKE

spec = importlib.util.spec_from_file_location("runner", os.path.join(NIGHT, "runner.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
runner.GATE_KEY = os.urandom(32)  # 本物の夜と同じく、関門の情報ファイルを署名する（runner.run_night が作る鍵の代わり）
runner.RUN_ID = "test-run"

fails, MAINS, violations = [], set(), []


def stale_heartbeat():
    """ブラウニーの司令塔が止まっている状態にする（朝にエヴァが「通すなら」を流す場面）。"""
    runner.write_json(runner.HEARTBEAT, {"at": "2000-01-01T00:00:00", "phase": "終了（試験）"})


def fresh_heartbeat():
    runner.write_json(runner.HEARTBEAT, {"at": datetime.datetime.now().isoformat(timespec="seconds"), "phase": "作業"})


def check(ok, what):
    print(("  ok   " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        fails.append(what)


# ⑤ 司令塔が本体（MAINS）に打つ git を全部見張る。本体へ書き込むのは wt.py（別のプロセス）だけのはず
_git = runner.git
WRITES = {"merge", "commit", "commit-tree", "reset", "checkout", "switch", "rebase", "cherry-pick", "revert", "add", "stash",
          "pull", "push", "am", "apply", "restore", "rm", "mv", "update-ref"}


def watched_git(args, cwd):
    if os.path.normcase(os.path.abspath(cwd)) in MAINS and args[:1] and args[0] in WRITES:
        violations.append(f"本体で git {' '.join(args)}")
    return _git(args, cwd)


runner.git = watched_git


def sh(args, cwd, **kw):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def write(path, text, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(text, bytes):
        open(path, mode + "b").write(text)
    else:
        open(path, mode, encoding="utf-8", newline="\n").write(text)


def commit(cwd, msg):
    sh(["git", "add", "-A"], cwd)
    r = sh(["git", "commit", "-qm", msg], cwd)
    assert r.returncode == 0, r.stderr


def make_repo(name, files, branch="master"):
    root = os.path.join(EVAR, name)
    os.makedirs(root)
    for c in (["git", "init", "-q", "-b", branch], ["git", "config", "user.name", "t"], ["git", "config", "user.email", "t@local"]):
        sh(c, root)
    if os.path.isfile(INSTALL):
        r = sh([sys.executable, INSTALL, root], root)  # 本番と同じ手でドライバを登録する
        assert r.returncode == 0, r.stderr
    for rel, body in files.items():
        write(os.path.join(root, rel), body)
    commit(root, "init")
    MAINS.add(os.path.normcase(os.path.abspath(root)))
    return root


def ticket(root, tid):
    t = {"id": tid, "project_dir": root, "title": f"試験 {tid}", "status": "done", "sessions": 1, "cost": 0,
         "goal": "試験", "done_check": "なし", "kind": "run"}
    assert runner.ensure_worktree(t), t.get("merge")
    return t


def script(d):
    cur = json.load(open(SCRIPT, encoding="utf-8"))
    cur.update(d)
    json.dump(cur, open(SCRIPT, "w", encoding="utf-8"))


def report_for(tickets):
    runner.write_json(runner.TONIGHT, {"night": runner.night_date(), "tickets": tickets, "skipped": []})
    one = open(runner.write_report(datetime.datetime.now()), encoding="utf-8").read()
    detail = open(os.path.join(runner.REPORTS, f"{runner.night_date()}_詳細.md"), encoding="utf-8").read()
    return one, detail


def ledger():
    try:
        return [json.loads(l) for l in open(runner.LEDGER, encoding="utf-8")]
    except OSError:
        return []


def stub_ends(branch):
    try:
        return [r for r in (json.loads(l) for l in open(STUB_LOG, encoding="utf-8")) if r.get("event") == "end" and r.get("branch") == branch]
    except OSError:
        return []


def on(n):
    return ONLY is None or n in ONLY


def head(root):
    return sh(["git", "rev-parse", "HEAD"], root).stdout.strip()


def conflict_repo(name, fname="notes.txt"):
    """本流と票が同じ行を変えた（ドライバの対象外なので git の行合流で本当に衝突する）"""
    root = make_repo(name, {fname: "base\n", "DECISIONS.md": "# DECISIONS\n"})
    t = ticket(root, "t" + name)
    write(os.path.join(t["worktree"], fname), "night\n")
    commit(t["worktree"], "night")
    write(os.path.join(root, fname), "human\n")
    sh(["git", "add", fname], root)
    sh(["git", "commit", "-qm", "human"], root)
    return root, t


os.makedirs(runner.STATE, exist_ok=True)
for c in (["git", "init", "-q", "-b", "master"], ["git", "config", "user.name", "t"], ["git", "config", "user.email", "t@local"],
          ["git", "commit", "-q", "--allow-empty", "-m", "home"]):
    sh(c, HOME)
MAINS.add(os.path.normcase(os.path.abspath(HOME)))

# ---------------------------------------------------------------- ③
if on("3") and os.path.isfile(INSTALL):
    for mode in ("nothing", "add_commit"):
        print(f"③ ドライバが落ちた（UTF-8 として読めない DECISIONS.md）・偽の解消役={mode}", flush=True)
        os.environ["FAKE_RESOLVE"] = mode
        root = make_repo(f"r3{mode}", {"DECISIONS.md": b"# DECISIONS\n\xff\xfe broken\n"})
        t = ticket(root, f"t3{mode}")
        write(os.path.join(t["worktree"], "DECISIONS.md"), b"- night\n", "a")
        commit(t["worktree"], "night")
        write(os.path.join(root, "DECISIONS.md"), b"- human\n", "a")
        sh(["git", "add", "DECISIONS.md"], root)
        sh(["git", "commit", "-qm", "human"], root)
        before = head(root)
        n0 = len(ledger())
        runner.finish_ticket(t)
        rows = [r for r in ledger()[n0:] if r.get("kind") == "resolve"]
        fins = [r.get("code") for r in ledger()[n0:] if r.get("kind") == "finish"]
        m = t["merge"]
        prompt = open(os.path.join(HOME, f"resolve_prompt_{mode}.md"), encoding="utf-8").read() \
            if os.path.exists(os.path.join(HOME, f"resolve_prompt_{mode}.md")) else ""
        check(fins == [10], f"wt.py finish が 10（本当の衝突）を返した（{fins}）")
        check("driver failed" in (m.get("note") or "") or "merge_md" in (m.get("note") or ""),
              f"本物の merge_md.py が落ちたことが理由に残った（{(m.get('note') or '')[:90]}）")
        check(bool(rows) and "`DECISIONS.md`" in prompt, "解消役が呼ばれ、DECISIONS.md を渡された")
        if mode == "nothing":
            check(bool(rows) and rows[-1].get("unmerged") == ["DECISIONS.md"], f"未解消のファイルとして拾った（{rows[-1].get('unmerged') if rows else None}）")
        else:
            check(bool(rows) and rows[-1].get("markers") == ["DECISIONS.md"], f"印（<<<<<<< ours (merge_md.py failed…）として拾った（{rows[-1].get('markers') if rows else None}）")
        check(bool(rows) and rows[-1].get("status") == "unresolved" and m.get("state") == "conflict"
              and (t.get("outcome") or {}).get("kind") == "failed", f"解消済み扱いにせず失敗にした（{m.get('state')}）")
        check(head(root) == before and b"<<<<<<<" not in open(os.path.join(root, "DECISIONS.md"), "rb").read(),
              "本体は動いておらず、印も入っていない")
    os.environ.pop("FAKE_RESOLVE", None)

# ---------------------------------------------------------------- ④
if on("4"):
    print("④ 作業コピーの中に junction", flush=True)
    target = os.path.join(TMP, "junction_target")
    write(os.path.join(target, "keep.txt"), "消えてはいけない\n")
    root = make_repo("r4", {"DECISIONS.md": "# DECISIONS\n"})
    t = ticket(root, "t4")
    write(os.path.join(t["worktree"], "note.md"), "夜の成果\n")
    commit(t["worktree"], "night: note")
    write(os.path.join(t["worktree"], "draft.md"), "書きかけ\n")
    link = os.path.join(t["worktree"], "linked")
    r = sh(["cmd", "/c", "mklink", "/J", link, target], TMP)
    check(r.returncode == 0 and os.path.isjunction(link), "junction を作れた（試験の前提）")
    before = head(root)
    n0 = len(ledger())
    runner.finish_ticket(t)
    m, o = t["merge"], t.get("outcome") or {}
    check(m.get("state") == "danger" and o.get("kind") == "failed" and head(root) == before, f"合流せず失敗にした（{m.get('state')}）")
    check(not [r for r in ledger()[n0:] if r.get("kind") == "finish"], "wt.py finish を呼ばなかった")
    tree = sh(["git", "ls-tree", "-r", "--name-only", t["branch"]], root).stdout
    check("linked/" not in tree and "draft.md" not in tree, "書きかけのコミットもしなかった（リンク先の中身がブランチに入っていない）")
    check("⚠" in (o.get("how") or ""), f"確かめ方が警告になっている（{(o.get('how') or '')[:60]}）")
    check(os.path.isdir(t["worktree"]) and os.path.isjunction(link), "作業コピーを消さなかった")
    check(open(os.path.join(target, "keep.txt"), encoding="utf-8").read() == "消えてはいけない\n", "リンク先の中身が無傷")
    check("junction" in (m.get("worktree_kept") or ""), f"t['merge'] に理由が入った（{(m.get('worktree_kept') or '')[:80]}）")
    check(sh(["git", "rev-parse", "--verify", "-q", f"refs/heads/{t['branch']}"], root).returncode == 0, "ブランチも消さなかった")
    one, detail = report_for([t])
    check("消さずに残した作業コピー" in detail and "linked" in detail and "## 失敗（1）" in one and "t4" in one.split("## 失敗")[1],
          "朝の報告の「失敗」に載り、全文に理由が出た")
    sh(["cmd", "/c", "rmdir", link], TMP)  # 後始末はリンクそのものだけ外す

# ---------------------------------------------------------------- 敵対レビューの指摘（ブラウニーの側に残ったもの）
if on("h1"):
    print("H1 作業コピーのフォルダはあるが .git が無い → 上へ辿った git が本体（HOME のリポジトリ）を触らない", flush=True)
    root, t = conflict_repo("h1")
    write(os.path.join(t["worktree"], "draft.md"), "書きかけ\n")
    os.rename(os.path.join(t["worktree"], ".git"), os.path.join(TMP, "h1_dotgit_moved"))
    h0, before = head(HOME), head(root)
    runner.finish_ticket(t)
    m = t["merge"]
    check(m.get("state") == "danger" and ".git" in (m.get("note") or ""), f"失敗で理由付き（{m.get('state')}: {(m.get('note') or '')[:60]}）")
    check(head(HOME) == h0 and sh(["git", "status", "--porcelain", "--untracked-files=no"], HOME).stdout.strip() == ""
          and not os.path.exists(os.path.join(HOME, ".git", "MERGE_HEAD")), "上のリポジトリ（HOME）にコミット・合流・索引の変更が無い")
    check(head(root) == before and os.path.isdir(t["worktree"]), "本流は動かず、作業コピーのフォルダも消していない")

if on("h3"):
    print("H3 作業役がリンク（mode 120000）をコミット済み → 関門が落とす", flush=True)
    root = make_repo("h3", {"DECISIONS.md": "# DECISIONS\n"})
    t = ticket(root, "th3")
    blob = sh(["git", "hash-object", "-w", "--stdin"], t["worktree"], input="../../../outside").stdout.strip()
    sh(["git", "update-index", "--add", "--cacheinfo", f"120000,{blob},lnk"], t["worktree"])
    sh(["git", "commit", "-qm", "night: link"], t["worktree"])
    sh(["git", "checkout", "--", "lnk"], t["worktree"])  # 作業ツリーにも置く（core.symlinks=false なら中身がリンク先の文字列のファイル）
    check("120000" in sh(["git", "ls-files", "-s", "lnk"], t["worktree"]).stdout
          and sh(["git", "status", "--porcelain"], t["worktree"]).stdout.strip() == "", "リンクをコミットできた（試験の前提）")
    before = head(root)
    runner.finish_ticket(t)
    m = t["merge"]
    check(m.get("state") == "check_failed" and "lnk" in (m.get("note") or "") and (t.get("outcome") or {}).get("kind") == "failed",
          f"関門が落として失敗（{m.get('state')}: {(m.get('note') or '')[:80]}）")
    check(head(root) == before, "本流は動いていない")

if on("h4"):
    print("H4 日本語・空白を含むファイル名で衝突 → 名前が割れずに解消役へ渡る", flush=True)
    os.environ["FAKE_RESOLVE"] = "nothing_h4"
    name = "日本語 メモ.txt"
    root, t = conflict_repo("h4", name)
    n0 = len(ledger())
    runner.finish_ticket(t)
    rows = [r for r in ledger()[n0:] if r.get("kind") == "resolve"]
    p = os.path.join(HOME, "resolve_prompt_nothing_h4.md")
    prompt = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
    check(bool(rows) and rows[-1].get("files") == [name], f"衝突ファイルの一覧が正しい（{rows[-1].get('files') if rows else None}）")
    check(f"`{name}`" in prompt, "解消役の指示文に正しい名前が載った")
    os.environ.pop("FAKE_RESOLVE", None)

if on("m4"):
    print("M4 関門の試験と凍結の判定は、本流を取り込んだ後の木に対して走る", flush=True)
    root = make_repo("m4", {"DECISIONS.md": "# DECISIONS\n",
                            "tests/night_gate.py": "import os, sys\nsys.exit(1 if os.path.exists(os.path.join(sys.argv[1], 'bad.txt')) else 0)\n"})
    t = ticket(root, "tm4")
    write(os.path.join(t["worktree"], "ok.txt"), "夜\n")
    commit(t["worktree"], "night")
    write(os.path.join(root, "bad.txt"), "本流が足した、関門に落ちる中身\n")
    sh(["git", "add", "bad.txt"], root)
    sh(["git", "commit", "-qm", "human: bad"], root)
    before = head(root)
    runner.finish_ticket(t)
    check(t["merge"].get("state") == "check_failed" and "関門の試験に落ちた" in (t["merge"].get("note") or "") and head(root) == before,
          f"取り込み後の木で関門の試験が落ちて失敗（{t['merge'].get('state')}: {(t['merge'].get('note') or '')[:60]}）")
    os.environ["FAKE_RESOLVE"] = "resolve_touch"
    root, t = conflict_repo("m4b")
    saved = runner.SELF_REPO
    runner.SELF_REPO = root  # ブラウニーが自分を直す票（凍結の対象）に見せる
    try:
        before = head(root)
        runner.finish_ticket(t)
    finally:
        runner.SELF_REPO = saved
        os.environ.pop("FAKE_RESOLVE", None)
    m = t["merge"]
    check(m.get("state") == "held" and "tests/frozen.py" in (m.get("note") or "") and head(root) == before
          and (t.get("outcome") or {}).get("kind") == "hold", f"解消役が tests/ を変えたら保留（{m.get('state')}: {(m.get('note') or '')[:80]}）")

if on("m5"):
    print("M5 解消役は1票に1回まで（解消の後にまた衝突したら失敗）", flush=True)
    os.environ["FAKE_RESOLVE"] = "resolve"
    root, t = conflict_repo("m5")
    orig_resolve = runner.resolve_in_worktree

    def resolve_then_human(tt):  # 解消の後、2回目の finish の前に人がまた本流を書く（セッションの外。スタブでも本物でも同じに起こせる）
        res = orig_resolve(tt)
        write(os.path.join(root, "notes.txt"), "human again\n")
        sh(["git", "commit", "-qam", "human again"], root)
        return res
    runner.resolve_in_worktree = resolve_then_human
    n0 = len(ledger())
    try:
        runner.finish_ticket(t)
    finally:
        runner.resolve_in_worktree = orig_resolve
    rows = [r for r in ledger()[n0:] if r.get("kind") == "resolve"]
    fins = [r.get("code") for r in ledger()[n0:] if r.get("kind") == "finish"]
    check(len(rows) == 1, f"解消役は1回だけ（{len(rows)}回）")
    check(fins == [10, 10] and t["merge"].get("state") == "conflict" and (t.get("outcome") or {}).get("kind") == "failed",
          f"2回目の衝突は失敗にした（finish {fins}・{t['merge'].get('state')}）")
    print("M5b 解消役のセッションの最中に本体が動いた → 作業コピーの外への変更として保留（③3）", flush=True)
    os.environ.update(FAKE_RESOLVE="resolve_and_human", FAKE_FILE="notes.txt")
    root, t = conflict_repo("m5b")
    runner.finish_ticket(t)
    check(t["merge"].get("state") == "held" and "本体の HEAD が動いた" in t["merge"].get("note", ""),
          f"保留にした（{t['merge'].get('state')}: {t['merge'].get('note', '')[:60]}）")
    os.environ.pop("FAKE_RESOLVE", None)

if on("m6"):
    print("M6 ブランチが既に別の作業コピーでチェックアウトされている → 直接書きに落ちない", flush=True)
    root = make_repo("m6", {"DECISIONS.md": "# DECISIONS\n"})
    t = ticket(root, "tm6")
    t2 = {"id": "tm6-again", "project_dir": root, "branch": t["branch"], "title": "再開"}
    r = runner.ensure_worktree(t2)
    check((t2.get("merge") or {}).get("state") != "direct", f"direct に落ちなかった（{t2.get('merge')}）")
    check(bool(r) and os.path.normcase(t2.get("worktree") or "") == os.path.normcase(t["worktree"]), "既存の作業コピーを再利用した")

if on("r1"):
    print("R1 評価役が NG → 保留で本体は動かない → 「通すなら」を流すと評価役を飛ばして合流する", flush=True)
    os.environ["FAKE_REVIEW_NG"] = "1"
    root = make_repo("r1ng", {"DECISIONS.md": "# DECISIONS\n"})
    t = ticket(root, "tr1")
    write(os.path.join(t["worktree"], "x.txt"), "夜\n")
    commit(t["worktree"], "night")
    before = head(root)
    n0 = len(ledger())
    runner.finish_ticket(t)
    m, o = t["merge"], t.get("outcome") or {}
    check(m.get("state") == "held" and "評価役が止めた" in (m.get("note") or "") and head(root) == before and o.get("kind") == "hold",
          f"保留で本体は動かない（{m.get('state')}: {(m.get('note') or '')[:60]}）")
    cmd = re.search(r"`([^`]+)`", o.get("how") or "")
    check(bool(cmd) and "--approved" in cmd.group(1) and os.path.isdir(t["worktree"]), "報告に「通すなら」のコマンド（--approved）があり、作業コピーを残した")
    if cmd:
        fresh_heartbeat()  # ③9 ブラウニーの司令塔が動いている間は --approved を受け付けない
        r = subprocess.run(cmd.group(1), shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=dict(os.environ, FAKE_REVIEW_NG="1"), timeout=300)
        # 関門の出力は、スタブは標準出力へ、本物の wt.py は <作業コピーの git dir>/wt-gate.log へ出す（標準出力には ASCII にした末尾だけ）
        logp = re.search(r"log:? ([^\s\"]*wt-gate\.log)", r.stdout)
        gate_text = r.stdout + (open(logp.group(1), encoding="utf-8", errors="replace").read() if logp and os.path.exists(logp.group(1)) else "")
        check(r.returncode == 20 and "動いている間は --approved" in gate_text and head(root) == before,
              f"③9 ブラウニーが動いている間は --approved を関門が拒んだ（終了コード {r.returncode}）")
        signed_before = runner.read_gate(runner.gate_file(t), runner.GATE_KEY)[1]
        stale_heartbeat()
        r = subprocess.run(cmd.group(1), shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=dict(os.environ, FAKE_REVIEW_NG="1"), timeout=300)
        last = [ln for ln in r.stdout.splitlines() if ln.startswith("WT-REPORT ")]
        rep = json.loads(last[-1][10:]) if last else {}
        check(r.returncode == 0 and rep.get("status") == "merged" and os.path.isfile(os.path.join(root, "x.txt")),
              f"「はい」の道: 通すならのコマンドで合流した（{rep.get('status')}・{rep.get('reason', '')[:60]}）")
        check(len([x for x in ledger()[n0:] if x.get("kind") == "review"]) == 1, "許可の後は評価役を呼ばなかった（評価役は最初の1回だけ）")
        check(signed_before is None and (not os.path.exists(runner.gate_file(t)) or runner.read_gate(runner.gate_file(t), runner.GATE_KEY)[1] is None),
              "鍵の無い手作業の関門は、ブラウニーの署名つきの情報ファイルを書き換えなかった")
    os.environ.pop("FAKE_REVIEW_NG", None)

for n in ("r2", "s6", "s7"):
    if REAL and on(n):
        print(f"{n} は wt.py のスタブの台本（関門の後に 30／固まる）で起こす試験なので、本物では飛ばした"
              "（本物での 30→再試行は tests/test_overnight_real.py で確かめる）", flush=True)

if on("r2") and STUB:
    print("R2 評価役は1票1回（30 で再試行して関門が2回流れても、評価役は1回）", flush=True)
    root = make_repo("r2once", {"DECISIONS.md": "# DECISIONS\n"})
    t = ticket(root, "tr2")
    script({"tr2": "30,0"})
    write(os.path.join(t["worktree"], "x.txt"), "夜\n")
    commit(t["worktree"], "night")
    runner.write_json(runner.TONIGHT, {"night": runner.night_date(), "tickets": [t], "skipped": []})
    n0 = len(ledger())
    runner.finish_ticket(t)
    runner.save_ticket(t)
    check(t["merge"].get("state") == "busy" and not t.get("outcome"), f"30 で合流を待つ（{t['merge'].get('state')}）")
    runner.retry_busy(force=True)
    t = runner.read_json(runner.TONIGHT, {})["tickets"][0]
    rows = [r for r in ledger()[n0:] if r.get("kind") == "review"]
    gates = [e for e in stub_ends(t["branch"]) if e.get("gate_ran")]
    check(len(gates) == 2 and len(rows) == 1, f"関門は2回流れ、評価役は1回だけ（関門 {len(gates)}回・評価役 {len(rows)}回）")
    check(t["merge"].get("state") == "merged" and (t.get("outcome") or {}).get("kind") == "done", f"再試行で合流した（{t['merge'].get('state')}）")

if on("w1"):
    print("W1 WT-REPORT の renumbered（仮番号を振った）・shifted（番号がずれた）が朝の報告に出る", flush=True)
    if STUB:
        root = make_repo("w1", {"DECISIONS.md": "# DECISIONS\n"})
        t = ticket(root, "tw1")
        script({"tw1": {"codes": "0", "renumbered": ["X-NEW->X-007"], "shifted": ["X-005->X-006"]}})
        write(os.path.join(t["worktree"], "x.txt"), "夜\n")
        commit(t["worktree"], "night")
        want_r, want_s = ["X-NEW->X-007"], ["X-005->X-006"]
    else:
        # 本物: 票が「## X-NEW」を足す → wt.py の④が X-005 を振る。「§0 へ:」の行は⑤で HANDOFF §0 の管理区間へ入る
        root = make_repo("w1", {"DECISIONS.md": "# DECISIONS\n\n## X-004 もとの判断\n本文\n",
                                "HANDOFF.md": "# HANDOFF\n\n## §0 今の状態\n\n今の話\n\n## 1 そのほか\n"})
        t = ticket(root, "tw1")
        write(os.path.join(t["worktree"], "DECISIONS.md"), "\n## X-NEW 夜の判断\n本文\n§0 へ: 夜の判断を §0 に載せる\n", "a")
        commit(t["worktree"], "night: X-NEW")
        want_r, want_s = ["X-NEW->X-005"], None
    runner.finish_ticket(t)
    one, detail = report_for([t])
    check(t["merge"].get("state") == "merged" and t["merge"].get("renumbered") == want_r, f"renumbered を拾った（{t['merge'].get('renumbered')}）")
    check(want_r[0] in one and "仮番号を振った" in one and want_r[0] in detail, "1枚目と全文の両方に出た")
    if want_s:
        check("番号がずれた" in one and want_s[0] in one, "shifted は「番号がずれた」として出た")
    else:
        dec = sh(["git", "show", "HEAD:DECISIONS.md"], root).stdout
        hand = sh(["git", "show", "HEAD:HANDOFF.md"], root).stdout
        check("## X-005 夜の判断" in dec and "夜の判断を §0 に載せる" in hand and "BEGIN wt.py" in hand,
              "本物の wt.py が本流の DECISIONS に X-005 を振り、§0 の管理区間に行を入れた")

if on("w2"):
    print("W2 WT-REPORT の行が無い／wt.py が finish --gate に対応していない", flush=True)
    bad = os.path.join(TMP, "bad_wt.py")
    write(bad, "import sys\nprint('usage: wt.py start|finish (old)')\nsys.exit(0)\n")
    root = make_repo("w2", {"DECISIONS.md": "# DECISIONS\n"})
    t = ticket(root, "tw2")
    write(os.path.join(t["worktree"], "x.txt"), "夜\n")
    commit(t["worktree"], "night")
    saved = runner.WT_PY
    runner.WT_PY = bad
    try:
        before = head(root)
        runner.finish_ticket(t)
        usable_bad = runner.wt_usable()
    finally:
        runner.WT_PY = saved
    m = t["merge"]
    check(m.get("state") == "abnormal" and "WT-REPORT" in (m.get("note") or "") and (t.get("outcome") or {}).get("kind") == "failed"
          and head(root) == before, f"異常として失敗にした（{m.get('state')}: {(m.get('note') or '')[:60]}）")
    check(not usable_bad and runner.wt_usable(), "finish --gate に対応していない wt.py を見分けた（スタブは対応している）")

# ---------------------------------------------------------------- 2026-09-25 敵対レビュー（③越えない線・②暴走）の差し戻し
if on("s1"):
    print("③4 関門の情報ファイルを書き換えたら、関門は使わない（署名が合わない）", flush=True)
    root = make_repo("s1", {"DECISIONS.md": "# DECISIONS\n"})
    t = ticket(root, "ts1")
    write(os.path.join(t["worktree"], "x.txt"), "夜\n")
    commit(t["worktree"], "night")
    path = runner.write_gate_file(t, "a1")
    d = json.load(open(path, encoding="utf-8"))
    d["review"] = {"verdict": "OK", "line": "OK", "diff": "偽", "count": 1}  # 作業役が評価役を飛ばそうとした
    json.dump(d, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    env = dict(os.environ, NIGHT_GATE_KEY=runner.GATE_KEY.hex(), WT_WORKTREE=t["worktree"], WT_BRANCH=t["branch"], WT_BASE="master",
               WT_PROJECT=root)
    r = subprocess.run([sys.executable, runner.GATE_SCRIPT, path], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, cwd=t["worktree"], timeout=120)
    check(r.returncode == 1 and "署名が合わない" in r.stdout, f"書き換えた情報ファイルを関門が拒んだ（{r.returncode}: {r.stdout.strip()[-80:]}）")
    runner.write_gate_file(t, "a2")  # 司令塔が書き直すと、書き換えた記憶は捨てる
    d2, err = runner.read_gate(path, runner.GATE_KEY)
    check(err is None and "review" not in d2, "司令塔は書き換えられた記憶（評価役の結果）を捨てて署名し直した")
    r = subprocess.run([sys.executable, runner.GATE_SCRIPT, path], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, cwd=t["worktree"], timeout=120)
    check(r.returncode == 0 and "合格" in r.stdout, f"署名が合えば関門は通る（評価役も走る）（{r.returncode}: {r.stdout.strip()[-60:]}）")

if on("s2"):
    print("③5 ブラウニー自身でないリポジトリでも、.claude/settings・hooks・agents・.githooks・.mcp.json に触れたら保留", flush=True)
    check(runner.protected_paths([".claude/settings.json", "a/.claude/hooks/x.py", ".claude/agents/r.md", ".githooks/pre-commit",
                                  ".mcp.json", "sub/.mcp.json", ".claude/worktrees/x/a.txt", "docs/claude.md", ".claude/commands/c.md"])
          == [".claude/settings.json", "a/.claude/hooks/x.py", ".claude/agents/r.md", ".githooks/pre-commit", ".mcp.json", "sub/.mcp.json"],
          "保護するファイルの判定（作業コピーの置き場 .claude/worktrees は含めない）")
    print("③3 関門フックの状態ファイル（hooks の直下の .spawn_gate_state）だけは外の変更に数えない。ほかは数える", flush=True)
    wh = os.path.join(runner.WATCH_HOME, ".claude")
    for rel in (("hooks", "hook.py"), ("hooks", ".spawn_gate_state", "s1.json"), ("hooks", "sub", ".spawn_gate_state", "x.json"),
                ("agents", ".spawn_gate_state", "x.json")):
        write(os.path.join(wh, *rel), "{}\n")
    snap = runner.outside_snapshot([])
    write(os.path.join(wh, "hooks", ".spawn_gate_state", "s1.json"), '{"n": 1}\n')  # 既にある状態ファイルの書き換え
    write(os.path.join(wh, "hooks", ".spawn_gate_state", "s2.json"), "{}\n")        # 新しいセッションの分
    write(os.path.join(wh, "hooks", ".spawn_gate_state", "s2.json.lock"), "")
    got = runner.outside_changes(snap, runner.outside_snapshot([]))
    check(got == [], f"hooks/.spawn_gate_state の中の書き換えと新しいファイルは数えない（{got}）")
    for rel in (("hooks", "hook.py"), ("hooks", "new_hook.py"), ("hooks", "sub", ".spawn_gate_state", "x.json"),
                ("agents", ".spawn_gate_state", "x.json")):
        snap = runner.outside_snapshot([])
        write(os.path.join(wh, *rel), "書き換えた\n")
        got = runner.outside_changes(snap, runner.outside_snapshot([]))
        check(len(got) == 1 and os.path.join(*rel) in got[0], f"{'/'.join(rel)} の書き換えは今までどおり数える（{got}）")
    root = make_repo("s2", {"DECISIONS.md": "# DECISIONS\n"})
    t = ticket(root, "ts2")
    write(os.path.join(t["worktree"], ".claude", "settings.json"), '{"permissions": {"allow": ["Bash(*)"]}}\n')
    commit(t["worktree"], "night: settings")
    before = head(root)
    runner.finish_ticket(t)
    m = t["merge"]
    check(m.get("state") == "held" and ".claude/settings.json" in (m.get("note") or "") and head(root) == before,
          f"保留で本体は動かない（{m.get('state')}: {(m.get('note') or '')[:60]}）")

if on("s3"):
    print("③7 git リポジトリなのに作業コピーを作れない（detached HEAD・壊れた .git）→ 直接書かずに失敗", flush=True)
    root = make_repo("s3", {"DECISIONS.md": "# DECISIONS\n", "PENDING.md": "# PENDING\n"})
    sh(["git", "checkout", "-q", "--detach"], root)
    t = {"id": "ts3", "project_dir": root, "title": "detached", "status": "pending", "sessions": 0, "cost": 0, "kind": "run"}
    n0 = len(ledger())
    runner.work(t, 10)
    rows = ledger()[n0:]
    check((t.get("outcome") or {}).get("kind") == "failed" and "作業コピーを作れない" in t["outcome"]["cause"]
          and "detached" in t["outcome"]["cause"], f"失敗（作業コピーを作れない: detached）（{(t.get('outcome') or {}).get('cause', '')[:60]}）")
    check(not [r for r in rows if r.get("kind") == "work" and r.get("session_id")] and sh(["git", "status", "--porcelain"], root).stdout == "",
          "作業セッションを起こさず、本流にも書いていない")
    broken = os.path.join(EVAR, "s3broken")
    os.makedirs(broken)
    write(os.path.join(broken, ".git"), "gitdir: C:/does/not/exist\n")
    write(os.path.join(broken, "PENDING.md"), "# PENDING\n")
    t = {"id": "ts3b", "project_dir": broken, "title": "壊れた .git", "status": "pending", "sessions": 0, "cost": 0, "kind": "run"}
    r = runner.ensure_worktree(t)
    check(r == "cannot" and (t.get("merge") or {}).get("state") == "no_worktree", f"壊れた .git も直接書きに落とさない（{r}・{t.get('merge')}）")

if on("s4"):
    print("③10 審判官の fail を PENDING に足すコミットは PENDING.md だけ（他のセッションが index に積んだ分を混ぜない）", flush=True)
    root = make_repo("s4", {"PENDING.md": "# PENDING\n", "other.txt": "a\n"})
    MAINS.discard(os.path.normcase(os.path.abspath(root)))  # この本体への commit は試験の対象そのもの（⑤の違反に数えない）
    write(os.path.join(root, "other.txt"), "他のセッションの書きかけ\n")
    sh(["git", "add", "other.txt"], root)
    saved = runner.ROOT
    runner.ROOT = root
    try:
        runner.add_judge_fails_to_pending({"items": [{"id": "evidence", "verdict": "fail", "note": "根拠が無い"}]},
                                          os.path.join(root, "j.md"))
    finally:
        runner.ROOT = saved
    files = sh(["git", "show", "--name-only", "--format=", "HEAD"], root).stdout.split()
    staged = sh(["git", "diff", "--cached", "--name-only"], root).stdout.split()
    check(files == ["PENDING.md"] and staged == ["other.txt"], f"コミットは PENDING.md だけ、他の人の分は index に残った（{files} ／ {staged}）")

if on("s5"):
    print("③11 失敗の票の作業コピーは、追跡外・無視設定のファイルを 排除/ へ移してから wt.py finish --discard で外す", flush=True)
    root = make_repo("s5", {"DECISIONS.md": "# DECISIONS\n", ".gitignore": "build/\n"})
    t = ticket(root, "ts5")
    write(os.path.join(t["worktree"], "x.txt"), "夜\n")
    commit(t["worktree"], "night")
    t["status"] = "error"
    t["last_result"] = "試験"
    runner.finish_ticket(t)  # コミット済みの変更がある失敗 → ブランチは残し、作業コピーだけ外す
    root2 = make_repo("s5b", {"DECISIONS.md": "# DECISIONS\n", ".gitignore": "build/\n"})
    t2 = ticket(root2, "ts5b")
    write(os.path.join(t2["worktree"], "build", "out.bin"), "無視設定の生成物\n")
    t2["status"] = "error"
    t2["last_result"] = "試験"
    runner.finish_ticket(t2)  # 変更なし（生成物だけ）→ 作業コピーを外し、ブランチは -d で消す
    m2 = t2["merge"]
    moved = m2.get("stashed") or ""
    check(not os.path.isdir(t2["worktree"]) and os.path.isfile(os.path.join(moved, "build", "out.bin"))
          and os.path.commonpath([moved, os.path.join(EVAR, "排除")]) == os.path.join(EVAR, "排除"),
          f"無視設定の生成物を 排除/ へ移してから作業コピーを外した（{moved}）")
    check(m2.get("discard") == "discarded" and (t.get("merge") or {}).get("discard") == "discarded",
          f"外すのは wt.py finish --discard を通した（{m2.get('discard')}・{(t.get('merge') or {}).get('discard')}）")
    check(sh(["git", "rev-parse", "--verify", "-q", f"refs/heads/{t2['branch']}"], root2).returncode != 0,
          "変更の無いブランチは -d で消えた")
    check(sh(["git", "rev-parse", "--verify", "-q", f"refs/heads/{t['branch']}"], root).returncode == 0 and not os.path.isdir(t["worktree"]),
          "変更のある失敗の票は、ブランチを記録として残し、作業コピーだけ外した")

if on("s6") and STUB:
    print("③12 解消役で差分が変わったら、評価役は1回だけ走り直す（1票最大2回）", flush=True)
    os.environ["FAKE_RESOLVE"] = "resolve"
    root = make_repo("s6", {"notes.txt": "base\n", "DECISIONS.md": "# DECISIONS\n"})
    t = ticket(root, "ts6")
    write(os.path.join(t["worktree"], "notes.txt"), "night\n")
    commit(t["worktree"], "night")
    script({"ts6": {"codes": "30,0", "human": {"at": 2, "files": {"notes.txt": "human\n"}}}})
    runner.write_json(runner.TONIGHT, {"night": runner.night_date(), "tickets": [t], "skipped": []})
    n0 = len(ledger())
    runner.finish_ticket(t)  # 1回目: 関門・評価役（1回目）→ 30
    runner.save_ticket(t)
    runner.retry_busy(force=True)  # 2回目: 人が本流を書き換えた → 10 → 解消役 → 3回目: 評価役（2回目）→ 合流
    t = runner.read_json(runner.TONIGHT, {})["tickets"][0]
    rows = ledger()[n0:]
    check([r.get("code") for r in rows if r.get("kind") == "finish"] == [30, 10, 0] and len([r for r in rows if r.get("kind") == "review"]) == 2
          and (t.get("outcome") or {}).get("kind") == "done",
          f"finish {[r.get('code') for r in rows if r.get('kind') == 'finish']}・評価役 {len([r for r in rows if r.get('kind') == 'review'])}回・{(t.get('outcome') or {}).get('kind')}")
    os.environ.pop("FAKE_RESOLVE", None)

if on("s7") and STUB:
    print("②4 wt.py を時間切れで止めたら、本体の index.lock が残っていないかを報告に書く（消さない）", flush=True)
    root = make_repo("s7", {"DECISIONS.md": "# DECISIONS\n"})
    t = ticket(root, "ts7")
    write(os.path.join(t["worktree"], "x.txt"), "夜\n")
    commit(t["worktree"], "night")
    script({"ts7": "hang"})
    lock = os.path.join(root, ".git", "index.lock")
    open(lock, "w").close()
    saved = runner.WT_TIMEOUT_SEC
    runner.WT_TIMEOUT_SEC = 4
    try:
        runner.finish_ticket(t)
    finally:
        runner.WT_TIMEOUT_SEC = saved
    note = (t.get("merge") or {}).get("note") or ""
    check("時間切れ" in note and "index.lock" in note and os.path.exists(lock), f"時間切れと index.lock を報告し、ロックは消していない（{note[-80:]}）")
    os.remove(lock)

if on("s8"):
    print("②6・②7・②15 git と子プロセスの時間切れ", flush=True)
    root = make_repo("s8", {"a.txt": "a\n"})
    MAINS.discard(os.path.normcase(os.path.abspath(root)))  # ここでの commit は試験の対象そのもの
    t0 = time.time()
    r = runner.git(["commit", "--allow-empty"], root)  # メッセージ無し＝エディタを開こうとする。無人なので待たない
    check(time.time() - t0 < 60 and r.returncode != 0, f"git がエディタを待たずに返った（{time.time() - t0:.1f}秒・{r.returncode}）")
    t0 = time.time()
    r = runner.run_capture([sys.executable, "-c", "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)']); print('親は終わった')"], timeout=60)
    check(time.time() - t0 < 15 and "親は終わった" in r.stdout, f"孫が出力を握っていても固まらない（{time.time() - t0:.1f}秒）")
    t0 = time.time()
    r = runner.run_capture([sys.executable, "-c", "import time; time.sleep(60)"], timeout=2)
    check(r.timed_out and time.time() - t0 < 20, f"時間切れで子プロセスごと止めた（{time.time() - t0:.1f}秒）")
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    check(runner.kill_tree(p) is True and p.poll() is not None, "kill_tree が止め切れたかを返す")

if on("s9"):
    print("②9・②10 審判官の差し戻しは時間切れ・合流済みを再開しない／持ち時間は通算", flush=True)
    tk = [{"id": "m", "status": "done", "merge": {"state": "merged"}, "sessions": 1},
          {"id": "to", "status": "timeout", "merge": {"state": "no_changes"}, "sessions": 1},
          {"id": "bl", "status": "blocked", "merge": {"state": "not_merged"}, "sessions": 1, "worked_sec": 100}]
    runner.write_json(runner.TONIGHT, {"night": runner.night_date(), "tickets": tk, "skipped": []})
    n = runner.apply_reopen({"reopen": [{"ticket": x["id"], "reason": "虚偽"} for x in tk]})
    after = {x["id"]: x for x in runner.read_json(runner.TONIGHT, {})["tickets"]}
    check(n == 1 and after["bl"]["status"] == "pending" and after["m"]["status"] == "done" and after["to"]["status"] == "timeout"
          and after["bl"].get("worked_sec") == 100, f"差し戻したのは止まった票だけ、持ち時間は引き継いだ（{n}件）")
    dirp = os.path.join(EVAR, "s9direct")
    os.makedirs(dirp)
    t = {"id": "ts9", "project_dir": dirp, "title": "持ち時間切れ", "status": "pending", "sessions": 0, "cost": 0, "kind": "run",
         "worked_sec": runner.TICKET_TIMEOUT_SEC}
    n0 = len(ledger())
    runner.work(t, 10)
    w = [r for r in ledger()[n0:] if r.get("kind") == "work"]
    check(t["status"] == "timeout" and w and w[-1].get("sec") == 0 and not w[-1].get("session_id"),
          "持ち時間を使い切った票は、次のセッションを起こさず時間切れ")

if on("s10"):
    print("②13・②18・②20・②21・②22", flush=True)
    root = make_repo("s10", {"DECISIONS.md": "# DECISIONS\n"})
    t = ticket(root, "ts10")
    write(os.path.join(t["worktree"], "x.txt"), "夜\n")
    commit(t["worktree"], "night")
    script({"ts10": "30*"})
    if REAL:  # 本物では、本体の同じパスに追跡外のファイルを置いて、いつも 30（本体が使用中）にする
        write(os.path.join(root, "x.txt"), "本体で誰かが書きかけ\n")
    saved = runner.MAX_FINISH_TRIES
    runner.MAX_FINISH_TRIES = 2
    try:
        runner.write_json(runner.TONIGHT, {"night": runner.night_date(), "tickets": [t], "skipped": []})
        runner.finish_ticket(t)
        runner.save_ticket(t)
        runner.retry_busy(force=True)
        runner.retry_busy(force=True)
    finally:
        runner.MAX_FINISH_TRIES = saved
    t = runner.read_json(runner.TONIGHT, {})["tickets"][0]
    check((t.get("outcome") or {}).get("kind") == "hold" and "2 回呼んでも" in t["outcome"]["cause"], f"②13 wt.py を呼ぶ回数に上限（{(t.get('outcome') or {}).get('cause', '')[:50]}）")
    only_gate = os.path.join(TMP, "wt_gate_only.py")
    write(only_gate, "import argparse\nap = argparse.ArgumentParser()\nsp = ap.add_subparsers(dest='c')\nf = sp.add_parser('finish')\n"
                     "f.add_argument('--gate')\nap.parse_args()\n")
    saved = runner.WT_PY
    runner.WT_PY = only_gate
    try:
        ok_only = runner.wt_usable()
    finally:
        runner.WT_PY = saved
    check(not ok_only and runner.wt_usable(), "②18 --gate だけで --gate-timeout の無い wt.py は使えないと見分けた")
    try:
        runner.cmdline(["python", "C:/a%PATH%b/gate.py"])
        pct = False
    except ValueError:
        pct = True
    check(pct and "作れない" in runner.approve_cmd(dict(t, repo="C:/x%y")), "②20 % を含むパスはコマンド行に載せない")
    os.environ["FAKE_REVIEW_LIMIT"] = "1"
    root = make_repo("s10b", {"DECISIONS.md": "# DECISIONS\n"})
    t = ticket(root, "ts10b")
    write(os.path.join(t["worktree"], "x.txt"), "夜\n")
    commit(t["worktree"], "night")
    runner._limit["hit"] = False
    runner.finish_ticket(t)
    os.environ.pop("FAKE_REVIEW_LIMIT", None)
    gd10b = runner.read_gate(runner.gate_file(t), runner.GATE_KEY)[0] or {}
    check(t["merge"].get("state") == "busy" and t["merge"].get("why") == "limit" and "利用上限" in t["merge"].get("note", "")
          and runner._limit["hit"] and not t.get("outcome") and not gd10b.get("review"),
          f"②21 評価役が利用上限に当たったら、評価なしで合流せず、枠が戻ってからの再試行に回し、ブラウニーにも知らせた（{t['merge'].get('note', '')[:40]}）")
    runner._limit["hit"] = False
    runner._usage["retry_at"] = 0.0
    cur = json.load(open(SCRIPT, encoding="utf-8"))
    cur.pop("ts10", None)  # 上の ②13 の台本（"ts10" は "ts10b" にも当たる）を外す
    json.dump(cur, open(SCRIPT, "w", encoding="utf-8"))
    runner.call_finish(t)
    check(t["merge"].get("state") == "merged" and (t.get("outcome") or {}).get("kind") == "done",
          f"②21b 枠が戻った後の再試行では評価役が走り直し、合流した（{t['merge'].get('state')}）")
    saved_wait = runner.USAGE_WAIT
    runner.USAGE_WAIT = False
    os.environ["FAKE_REVIEW_LIMIT"] = "1"
    root = make_repo("s10w", {"DECISIONS.md": "# DECISIONS\n"})
    t = ticket(root, "tw10")
    write(os.path.join(t["worktree"], "x.txt"), "夜\n")
    commit(t["worktree"], "night")
    runner.finish_ticket(t)
    os.environ.pop("FAKE_REVIEW_LIMIT", None)
    runner.USAGE_WAIT = saved_wait
    check(t["merge"].get("state") == "held" and runner._limit["hit"], "②21c NIGHT_USAGE_WAIT=0 のときは、以前どおり保留で確定する")
    runner._limit["hit"] = False
    root = make_repo("s10d", {"DECISIONS.md": "# DECISIONS\n"})
    t = ticket(root, "ts10d")
    write(os.path.join(t["worktree"], "x.txt"), "夜\n")
    commit(t["worktree"], "night")
    saved = runner.BUDGET_USD
    runner.BUDGET_USD = 0.2  # 残り額が評価役の最低額に足りない
    try:
        runner.finish_ticket(t)
    finally:
        runner.BUDGET_USD = saved
    check(t["merge"].get("state") == "held" and "上限の残りが少なく" in t["merge"].get("note", ""),
          "②11 残り額が足りなければ評価役を起こさず、評価なしでは合流しない（保留）")
    check(runner.BUSY_RETRY_SEC == 5, f"②19 NIGHT_BUSY_RETRY_SEC=0 でも再試行の間隔は下限5秒（{runner.BUSY_RETRY_SEC}）")
    open(runner.STOP, "w").close()
    root, t = conflict_repo("s10c")
    n0 = len(ledger())
    runner.finish_ticket(t)
    os.remove(runner.STOP)
    check(t["merge"].get("state") == "conflict" and "止める" in t["merge"].get("note", "")
          and not [r for r in ledger()[n0:] if r.get("kind") == "resolve"], "②22 STOP が押されていたら解消役を起こさない")

if on("s11"):
    print("③6 は取り下げ（既定は空）。NIGHT_GAME_PROJECTS を与えたときだけ、ゲームの decide をブランチに残して保留にする", flush=True)
    check(runner.GAME_PROJECTS == [], f"既定の NIGHT_GAME_PROJECTS は空（{runner.GAME_PROJECTS}）")
    root = make_repo("s11game", {"DECISIONS.md": "# DECISIONS\n"})
    t = ticket(root, "ts11")
    t.update(kind="decide", status="needs_decision")
    write(os.path.join(t["worktree"], "DECISIONS.md"), "## 判断待ち: 攻撃力\n", "a")
    commit(t["worktree"], "night: decide")
    saved = runner.GAME_PROJECTS
    runner.GAME_PROJECTS = ["s11game"]
    try:
        before = head(root)
        runner.finish_ticket(t)
    finally:
        runner.GAME_PROJECTS = saved
    check(t["merge"].get("state") == "held" and "ゲームのプロジェクト" in t["merge"].get("note", "") and head(root) == before,
          f"環境変数で与えたときは保留（{t['merge'].get('state')}）")

# ---------------------------------------------------------------- ⑤
if ONLY is None or ONLY:
    print("⑤ 司令塔は本体に書き込む git を1回も打っていない（本体へ入れるのは wt.py だけ）", flush=True)
    check(not violations, "違反なし" if not violations else "違反: " + " ／ ".join(violations[:5]))

shutil.rmtree(TMP, ignore_errors=True) if not fails else None
print(("合格" if not fails else f"不合格 {len(fails)} 件: " + " ／ ".join(fails)) + f"（使い捨て {TMP}）", flush=True)
sys.exit(1 if fails else 0)
