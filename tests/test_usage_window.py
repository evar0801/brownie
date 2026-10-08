"""利用枠（5時間枠）の見張りの試験（[W-002]。偽の claude・wt.py のスタブ・トークン0・約1分）。

    python tests/test_usage_window.py [--only U0,U2]
    終了コード 0＝全項目合格、1＝不合格

U0 読み取りと判定（usage_window.py・runner.result_line）: 書きかけの行を読まない／道具の結果の中の同じ文字列を拾わない／
   上限で断られた窓は 100%／戻る時刻を過ぎた窓は落とす／しきい値の判定／止めたセッションの途中の行を結果と取り違えない
U1 しきい値で待って再開: 1票目のセッションが 90% を流す → 2票目を始めずに待つ → 戻る時刻の後に残りの票を進める
U2 上限に当たっても夜を終えない: 1票目が上限で断られる（戻る時刻つき）→ 待つ → 戻った後に残りの票を進める
U3 NIGHT_USAGE_WAIT=0: 待たずに、理由つきで夜を終える
U4 戻る時刻が分からない上限: 小さな1往復で読み直し、通ったら続ける
U5 読み直しは通るのに本番は断られる: 成功を挟まずにまた当たったら、待ちを延ばしてから確かめる（間隔なしで繰り返さない）
U6 始める直前の確認（usage_blocked）・上限の実物の保存・.out の縮め・仕分けが断られたときの tonight.json の戻し
砂場は BrownieProject/sandbox/<日時>_usage_<項目>/。
"""
import datetime, json, os, subprocess, sys, tempfile, time

sys.stdout.reconfigure(encoding="utf-8")
NIGHT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ONLY = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else None
fails = []
# runner を読み込む項目（U0・U6）のために、状態の置き場と走査の根を使い捨ての場所へ向ける（本番の state/ と Workspace に触らない）
_UNIT = tempfile.mkdtemp(prefix="usage_unit_")
os.makedirs(os.path.join(_UNIT, "evar"))
os.environ.update(NIGHT_HOME=os.path.join(_UNIT, "home"), NIGHT_EVAR_ROOT=os.path.join(_UNIT, "evar"))


