"""前の夜の合流待ちを、次の起動のはじめに流し直す（runner.reflow_previous・finalize_previous・[W-004]）の試験。トークン0。
使い捨てのリポジトリを %TEMP% の下に作る。偽の claude・wt.py はスタブ。

    python tests/test_reflow.py     # 終了コード 0＝合格、1＝不合格

R0 前の夜: 5票とも wt.py が 30（本体が使用中）を返し、夜の締めで保留になる（merge.state は busy のまま残る）
R1 次の起動のはじめ（finalize_previous）: 本体が空いている票は合流する。行き先は前の夜の名前で台帳に残り、夜の名前は元に戻る
R2 本体がまだ塞がっている票は wt.py を呼ばずに保留のまま（本体の未コミットは1バイトも変わらない）
R3 許可が要る操作を残した票（needs_permission）は呼ばない ／ 利用枠待ちだった票（why=limit）は対象にしない
R4 呼んでもまた 30 だった票は、今までどおり保留で締まる
R5 今夜の報告の1枚目に「前の夜から流し直した票」の節が出る ／ もう一度呼んでも流し直さない ／ NIGHT_REFLOW_PREVIOUS=0 で切れる
"""
import datetime, importlib.util, json, os, shutil, subprocess, sys, tempfile

sys.stdout.reconfigure(encoding="utf-8")
NIGHT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="night_reflow_")
EVAR = os.path.join(TMP, "evar")
HOME = os.path.join(TMP, "home")
os.makedirs(EVAR)
os.makedirs(os.path.join(HOME, "state"))
SCRIPT = os.path.join(TMP, "wt_script.json")
json.dump({"t-ok": "30,0", "t-dirty": "30*", "t-perm": "30*", "t-still": "30*", "t-limit": "30*"}, open(SCRIPT, "w", encoding="utf-8"))

