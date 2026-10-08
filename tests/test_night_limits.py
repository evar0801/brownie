"""ブラウニーが暴走しないための歯止めの試験（偽の claude・wt.py のスタブ・トークン0・約2分）。runner.py を本物のプロセスとして起こす。

    python tests/test_night_limits.py [--only L1,L3]
    終了コード 0＝全項目合格、1＝不合格

L1 夜の終わり（②2）: 起動から10秒で夜を終える → 新しい票を始めない・合流待ち（30）は保留・審判官は止める・終了する
L2 一晩の上限（②1）: 固まって止めた票は渡した上限額（$15）を使ったものとして数える → 上限 $15.5 で次の票を始めずに終える
L3 空き容量（②12）: しきい値を下回っていたら始めない
L4 司令塔の例外が続いた（②8）: 票の並べ替えで毎回落ちる → 3回続いたら夜を終える（それまでは落ちずに続ける）。票は行き先つきで残る
L5 前の夜の票（②3）: 締めずに終わった前の夜の「合流待ち」の票に、今夜を始める前に行き先（保留）を付ける
L6 ロック（②17）: ブラウニーが動いている（ロックの pid が生きている）なら、自己試験より前に始めずに終わる
砂場は BrownieProject/sandbox/<日時>_limits_<項目>/。
"""
import datetime, json, os, subprocess, sys, time

sys.stdout.reconfigure(encoding="utf-8")
NIGHT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ONLY = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else None
fails = []