def check(ok, what):
    print(("  ok   " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        fails.append(what)


def on(n):
    return ONLY is None or n in ONLY


def sh(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def sandbox(name, items):
    run = os.path.join(NIGHT, "sandbox", f"{datetime.datetime.now():%Y-%m-%d_%H%M%S}_usage_{name}")
    evar, home = os.path.join(run, "evar"), os.path.join(run, "home")
    os.makedirs(os.path.join(home, "state"))
    os.makedirs(evar)
    for c in (["git", "init", "-q", "-b", "master"], ["git", "config", "user.name", "t"], ["git", "config", "user.email", "t@local"]):
        sh(c, evar)
    open(os.path.join(evar, ".gitignore"), "w").write("/*/\n")
    sh(["git", "add", ".gitignore"], evar)
    sh(["git", "commit", "-qm", "root"], evar)
    d = os.path.join(evar, "ProjA")
    os.makedirs(d)
    for rel, body in {"CLAUDE.md": "# 試験\n", "DECISIONS.md": "# DECISIONS\n",
                      "PENDING.md": "# PENDING\n\n" + "".join(f"- [ ] {x}\n" for x in items)}.items():
        open(os.path.join(d, rel), "w", encoding="utf-8", newline="\n").write(body)
    for c in (["git", "init", "-q", "-b", "master"], ["git", "config", "user.name", "t"], ["git", "config", "user.email", "t@local"],
              ["git", "add", "-A"], ["git", "commit", "-qm", "init"]):
        sh(c, d)
    sp = os.path.join(run, "wt_script.json")
    json.dump({}, open(sp, "w", encoding="utf-8"))
    return run, evar, home, sp


def night(run, evar, home, sp, usage, wall=90, stop_when="作業票が尽きた", **env_over):
    us = os.path.join(run, "usage_script.json")
    json.dump(usage, open(us, "w", encoding="utf-8"))
    env = dict(os.environ, NIGHT_EVAR_ROOT=evar, NIGHT_HOME=home, NIGHT_CLAUDE_BIN=os.path.join(NIGHT, "tests", "fake_claude.py"),
               NIGHT_WT_PY=os.path.join(NIGHT, "tests", "wt_stub.py"), WT_STUB_SCRIPT=sp, WT_STUB_LOG=os.path.join(run, "stub.jsonl"),
               NIGHT_WATCH_HOME=os.path.join(run, "watch_home"), NIGHT_MIN_FREE_PCT="0", NIGHT_MIN_FREE_GB="0",
               NIGHT_JUDGE_INTERVAL_SEC="0", NIGHT_MAX_TRIAGE="1", NIGHT_BUDGET_USD="1000", NIGHT_IDLE_POLL_SEC="2",
               NIGHT_END_AT="", NIGHT_MAX_HOURS="10", FAKE_CLAUDE_SLEEP="0.2", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               FAKE_USAGE_SCRIPT=us, NIGHT_USAGE_MARGIN_SEC="1")
    for k in ("NIGHT_MODEL", "NIGHT_RUNNER", "NIGHT_PROGRESS", "NIGHT_KIND", "NIGHT_SELF_REPO", "NIGHT_LABEL", "NIGHT_RUN_ID",
              "NIGHT_USAGE_WARN_PCT", "NIGHT_USAGE_HOLD_PCT", "NIGHT_USAGE_WEEK_HOLD_PCT", "NIGHT_USAGE_WAIT", "NIGHT_USAGE_PROBE_SEC", "NIGHT_USAGE_RETRY_SEC", "NIGHT_USAGE_PROBE_MODEL"):
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
    log = open(os.path.join(state, "runner.log"), encoding="utf-8", errors="replace").read() if os.path.exists(os.path.join(state, "runner.log")) else ""
    reports = os.path.join(home, "reports")
    rep = ""
    for f in sorted(os.listdir(reports)) if os.path.isdir(reports) else []:
        if f.endswith(".md") and "詳細" not in f:
            rep = open(os.path.join(reports, f), encoding="utf-8").read()
    return {"rows": rows, "log": log, "exit": p.returncode, "sec": time.time() - t0, "hung": hung, "report": rep}


def works(res):
    return [r for r in res["rows"] if r.get("kind") == "work"]


def ts(iso):
    return datetime.datetime.fromisoformat(iso).timestamp()


if on("U0"):
    print("U0 読み取りと判定", flush=True)
    sys.path.insert(0, NIGHT)
    import usage_window as uw
    ev = lambda u, reset, **kw: json.dumps({"type": "rate_limit_event", "rate_limit_info": dict(
        {"status": "allowed", "resetsAt": reset, "rateLimitType": "five_hour",
         "unifiedWindows": {"five_hour": {"utilization": u, "resetsAt": reset}, "seven_day": {"utilization": 0.3, "resetsAt": reset + 5000}}}, **kw)})
    tmp = os.path.join(tempfile.mkdtemp(prefix="usage_u0_"), "s.out")
    nested = json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": ev(0.99, 2000)}]}})
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"type": "system", "subtype": "init"}) + "\n" + ev(0.40, 2000) + "\n" + nested + "\n" + ev(0.62, 2000)[:50])
    info, off = uw.scan(tmp, 0)
    check(info is not None and uw.windows_of(info)["five_hour"]["utilization"] == 0.40,
          "書きかけの最終行と、道具の結果の中の rate_limit_event を拾わない（拾うのは外側の 40% だけ）")
    with open(tmp, "a", encoding="utf-8", newline="\n") as f:
        f.write(ev(0.62, 2000)[50:] + "\n")
    info2, off2 = uw.scan(tmp, off)
    check(info2 is not None and uw.windows_of(info2)["five_hour"]["utilization"] == 0.62 and off2 > off,
          "続きから読むと、書き終わった行（62%）を拾う")
    check(uw.scan(tmp, off2) == (None, off2) and uw.scan(tmp + ".none", 7) == (None, 7), "新しい行が無い・ファイルが無いときは None で、位置を進めない")
    st = uw.merge({}, info2, 1000, "s1")
    check(uw.pct(st, 1000) == 62 and uw.pct(st, 1000, "seven_day") == 30 and uw.pct(st, 2001) is None and uw.pct(st, 2001, "seven_day") == 30,
          "戻る時刻を過ぎた窓は落とす（5時間枠は 2000 で消え、7日枠は残る）")
    check(uw.verdict(st, 1000, 85, 95) is None and uw.verdict(st, 1000, 60, 95) == {"window": "five_hour", "pct": 62, "until": 2000.0}
          and uw.verdict(st, 1000, 0, 0) is None and uw.verdict(st, 1000, 85, 30)["window"] == "seven_day",
          "しきい値の判定（85% では待たない・60% なら待つ・0 は上限に当たるまで待たない・7日枠は別のしきい値）")
    rej = {"status": "rejected", "resetsAt": 3000, "rateLimitType": "seven_day_opus"}
    st2 = uw.merge(st, rej, 1000)
    check(uw.windows_of(rej) == {"seven_day_opus": {"utilization": 1.0, "resetsAt": 3000.0}} and uw.verdict(st2, 1000, 0, 0)["window"] == "seven_day_opus" and uw.live({"windows": {"five_hour": {"utilization": 0.9, "resetsAt": 1000 + 9 * 3600}}}, 1000) == {} and uw.windows_of({"unifiedWindows": {"five_hour": {"utilization": 0.5, "resetsAt": 1790953800000}}})["five_hour"]["resetsAt"] == 1790953800.0
          and uw.pct(st2, 1000) == 62, "上限で断られた窓は、使用率が載っていなくても 100% として待つ（ほかの窓の記録は残す）")
    other = {"windows": {"seven_day_sonnet": {"utilization": 0.97, "resetsAt": 5000}}}
    check(uw.verdict(other, 1000, 85, 95) is None and uw.verdict(st2, 1000, 85, 95)["window"] == "seven_day_opus",
          "モデル別の窓は、しきい値では待たない（97% でも進む）。実際に断られた窓（100%）だけ待つ")
    check("5時間枠 62%" in uw.label(st, 1000) and "7日枠 30%" in uw.label(st, 1000) and uw.label({}, 1000) == "" and uw.label({"windows": "x"}, 1) == "",
          f"表示（{uw.label(st, 1000)}）。記録が無い・壊れているときは空")
    import runner
    stream = "\n".join([json.dumps({"type": "system"}), json.dumps({"type": "assistant", "message": {"content": "途中"}})])
    done = stream + "\n" + json.dumps({"type": "result", "is_error": False, "total_cost_usd": 1.5, "result": "OK"})
    check(runner.result_line(stream) is None and runner.result_line(done)["total_cost_usd"] == 1.5
          and runner.result_line(json.dumps({"is_error": True, "result": "x"}))["result"] == "x",
          "止めたセッションの途中の行を結果と取り違えない（結果の行が無ければ None＝渡した上限額で数える道に入る）")

