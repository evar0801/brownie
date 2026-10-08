"""ブラウニーを一晩ぶん無人で回し切る試験（偽の claude・wt.py のスタブ・トークン0・約2分）。

    python tests/test_overnight.py [--wall 600] [--quiet]
    終了コード 0＝全項目合格、1＝不合格（落ちた項目を FAIL で書く）

砂場は tests/sandbox.py と同じく BrownieProject/sandbox/<日時>_overnight/ に作る（evar＝砂場のプロジェクト、home＝台帳・報告）。
砂場の根（evar）は本番の C:\\Workspace と同じく git リポジトリにして、直下を全部除外する（独立したリポジトリの無いプロジェクトは直接書く）。
作業コピーの外の本番のプロジェクトは読まない・書かない（NIGHT_EVAR_ROOT・NIGHT_HOME・NIGHT_WT_PY を全部砂場へ向ける）。

20票の内訳（PENDING の行の印で偽の claude の振る舞いを、wt.py のスタブの台本で終了コードを決める）:
  ProjA  ふつうの run 2枚＋decide 1枚 → できた（同じプロジェクトで続けて合流）
  ProjB  [conflict]（本流の書き換えはスタブの台本 human）→ 10 → 解消役1回 → もう一度 finish → できた
  ProjC  [conflict][resolve-fail]   → 10 → 解消役が解けない → 失敗（衝突）
  ProjD  スタブが 20                → 失敗（検査で弾いた・原因つき）
  ProjE  スタブが 30,0              → 30 → 再試行 → できた（評価役は1回だけ）
  ProjF  スタブがいつも 30          → 再試行を重ね、夜の終わりまで 30 → 保留（本体使用中）
  ProjG  [hang]                     → 票ごとの時間切れで子プロセスごと止める → 失敗（時間切れ）。後ろの票は進む
  ProjH  スタブが眠り続ける         → wt.py の呼び出しの時間切れ → 失敗
  ProjI  [frozen]（ブラウニー自身に見立てる）→ 関門が凍結に触れたと判定 → 保留（許可待ち）
  ProjJ  [review-ng]                → 評価役が止める → 保留（許可待ち）
  ProjK  [error]                    → エラー → 2回まで再開 → 失敗（エラー）
  ProjZ  git リポジトリなし         → 本流に直接書く → できた
  ProjL  [permission]               → 手前の変更は合流し、票は保留（許可待ち。操作・理由・コマンドが報告に載る）＝③1
  ProjM  [outside]                  → 作業コピーの外（偽の家の .claude/settings.json）を書く → 合流せず保留＝③3
  ProjN  [push]                     → 本流の refs/remotes を動かす（push の痕跡）→ 合流せず保留＝③3
  ProjO  [protected]                → .claude/settings.json に触れた差分 → 関門が保留（ブラウニー自身でなくても）＝③5
  ProjP  設計判断の decide（ゲームの数値）→ 既定では今までどおり推奨案で実装して合流（③6 は 2026-09-25 エヴァの決裁で取り下げ）
  ProjQ  [code] 合流しないプロジェクト → 保留、報告に「.md 以外」＝③8
固まる票（ProjG）と、結果の JSON を返さずに落ちる票（ProjK）は、渡した上限額を使ったものとして台帳に数える＝②1
"""
import datetime, json, os, re, shutil, subprocess, sys, time

sys.stdout.reconfigure(encoding="utf-8")
NIGHT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WALL = int(sys.argv[sys.argv.index("--wall") + 1]) if "--wall" in sys.argv else 600
TICKET_TIMEOUT, WT_TIMEOUT = 8, 25
fails = []


