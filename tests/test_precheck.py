"""着手前の検査（runner.precheck_clash・pick_startable・[W-004]）の試験。トークン0。使い捨てのリポジトリを %TEMP% の下に作る。

    python tests/test_precheck.py     # 終了コード 0＝合格、1＝不合格

P1 files の読み方: 絶対パス・「（説明）」付き・「…/ 配下の〜」のフォルダ・project_dir からの相対・根の外は捨てる
P2 重なりの判定: きれいな本体 → 無し ／ 追跡しているファイルの変更・追跡外の同名 → 有り ／ フォルダの担当は追跡分だけ
   ／ 記録（DECISIONS.md）だけの重なり → 無し（NIGHT_PRECHECK_RECORDS=1 なら有り）／ もう始めた票・NIGHT_PRECHECK=0 → 見ない
P3 司令塔の1歩（偽の claude・wt.py はスタブ）: 重なる票は作業セッションが起きず、重ならない票は今までどおり走って合流する。
   本体の未コミットは1バイトも変わらない。始められる票が無い間はセッションを起こさず待つ。本体がコミットされたら、同じ夜のうちに始める
P4 夜の締め: 一度も始められなかった票は、失敗ではなく見送りに理由つきで載り、報告の1枚目に節が出る
"""
import datetime, importlib.util, json, os, shutil, subprocess, sys, tempfile

sys.stdout.reconfigure(encoding="utf-8")
NIGHT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="night_precheck_")
EVAR = os.path.join(TMP, "evar")
HOME = os.path.join(TMP, "home")
os.makedirs(EVAR)
os.makedirs(os.path.join(HOME, "state"))
SCRIPT = os.path.join(TMP, "wt_script.json")
json.dump({}, open(SCRIPT, "w", encoding="utf-8"))
STARTED = os.path.join(HOME, "sessions_started.txt")

# 偽の claude。作業セッションは、起きた印を残し、作業コピーにファイルを1つ作って done で終える。評価役は OK を返す
FAKE = os.path.join(TMP, "fake_claude.py")
open(FAKE, "w", encoding="utf-8").write(r'''
import json, os, sys
args = sys.argv[1:]
sid = args[args.index("--session-id") + 1] if "--session-id" in args else "s"
sys.stdin.buffer.read()
kind = os.environ.get("NIGHT_KIND", "")
res = "OK"
if kind != "review":
    root = os.environ["NIGHT_WORK_ROOT"]
    with open(os.path.join(os.environ["NIGHT_HOME"], "sessions_started.txt"), "a", encoding="utf-8") as f:
        f.write(os.path.basename(root) + "\n")
    with open(os.path.join(root, "made_" + os.path.basename(root) + ".txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("made\n")
    with open(os.environ["NIGHT_PROGRESS"], "w", encoding="utf-8", newline="\n") as f:
        f.write("STATUS: done\n\n## ひとことで\n偽の作業役が1ファイル作った\n")
    res = "done"
print(json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0, "num_turns": 1, "session_id": sid, "result": res}))
''')
os.environ.update(NIGHT_EVAR_ROOT=EVAR, NIGHT_HOME=HOME, NIGHT_JUDGE_INTERVAL_SEC="0", PYTHONUTF8="1", NIGHT_CLAUDE_BIN=FAKE,
                  NIGHT_WT_PY=os.path.join(NIGHT, "tests", "wt_stub.py"), WT_STUB_SCRIPT=SCRIPT,
                  NIGHT_WATCH_HOME=os.path.join(TMP, "watch_home"), NIGHT_MIN_FREE_PCT="0", NIGHT_MIN_FREE_GB="0",
                  NIGHT_USAGE_PROBE_SEC="0", NIGHT_USAGE_HOLD_PCT="0", NIGHT_USAGE_WARN_PCT="0", NIGHT_STALE_DIRTY_HOURS="0",
                  NIGHT_GATE_TIMEOUT_SEC="120", NIGHT_WT_TIMEOUT_SEC="180", NIGHT_PRECHECK_POLL_SEC="1")