if on("U1"):
    print("U1 しきい値で待って再開", flush=True)
    run, evar, home, sp = sandbox("U1", ["作業1", "作業2", "作業3"])
    reset = time.time() + 14
    res = night(run, evar, home, sp, {"by_kind": {"run": [{"u": 0.90, "resetsAt": reset}, {"u": 0.10, "resetsAt": reset + 18000}, {"u": 0.12, "resetsAt": reset + 18000}]},
                                         "default": None})  # 評価役などほかのセッションは通知を流さない（流すと 90% の記録を上書きする）
    w, holds = works(res), [r for r in res["rows"] if r.get("kind") == "usage_hold"]
    check(not res["hung"] and res["exit"] == 0, f"自分で終わった（{res['sec']:.0f}秒・exit {res['exit']}）")
    check(len(holds) == 1 and holds[0].get("pct") == 90 and holds[0].get("window") == "five_hour" and "⏸ 5時間枠 90%" in res["log"],
          f"90% で待ちに入り、台帳とログに残した（usage_hold {len(holds)} 行）")
    check(len(w) == 3 and all(r.get("status") == "done" for r in w), f"3票とも最後まで進んだ（{[r.get('status') for r in w]}）")
    check(len(w) == 3 and ts(w[0]["at"]) < reset and all(ts(r["at"]) >= reset for r in w[1:]),
          "2票目と3票目は、戻る時刻より後に終わっている（待っている間に始めていない）")
    check("利用枠が戻ったので続ける" in res["log"] and "ブラウニーを終了: 「ブラウニーを止める」" in res["log"], "戻った後に続け、止めたのは STOP")
    check("- 利用枠:" in res["report"] and "枠が戻るのを待った回数 1" in res["report"], "報告の先頭に利用枠の行がある")
    check(w and w[0].get("u5") == 90, f"台帳の行に、その時点の5時間枠の使用率が残る（u5={w[0].get('u5') if w else None}）")

if on("U2"):
    print("U2 上限に当たっても夜を終えない", flush=True)
    run, evar, home, sp = sandbox("U2", ["作業1", "作業2"])
    reset = time.time() + 14
    res = night(run, evar, home, sp, {"by_kind": {"run": [{"u": 1.0, "resetsAt": reset, "limit": True}]}, "default": {"u": 0.05, "resetsAt": reset + 18000}},
                stop_when="→ done")
    w = works(res)
    check(not res["hung"] and res["exit"] == 0, f"自分で終わった（{res['sec']:.0f}秒・exit {res['exit']}）")
    check("ブラウニーを終了: 利用上限" not in res["log"] and "⏸ 5時間枠 100%" in res["log"], "上限に当たっても終わらず、待ちに入った")
    check(len(w) >= 2 and w[0].get("is_error") and ts(w[1]["at"]) >= reset and any(r.get("status") == "done" for r in w[1:]),
          f"戻る時刻の後に、次のセッションが進んだ（{[r.get('status') for r in w]}）")

