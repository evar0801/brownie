"""ブラウニーを本物の wt.py で一晩ぶん回し切る結合試験（偽の claude・トークン0・約3分）。スタブの台本は使わない。

    python tests/test_overnight_real.py [--wt <wt.py>] [--wall 900]
    wt.py の既定は環境変数 NIGHT_SANDBOX_WT_PY、無ければ ~/.claude/tools/wt.py。終了コード 0＝全項目合格、1＝不合格

状況は本物で起こす（砂場の本流を試験の側が書き換える。ブラウニーの作業セッションの外で起こすので、③3 の見張りには当たらない）:
  ProjA  ふつうの run 2枚＋decide 1枚 → 合流（できた）
  ProjB  [conflict]。本流の同じパスに追跡外のファイルがあるので最初は 30。30 を見たら試験の側が本流の shared.txt の同じ行を
         書き換えてコミットし、追跡外のファイルを片付ける → 再試行で本当の衝突（10）→ 解消役1回 → 合流（できた）
  ProjC  [conflict][resolve-fail]。B と同じ → 10 → 解消役が解けない → 失敗。本流は人のコミットのまま無傷
  ProjD  [gatefail]。本流側の tests/night_gate.py が落ちる → 20 → 失敗
  ProjE  本流の同じパスに追跡外のファイル → 30。30 を見たら試験の側が片付ける → 再試行 → 合流（できた）
  ProjF  本流の同じパスに追跡外のファイルが残り続ける → 夜の終わりまで 30 → 保留
  ProjG  [hang] → 票ごとの時間切れで子プロセスごと止める → 失敗
  ProjH  [s0] ブランチが HANDOFF の §0 を書いた → wt.py が 20 → 失敗
  ProjL  [permission] → 手前の変更は合流し、票は保留（許可待ち）
  ProjM  [review-ng] → 評価役が止める → 保留
  ProjN  [protected] → .claude/settings.json に触れた → 関門が保留
  ProjP  [s0line] → 合流し、wt.py が「§0 へ:」の行を本流の HANDOFF §0 の管理区間に写す
  ProjQ  [slowgate]。本流側の tests/night_gate.py が眠り続ける → wt.py が --gate-timeout（30秒）で関門の木ごと止めて 20 → 失敗
  ProjZ  git リポジトリなし → 直接書く
完了判定: 放置0・3分類のちょうど1つ・報告の3節・壁時計の上限、に加えて、合流した票の中身が本流に本当に入っていること、
衝突の票・関門で弾いた票で本流が無傷なことを git で確かめる。
"""
import datetime, json, os, re, subprocess, sys, time

sys.stdout.reconfigure(encoding="utf-8")
NIGHT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WALL = int(sys.argv[sys.argv.index("--wall") + 1]) if "--wall" in sys.argv else 900
WT = (sys.argv[sys.argv.index("--wt") + 1] if "--wt" in sys.argv else
      os.environ.get("NIGHT_SANDBOX_WT_PY") or os.path.join(os.path.expanduser("~"), ".claude", "tools", "wt.py"))
INSTALL = os.path.join(os.path.dirname(os.path.abspath(WT)), "install_md_merge.py")
TICKET_TIMEOUT = 8
fails = []