for k in ("NIGHT_SELF_REPO", "NIGHT_MODEL", "NIGHT_LABEL", "NIGHT_PRECHECK", "NIGHT_PRECHECK_RECORDS", "NIGHT_MAX_HOURS", "NIGHT_END_AT",
          "WT_PROJECT", "WT_WORKTREE", "WT_BRANCH", "WT_BASE"):
    os.environ.pop(k, None)

spec = importlib.util.spec_from_file_location("runner", os.path.join(NIGHT, "runner.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
runner.GATE_KEY = os.urandom(32)
runner.RUN_ID = "test-run"
runner.NIGHT = "2099-01-01"
runner.toast = lambda *a, **k: None
fails = []


def check(ok, what):
    print(("  ok   " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        fails.append(what)


def sh(args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8").stdout


def commit(root, msg, *paths):
    sh(["git", "add", "--", *paths], root)
    sh(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", msg], root)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def make_repo(name, files):
    root = os.path.join(EVAR, name)
    for rel, text in files.items():
        write(os.path.join(root, rel), text)
    sh(["git", "init", "-q", "-b", "master"], root)
    sh(["git", "config", "user.name", "t"], root)
    sh(["git", "config", "user.email", "t@example.invalid"], root)
    commit(root, "init", ".")
    return root


def ticket(tid, root, files, **kw):
    return dict({"id": tid, "title": tid, "kind": "run", "effort": "low", "priority": 3, "project_dir": root, "files": files,
                 "source": os.path.join(root, "PENDING.md") + ":1", "goal": "g", "done_check": "d", "status": "pending", "sessions": 0}, **kw)


def started():
    return open(STARTED, encoding="utf-8").read().split() if os.path.exists(STARTED) else []


def tonight():
    return {t["id"]: t for t in runner.read_json(runner.TONIGHT, {"tickets": []})["tickets"]}


try:
    a = make_repo("ProjA", {"a.txt": "1\n", "b.txt": "1\n", "docs/資料/一覧 表.md": "1\n", "docs/資料/別.md": "1\n", "old.txt": "1\n",
                            "DECISIONS.md": "# d\n", "PENDING.md": "# p\n", ".gitignore": "bin/\n"})
    sub = os.path.join(a, "Sub")
    os.makedirs(sub)
    write(os.path.join(HOME, "projects.txt"), "ProjA\n")
    J = os.path.join

    print("P1 files の読み方", flush=True)
    got = runner.ticket_paths({"project_dir": sub, "files": [
        J(a, "docs", "資料", "一覧 表.md"), J(a, "bin", "x.dll") + "（本体側のビルド出力。上書き前に退避）", J(a, "docs") + "\\ 配下の GearDump のファイル（1〜2本）",
        "rel.md", J(a, "docs", "資料") + "\\（退避先）", J(TMP, "outside.md"), a, ""]}, a)
    check(got == [("docs/資料/一覧 表.md", False), ("bin/x.dll", False), ("docs", True), ("Sub/rel.md", False), ("docs/資料", True)],
          f"P1 絶対パス・説明つき・フォルダ・相対が読め、根の外と根そのものは捨てる（{got}）")

    print("P2 重なりの判定", flush=True)
    tA = ticket("t-a", a, [J(a, "docs", "資料", "一覧 表.md"), J(a, "DECISIONS.md"), J(a, "PENDING.md")])
    check(runner.precheck_clash(tA) == [], "P2 きれいな本体では重ならない")
    write(J(a, "docs", "資料", "一覧 表.md"), "2 本体の書きかけ\n")
    check(runner.precheck_clash(tA) == ["docs/資料/一覧 表.md"], f"P2 追跡しているファイルの変更は重なる・日本語と空白の名前が割れない（{runner.precheck_clash(tA)}）")
    check(runner.precheck_clash(ticket("t-b", a, [J(a, "b.txt")])) == [], "P2 別のファイルを担当する票は重ならない")
    check(runner.precheck_clash(ticket("t-dir", a, [J(a, "docs") + "\\ 配下の md"])) == ["docs/資料/一覧 表.md"], "P2 フォルダの担当は、配下の追跡しているファイルの変更で重なる")
    write(J(a, "new", "n.md"), "x\n")
    check(runner.precheck_clash(ticket("t-new", a, [J(a, "new", "n.md")])) == ["new/n.md"], "P2 追跡外でも、同じ名前のファイルが本体にあれば重なる")
    check(runner.precheck_clash(ticket("t-newdir", a, [J(a, "new") + "\\（退避先）"])) == [], "P2 フォルダの担当は、追跡外のファイルでは重ならない")
    write(J(a, "bin", "x.dll"), "x\n")
    check(runner.precheck_clash(ticket("t-ign", a, [J(a, "bin", "x.dll") + "（ビルド出力）"])) == [], "P2 無視設定のファイルは重ならない")
    sh(["git", "mv", "old.txt", "renamed.txt"], a)
    check(runner.precheck_clash(ticket("t-ren", a, [J(a, "old.txt")])) == ["old.txt"], "P2 名前を変えた元のファイルも重なる")
    sh(["git", "mv", "renamed.txt", "old.txt"], a)
    write(J(a, "DECISIONS.md"), "# d\n\n## 本体の書きかけ\n")
    tRec = ticket("t-rec", a, [J(a, "a.txt"), J(a, "DECISIONS.md"), J(a, "PENDING.md")])
    check(runner.precheck_clash(tRec) == [], "P2 記録（DECISIONS.md）だけの重なりでは止めない")
    runner.PRECHECK_RECORDS = True
    check(runner.precheck_clash(tRec) == ["DECISIONS.md"], "P2 NIGHT_PRECHECK_RECORDS=1 なら記録の重なりでも止める")
    runner.PRECHECK_RECORDS = False
    check(runner.precheck_clash(dict(tA, branch="night/x/t-a")) == [], "P2 もう始めた票（ブランチがある）は見ない")
    runner.PRECHECK = False
    check(runner.precheck_clash(tA) == [], "P2 NIGHT_PRECHECK=0 なら見ない")
    runner.PRECHECK = True
    check(runner.precheck_clash(ticket("t-nogit", os.path.join(EVAR, "NoGit"), ["x.md"])) == [], "P2 git の無いプロジェクトは見ない")
    os.remove(J(a, "new", "n.md"))
    os.rmdir(J(a, "new"))

    print("P3 司令塔の1歩", flush=True)
    waits = []
    runner.sleep_watch = lambda sec: waits.append(sec)
    runner.judge_tick = lambda *x, **k: None
    dirty_file = J(a, "docs", "資料", "一覧 表.md")
    before = (open(dirty_file, "rb").read(), open(J(a, "DECISIONS.md"), "rb").read(), sh(["git", "status", "--porcelain"], a))
    runner.write_json(runner.TONIGHT, {"night": runner.night_date(), "skipped": [], "tickets": [
        dict(tA, priority=1), ticket("t-b", a, [J(a, "b.txt"), J(a, "DECISIONS.md")], priority=2)]})
    st = {"triaged": 0, "last_hash": None}
    r = runner.step(st, datetime.datetime.now())
    T = tonight()
    check(r is None and started() == ["t-b"], f"P3 重なる票（優先度が上）は作業セッションが起きず、重ならない票が走る（{started()}）")
    check((T["t-a"].get("waiting") or {}).get("files") == ["docs/資料/一覧 表.md"] and T["t-a"]["status"] == "pending" and not T["t-a"].get("branch"),
          f"P3 重なる票は pending のまま、待っている理由が付く（{T['t-a'].get('waiting')}）")
    check((T["t-b"].get("outcome") or {}).get("kind") == "done" and (T["t-b"].get("merge") or {}).get("state") == "merged"
          and "made_t-b.txt" in sh(["git", "ls-tree", "-r", "--name-only", "HEAD"], a),
          f"P3 重ならない票は今までどおり合流する（{T['t-b'].get('outcome')}・{T['t-b'].get('merge')}）")
    check(before == (open(dirty_file, "rb").read(), open(J(a, "DECISIONS.md"), "rb").read(), sh(["git", "status", "--porcelain"], a)),
          "P3 本体の未コミットの変更は1バイトも変わらない")
    check("worktree" not in sh(["git", "worktree", "list", "--porcelain"], a).split("\n\n", 1)[-1] or "t-a" not in sh(["git", "worktree", "list"], a),
          "P3 重なる票の作業コピーは作られない")
    n_wait = len(waits)
    r = runner.step(st, datetime.datetime.now())
    r2 = runner.step(st, datetime.datetime.now())
    check(r is None and r2 is None and started() == ["t-b"] and len(waits) == n_wait + 2 and waits[-1] == 1,
          f"P3 始められる票が無い間は、セッションを起こさずに待つ（{started()}・待ち {waits[n_wait:]}）")
    log_text = runner.read_text(os.path.join(runner.STATE, "runner.log"))
    check(log_text.count("t-a は始めない") <= 1 and log_text.count("始められる票が無い") <= 1, "P3 待っている間、同じ知らせを繰り返さない")
    rep = open(runner.write_report(datetime.datetime.now(), None), encoding="utf-8").read()
    check("担当ファイルが本体で使用中" in rep and "t-a" in rep, "P3 稼働中の報告の「まだ途中」に、待っている理由が出る")
    commit(a, "human: commit the work in progress", "docs/資料/一覧 表.md")
    r = runner.step(st, datetime.datetime.now())
    T = tonight()
    check(started() == ["t-b", "t-a"] and not T["t-a"].get("waiting") and (T["t-a"].get("outcome") or {}).get("kind") == "done",
          f"P3 本体がコミットされたら、同じ夜のうちに始めて合流する（{started()}・{T['t-a'].get('outcome')}）")
    check("本体の書きかけ" in open(dirty_file, encoding="utf-8").read(), "P3 人がコミットした中身が本流に残っている")

    print("P4 夜の締め", flush=True)
    write(J(a, "a.txt"), "2 本体の書きかけ\n")
    runner.write_json(runner.TONIGHT, {"night": runner.night_date(), "skipped": [], "tickets": [
        ticket("t-c", a, [J(a, "a.txt")]), tonight()["t-b"]]})
    n = len(started())
    runner.step(st, datetime.datetime.now())
    check(len(started()) == n and (tonight()["t-c"].get("waiting") or {}).get("files") == ["a.txt"], "P4 重なる票は始まらず、待っている理由が付く")
    runner.finalize_night("試験の終わり")
    d = runner.read_json(runner.TONIGHT, {})
    ids = [t["id"] for t in d["tickets"]]
    sk = [s for s in d.get("skipped", []) if s.get("ticket") == "t-c"]
    check("t-c" not in ids and len(sk) == 1 and sk[0].get("why") == "main_dirty" and "a.txt" in sk[0].get("reason", "")
          and sk[0].get("source") == J(a, "PENDING.md") + ":1",
          f"P4 一度も始められなかった票は、見送りに理由と出典つきで載る（{sk}）")
    rows = [r_ for r_ in runner.ledger_rows() if r_.get("kind") == "outcome" and r_.get("ticket") == "t-c"]
    check(not rows, "P4 見送りにした票には、失敗の行き先を付けない（次の夜の仕分けで拾い直される）")
    rep = open(runner.write_report(datetime.datetime.now(), "試験の終わり"), encoding="utf-8").read()
    check("## 始めなかった票（1。" in rep and "`t-c`" in rep and "`a.txt`" in rep, "P4 報告の1枚目に「始めなかった票」の節が出る")
    det = open(os.path.join(runner.REPORTS, f"{runner.night_date()}_詳細.md"), encoding="utf-8").read()
    check("担当ファイルに本体の未コミットの変更が重なっていたので始めなかった" in det, "P4 全文の「見送り」に理由が出る")
finally:
    runner.git(["worktree", "prune"], os.path.join(EVAR, "ProjA")) if os.path.isdir(os.path.join(EVAR, "ProjA")) else None
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n不合格: {len(fails)} 件が落ちた" if fails else "\n合格")
sys.exit(1 if fails else 0)