if on("U3"):
    print("U3 NIGHT_USAGE_WAIT=0 は待たずに終える", flush=True)
    run, evar, home, sp = sandbox("U3", ["作業1", "作業2"])
    reset = time.time() + 3600
    res = night(run, evar, home, sp, {"by_kind": {"run": [{"u": 0.90, "resetsAt": reset}]}, "default": None},
                stop_when=None, wall=60, NIGHT_USAGE_WAIT=0)
    check(not res["hung"] and res["exit"] == 0 and "ブラウニーを終了: 5時間枠 90% に達した" in res["log"],
          f"理由つきで自分で終わった（{res['sec']:.0f}秒）")
    check(len(works(res)) == 1, f"2票目を始めていない（作業 {len(works(res))} 本）")
    check(any(r.get("kind") == "outcome" and r.get("outcome") == "done" for r in res["rows"]), "走り終えた1票目は、90% でも合流まで進んだ")

if on("U4"):
    print("U4 戻る時刻が分からない上限は、読み直して続ける", flush=True)
    run, evar, home, sp = sandbox("U4", ["作業1", "作業2"])
    res = night(run, evar, home, sp, {"by_kind": {"run": [{"limit": True, "bare": True}]}, "default": None}, stop_when="→ done")
    w, probes = works(res), [r for r in res["rows"] if r.get("kind") == "probe"]
    check(not res["hung"] and res["exit"] == 0 and "ブラウニーを終了: 利用上限" not in res["log"], f"上限で終わらなかった（{res['sec']:.0f}秒）")
    check(len(probes) >= 1 and not probes[0].get("is_error"), f"小さな1往復で読み直した（probe {len(probes)} 行）")
    check(any(r.get("status") == "done" for r in w[1:]), f"読み直しが通った後に、次のセッションが進んだ（{[r.get('status') for r in w]}）")

if on("U5"):
    print("U5 読み直しは通るのに本番は断られる、を間隔なしで繰り返さない", flush=True)
    run, evar, home, sp = sandbox("U5", ["作業1", "作業2"])
    res = night(run, evar, home, sp, {"by_kind": {"run": [{"limit": True, "bare": True}, {"limit": True, "bare": True}]}, "default": None},
                stop_when="→ done", NIGHT_USAGE_RETRY_SEC=4)
    w = works(res)
    check(not res["hung"] and res["exit"] == 0 and [r.get("status") for r in w[:3]] == ["limit", "limit", "done"],
          f"2回続けて断られても票は失敗にならず、3本目で進んだ（{[r.get('status') for r in w]}）")
    check(len(w) >= 3 and ts(w[2]["at"]) - ts(w[1]["at"]) >= 8 and "戻る時刻は分からない" in res["log"],
          f"2回目に当たった後は、読み直しの前に待った（{ts(w[2]['at']) - ts(w[1]['at']) if len(w) >= 3 else None:.0f}秒。4秒×2 以上）")