def check(ok, what):
    print(("  ok   " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        fails.append(what)


def sh(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def pid_alive(pid):
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return str(pid) in r.stdout.split()


print(f"本物の wt.py: {WT}", flush=True)
if not os.path.isfile(WT):
    print("wt.py が無いので試せない", flush=True)
    sys.exit(1)
run = os.path.join(NIGHT, "sandbox", f"{datetime.datetime.now():%Y-%m-%d_%H%M%S}_overnight_real")
evar, home, watch_home = os.path.join(run, "evar"), os.path.join(run, "home"), os.path.join(run, "watch_home")
for d in (evar, home, os.path.join(watch_home, ".claude", "hooks")):
    os.makedirs(d)
open(os.path.join(watch_home, ".claude", "settings.json"), "w").write("{}\n")
print(f"砂場: {run}", flush=True)


def init_repo(d):
    for c in (["git", "init", "-q", "-b", "master"], ["git", "config", "user.name", "night-real"], ["git", "config", "user.email", "t@local"]):
        sh(c, d)


def register_driver(d):
    """本番と同じく統治文書のマージドライバ（wt.py と同じ版の merge_md.py）を登録する。
    install_md_merge.py は .claude の下を飛ばす（この砂場は作業コピー .claude/worktrees/… の中にある）ので、
    tests/sandbox.py と同じく、同じ関数で同じ設定を書く。"""
    import importlib.util, pathlib
    if not os.path.isfile(INSTALL):
        return
    spec = importlib.util.spec_from_file_location("install_md_merge", INSTALL)
    imm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(imm)
    for k, v in (("merge.evarmd.name", "Workspace governance docs"), ("merge.evarmd.driver", f'python "{imm.TOOL}" %O %A %B %P'),
                 ("merge.evarmd.recursive", "text")):
        sh(["git", "config", k, v], d)
    info = pathlib.Path(d, ".git", "info")
    info.mkdir(exist_ok=True)
    imm.rewrite(info / "attributes", [f"{f} merge=evarmd" for f in imm.ATTRS])
    imm.rewrite(info / "exclude", imm.EXCLUDES)


init_repo(evar)
open(os.path.join(evar, ".gitignore"), "w").write("/*/\n")
sh(["git", "add", ".gitignore"], evar)
sh(["git", "commit", "-qm", "root"], evar)

PROJ = {
    "ProjA": ["ふつうの作業その1 docs を1枚書く", "ふつうの作業その2 docs をもう1枚書く", "設定の優先順を決めたい（エヴァの判断待ち）"],
    "ProjB": ["[conflict] 本流と同じ行を書き換える作業"],
    "ProjC": ["[conflict][resolve-fail] 解けない衝突の作業"],
    "ProjD": ["[gatefail] 関門の試験に落ちる作業"],
    "ProjE": ["一度だけ本体が使用中の作業"],
    "ProjF": ["ずっと本体が使用中の作業"],
    "ProjG": ["[hang] 固まる作業（子プロセスつき）"],
    "ProjH": ["[s0] HANDOFF の §0 を書いてしまう作業"],
    "ProjL": ["[permission] push が要る作業"],
    "ProjM": ["[review-ng] 評価役に止められる作業"],
    "ProjN": ["[protected] Claude の設定に触れる作業"],
    "ProjP": ["[s0line] §0 に1行載せたい作業"],
    "ProjQ": ["[slowgate] 関門の試験が固まる作業"],
    "ProjZ": ["git の無いプロジェクトの作業"],
}
GATE = ("import os, sys\n"
        "bad = os.path.exists(os.path.join(sys.argv[1], 'gate_fail.txt'))\n"
        "print('関門の試験: gate_fail.txt がある' if bad else '関門の試験: 合格')\n"
        "sys.exit(1 if bad else 0)\n")
SLOW_GATE = ("import os, sys, time\n"
             "if os.path.exists(os.path.join(sys.argv[1], 'slow_gate.txt')):\n"
             "    open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', 'slow_gate_pid.txt'), 'w').write(str(os.getpid()))\n"
             "    print('関門の試験: 固まった', flush=True)\n"
             "    time.sleep(600)\n"
             "print('関門の試験: 合格')\n")
for name, items in PROJ.items():
    d = os.path.join(evar, name)
    os.makedirs(d)
    files = {"CLAUDE.md": f"# {name}\n試験用。\n", "DECISIONS.md": "# DECISIONS\n\n## X-001 はじめ\n本文\n", "shared.txt": "base\n",
             "HANDOFF.md": "# HANDOFF\n\n## §0 今の状態\n\n今の話\n\n## 1 そのほか\n",
             "PENDING.md": "# PENDING\n\n" + "".join(f"- [ ] {x}\n" for x in items)}
    if name == "ProjD":
        files["tests/night_gate.py"] = GATE
    if name == "ProjQ":
        files["tests/night_gate.py"] = SLOW_GATE
    for rel, body in files.items():
        os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
        open(os.path.join(d, rel), "w", encoding="utf-8", newline="\n").write(body)
    if name == "ProjZ":
        continue
    init_repo(d)
    register_driver(d)
    sh(["git", "add", "-A"], d)
    sh(["git", "commit", "-qm", "init"], d)

tid = lambda p, i=3, k="run": f"{p.lower()}-{k}-{i}"
# 本体が使用中（30）を本物で起こす: 票が足すファイルと同じパスに、本流で追跡外のファイルを置いておく
CLASH = {p: os.path.join(evar, p, "docs", f"{tid(p)}.md") for p in ("ProjB", "ProjC", "ProjE", "ProjF")}
for p, f in CLASH.items():
    os.makedirs(os.path.dirname(f), exist_ok=True)
    open(f, "w", encoding="utf-8").write("本流で誰かが書きかけのファイル\n")
EXPECT = {
    tid("ProjA"): ("done", "合流"), tid("ProjA", 4): ("done", "合流"), tid("ProjA", 5, "decide"): ("done", "合流"),
    tid("ProjB"): ("done", "合流"), tid("ProjC"): ("failed", "衝突"), tid("ProjD"): ("failed", "関門の試験に落ちた"),
    tid("ProjE"): ("done", "回目"), tid("ProjF"): ("hold", "本体が使用中"), tid("ProjG"): ("failed", "時間切れ"),
    tid("ProjH"): ("failed", "section 0"), tid("ProjL"): ("hold", "許可が要る"), tid("ProjM"): ("hold", "評価役が止めた"),
    tid("ProjN"): ("hold", ".claude/settings.json"), tid("ProjP"): ("done", "合流"), tid("ProjZ"): ("done", "直接"),
    tid("ProjQ"): ("failed", "timed out"),
}

env = dict(os.environ, NIGHT_EVAR_ROOT=evar, NIGHT_HOME=home, NIGHT_CLAUDE_BIN=os.path.join(NIGHT, "tests", "fake_claude.py"),
           NIGHT_WT_PY=WT, NIGHT_WATCH_HOME=watch_home, NIGHT_MIN_FREE_PCT="0", NIGHT_MIN_FREE_GB="0",
           NIGHT_TICKET_TIMEOUT_SEC=str(TICKET_TIMEOUT), NIGHT_WT_TIMEOUT_SEC="180", NIGHT_GATE_TIMEOUT_SEC="30",
           NIGHT_REVIEW_TIMEOUT_SEC="60", NIGHT_BUSY_RETRY_SEC="5", NIGHT_IDLE_POLL_SEC="2", NIGHT_JUDGE_INTERVAL_SEC="30",
           NIGHT_MAX_TRIAGE="1", NIGHT_BUDGET_USD="1000", NIGHT_END_AT="", NIGHT_MAX_HOURS="10", FAKE_CLAUDE_SLEEP="0.2",
           PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
for k in ("NIGHT_MODEL", "NIGHT_RUNNER", "NIGHT_PROGRESS", "NIGHT_KIND", "NIGHT_SELF_REPO", "NIGHT_LABEL", "NIGHT_RUN_ID",
          "NIGHT_GAME_PROJECTS", "NIGHT_NO_MERGE_PROJECTS", "WT_STUB_SCRIPT", "WT_STUB_LOG", "FAKE_REVIEW_NG"):
    env.pop(k, None)

print(f"ブラウニーを起動（壁時計の上限 {WALL} 秒）", flush=True)
state = os.path.join(home, "state")
runlog, ledger = os.path.join(state, "runner.log"), os.path.join(state, "ledger.jsonl")
console = open(os.path.join(run, "console.log"), "w", encoding="utf-8")
t0 = time.time()
p = subprocess.Popen([sys.executable, "-u", os.path.join(NIGHT, "runner.py")], env=env, stdout=console, stderr=subprocess.STDOUT)


def rows():
    try:
        return [json.loads(l) for l in open(ledger, encoding="utf-8") if l.strip()]
    except OSError:
        return []


done_acts, human = set(), {}
stopped, hung = False, False
while p.poll() is None:
    if time.time() - t0 > WALL:
        hung = True
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        break
    time.sleep(0.5)
    R = rows()
    codes = lambda i: [r.get("code") for r in R if r.get("kind") == "finish" and r.get("ticket") == i]
    # 30 を見たら（＝その票の作業セッションは終わっている）、人の操作を本物で起こす
    for proj in ("ProjB", "ProjC"):
        if proj not in done_acts and 30 in codes(tid(proj)):
            d = os.path.join(evar, proj)
            open(os.path.join(d, "shared.txt"), "w", encoding="utf-8", newline="\n").write(f"human {tid(proj)}\n")
            sh(["git", "add", "shared.txt"], d)
            sh(["git", "commit", "-qm", f"human: {proj} の shared.txt を書き換えた（夜の間）"], d)
            human[proj] = sh(["git", "rev-parse", "HEAD"], d).stdout.strip()
            os.remove(CLASH[proj])
            done_acts.add(proj)
            print(f"  {proj}: 30 を見たので、本流の shared.txt を書き換えてコミットし、書きかけのファイルを片付けた", flush=True)
    if "ProjE" not in done_acts and 30 in codes(tid("ProjE")):
        os.remove(CLASH["ProjE"])
        done_acts.add("ProjE")
        print("  ProjE: 30 を見たので、書きかけのファイルを片付けた", flush=True)
    log = open(runlog, encoding="utf-8", errors="replace").read() if os.path.exists(runlog) else ""
    last = {}
    for r in R:
        if r.get("kind") == "outcome":
            last[r.get("ticket")] = r.get("outcome")
    settled = all(tid(x) in last for x in ("ProjB", "ProjC", "ProjE"))
    if not stopped and "作業票が尽きた" in log and len(codes(tid("ProjF"))) >= 3 and settled:
        open(os.path.join(state, "STOP"), "w").close()
        stopped = True
        print(f"作業票が尽き、使用中の票を {len(codes(tid('ProjF')))} 回試したので STOP を置いた（{time.time() - t0:.0f}秒）", flush=True)
console.close()
elapsed = time.time() - t0
print(f"ブラウニーが終わった（{elapsed:.0f}秒・exit {p.returncode}）", flush=True)

# ---------------------------------------------------------------- 判定
R = rows()
log = open(runlog, encoding="utf-8", errors="replace").read() if os.path.exists(runlog) else ""
con = open(os.path.join(run, "console.log"), encoding="utf-8", errors="replace").read()
tonight = json.load(open(os.path.join(state, "tonight.json"), encoding="utf-8")) if os.path.exists(os.path.join(state, "tonight.json")) else {}
T = {t["id"]: t for t in tonight.get("tickets", [])}
by = lambda kind, i: [r for r in R if r.get("kind") == kind and r.get("ticket") == i]
codes = lambda i: [r.get("code") for r in by("finish", i)]
repo = lambda n: os.path.join(evar, n)
at_head = lambda n, path: sh(["git", "show", f"HEAD:{path}"], repo(n))

print("1. 10票以上を無人で一晩回し切る（本物の wt.py）", flush=True)
check(not hung, f"壁時計の上限 {WALL} 秒の内に終わった（{elapsed:.0f}秒）")
check(stopped and "ブラウニーを終了: 「ブラウニーを止める」が押された" in log, "作業票が尽きた後、STOP で夜が終わった")
check(len(T) >= 10 and set(EXPECT) <= set(T), f"作業票 {len(T)} 枚（足りない {sorted(set(EXPECT) - set(T))}）")
check("Traceback" not in log + con and "司令塔の例外" not in log, "例外が出ていない")
outs = [f for f in os.listdir(os.path.join(state, "finish")) if f.endswith(".out")] if os.path.isdir(os.path.join(state, "finish")) else []
check(not any("merge driver is not registered" in open(os.path.join(state, "finish", f), encoding="utf-8", errors="replace").read() for f in outs),
      "統治文書のマージドライバが登録された状態で合流した（本番と同じ）")
real_lines = sum("WT-REPORT" in open(os.path.join(state, "finish", f), encoding="utf-8", errors="replace").read() for f in outs)
check(outs and real_lines == len(outs) and any("fast-forward" in open(os.path.join(state, "finish", f), encoding="utf-8", errors="replace").read() for f in outs),
      f"合流はすべて本物の wt.py を通った（{real_lines}/{len(outs)} 回が WT-REPORT を返し、早送りの記録がある）")

print("2. 各票は「できた」「保留」「失敗」のちょうど1つ（放置0）", flush=True)
last = {}
for r in R:
    if r.get("kind") == "outcome":
        last[r.get("ticket")] = r
check(not [i for i in T if last.get(i, {}).get("outcome") not in ("done", "hold", "failed")], "台帳に行き先の無い票が0件")
check(all(T[i].get("status") != "pending" for i in T) and not any("決められなかった" in str(r.get("cause")) for r in last.values()),
      "途中のまま・安全網に落ちた票が無い")
for i, (kind, word) in EXPECT.items():
    r = last.get(i, {})
    check(r.get("outcome") == kind and word in str(r.get("cause")) + str((T.get(i) or {}).get("merge", {}).get("note", "")),
          f"{i} → {kind}（{word}）: 実際 {r.get('outcome')} — {str(r.get('cause'))[:100]}")

print("3. 本物の wt.py の終了コードの流れ", flush=True)
b, c, e, f = codes(tid("ProjB")), codes(tid("ProjC")), codes(tid("ProjE")), codes(tid("ProjF"))
check(b and b[0] == 30 and b[-2:] == [10, 0] and set(b[:-2]) == {30}, f"ProjB: 30 → 本当の衝突 10 → 解消役 → 0（{b}）")
check([r.get("status") for r in by("resolve", tid("ProjB"))] == ["resolved"], "ProjB: 解消役は1回で解いた")
check(c and c[0] == 30 and c[-1] == 10 and set(c[:-1]) == {30} and [r.get("status") for r in by("resolve", tid("ProjC"))] == ["unresolved"],
      f"ProjC: 30 → 10 → 解消役が解けず、もう一度は呼ばない（{c}）")
check(e and e[0] == 30 and e[-1] == 0 and set(e[:-1]) == {30}, f"ProjE: 30 → 再試行 → 0（{e}）")
check(len(f) >= 3 and set(f) == {30}, f"ProjF: 夜の終わりまで 30（{f}）")
check(codes(tid("ProjD")) == [20] and codes(tid("ProjH")) == [20], f"ProjD・ProjH: 20（{codes(tid('ProjD'))}・{codes(tid('ProjH'))}）")
g = by("work", tid("ProjG"))
hp = os.path.join(state, f"fake_hang_{tid('ProjG')}.json")
pids = json.load(open(hp)) if os.path.exists(hp) else {}
check(g and g[0].get("status") == "timeout" and pids and not any(pid_alive(x) for x in pids.values()) and not by("finish", tid("ProjG")),
      "ProjG: 時間切れで子プロセスごと止め、wt.py は呼ばなかった")
qlog = re.search(r"log: (\S*\.gate\.log)", str(last.get(tid("ProjQ"), {}).get("cause")))
qtext = open(qlog.group(1), encoding="utf-8", errors="replace").read() if qlog and os.path.isfile(qlog.group(1)) else ""
qout = re.search(r"出力は (\S+\.out)）", qtext)
check("関門の試験を流す" in qtext and qout and os.path.isfile(qout.group(1)) and "固まった" in open(qout.group(1), encoding="utf-8", errors="replace").read(),
      "ProjQ: 関門の出力（wt-gate.log）は作業コピーを外す前に state/finish へ写し、報告はその写しを指す（固まった試験の出力まで辿れる）")
sp = os.path.join(run, "slow_gate_pid.txt")
spid = int(open(sp).read()) if os.path.exists(sp) else None
check(codes(tid("ProjQ")) == [20] and spid and not pid_alive(spid),
      f"ProjQ: 関門が固まったら wt.py が {30} 秒で関門の木ごと止めて 20（{codes(tid('ProjQ'))}・固まった関門の pid {spid} は残っていない）")
check(len(by("review", tid("ProjE"))) == 1 and len(by("review", tid("ProjF"))) == 0,
      "30 の票: 本物の wt.py は関門より前に使用中を返すので、評価役は合流できるときに1回だけ（F は0回）")

print("4. 合流した票の中身が砂場の本流に本当に入った（git で確かめる）", flush=True)
for n, i in (("ProjA", tid("ProjA")), ("ProjA", tid("ProjA", 4)), ("ProjB", tid("ProjB")), ("ProjE", tid("ProjE")),
             ("ProjL", tid("ProjL")), ("ProjP", tid("ProjP"))):
    br = (T.get(i) or {}).get("branch", "")
    check(at_head(n, f"docs/{i}.md").returncode == 0 and not sh(["git", "branch", "--list", br], repo(n)).stdout.strip()
          and not os.path.isdir((T.get(i) or {}).get("worktree") or "_"),
          f"{i}: 本流の HEAD に docs/{i}.md がある・ブランチと作業コピーは wt.py が片付けた")
check("判断待ち" in at_head("ProjA", "DECISIONS.md").stdout, "decide の票の DECISIONS の追記が本流にある")
sb = at_head("ProjB", "shared.txt").stdout
check(f"night {tid('ProjB')}" in sb and f"human {tid('ProjB')}" in sb and "<<<<<<<" not in sb, f"ProjB: 解消した shared.txt（ブラウニーと人の両方）が本流にある（{sb.strip()!r}）")
check(human.get("ProjB") and sh(["git", "merge-base", "--is-ancestor", human["ProjB"], "HEAD"], repo("ProjB")).returncode == 0,
      "ProjB: 人のコミットは本流に残っている")
hand = at_head("ProjP", "HANDOFF.md").stdout
check(f"{tid('ProjP')} の結果を §0 に載せる" in hand and "BEGIN wt.py" in hand, "ProjP: 「§0 へ:」の行が本流の HANDOFF §0 の管理区間に入った")
for n in PROJ:
    if n == "ProjZ":
        continue
    check(sh(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], repo(n)).returncode != 0 and not os.path.exists(os.path.join(repo(n), ".git", "index.lock"))
          and not sh(["git", "diff", "--cached", "--name-only"], repo(n)).stdout.strip(),
          f"{n}: 本体に合流の途中・index.lock・ステージされた物が残っていない")

print("5. 衝突・関門で弾いた票で本流が無傷", flush=True)
check(human.get("ProjC") and sh(["git", "rev-parse", "HEAD"], repo("ProjC")).stdout.strip() == human["ProjC"]
      and at_head("ProjC", "shared.txt").stdout == f"human {tid('ProjC')}\n" and not sh(["git", "status", "--porcelain"], repo("ProjC")).stdout.strip(),
      "ProjC: 本流は人のコミットのまま（ブラウニーの変更・衝突の印は入っていない・作業ツリーも綺麗）")
check(sh(["git", "rev-parse", "--verify", "-q", f"refs/heads/{(T.get(tid('ProjC')) or {}).get('branch', 'x')}"], repo("ProjC")).returncode == 0,
      "ProjC: ブラウニーのブランチは記録として残した")
for n in ("ProjD", "ProjH", "ProjM", "ProjN", "ProjQ"):
    check(sh(["git", "rev-list", "--count", "HEAD"], repo(n)).stdout.strip() == "1" and at_head(n, "HANDOFF.md").stdout.find("今の話") >= 0,
          f"{n}: 本流は最初のコミットのまま（HANDOFF の §0 も元のまま）")
check(os.path.exists(CLASH["ProjF"]) and os.path.isdir((T.get(tid("ProjF")) or {}).get("worktree") or "_"),
      "ProjF: 本流の書きかけのファイルに触らず、作業コピーを残した（後で通せる）")

print("6. 報告が5分で読める1枚", flush=True)
rep_dir = os.path.join(home, "reports")
night = sorted(x for x in (os.listdir(rep_dir) if os.path.isdir(rep_dir) else []) if re.match(r"\d{4}-\d{2}-\d{2}\.md$", x))[-1:]
one = open(os.path.join(rep_dir, night[0]), encoding="utf-8").read() if night else ""
heads = [ln for ln in one.splitlines() if ln.startswith("## ")]
check([h.split("（")[0] for h in heads] == ["## できたこと", "## 保留中の許可", "## 失敗"], f"1枚目の節は3つだけで、この順（{heads}）")
secs = dict(re.findall(r"^## (できたこと|保留中の許可|失敗)[^\n]*\n(.*?)(?=^## |\Z)", one, re.S | re.M))
where = {i: [k for k, body in secs.items() if f"`{i}`" in body] for i in T}
check(all(len(v) == 1 for v in where.values()), f"どの票も3節のちょうど1つに載った（{ {i: v for i, v in where.items() if len(v) != 1} }）")
want = {"done": "できたこと", "hold": "保留中の許可", "failed": "失敗"}
check(all(where[i] == [want[last[i]["outcome"]]] for i in T if i in last), "載った節が台帳の行き先と一致")

print(("合格" if not fails else f"不合格 {len(fails)} 件: " + " ／ ".join(fails)) + f"（砂場 {run}・{elapsed:.0f}秒）", flush=True)
sys.exit(1 if fails else 0)