# 偽の claude。作業セッションは作業コピーにファイルを1つ作って終える（t-perm だけ needs_permission）。評価役は OK を返す
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
    name = os.path.basename(root)
    with open(os.path.join(root, "made_" + name + ".txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("made\n")
    status = "needs_permission" if name == "t-perm" else "done"
    with open(os.environ["NIGHT_PROGRESS"], "w", encoding="utf-8", newline="\n") as f:
        f.write("STATUS: " + status + "\n\n## ひとことで\n偽の作業役が1ファイル作った\n\n## 許可が要る操作\n- 操作: 試験の操作\n")
    res = status
print(json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0, "num_turns": 1, "session_id": sid, "result": res}))
''')
os.environ.update(NIGHT_EVAR_ROOT=EVAR, NIGHT_HOME=HOME, NIGHT_JUDGE_INTERVAL_SEC="0", PYTHONUTF8="1", NIGHT_CLAUDE_BIN=FAKE,
                  NIGHT_WT_PY=os.path.join(NIGHT, "tests", "wt_stub.py"), WT_STUB_SCRIPT=SCRIPT,
                  NIGHT_WATCH_HOME=os.path.join(TMP, "watch_home"), NIGHT_MIN_FREE_PCT="0", NIGHT_MIN_FREE_GB="0",
                  NIGHT_USAGE_PROBE_SEC="0", NIGHT_USAGE_HOLD_PCT="0", NIGHT_USAGE_WARN_PCT="0", NIGHT_STALE_DIRTY_HOURS="0",
                  NIGHT_GATE_TIMEOUT_SEC="120", NIGHT_WT_TIMEOUT_SEC="180", NIGHT_PRECHECK_POLL_SEC="1")
for k in ("NIGHT_SELF_REPO", "NIGHT_MODEL", "NIGHT_LABEL", "NIGHT_PRECHECK", "NIGHT_PRECHECK_RECORDS", "NIGHT_MAX_HOURS", "NIGHT_END_AT",
          "NIGHT_REFLOW_PREVIOUS", "NIGHT_BUSY_RETRY_SEC", "WT_PROJECT", "WT_WORKTREE", "WT_BRANCH", "WT_BASE"):
    os.environ.pop(k, None)

spec = importlib.util.spec_from_file_location("runner", os.path.join(NIGHT, "runner.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
runner.GATE_KEY = os.urandom(32)
runner.RUN_ID = "test-run-1"
runner.NIGHT = "2099-01-01"
runner.toast = lambda *a, **k: None
runner.sleep_watch = lambda sec: None
runner.judge_tick = lambda *x, **k: None
fails = []
IDS = ["t-ok", "t-dirty", "t-perm", "t-still", "t-limit"]
J = os.path.join


def check(ok, what):
    print(("  ok   " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        fails.append(what)


def sh(args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8").stdout


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
    sh(["git", "add", "--", "."], root)
    sh(["git", "commit", "-q", "-m", "init"], root)
    return root


def ticket(tid, root, n):
    return {"id": tid, "title": tid, "kind": "run", "effort": "low", "priority": n, "project_dir": root,
            "files": [os.path.join(root, f"made_{tid}.txt")], "source": os.path.join(root, "PENDING.md") + ":1",
            "goal": "g", "done_check": "d", "status": "pending", "sessions": 0}


def tonight():
    return {t["id"]: t for t in runner.read_json(runner.TONIGHT, {"tickets": []})["tickets"]}


def rows(kind, tid=None):
    return [r for r in runner.ledger_rows() if r.get("kind") == kind and (tid is None or r.get("ticket") == tid)]


def state(t):
    return ((t.get("outcome") or {}).get("kind"), (t.get("merge") or {}).get("state"))


try:
    a = make_repo("ProjA", {"a.txt": "1\n", "DECISIONS.md": "# d\n", "PENDING.md": "# p\n"})
    write(J(HOME, "projects.txt"), "ProjA\n")

    print("R0 前の夜: 5票とも本体が使用中（30）で合流できずに夜が終わる", flush=True)
    runner.write_json(runner.TONIGHT, {"night": runner.night_date(), "skipped": [],
                                      "tickets": [ticket(tid, a, i) for i, tid in enumerate(IDS, 1)]})
    st = {"triaged": 0, "last_hash": None}
    for _ in range(12):
        if all((t.get("merge") or {}).get("state") == "busy" for t in tonight().values()):
            break
        runner.step(st, datetime.datetime.now())
    T = tonight()
    check(all((T[i].get("merge") or {}).get("state") == "busy" for i in IDS), f"R0 5票とも合流待ち（busy）になった（{[state(T[i]) for i in IDS]}）")
    check(T["t-perm"].get("status") == "needs_permission", f"R0 t-perm は許可が要る操作を残して止まった（{T['t-perm'].get('status')}）")
    runner.finalize_night("「ブラウニーを止める」が押された")
    d = runner.read_json(runner.TONIGHT, {})
    for t in d["tickets"]:
        if t["id"] == "t-limit":
            t["merge"]["why"] = "limit"  # 利用枠待ちで夜を越えた票の形
    runner.write_json(runner.TONIGHT, d)
    T = tonight()
    check(all(state(T[i]) == ("hold", "busy") for i in IDS), f"R0 夜の締めで5票とも保留になり、合流待ちの印は残る（{[state(T[i]) for i in IDS]}）")

    # 朝: 人が本体で、t-dirty のブランチが持ち込むのと同じ名前のファイルを書きかけている
    dirty_file = J(a, "made_t-dirty.txt")
    write(dirty_file, "本体の書きかけ\n")
    before = (open(dirty_file, "rb").read(), sh(["git", "status", "--porcelain"], a))
    calls = {i: len(rows("finish", i)) for i in IDS}

    print("R1〜R4 次の起動のはじめ", flush=True)
    runner.NIGHT = "2099-01-02"
    runner.RUN_ID = "test-run-2"
    runner.GATE_KEY = os.urandom(32)  # 夜の回ごとに署名鍵が変わる（前の回の評価役の結果は読めない）
    runner.finalize_previous()
    T = tonight()
    added = {i: len(rows("finish", i)) - calls[i] for i in IDS}
    check(runner.NIGHT == "2099-01-02", "R1 夜の名前は今夜のものに戻っている")
    check(state(T["t-ok"]) == ("done", "merged") and "made_t-ok.txt" in sh(["git", "ls-tree", "-r", "--name-only", "HEAD"], a) and added["t-ok"] == 1,
          f"R1 本体が空いている票は、起動のはじめに合流する（{state(T['t-ok'])}・呼んだ回数 {added['t-ok']}）")
    last = rows("outcome", "t-ok")[-1]
    check(last.get("outcome") == "done" and last.get("night") == "2099-01-01", f"R1 行き先は前の夜の名前で台帳に残る（{last.get('night')}・{last.get('outcome')}）")
    check(added["t-dirty"] == 0 and state(T["t-dirty"]) == ("hold", "held"),
          f"R2 本体がまだ塞がっている票は wt.py を呼ばず、保留のまま（{state(T['t-dirty'])}・呼んだ回数 {added['t-dirty']}）")
    check(before == (open(dirty_file, "rb").read(), sh(["git", "status", "--porcelain"], a)), "R2 本体の未コミットの変更は1バイトも変わらない")
    check(added["t-perm"] == 0 and state(T["t-perm"]) == ("hold", "held"),
          f"R3 許可が要る操作を残した票は呼ばない（{state(T['t-perm'])}・呼んだ回数 {added['t-perm']}）")
    check(added["t-limit"] == 0 and state(T["t-limit"]) == ("hold", "held") and not rows("reflow", "t-limit"),
          f"R3 利用枠待ちだった票は対象にしない（{state(T['t-limit'])}・呼んだ回数 {added['t-limit']}）")
    check(added["t-still"] == 1 and state(T["t-still"]) == ("hold", "held"),
          f"R4 呼んでもまた 30 だった票は、保留で締まる（{state(T['t-still'])}・呼んだ回数 {added['t-still']}）")
    rf = {r["ticket"]: r for r in rows("reflow")}
    check({k: v.get("result") for k, v in rf.items()} == {"t-ok": "merged", "t-dirty": "skipped", "t-perm": "skipped", "t-still": "busy"}
          and all(v.get("night") == "2099-01-02" and v.get("from_night") == "2099-01-01" for v in rf.values()),
          f"R4 流し直しの結果が、今夜の名前で台帳に残る（{ {k: v.get('result') for k, v in rf.items()} }）")
    check("made_t-dirty.txt" in str(rf.get("t-dirty", {}).get("note")), f"R2 流さなかった理由に、塞いでいるファイルが出る（{rf.get('t-dirty', {}).get('note')}）")

    print("R5 報告・二度目・切り替え", flush=True)
    n_fin = len(rows("finish"))
    runner.finalize_previous()
    check(len(rows("finish")) == n_fin and len(rows("reflow")) == len(rf), "R5 もう一度呼んでも、締めた票は流し直さない")
    runner.write_json(runner.TONIGHT, {"night": "2099-01-02", "skipped": [], "tickets": []})
    rep = open(runner.write_report(datetime.datetime.now(), None), encoding="utf-8").read()
    check("## 前の夜から流し直した票（4。本流に入った 1）" in rep and "`t-ok`（2099-01-01 の票）" in rep and "made_t-dirty.txt" in rep,
          "R5 今夜の報告の1枚目に「前の夜から流し直した票」の節が出る")
    runner.REFLOW_PREVIOUS = False
    fake = {"night": "2098-12-31", "tickets": [{"id": "t-x", "merge": {"state": "busy"}, "status": "done"}]}
    check(runner.reflow_previous(fake) == 0 and len(rows("reflow")) == len(rf) and fake["tickets"][0]["merge"]["state"] == "busy",
          "R5 NIGHT_REFLOW_PREVIOUS=0 なら流し直さない")
    runner.REFLOW_PREVIOUS = True
finally:
    runner.git(["worktree", "prune"], os.path.join(EVAR, "ProjA")) if os.path.isdir(os.path.join(EVAR, "ProjA")) else None
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n不合格: {len(fails)} 件が落ちた" if fails else "\n合格")
sys.exit(1 if fails else 0)