if on("U6"):
    print("U6 始める直前の確認・実物の保存・出力の縮め・仕分けの退避の戻し", flush=True)
    sys.path.insert(0, NIGHT)
    import runner
    runner.toast = lambda *a: None  # 試験から本物の通知を出さない
    os.makedirs(runner.SESSIONS, exist_ok=True)
    rej = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": time.time() + 900, "rateLimitType": "five_hour"}}
    base = os.path.join(runner.SESSIONS, "u6a")
    open(base + ".out", "w", encoding="utf-8", newline="\n").write(json.dumps(rej) + "\n")
    runner.usage_feed({"base": base, "sid": "u6a", "uoff": 0})
    kept = [json.loads(l) for l in open(runner.USAGE_EVENTS, encoding="utf-8")] if os.path.exists(runner.USAGE_EVENTS) else []
    check(len(kept) == 1 and kept[0]["kind"] == "event" and kept[0]["data"]["status"] == "rejected" and runner.usage_window.pct(runner.usage_state(), time.time()) == 100,
          "上限で断られた通知を、実物のまま usage_events.jsonl に残し、5時間枠を 100% と記録した")
    check(runner.usage_blocked() is True and runner.usage_blocked(follow=True) is True, "100% なら、審判官も、票の途中のセッション（評価役・解消役）も始めない")
    open(runner.USAGE_FILE, "w", encoding="utf-8").write(json.dumps({"at": time.time(), "windows": {"five_hour": {"utilization": 0.9, "resetsAt": time.time() + 900}}}))
    check(runner.usage_blocked() is True and runner.usage_blocked(follow=True) is False,
          "90% なら、審判官は始めないが、走り終えた票の評価役は起こす（85% で終わった票を数時間待たせない）")
    os.remove(runner.USAGE_FILE)
    check(runner.usage_blocked() is False, "記録が無ければ止めない（読み直しもしない）")

    class P:  # 終わったプロセスの代わり
        returncode = 1
        def poll(self): return 1
    def handle(name, body):
        b = os.path.join(runner.SESSIONS, name)
        for ext, text in ((".out", body), (".err", ""), (".prompt.md", "x")):
            open(b + ext, "w", encoding="utf-8", newline="\n").write(text)
        return {"p": P(), "sid": name, "guess": "x", "t0": time.time(), "base": b, "files": (), "cap": 1.5, "uoff": 0}
    big = "\n".join(json.dumps({"type": "assistant", "message": {"content": "上限 usage limit overloaded " + "あ" * 2000}}) for _ in range(200))
    r1 = runner.finish_claude(handle("u6b", big))
    size1 = os.path.getsize(os.path.join(runner.SESSIONS, "u6b.out"))
    check(r1.get("_cost_estimated") and r1["total_cost_usd"] == 1.5 and not runner.hit_usage_limit(r1) and not runner.overloaded(r1) and size1 < 300 * 1024,
          f"止めたセッション: 途中の発言の「上限」「overloaded」を取り違えず、渡した上限額で数え、.out は末尾だけ残す（{size1 // 1024}KB）")
    r2 = runner.finish_claude(handle("u6c", big + "\n" + json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0.7, "result": "OK"}) + "\n"))
    body2 = open(os.path.join(runner.SESSIONS, "u6c.out"), encoding="utf-8").read()
    check(r2["total_cost_usd"] == 0.7 and body2.count("\n") == 1 and json.loads(body2)["result"] == "OK", "終わったセッション: .out を結果の1行に縮める")

    us = os.path.join(_UNIT, "u6_usage.json")
    json.dump({"by_kind": {"triage": [{"limit": True, "bare": True}]}, "default": None}, open(us, "w", encoding="utf-8"))
    os.environ.update(FAKE_USAGE_SCRIPT=us, FAKE_CLAUDE_SLEEP="0")
    runner.CLAUDE = os.path.join(NIGHT, "tests", "fake_claude.py")
    os.makedirs(runner.STATE, exist_ok=True)
    json.dump({"night": runner.night_date(), "tickets": [{"id": "old-1", "status": "done", "merge": {"state": "busy"}}]},
              open(runner.TONIGHT, "w", encoding="utf-8"))
    runner._limit["hit"] = False
    res_t, tickets = runner.triage()
    os.environ.pop("FAKE_USAGE_SCRIPT", None)
    back = runner.read_json(runner.TONIGHT, None)
    check(runner.hit_usage_limit(res_t) and tickets == [] and back is not None and back["tickets"][0]["id"] == "old-1" and len(runner.busy_tickets()) == 1,
          "仕分けが上限で断られたら、退避した tonight.json を戻す（合流待ちの票を見失わない）")
    runner._limit["hit"] = False
    json.dump({"by_kind": {"triage": [{"limit": True, "bare": True}]}, "default": None}, open(us, "w", encoding="utf-8"))
    os.environ["FAKE_USAGE_SCRIPT"] = us
    json.dump({"night": "2001-01-01", "tickets": [{"id": "old-night", "status": "pending"}]}, open(runner.TONIGHT, "w", encoding="utf-8"))
    runner.triage()
    os.environ.pop("FAKE_USAGE_SCRIPT", None)
    check(not os.path.exists(runner.TONIGHT), "前の夜の票は戻さない（戻すと、今夜の票として動いてしまう）")
    runner._limit["hit"] = False
    open(runner.USAGE_FILE, "w", encoding="utf-8").write(json.dumps({"at": time.time() - 99999, "windows": {}}))
    calls = []
    real_probe, runner.usage_probe = runner.usage_probe, lambda: calls.append(1) or True
    runner._usage["probed_at"] = 0.0
    runner.usage_blocked(); runner.usage_blocked(); runner.usage_blocked()
    runner.usage_probe = real_probe
    check(len(calls) == 1, f"記録が古くても、読み直しは間隔を空けて1回だけ撃つ（{len(calls)} 回）")
    os.remove(runner.USAGE_FILE)

print(("全項目合格" if not fails else f"不合格 {len(fails)} 件: " + " ／ ".join(fails)), flush=True)
sys.exit(1 if fails else 0)
