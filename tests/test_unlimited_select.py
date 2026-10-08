"""時間で終わらない既定・審判官の空回り止め・起動時の選択・前の夜の締め（旧形式の票）の試験（トークン0・数秒）。

    python tests/test_unlimited_select.py
    終了コード 0＝全項目合格、1＝不合格

U1 既定では夜の終わりが無い（compute_end が None）。NIGHT_MAX_HOURS / NIGHT_END_AT を指定すれば従来どおり終わる
U2 審判官は、前回から work・finish が増えたか pending があるときだけ起こす
U3 前の夜の締め: outcome の無い merged の票は done、merge=None の done 票は failed（旧形式の理由）。merge が文字列でも落ちない
U4 本番で projects.txt に有効な行が無ければ preflight で始めない
U5 choose_projects.py: EOF・0件・不正な番号・Enter だけ・書き戻し
砂場は一時ディレクトリ（本番の state/ には触らない）。
"""
import copy, datetime, json, os, shutil, subprocess, sys, tempfile

sys.stdout.reconfigure(encoding="utf-8")
NIGHT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []


def check(ok, what):
    print(("  ok   " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        fails.append(what)


tmp = tempfile.mkdtemp(prefix="brownie_unlimited_")
EVAR, HOME = os.path.join(tmp, "evar"), os.path.join(tmp, "home")
os.makedirs(os.path.join(HOME, "state"))
os.makedirs(EVAR)
for k in ("NIGHT_MAX_HOURS", "NIGHT_END_AT", "NIGHT_RUN_ID"):
    os.environ.pop(k, None)
os.environ.update(NIGHT_EVAR_ROOT=EVAR, NIGHT_HOME=HOME, PYTHONUTF8="1")
sys.path.insert(0, NIGHT)
import runner  # noqa: E402  （環境変数を向けてから読み込む）

runner.toast = lambda *a, **k: None
assert os.path.normcase(runner.STATE).startswith(os.path.normcase(tmp))

try:
    print("U1 時間で終わらない既定")
    check(runner.MAX_HOURS == 0 and runner.END_CLOCK == "", f"既定は MAX_HOURS=0・END_AT 空（{runner.MAX_HOURS}・{runner.END_CLOCK!r}）")
    check(runner.compute_end(datetime.datetime.now()) is None, "compute_end は None")
    code = ("import runner,datetime;s=datetime.datetime(2026,10,1,22,0);e=runner.compute_end(s);"
            "print(datetime.datetime.fromtimestamp(e).strftime('%H:%M'))")
    for env, want in (({"NIGHT_MAX_HOURS": "1"}, "23:00"), ({"NIGHT_END_AT": "08:00"}, "08:00")):
        r = subprocess.run([sys.executable, "-c", code], cwd=NIGHT, capture_output=True, text=True, encoding="utf-8",
                           env=dict(os.environ, **env))
        check(r.stdout.strip() == want, f"{env} なら {want} に終わる（{r.stdout.strip() or r.stderr[-200:]}）")
    src = open(os.path.join(NIGHT, "runner.py"), encoding="utf-8").read()
    check("夜の終わり なし（止めるまで）" in src, "起動ログは「夜の終わり なし（止めるまで）」")

    print("U2 審判官の空回り止め")
    runner.RUN_ID = "R1"
    runner.JUDGE_INTERVAL_SEC = 3600
    started = []
    runner.start_claude = lambda *a, **k: started.append(1) or {"p": None, "t0": 0}
    runner.remaining_budget = lambda: 1000.0
    runner.judge_tick()
    check(not started, "起動直後（work・finish も pending も無い）は起こさない")
    runner.ledger_add({"kind": "work", "ticket": "x"})
    runner.ledger_add({"kind": "work", "ticket": "y", "run": "OTHER"})
    check(runner.judge_progress_count() == 1, "今夜の回の行だけ数える")
    runner._judge["h"] = None
    runner.judge_tick()
    check(len(started) == 1, "work が増えたら起こす")
    runner._judge.update(h=None, next_at=0)
    runner.judge_tick()
    check(len(started) == 1, "増えていなければ次の時刻でも起こさない")
    runner.write_json(runner.TONIGHT, {"night": runner.night_date(), "tickets": [{"id": "p", "status": "pending"}]})
    runner._judge.update(h=None, next_at=0)
    runner.judge_tick()
    check(len(started) == 2, "pending があれば起こす（停滞を見る）")
    runner.ledger_add({"kind": "finish", "ticket": "x"})
    os.remove(runner.TONIGHT)
    runner._judge.update(h=None, next_at=0)
    runner.judge_tick()
    check(len(started) == 3, "finish が増えたら起こす")
    runner._judge.update(h=None, next_at=0)

    print("U3 前の夜の締め（旧形式の票）")
    merged = {"id": "a", "status": "done", "outcome": None,
              "merge": {"branch": "night/2026-09-24/a", "base": "master", "repo": "X", "state": "merged", "commit": "e169668"}}
    nochg = {"id": "b", "status": "done", "merge": {"state": "no_changes"}}
    direct = {"id": "c", "status": "done", "merge": {"state": "direct"}}
    nomerge = {"id": "d", "status": "done", "merge": None, "outcome": None}
    conflict = {"id": "e", "status": "done", "merge": {"state": "conflict"}}
    weird = {"id": "f", "status": "done", "merge": "merged"}
    path = os.path.join(tmp, "prev_tonight.json")
    runner.write_json(path, {"night": "2026-09-24", "tickets": [copy.deepcopy(x) for x in (merged, nochg, direct, nomerge, conflict, weird)]})
    runner.finalize_night("前の夜の回が締めずに終わった", path, previous=True)
    out = {t["id"]: t["outcome"] for t in runner.read_json(path, {})["tickets"]}
    for k, want in (("a", "done"), ("b", "done"), ("c", "done"), ("d", "failed"), ("e", "failed"), ("f", "failed")):
        check((out[k] or {}).get("kind") == want, f"{k}（{[merged, nochg, direct, nomerge, conflict, weird][ord(k) - 97]['merge']}）→ {want}")
    check("合流の記録から決めた" in out["a"]["cause"], "merged の理由は「合流の記録から決めた」")
    check("旧形式か強制終了" in out["d"]["cause"], "merge=None の理由は「旧形式か強制終了」")
    for rel in ("2026-09-25_034911_ブランチ_通常", "2026-09-25_3_区切りと続き"):
        real = os.path.join(NIGHT, "sandbox", rel, "home", "state", "tonight.json")
        if not os.path.isfile(real):
            print(f"  skip 実例 {rel} が無い")
            continue
        p = os.path.join(tmp, "real.json")
        shutil.copy(real, p)
        before = runner.read_json(p, {})["tickets"]
        runner.finalize_night("前の夜", p, previous=True)
        after = runner.read_json(p, {})["tickets"]
        ok = all((a["outcome"] or {}).get("kind") == ("done" if isinstance(b.get("merge"), dict) and b["merge"].get("state") == "merged"
                                                      else (a["outcome"] or {}).get("kind")) for a, b in zip(after, before))
        ok = ok and all(a.get("outcome") for a in after)
        check(ok, f"実例 {rel}: merged は done、全票に行き先（{[(a['outcome'] or {}).get('kind') for a in after]}）")

    print("U4 本番で選択なしなら始めない")
    runner.production = lambda: True
    open(runner.PROJECTS_FILE, "w", encoding="utf-8").write("# だけ\n")
    logs = []
    orig_log = runner.log
    runner.log = lambda s: logs.append(s)
    check(runner.preflight() is False and any("選ばれていない" in s for s in logs), "preflight が False・理由をログ")
    runner.log = orig_log

    print("U5 choose_projects.py")
    for p, n in (("ProjA", 2), ("ProjB", 0), (os.path.join("排除", "Old"), 1), (os.path.join("Group", "ProjC"), 0)):
        d = os.path.join(EVAR, p)
        os.makedirs(d, exist_ok=True)
        body = "# PENDING\n\n## [P-00N] タイトル\n" + "".join(f"## [P-{i:03d}] x\n" for i in range(1, n + 1))
        open(os.path.join(d, "PENDING.md" if p != os.path.join("Group", "ProjC") else "HANDOFF.md"), "w", encoding="utf-8").write(body)
    pf = runner.PROJECTS_FILE
    wt = os.path.join(EVAR, "ProjA", ".claude", "worktrees", "x")
    os.makedirs(wt)
    open(os.path.join(wt, "PENDING.md"), "w", encoding="utf-8").write("# PENDING\n")
    os.makedirs(os.path.join(EVAR, "ProjA", ".claude_foo"))
    open(os.path.join(EVAR, "ProjA", ".claude_foo", "PENDING.md"), "w", encoding="utf-8").write("# PENDING\n")
    open(pf, "w", encoding="utf-8").write("ProjA\n")
    srcs = [os.path.relpath(f, EVAR).replace("\\", "/") for f in runner.source_files()]
    check(not any("/.claude/" in f for f in srcs), f"source_files は .claude の中を拾わない: {srcs}")
    # glob は隠しディレクトリを辿らないので、要素での除外そのものも直接確かめる
    import re as _re
    check(".claude" in _re.split(r"[\\/]", r"ProjA\.claude\worktrees\x\PENDING.md")
          and ".claude" not in _re.split(r"[\\/]", r"ProjA\.claude_foo\PENDING.md"), "要素が .claude のときだけ外す（.claude_foo は巻き込まない）")

    print("U6 入れ子のプロジェクト（いちばん深い行に従う）")
    for rel in ("Parent", os.path.join("Parent", "Child")):
        os.makedirs(os.path.join(EVAR, rel), exist_ok=True)
        open(os.path.join(EVAR, rel, "PENDING.md"), "w", encoding="utf-8").write("# PENDING\n")
    for txt, want in (("Parent\n# Parent/Child\n", ["Parent/PENDING.md"]), ("# Parent\nParent/Child\n", ["Parent/Child/PENDING.md"])):
        open(pf, "w", encoding="utf-8").write(txt)
        got = [os.path.relpath(f, EVAR).replace("\\", "/") for f in runner.source_files()[:-1]]
        check(got == want, f"{txt.strip().replace(chr(10), ' / ')} → {got}")

    print("U7 何日も走るとき、日付が変わったら仕分けし直す")
    saved = {k: getattr(runner, k) for k in ("END_TS", "calendar_night", "triage", "judge_tick", "retry_busy", "disk_problem",
                                             "sources_hash", "remaining_budget", "spent_tonight", "write_json")}
    calls = []
    runner.END_TS = None
    runner.judge_tick = lambda *a, **k: None
    runner.retry_busy = lambda *a, **k: 0
    runner.disk_problem = lambda: None
    runner.sources_hash = lambda: "same"
    runner.triage = lambda: calls.append(1) or ({"is_error": False, "result": ""}, [])
    if os.path.exists(runner.TONIGHT):
        os.remove(runner.TONIGHT)
    runner.calendar_night = lambda: "2026-10-01"
    st = {"triaged": runner.MAX_TRIAGE_PER_NIGHT, "last_hash": "same", "day": "2026-10-01"}
    runner.sleep_watch = lambda sec: None
    runner.revive_leftovers = lambda: False
    runner.write_report = lambda *a, **k: None
    runner.step(st, datetime.datetime.now())
    check(not calls, "上限に達していて日付が同じなら仕分けしない")
    runner.calendar_night = lambda: "2026-10-02"
    runner.step(st, datetime.datetime.now())
    check(calls == [1] and st["triaged"] == 1 and st["day"] == "2026-10-02", f"日付が変わったら次の歩で仕分けする（{calls}・{st}）")
    for k, v in saved.items():
        setattr(runner, k, v)

    def choose(stdin):
        return subprocess.run([sys.executable, os.path.join(NIGHT, "choose_projects.py")], input=stdin, capture_output=True,
                              text=True, encoding="utf-8", env=os.environ, timeout=60)

    open(pf, "w", encoding="utf-8").write("# 説明の行。\n")
    r = choose("")
    check(r.returncode == 1 and "選べないので始めません" in r.stdout, "EOF なら始めない")
    r = choose("\n")
    check(r.returncode == 1 and "選ばれていないので始めません" in r.stdout, "前回なしで Enter だけなら始めない")
    check(".claude/" not in r.stdout and "ProjA/.claude_foo" in r.stdout, "選択画面の候補に .claude の中の作業コピーが出ない")
    check("Group/ProjC" in r.stdout and "排除" not in r.stdout and "ProjA  PENDING 2件" in r.stdout, "候補と件数（雛形は数えない・排除は出ない）")
    open(pf, "w", encoding="utf-8").write("# 説明。\nProjB\n# Gone/Old\nNotExist\n")
    r = choose("1 x\n9\n1 6\n")
    check(r.returncode == 0 and "数字以外" in r.stdout and "存在しない番号" in r.stdout, "不正な入力は理由を出して聞き直す")
    check("ProjB  PENDING 0件  ← 前回" in r.stdout, "前回の行に「← 前回」")
    lines = open(pf, encoding="utf-8").read().splitlines()
    act = [l for l in lines if l and not l.startswith("#")]
    check(act == ["Group/ProjC", "ProjB"] and "# ProjA" in lines and "# NotExist" in lines and "# Gone/Old" in lines,
          f"書き戻し（選んだものだけ # なし・他は # 付きで残る）: {lines[-5:]}")
    check("inbox.md に書いたものは選択に関係なく実行します" in r.stdout, "最後に対象と inbox の扱いを出す")
    r = choose("\n")
    act2 = [l for l in open(pf, encoding="utf-8").read().splitlines() if l and not l.startswith("#")]
    check(r.returncode == 0 and act2 == act, "Enter だけ＝前回と同じ")
    runner.production = lambda: False
    check(runner.selected_projects() is not None, "書き戻した projects.txt を runner が読める")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("合格" if not fails else f"不合格 {len(fails)}件")
sys.exit(1 if fails else 0)