def check(ok, what):
    print(("  ok   " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        fails.append(what)


def sh(args, cwd, **kw):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def pid_alive(pid):
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return str(pid) in r.stdout.split()


def kill_tree(p):
    if p.poll() is None:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)


# ---------------------------------------------------------------- 砂場を作る
run = os.path.join(NIGHT, "sandbox", f"{datetime.datetime.now():%Y-%m-%d_%H%M%S}_overnight")
evar, home = os.path.join(run, "evar"), os.path.join(run, "home")
os.makedirs(evar)
os.makedirs(home)
print(f"砂場: {run}", flush=True)


def init_repo(d):
    for c in (["git", "init", "-q", "-b", "master"], ["git", "config", "user.name", "night-test"], ["git", "config", "user.email", "t@local"]):
        sh(c, d)


init_repo(evar)
open(os.path.join(evar, ".gitignore"), "w", encoding="utf-8").write("/*/\n")  # 本番の Workspace と同じく、直下のフォルダを追跡しない
sh(["git", "add", ".gitignore"], evar)
sh(["git", "commit", "-qm", "root"], evar)

PROJ = {  # プロジェクト → PENDING の行（先頭の印が振る舞いを決める。題は行の 6〜40 文字目）
    "ProjA": ["ふつうの作業その1 docs を1枚書く", "ふつうの作業その2 docs をもう1枚書く", "設定の優先順を決めたい（エヴァの判断待ち）"],
    "ProjB": ["[conflict] 本流と同じ行を書き換える作業"],
    "ProjC": ["[conflict][resolve-fail] 解けない衝突の作業"],
    "ProjD": ["検査で弾かれる作業（スタブ 20）"],
    "ProjE": ["一度だけ本体が使用中の作業（30→0）"],
    "ProjF": ["ずっと本体が使用中の作業（30 のまま）"],
    "ProjG": ["[hang] 固まる作業（子プロセスつき）"],
    "ProjH": ["wt.py が固まる作業（スタブが眠る）"],
    "ProjI": ["[frozen] ブラウニーの tests に触れる作業"],
    "ProjJ": ["[review-ng] 評価役に止められる作業"],
    "ProjK": ["[error] 毎回エラーで落ちる作業"],
    "ProjZ": ["git の無いプロジェクトの作業"],
    "ProjL": ["[permission] push が要る作業"],
    "ProjM": ["[outside] 家の設定を書き換える作業"],
    "ProjN": ["[push] リモートを動かす作業"],
    "ProjO": ["[protected] Claude の設定に触れる作業"],
    "ProjP": ["武器の攻撃力を決めたい（エヴァの判断待ち）"],
    "ProjQ": ["[code] 実装が禁じられたプロジェクトの作業"],
}
for name, items in PROJ.items():
    d = os.path.join(evar, name)
    os.makedirs(d)
    files = {"CLAUDE.md": f"# {name}\n試験用。\n", "DECISIONS.md": "# DECISIONS\n", "shared.txt": "base\n",
             "PENDING.md": "# PENDING\n\n" + "".join(f"- [ ] {x}\n" for x in items)}
    for rel, body in files.items():
        open(os.path.join(d, rel), "w", encoding="utf-8", newline="\n").write(body)
    if name != "ProjZ":
        init_repo(d)
        sh(["git", "add", "-A"], d)
        sh(["git", "commit", "-qm", "init"], d)

# 偽の仕分けの ID は「プロジェクト名の小文字-run|decide-行番号」（PENDING の1件目は3行目）
tid = lambda p, i=3, k="run": f"{p.lower()}-{k}-{i}"
EXPECT = {  # 票 → (行き先, 原因か理由に含まれるべき言葉)
    tid("ProjA"): ("done", "合流"), tid("ProjA", 4): ("done", "合流"), tid("ProjA", 5, "decide"): ("done", "合流"),
    tid("ProjB"): ("done", "合流"), tid("ProjC"): ("failed", "衝突"), tid("ProjD"): ("failed", "台本: 検査で弾いた"),
    tid("ProjE"): ("done", "2回目"), tid("ProjF"): ("hold", "本体が使用中"), tid("ProjG"): ("failed", "時間切れ"),
    tid("ProjH"): ("failed", "時間切れ"), tid("ProjI"): ("hold", "tests/frozen_touch.py"), tid("ProjJ"): ("hold", "評価役が止めた"),
    tid("ProjK"): ("failed", "エラー"), tid("ProjZ"): ("done", "直接"),
    tid("ProjL"): ("hold", "許可が要る"), tid("ProjM"): ("hold", "作業コピーの外"), tid("ProjN"): ("hold", "リモート"),
    tid("ProjO"): ("hold", ".claude/settings.json"), tid("ProjP", 3, "decide"): ("done", "合流"),
    tid("ProjQ"): ("hold", ".md 以外"),
}
script = os.path.join(run, "wt_script.json")
json.dump({tid("ProjD"): "20", tid("ProjE"): "30,0", tid("ProjF"): "30*", tid("ProjH"): "hang",
           # 夜の間に人が本流の shared.txt を書き換えた（合流の最初＝作業セッションの外で入れる）
           tid("ProjB"): {"codes": "0", "human": {"at": 1, "files": {"shared.txt": f"human {tid('ProjB')}\n"}}},
           tid("ProjC"): {"codes": "0", "human": {"at": 1, "files": {"shared.txt": f"human {tid('ProjC')}\n"}}}},
          open(script, "w", encoding="utf-8"))
watch_home = os.path.join(run, "watch_home")  # 作業コピーの外の見張り（③3）が見る偽の家
for rel, body in ((".claude/settings.json", "{}\n"), (".claude/hooks/hook.py", "# hook\n"), (".claude/agents/a.md", "# agent\n"),
                  (".git-hooks/pre-commit", "#!/bin/sh\n"), (".claude/hooks/hook.log", "動くたびに書かれる\n"),
                  (".claude/hooks/.spawn_gate_state/seed.json", "{}\n")):  # 関門フックの状態ファイルの置き場（偽の claude が票ごとに書く）
    os.makedirs(os.path.dirname(os.path.join(watch_home, rel)), exist_ok=True)
    open(os.path.join(watch_home, rel), "w", encoding="utf-8").write(body)
stub_log = os.path.join(run, "wt_stub_log.jsonl")

env = dict(os.environ, NIGHT_EVAR_ROOT=evar, NIGHT_HOME=home, NIGHT_CLAUDE_BIN=os.path.join(NIGHT, "tests", "fake_claude.py"),
           NIGHT_WT_PY=os.path.join(NIGHT, "tests", "wt_stub.py"), WT_STUB_SCRIPT=script, WT_STUB_LOG=stub_log,
           NIGHT_SELF_REPO=os.path.join(evar, "ProjI"),  # ProjI をブラウニー自身に見立てて、凍結（tests/）の判定を試す
           NIGHT_WATCH_HOME=watch_home, NIGHT_NO_MERGE_PROJECTS="ProjQ",  # NIGHT_GAME_PROJECTS は既定（空）のまま
           NIGHT_MIN_FREE_PCT="0", NIGHT_MIN_FREE_GB="0", NIGHT_SESSION_CAP_USD="15",
           NIGHT_TICKET_TIMEOUT_SEC=str(TICKET_TIMEOUT), NIGHT_WT_TIMEOUT_SEC=str(WT_TIMEOUT), NIGHT_GATE_TIMEOUT_SEC="20",
           NIGHT_REVIEW_TIMEOUT_SEC="15", NIGHT_BUSY_RETRY_SEC="1", NIGHT_IDLE_POLL_SEC="2", NIGHT_JUDGE_INTERVAL_SEC="20",
           NIGHT_MAX_TRIAGE="1", NIGHT_BUDGET_USD="1000", FAKE_CLAUDE_SLEEP="0.2", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
for k in ("NIGHT_MODEL", "NIGHT_RUNNER", "NIGHT_PROGRESS", "NIGHT_KIND", "FAKE_CONFLICT", "FAKE_REVIEW_NG", "FAKE_BLOCK_ONCE",
          "FAKE_ERROR_ONCE", "WT_PROJECT", "WT_WORKTREE", "WT_BRANCH", "WT_BASE", "NIGHT_GAME_PROJECTS"):
    env.pop(k, None)

# ---------------------------------------------------------------- 一晩ぶん回す（壁時計の上限つき）
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


stopped, hung = False, False
while p.poll() is None:
    if time.time() - t0 > WALL:
        hung = True
        kill_tree(p)
        break
    time.sleep(1)
    log = open(runlog, encoding="utf-8", errors="replace").read() if os.path.exists(runlog) else ""
    f_tries = sum(1 for r in rows() if r.get("kind") == "finish" and r.get("ticket") == tid("ProjF"))
    # 夜の終わり：作業票が尽き、ずっと使用中の票が3回以上再試行されたら STOP を置く
    if not stopped and "作業票が尽きた" in log and f_tries >= 3:
        open(os.path.join(state, "STOP"), "w").close()
        stopped = True
        print(f"作業票が尽き、使用中の票を {f_tries} 回試したので STOP を置いた（{time.time() - t0:.0f}秒）", flush=True)
console.close()
elapsed = time.time() - t0
print(f"ブラウニーが終わった（{elapsed:.0f}秒・exit {p.returncode}）", flush=True)

# ---------------------------------------------------------------- 判定
R = rows()
log = open(runlog, encoding="utf-8", errors="replace").read() if os.path.exists(runlog) else ""
con = open(os.path.join(run, "console.log"), encoding="utf-8", errors="replace").read()
tonight = json.load(open(os.path.join(state, "tonight.json"), encoding="utf-8")) if os.path.exists(os.path.join(state, "tonight.json")) else {}
T = {t["id"]: t for t in tonight.get("tickets", [])}
by_ticket = lambda kind, i: [r for r in R if r.get("kind") == kind and r.get("ticket") == i]

print("1. 使い捨ての inbox から10票以上を無人で一晩回し切る", flush=True)
check(not hung, f"壁時計の上限 {WALL} 秒の内に終わった（固まっていない。{elapsed:.0f}秒）")
check(stopped and "ブラウニーを終了: 「ブラウニーを止める」が押された" in log, "作業票が尽きた後、STOP で夜が終わった")
check(len(T) >= 10, f"作業票が10枚以上ある（{len(T)}枚）")
check(set(EXPECT) <= set(T), f"想定した20票がそろった（足りない {sorted(set(EXPECT) - set(T))}）")
check(all(by_ticket("work", i) for i in T), f"どの票も作業セッションが走った（走らなかった票 {[i for i in T if not by_ticket('work', i)]}）")
check("Traceback" not in log + con and "司令塔の例外" not in log, "例外が出ていない")

print("2. 各票は「できた」「保留」「失敗」のちょうど1つで終わる（放置0件を台帳から数える）", flush=True)
last = {}
for r in R:
    if r.get("kind") == "outcome":
        last[r.get("ticket")] = r
left = [i for i in T if last.get(i, {}).get("outcome") not in ("done", "hold", "failed")]
check(not left, f"放置された票（台帳に行き先が無い）が0件（{left}）")
# 合流待ち（busy）の票は、夜の終わりに保留へ入る（merge.state は busy のまま残し、同じ夜にブラウニーを起こし直したら再試行される）
check(all(T[i].get("status") != "pending" for i in T) and
      all((T[i].get("outcome") or {}).get("kind") == "hold" for i in T if (T[i].get("merge") or {}).get("state") == "busy"),
      "tonight.json に途中（pending）の票が無く、合流待ち（busy）の票は保留に入った")
check(all((T[i].get("outcome") or {}).get("kind") == last.get(i, {}).get("outcome") for i in T), "tonight.json の行き先と台帳の最後の行き先が一致")
check(not any("決められなかった" in str(r.get("cause")) for r in last.values()), "司令塔の安全網（行き先を決められなかった）に落ちた票が無い")
for i, (kind, word) in EXPECT.items():
    r = last.get(i, {})
    check(r.get("outcome") == kind and word in str(r.get("cause")) + str(T.get(i, {}).get("merge", {}).get("note", "")),
          f"{i} → {kind}（{word}）: 実際 {r.get('outcome')} — {str(r.get('cause'))[:90]}")
counts = {k: sum(1 for r in last.values() if r.get("outcome") == k) for k in ("done", "hold", "failed")}
print(f"     内訳: できた {counts['done']} ／ 保留 {counts['hold']} ／ 失敗 {counts['failed']}（計 {sum(counts.values())}）", flush=True)

print("3. 1票が固まっても他の票は進む", flush=True)
g = tid("ProjG")
hang = os.path.join(state, f"fake_hang_{g}.json")
pids = json.load(open(hang, encoding="utf-8")) if os.path.exists(hang) else {}
check(bool(pids), "固まる票の偽の claude が子プロセスを起こした（試験の前提）")
check(pids and not pid_alive(pids["fake"]) and not pid_alive(pids["child"]), f"時間切れで子プロセスごと止めた（{pids}）")
gw = by_ticket("work", g)
check(gw and gw[0].get("status") == "timeout" and (gw[0].get("sec") or 99) < TICKET_TIMEOUT + 20,
      f"固まった票は {TICKET_TIMEOUT} 秒の持ち時間で打ち切られた（{[(r.get('status'), r.get('sec')) for r in gw]}）")
check(len(gw) == 1, f"時間切れの票は再開しない（作業セッション {len(gw)} 本）")
later = [i for i in (tid("ProjH"), tid("ProjI"), tid("ProjJ"), tid("ProjK"), tid("ProjL"))
         if by_ticket("work", i) and gw and by_ticket("work", i)[0]["at"] >= gw[0]["at"]]
check(len(later) == 5, f"固まった票の後ろの票も処理された（{later}）")
h = tid("ProjH")
starts = [json.loads(l) for l in open(stub_log, encoding="utf-8") if l.strip()] if os.path.exists(stub_log) else []
hstart = [s for s in starts if s.get("event") == "start" and h in s.get("branch", "")]
check(hstart and not pid_alive(hstart[0]["pid"]), f"wt.py の呼び出しも時間切れで止めた（スタブの pid {hstart[0]['pid'] if hstart else None} が残っていない）")
hf = by_ticket("finish", h)
check(hf and hf[0].get("code") == "timeout" and (hf[0].get("sec") or 99) < WT_TIMEOUT + 20, f"wt.py の時間切れは {WT_TIMEOUT} 秒（{[(r.get('code'), r.get('sec')) for r in hf]}）")

print("4. 成果物は wt.py finish（スタブ）を通して本流へ入る。10・20・30 の扱い", flush=True)
for i in (tid("ProjA"), tid("ProjA", 4), tid("ProjB"), tid("ProjE")):
    proj = os.path.join(evar, "Proj" + i[4].upper())
    ok0 = [r for r in by_ticket("finish", i) if r.get("code") == 0]
    check(ok0 and os.path.isfile(os.path.join(proj, "docs", f"{i}.md")) and sh(["git", "status", "--porcelain"], proj).stdout.strip() == "",
          f"{i}: finish が 0 を返し、成果物 docs/{i}.md が本流にある（本体は綺麗なまま）")
ends = [s for s in starts if s.get("event") == "end"]
check(all(any(e.get("code") == 0 and T[i]["merge"]["branch"] == e.get("branch") for e in ends)
          for i in T if (T[i].get("merge") or {}).get("state") == "merged"), "合流済みの票は、どれもスタブの finish が 0 を返したもの")
b = tid("ProjB")
check([r.get("code") for r in by_ticket("finish", b)] == [10, 0], f"10 → 解消役 → もう一度 finish → 0（{[r.get('code') for r in by_ticket('finish', b)]}）")
check([r.get("status") for r in by_ticket("resolve", b)] == ["resolved"], "解消役は1回だけ起き、解消した")
shared = open(os.path.join(evar, "ProjB", "shared.txt"), encoding="utf-8").read()
check(f"night {b}" in shared and f"human {b}" in shared and "<<<<<<<" not in shared, "解消役が本流とブラウニーの両方の行を残した")
c = tid("ProjC")
check([r.get("code") for r in by_ticket("finish", c)] == [10] and [r.get("status") for r in by_ticket("resolve", c)] == ["unresolved"],
      "解けない衝突は 10 → 解消役1回（解けない）→ 失敗（もう一度は呼ばない）")
check(os.path.exists(os.path.join(evar, "ProjC", "shared.txt")) and "<<<<<<<" not in open(os.path.join(evar, "ProjC", "shared.txt"), encoding="utf-8").read(),
      "解けない衝突でも本流に衝突の印が入っていない")
d_ = tid("ProjD")
check([r.get("code") for r in by_ticket("finish", d_)] == [20], "20 は再試行せず失敗（原因つき）")
e = tid("ProjE")
check([r.get("code") for r in by_ticket("finish", e)] == [30, 0], f"30 → 再試行 → 0（{[r.get('code') for r in by_ticket('finish', e)]}）")
e_gate = [s for s in ends if e in s.get("branch", "") and s.get("gate_ran")]
check(len(e_gate) == 2 and len(by_ticket("review", e)) == 1, f"関門は再試行のたびに流れ（{len(e_gate)}回）、評価役は1票1回（{len(by_ticket('review', e))}回）")
f = tid("ProjF")
fc = [r.get("code") for r in by_ticket("finish", f)]
check(len(fc) >= 3 and set(fc) == {30}, f"ずっと使用中の票は夜の終わりまで再試行した（{fc}）")
check(len(by_ticket("review", f)) == 1, f"再試行を重ねても評価役は1回（{len(by_ticket('review', f))}回）")
check(os.path.isdir(T.get(f, {}).get("worktree", "")), "保留（使用中）の票は作業コピーを残した（後で通せる）")
check(len(by_ticket("review", tid("ProjJ"))) == 1 and len(by_ticket("review", tid("ProjI"))) == 0,
      "評価役が止めた票は評価役1回、凍結に触れた票は評価役の前に止めた（お金を使わない）")

print("5. 報告が5分で読める1枚（冒頭から「できたこと」「保留中の許可」「失敗」）", flush=True)
rep_dir = os.path.join(home, "reports")
night = sorted(x for x in (os.listdir(rep_dir) if os.path.isdir(rep_dir) else []) if re.match(r"\d{4}-\d{2}-\d{2}\.md$", x))[-1:]
one = open(os.path.join(home, "reports", night[0]), encoding="utf-8").read() if night else ""
detail_path = os.path.join(home, "reports", night[0][:-3] + "_詳細.md") if night else ""
detail = open(detail_path, encoding="utf-8").read() if os.path.exists(detail_path) else ""
heads = [ln for ln in one.splitlines() if ln.startswith("## ")]
check([h.split("（")[0] for h in heads] == ["## できたこと", "## 保留中の許可", "## 失敗"], f"1枚目の節は3つだけで、この順（{heads}）")
secs = dict(re.findall(r"^## (できたこと|保留中の許可|失敗)[^\n]*\n(.*?)(?=^## |\Z)", one, re.S | re.M))
where = {i: [k for k, body in secs.items() if f"`{i}`" in body] for i in T}
check(all(len(v) == 1 for v in where.values()), f"どの票も3節のちょうど1つに載った（{ {i: v for i, v in where.items() if len(v) != 1} }）")
want = {"done": "できたこと", "hold": "保留中の許可", "failed": "失敗"}
check(all(where[i] == [want[last[i]["outcome"]]] for i in T if i in last), "載った節が台帳の行き先と一致")
hold_body = secs.get("保留中の許可", "")
hold_items = re.findall(r"^\d+\. .*\n   - (.*)$", hold_body, re.M)
check("全部通すときの一言" in hold_body and re.search(r"^1\. ", hold_body, re.M) and len(hold_items) == counts["hold"]
      and all("`" in x for x in hold_items), "保留は番号つきで、全部通すときの一言と、1件ずつの通し方（コマンドか差分）がある")
lp = next((x for x in hold_items if "操作:" in x), "")
check("操作:" in lp and "理由:" in lp and "流すコマンドか差分:" in lp and "git push" in lp,
      f"許可待ち（needs_permission）の票は〈操作・理由・流すコマンドか差分〉が載った（{lp[:80]}）")
check(all("原因:" in ln for ln in secs.get("失敗", "").splitlines() if re.match(r"^\d+\. ", ln)), "失敗の各行に原因が付いている")
check(len(one.splitlines()) <= 100 and len(one) <= 24000 and "#### " not in one and "## 時系列" not in one and "⚖ 審判官の採点" not in one,
      f"1枚目は短い（{len(one.splitlines())}行・{len(one)}字）。進捗ファイル全文・採点・時系列は載せていない")
check(all(k in detail for k in ("## 一覧", "## ⚖ 審判官の採点", "## 時系列", "#### なぜやったか")), "全文（_詳細.md）に一覧・採点・時系列・進捗ファイル全文がある")

print("7. 越えない線（③）と暴走の歯止め（②）", flush=True)
g_rows = by_ticket("work", tid("ProjG"))
check(g_rows and float(g_rows[0].get("cost") or 0) == 15 and g_rows[0].get("cost_estimated"),
      f"②1 固まって止めた票は、渡した上限額 $15 を使ったものとして数えた（{[(r.get('cost'), r.get('cost_estimated')) for r in g_rows]}）")
k_rows = by_ticket("work", tid("ProjK"))
check(len(k_rows) == 3 and all(float(r.get("cost") or 0) == 15 and r.get("cost_estimated") for r in k_rows),
      f"②1 結果を返さずに落ちたセッションも上限額で数えた（{[(r.get('cost'), r.get('cost_estimated')) for r in k_rows]}）")
spent = sum(float(r.get("cost") or 0) for r in R)
check(abs(spent - 15 * 4) < 0.01, f"②1 台帳の合計が 固まった1本＋落ちた3本＝$60（${spent:.2f}）")
check(len({r.get("run") for r in R if r.get("kind") in ("work", "finish", "outcome")}) == 1 and all(r.get("run") for r in R),
      "②2 台帳のどの行にも夜の回（run）が付いた")
lmain = os.path.join(evar, "ProjL")
check(os.path.isfile(os.path.join(lmain, "docs", f"{tid('ProjL')}.md")) and (T[tid("ProjL")].get("merge") or {}).get("state") == "merged",
      "③1 許可待ちの票も、線の手前の安全な変更は合流した（票は保留）")
m_ = T.get(tid("ProjM"), {})
check(any("settings.json" in x for x in m_.get("outside", [])) and not os.path.isfile(os.path.join(evar, "ProjM", "docs", f"{tid('ProjM')}.md"))
      and not by_ticket("finish", tid("ProjM")), f"③3 作業コピーの外（設定）を書いた票は合流させなかった（{m_.get('outside')}）")
n_ = T.get(tid("ProjN"), {})
check(any("リモート" in x for x in n_.get("outside", [])) and not by_ticket("finish", tid("ProjN")),
      f"③3 push の痕跡（refs/remotes）を残した票は合流させなかった（{n_.get('outside')}）")
check(not [i for i in T if T[i].get("outside") and i not in (tid("ProjM"), tid("ProjN"))],
      "③3 ほかの票は外の変更と取り違えなかった（毎回書かれるフックのログは数えない）")
gate_state = [f for f in os.listdir(os.path.join(watch_home, ".claude", "hooks", ".spawn_gate_state")) if f != "seed.json"]
check(len([f for f in gate_state if f.endswith(".json")]) >= 3 and not [x for i in T for x in T[i].get("outside", []) if ".spawn_gate_state" in x],
      f"③3 関門フックの状態ファイル（hooks/.spawn_gate_state）は票のたびに書かれたが、外の変更に数えなかった（{len(gate_state)} 個）")
check(not os.path.exists(os.path.join(evar, "ProjO", ".claude", "settings.json")) and [r.get("gate") for r in by_ticket("finish", tid("ProjO"))] == ["hold"],
      "③5 ブラウニー自身ではないリポジトリでも、.claude/settings.json に触れた差分は関門が保留にした")
p_ = tid("ProjP", 3, "decide")
check([r.get("code") for r in by_ticket("finish", p_)] == [0] and "判断待ち: 武器の攻撃力" in
      sh(["git", "show", "HEAD:DECISIONS.md"], os.path.join(evar, "ProjP")).stdout,
      "③6 は取り下げ（既定の NIGHT_GAME_PROJECTS は空）: 設計判断の decide も推奨案で実装して合流した")
q_ = T.get(tid("ProjQ"), {})
check(not by_ticket("finish", tid("ProjQ")) and "tool.py" in (q_.get("merge") or {}).get("note", ""),
      "③8 合流しないプロジェクトは wt.py を呼ばず、差分の .md 以外（tool.py）を報告に出した")

print("6. 後片付けと本番への非接触", flush=True)
for name in PROJ:
    if name == "ProjZ":
        continue
    d = os.path.join(evar, name)
    wts = sh(["git", "worktree", "list", "--porcelain"], d).stdout.count("\nworktree ") + 1
    # 保留（F・I・J）は後で通せるよう、wt.py が時間切れの H は状態が分からないので触らずに、作業コピーを残す
    keep = 2 if name in ("ProjF", "ProjI", "ProjJ", "ProjH", "ProjM", "ProjN", "ProjO", "ProjQ") else 1
    check(wts == keep, f"{name}: 作業コピーは {'1つだけ残した' if keep == 2 else '残っていない'}（{wts - 1}件）")
check(os.path.isfile(os.path.join(evar, "ProjZ", "docs", f"{tid('ProjZ')}.md")), "git の無いプロジェクトは本流に直接書いた")
check(all(os.path.commonpath([os.path.abspath(T[i].get("worktree") or run), run]) == run for i in T),
      "作業コピーはすべて砂場の中に作った")

print(("合格" if not fails else f"不合格 {len(fails)} 件: " + " ／ ".join(fails)) + f"（砂場 {run}・{elapsed:.0f}秒）", flush=True)
sys.exit(1 if fails else 0)