def check(ok, what):
    print(("  ok   " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        fails.append(what)


def sh(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def sandbox(name, projects, script=None):
    run = os.path.join(NIGHT, "sandbox", f"{datetime.datetime.now():%Y-%m-%d_%H%M%S}_limits_{name}")
    evar, home = os.path.join(run, "evar"), os.path.join(run, "home")
    os.makedirs(os.path.join(home, "state"))
    os.makedirs(evar)
    for c in (["git", "init", "-q", "-b", "master"], ["git", "config", "user.name", "t"], ["git", "config", "user.email", "t@local"]):
        sh(c, evar)
    open(os.path.join(evar, ".gitignore"), "w").write("/*/\n")
    sh(["git", "add", ".gitignore"], evar)
    sh(["git", "commit", "-qm", "root"], evar)
    for pname, items in projects.items():
        d = os.path.join(evar, pname)
        os.makedirs(d)
        for rel, body in {"CLAUDE.md": "# 試験\n", "DECISIONS.md": "# DECISIONS\n",
                          "PENDING.md": "# PENDING\n\n" + "".join(f"- [ ] {x}\n" for x in items)}.items():
            open(os.path.join(d, rel), "w", encoding="utf-8", newline="\n").write(body)
        for c in (["git", "init", "-q", "-b", "master"], ["git", "config", "user.name", "t"], ["git", "config", "user.email", "t@local"],
                  ["git", "add", "-A"], ["git", "commit", "-qm", "init"]):
            sh(c, d)
    sp = os.path.join(run, "wt_script.json")
    json.dump(script or {}, open(sp, "w", encoding="utf-8"))
    return run, evar, home, sp


def night(run, evar, home, sp, wall, stop_when=None, **env_over):
    env = dict(os.environ, NIGHT_EVAR_ROOT=evar, NIGHT_HOME=home, NIGHT_CLAUDE_BIN=os.path.join(NIGHT, "tests", "fake_claude.py"),
               NIGHT_WT_PY=os.path.join(NIGHT, "tests", "wt_stub.py"), WT_STUB_SCRIPT=sp, WT_STUB_LOG=os.path.join(run, "stub.jsonl"),
               NIGHT_WATCH_HOME=os.path.join(run, "watch_home"), NIGHT_MIN_FREE_PCT="0", NIGHT_MIN_FREE_GB="0",
               NIGHT_JUDGE_INTERVAL_SEC="0", NIGHT_MAX_TRIAGE="1", NIGHT_BUDGET_USD="1000", NIGHT_IDLE_POLL_SEC="2",
               NIGHT_END_AT="", NIGHT_MAX_HOURS="10", FAKE_CLAUDE_SLEEP="0.2", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    for k in ("NIGHT_MODEL", "NIGHT_RUNNER", "NIGHT_PROGRESS", "NIGHT_KIND", "NIGHT_SELF_REPO", "NIGHT_LABEL", "NIGHT_RUN_ID"):
        env.pop(k, None)
    env.update({k: str(v) for k, v in env_over.items()})
    state = os.path.join(home, "state")
    con = open(os.path.join(run, "console.log"), "w", encoding="utf-8")
    t0 = time.time()
    p = subprocess.Popen([sys.executable, "-u", os.path.join(NIGHT, "runner.py")], env=env, stdout=con, stderr=subprocess.STDOUT)
    hung, stopped = False, False
    while p.poll() is None:
        if time.time() - t0 > wall:
            hung = True
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
            break
        time.sleep(0.5)
        log = open(os.path.join(state, "runner.log"), encoding="utf-8", errors="replace").read() if os.path.exists(os.path.join(state, "runner.log")) else ""
        if stop_when and not stopped and stop_when in log:
            open(os.path.join(state, "STOP"), "w").close()
            stopped = True
    p.wait()
    con.close()
    rows = [json.loads(l) for l in open(os.path.join(state, "ledger.jsonl"), encoding="utf-8")] if os.path.exists(os.path.join(state, "ledger.jsonl")) else []
    tonight = json.load(open(os.path.join(state, "tonight.json"), encoding="utf-8")) if os.path.exists(os.path.join(state, "tonight.json")) else {}
    log = open(os.path.join(state, "runner.log"), encoding="utf-8", errors="replace").read() if os.path.exists(os.path.join(state, "runner.log")) else ""
    last = {}
    for r in rows:
        if r.get("kind") == "outcome":
            last[r.get("ticket")] = r
    return {"rows": rows, "tonight": tonight, "log": log, "exit": p.returncode, "sec": time.time() - t0, "hung": hung, "last": last,
            "state": state}


def on(n):
    return ONLY is None or n in ONLY


def all_have_outcome(res):
    ids = [t["id"] for t in res["tonight"].get("tickets", [])]
    return bool(ids) and all(res["last"].get(i, {}).get("outcome") in ("done", "hold", "failed") for i in ids)


if on("L1"):
    print("L1 夜の終わり（起動から10秒）", flush=True)
    run, evar, home, sp = sandbox("L1", {"Proj0": ["ずっと本体が使用中"], "ProjA": ["作業1", "作業2", "作業3", "作業4"]},
                                  {"proj0-run-3": "30*"})
    res = night(run, evar, home, sp, wall=120, NIGHT_MAX_HOURS=10 / 3600, NIGHT_END_GRACE_SEC=5, FAKE_CLAUDE_SLEEP=2,
                NIGHT_JUDGE_INTERVAL_SEC=3, NIGHT_BUSY_RETRY_SEC=5)
    check(not res["hung"] and res["exit"] == 0 and res["sec"] < 60, f"夜の終わりの後に自分で終わった（{res['sec']:.0f}秒・exit {res['exit']}）")
    check("ブラウニーを終了: 夜の終わり" in res["log"], "終わった理由が「夜の終わり」")
    works = [r for r in res["rows"] if r.get("kind") == "work"]
    check(0 < len(works) < 5, f"夜の終わりの後は新しい票を始めなかった（作業セッション {len(works)} 本・票5枚）")
    check(all_have_outcome(res), "どの票にも行き先がある（放置0件）")
    check(res["last"].get("proj0-run-3", {}).get("outcome") == "hold", "合流待ち（30）の票は保留に入った")
    check(any("未着手" in str(r.get("cause")) for r in res["last"].values()), "始めなかった票は「未着手のまま夜が終わった」")
    check(all(float(r.get("cost") or 0) == 0 for r in res["rows"] if r.get("kind") == "judge") and "審判官" in res["log"],
          "審判官は夜の間に走り、夜の終わりの後に残っていない（終了まで待たずに止める）")

if on("L2"):
    print("L2 一晩の上限（固まった票は上限額で数える）", flush=True)
    run, evar, home, sp = sandbox("L2", {"Proj0": ["[hang] 固まる作業"], "ProjA": ["作業1", "作業2"]})
    res = night(run, evar, home, sp, wall=120, NIGHT_BUDGET_USD=15.5, NIGHT_SESSION_CAP_USD=15, NIGHT_TICKET_TIMEOUT_SEC=4)
    w = [r for r in res["rows"] if r.get("kind") == "work"]
    check(not res["hung"] and res["exit"] == 0, f"固まらずに終わった（{res['sec']:.0f}秒）")
    check(w and float(w[0].get("cost") or 0) == 15 and w[0].get("cost_estimated") and w[0].get("status") == "timeout",
          f"固まった票を $15（渡した上限）として数えた（{[(r.get('cost'), r.get('cost_estimated'), r.get('status')) for r in w]}）")
    check("ブラウニーを終了: 一晩の上限" in res["log"], "上限に達して夜を終えた")
    check(len(w) == 1 and all_have_outcome(res), f"次の票は始めず、どの票にも行き先がある（作業セッション {len(w)} 本）")
    hp = os.path.join(res["state"], "fake_hang_proj0-run-3.json")
    pids = json.load(open(hp)) if os.path.exists(hp) else {}
    alive = [pid for pid in pids.values() if str(pid) in subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout.split()]
    check(pids and not alive, "固まった票の子プロセスは残っていない")

if on("L3"):
    print("L3 空き容量が足りなければ始めない", flush=True)
    run, evar, home, sp = sandbox("L3", {"ProjA": ["作業1"]})
    res = night(run, evar, home, sp, wall=60, NIGHT_MIN_FREE_PCT=100, NIGHT_MIN_FREE_GB=10 ** 9)
    check(res["exit"] == 0 and "空き容量が足りない" in res["log"] and "ので始めない" in res["log"], "理由を書いて始めなかった")
    check(not [r for r in res["rows"] if r.get("kind") in ("triage", "work")] and not os.path.exists(os.path.join(res["state"], "runner.lock")),
          "仕分けも作業もせず、ロックも残していない")

if on("L4"):
    print("L4 司令塔の例外が続いたら夜を終える（それまでは続ける）", flush=True)
    run, evar, home, sp = sandbox("L4", {"ProjA": ["[badprio] 並べ替えで落ちる票", "ふつうの票"]})
    res = night(run, evar, home, sp, wall=120)
    check(not res["hung"] and res["exit"] == 0, f"例外で落ちずに終わった（{res['sec']:.0f}秒・exit {res['exit']}）")
    check(res["log"].count("司令塔の例外（") == 3 and "ブラウニーを終了: 司令塔の例外が 3 回続いた" in res["log"],
          f"3回続けて落ちるまでは続け、3回で夜を終えた（{res['log'].count('司令塔の例外（')}回）")
    check(all_have_outcome(res), "どの票にも行き先がある（夜の締めは走った）")

if on("L5"):
    print("L5 前の夜の「合流待ち」の票に、今夜を始める前に行き先を付ける", flush=True)
    run, evar, home, sp = sandbox("L5", {"ProjA": []})
    old = {"night": "2000-01-01", "tickets": [{"id": "old-busy", "title": "前の夜の票", "status": "done", "project_dir": os.path.join(evar, "ProjA"),
                                               "repo": os.path.join(evar, "ProjA"), "branch": "night/2000-01-01/old-busy", "base": "master",
                                               "merge": {"state": "busy", "tries": 3}}], "skipped": []}
    json.dump(old, open(os.path.join(home, "state", "tonight.json"), "w", encoding="utf-8"), ensure_ascii=False)
    res = night(run, evar, home, sp, wall=90, stop_when="作業票が尽きた")
    o = [r for r in res["rows"] if r.get("kind") == "outcome" and r.get("ticket") == "old-busy"]
    check(len(o) == 1 and o[0].get("outcome") == "hold" and o[0].get("night") == "2000-01-01" and "前の夜" in o[0].get("cause", ""),
          f"前の夜の票を保留として1回だけ締め、今夜の名前で合流させ直さなかった（{[(r.get('outcome'), r.get('night')) for r in o]}）")
    check(not [r for r in res["rows"] if r.get("kind") == "finish" and r.get("ticket") == "old-busy"], "前の夜の票に wt.py を呼ばなかった")

if on("L6"):
    print("L6 ブラウニーが動いていれば、自己試験より前に始めずに終わる", flush=True)
    run, evar, home, sp = sandbox("L6", {"ProjA": ["作業1"]})
    open(os.path.join(home, "state", "runner.lock"), "w").write(str(os.getpid()))
    res = night(run, evar, home, sp, wall=60)
    check(res["exit"] == 0 and "ブラウニーはすでに動いています" in res["log"] and res["sec"] < 30 and not res["rows"],
          f"すぐに終わり、何もしなかった（{res['sec']:.0f}秒）")
    check(open(os.path.join(home, "state", "runner.lock")).read().strip() == str(os.getpid()), "他人のロックは消していない")

print(("合格" if not fails else f"不合格 {len(fails)} 件: " + " ／ ".join(fails)), flush=True)
sys.exit(1 if fails else 0)
