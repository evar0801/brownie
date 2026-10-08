"""記録だけ後から合流（wt.py finish --records-later・[W-004]）の試験。トークン0。実物の wt.py を %TEMP% の使い捨てリポジトリに向ける。

    python tests/test_records_later.py [--wt <wt.py>] [--old <直す前の wt.py>]     # 終了コード 0＝合格、1＝不合格
    --wt の既定は環境変数 NIGHT_SANDBOX_WT_PY、無ければ ~/.claude/tools/wt.py。
    --old の既定は ~/.claude/tools/排除/wt_2026-10-08_before_records_later.py（無ければ「直す前と同じ」の比較だけ飛ばし、飛ばしたと出す）。

A 受け入れ条件: 本体の DECISIONS.md が未コミットでも成果物が本流に入り、本体の未コミットは1バイトも変わらず、
  本体をコミットした後の再試行で記録が入る。本流で増えた節が消えない
B オプションが効かない形（どれも今までどおり 30 で、本流は動かない）: 成果物も塞がっている／ブランチが記録しか変えていない／
  本体に何かがステージされている／本流が追跡していない記録が塞いでいる
C 塞がっていなければ、オプションを付けても今までどおり 0 で合流する
E オプションなしの呼び出しは、終了コードが決まりどおりで、直す前の wt.py と標準出力・終了コードが同じ（9つの場面）
R runner.py: --records-later を付けて呼ぶ／records_held を「成果は本流に入った。記録は本体が空いてから入る」として扱う／
  流し直しの前の検査（reflow_skip_reason）が記録だけの重なりを通す
"""
import importlib.util, json, os, shutil, stat, subprocess, sys, tempfile

sys.stdout.reconfigure(encoding="utf-8")
NIGHT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(os.path.expanduser("~"), ".claude", "tools")


def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


WT = arg("--wt", os.environ.get("NIGHT_SANDBOX_WT_PY") or os.path.join(TOOLS, "wt.py"))
OLD = arg("--old", os.path.join(TOOLS, "排除", "wt_2026-10-08_before_records_later.py"))
TMP = tempfile.mkdtemp(prefix="night_records_later_")
# 日時と名前を固定する（直す前と後で、同じ場所に同じコミットができる＝出力をそのまま比べられる）
GENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
            GIT_COMMITTER_EMAIL="t@example.invalid", GIT_AUTHOR_DATE="2026-10-08T12:00:00+09:00",
            GIT_COMMITTER_DATE="2026-10-08T12:00:00+09:00", PYTHONUTF8="1")
for k in ("WT_PROJECT", "WT_WORKTREE", "WT_BRANCH", "WT_BASE"):
    GENV.pop(k, None)
FAILS = []


def ok(cond, label, extra=""):
    print(("  ok   " if cond else "  NG   ") + label + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(label)


def g(args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=GENV)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)} ({cwd}): {r.stderr.strip()}")
    return r.stdout


def wt(tool, *args):
    """wt.py を1回呼ぶ。(終了コード, 標準出力, WT-REPORT の dict) を返す。"""
    r = subprocess.run([sys.executable, tool, *args], cwd=TMP, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=GENV, stdin=subprocess.DEVNULL)
    rep = {}
    for line in reversed(r.stdout.splitlines()):
        if line.startswith("WT-REPORT "):
            rep = json.loads(line[len("WT-REPORT "):])
            break
    return r.returncode, r.stdout, rep


def put(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def get(root, rel):
    with open(os.path.join(root, *rel.split("/")), "rb") as f:
        return f.read()


def rm(path):
    def fix(fn, p, _):
        os.chmod(p, stat.S_IWRITE)
        fn(p)
    if os.path.exists(path):
        shutil.rmtree(path, onerror=fix)


def head(root):
    return g(["rev-parse", "HEAD"], root).strip()


def show(root, rel, ref="HEAD"):
    return g(["show", f"{ref}:{rel}"], root)


def status(root):
    return g(["status", "--porcelain"], root)


BASE = {"a.txt": "a\n",
        "DECISIONS.md": "# D\n\n## X-001 first (2026-01-01)\n\n- body\n",
        "HANDOFF.md": "# H\n\n## §0 now\n\n- state\n\n## §1 other\n\n- x\n",
        "PENDING.md": "# P\n\n- [ ] item\n"}
NEW_SEC = "\n## X-NEW branch decision (2026-10-08)\n\n- did it\n- §0 へ: branch line\n"
MAIN_SEC = "\n## X-002 main decision (2026-10-08)\n\n- main body\n"
MAIN_WIP = BASE["DECISIONS.md"] + MAIN_SEC  # 本体の書きかけ（別のセッションの未コミット）
STD = {"a.txt": "a\nbranch\n", "DECISIONS.md": BASE["DECISIONS.md"] + NEW_SEC, "PENDING.md": "# P\n\n"}
RECORDS = ["DECISIONS.md", "HANDOFF.md", "PENDING.md"]


def build(tool, name, edits):
    """使い捨てのリポジトリ <TMP>/<name> と、作業コピー night/t1（edits を書いてコミット済み）を作る。"""
    root = os.path.join(TMP, name)
    rm(root)
    os.makedirs(root)
    g(["init", "-q", "-b", "master"], root)
    g(["config", "core.autocrlf", "false"], root)
    for rel, text in BASE.items():
        put(root, rel, text)
    g(["add", "--", *BASE], root)
    g(["commit", "-q", "-m", "base"], root)
    code, out, _ = wt(tool, "start", root, "t1", "--prefix", "night")
    w = os.path.join(root, ".claude", "worktrees", "t1")
    if code or not os.path.isdir(w):
        raise RuntimeError("wt.py start: " + out)
    for rel, text in edits.items():
        put(w, rel, text)
    g(["add", "--", *edits], w)
    g(["commit", "-q", "-m", "branch work"], w)
    return root, w


print(f"wt.py: {WT}")
print("A 受け入れ条件（本体の DECISIONS.md が未コミット）")
root, w = build(WT, "accept", STD)
put(root, "DECISIONS.md", MAIN_WIP)
dirty0, st0, h0 = get(root, "DECISIONS.md"), status(root), head(root)
code, out, rep = wt(WT, "finish", root, "night/t1")
ok(code == 30 and rep.get("status") == "busy" and rep.get("files") == ["DECISIONS.md"] and head(root) == h0,
   "A1 オプションなしは今までどおり 30（本流は動かない）", out[-400:])
code, out, rep = wt(WT, "finish", root, "night/t1", "--records-later")
h1 = head(root)
ok(code == 30 and rep.get("status") == "records_held", "A2 オプションつきは 30・records_held", out[-800:])
ok(rep.get("files") == RECORDS, "A2 残した記録は DECISIONS.md・HANDOFF.md・PENDING.md", str(rep.get("files")))
ok(h1 != h0 and bool(rep.get("head")) and h1.startswith(str(rep.get("head"))), "A2 本流が進み、WT-REPORT の head がその先頭")
ok(get(root, "a.txt") == b"a\nbranch\n", "A2 成果物が本流に入った")
ok(get(root, "DECISIONS.md") == dirty0 and status(root) == st0, "A2 本体の未コミットは1バイトも変わらず、git status も同じ")
ok(all(show(root, f) == BASE[f] for f in RECORDS), "A2 本流の記録は元のまま")
ok(os.path.isdir(w) and status(w) == "" and "branch decision" in show(root, "DECISIONS.md", "night/t1")
   and show(root, "PENDING.md", "night/t1") == STD["PENDING.md"], "A2 記録はブランチに残り、作業コピーはきれい")
ok(g(["diff", "--name-only", "HEAD...night/t1"], root).split() == RECORDS, "A2 ブランチに残った差分は記録だけ")
code, out, rep = wt(WT, "finish", root, "night/t1", "--records-later")
ok(code == 30 and rep.get("status") == "busy" and head(root) == h1 and get(root, "DECISIONS.md") == dirty0,
   "A3 本体が塞がったままのもう1回は 30（本流も本体も動かない）", out[-400:])
g(["commit", "-q", "-m", "main decision", "--", "DECISIONS.md"], root)
code, out, rep = wt(WT, "finish", root, "night/t1", "--records-later")
text = get(root, "DECISIONS.md").decode("utf-8")
heads = [ln for ln in text.splitlines() if ln.startswith("## X-")]
ok(code == 0 and rep.get("status") == "merged", "A4 本体をコミットした後の再試行で 0", out[-800:])
ok(all(s in text for s in ("X-001 first", "main decision", "main body", "branch decision", "did it")), "A4 本流で増えた節が消えず、記録も入った", text)
ok(len(heads) == 3 and len({h.split()[1] for h in heads}) == 3, "A4 番号は重ならない", str(heads))
num = next((h.split()[1] for h in heads if "branch decision" in h), "?")
hand = get(root, "HANDOFF.md").decode("utf-8")
ok(f"[{num}] branch line" in hand and "- state" in hand, "A4 HANDOFF §0 の行が、入った後の番号で入った", hand)
ok(get(root, "PENDING.md").decode("utf-8") == STD["PENDING.md"], "A4 PENDING.md も入った")
ok(not os.path.isdir(w) and g(["branch", "--list", "night/t1"], root).strip() == ""
   and g(["status", "--porcelain", "--untracked-files=no"], root) == "", "A4 作業コピーとブランチが片付き、本体に未コミットが無い")

print("B オプションが効かない形（今までどおり 30）")
root, w = build(WT, "b1", STD)
put(root, "DECISIONS.md", MAIN_WIP)
put(root, "a.txt", "a\nmain wip\n")
h0 = head(root)
code, out, rep = wt(WT, "finish", root, "night/t1", "--records-later")
ok(code == 30 and rep.get("status") == "busy" and head(root) == h0 and get(root, "a.txt") == b"a\nmain wip\n",
   "B1 成果物も塞がっている", out[-400:])
root, w = build(WT, "b2", {"DECISIONS.md": STD["DECISIONS.md"]})
put(root, "DECISIONS.md", MAIN_WIP)
h0 = head(root)
code, out, rep = wt(WT, "finish", root, "night/t1", "--records-later")
ok(code == 30 and rep.get("status") == "busy" and head(root) == h0, "B2 ブランチが記録しか変えていない", out[-400:])
root, w = build(WT, "b3", STD)
put(root, "DECISIONS.md", MAIN_WIP)
g(["add", "--", "DECISIONS.md"], root)
h0 = head(root)
code, out, rep = wt(WT, "finish", root, "night/t1", "--records-later")
ok(code == 30 and rep.get("status") == "busy" and head(root) == h0, "B3 本体に何かがステージされている", out[-400:])
root, w = build(WT, "b4", dict(STD, **{"sub/DECISIONS.md": "# sub\n"}))
put(root, "sub/DECISIONS.md", "# someone's untracked\n")
h0 = head(root)
code, out, rep = wt(WT, "finish", root, "night/t1", "--records-later")
ok(code == 30 and rep.get("status") == "busy" and head(root) == h0 and get(root, "sub/DECISIONS.md") == b"# someone's untracked\n",
   "B4 本流が追跡していない記録が塞いでいる", out[-400:])

print("C 塞がっていなければ今までどおり")
root, w = build(WT, "c1", STD)
code, out, rep = wt(WT, "finish", root, "night/t1", "--records-later")
ok(code == 0 and rep.get("status") == "merged" and get(root, "a.txt") == b"a\nbranch\n"
   and "## X-002 branch decision" in get(root, "DECISIONS.md").decode("utf-8") and not os.path.isdir(w),
   "C1 オプションつきでも 0 で合流する", out[-400:])


EQ_TOOLS = os.path.join(TMP, "tools")


def scene(src, name):
    """オプションなしの呼び出しを1場面流し、[(終了コード, 標準出力)] を返す。直す前と後で同じ場所（<TMP>/eq）に作る。
    wt.py は隣の install_md_merge.py・merge_md.py を呼ぶので、道具を <TMP>/tools に写し、wt.py だけを src に差し替えて流す。"""
    if not os.path.isdir(EQ_TOOLS):
        os.makedirs(EQ_TOOLS)
        for fn in os.listdir(os.path.dirname(os.path.abspath(WT))):
            if fn.endswith(".py"):
                shutil.copyfile(os.path.join(os.path.dirname(os.path.abspath(WT)), fn), os.path.join(EQ_TOOLS, fn))
    tool = os.path.join(EQ_TOOLS, "wt.py")
    shutil.copyfile(src, tool)
    if name == "badargs":
        return [wt(tool, "finish")[:2]]
    edits = dict(STD)
    if name == "s0":
        edits["HANDOFF.md"] = BASE["HANDOFF.md"].replace("- state", "- state changed on the branch")
    root, w = build(tool, "eq", edits)
    extra = []
    if name == "busy_records":
        put(root, "DECISIONS.md", MAIN_WIP)
    elif name == "busy_deliv":
        put(root, "a.txt", "a\nmain wip\n")
    elif name == "dirty_wt":
        put(w, "a.txt", "a\nbranch\nuncommitted\n")
    elif name == "conflict":
        put(root, "a.txt", "a\nmain\n")
        g(["commit", "-q", "-m", "main edit", "--", "a.txt"], root)
    elif name in ("discard", "keep"):
        extra = ["--" + name]
    res = [wt(tool, "finish", root, "night/t1", *extra)[:2]]
    if name == "busy_records":  # 本体をコミットした後のもう1回（番号のずらしを通る）
        g(["commit", "-q", "-m", "main decision", "--", "DECISIONS.md"], root)
        res.append(wt(tool, "finish", root, "night/t1")[:2])
    return res


print("E オプションなしの呼び出しは変わらない")
EXPECT = {"clean": [0], "busy_records": [30, 0], "busy_deliv": [30], "dirty_wt": [20], "conflict": [10], "discard": [0],
          "keep": [0], "badargs": [2], "s0": [20]}
have_old = os.path.isfile(OLD)
if not have_old:
    print(f"  飛ばした: 直す前の wt.py（{OLD}）が無いので、出力の比較はしない（終了コードだけ見る）")
for name, want in EXPECT.items():
    new = scene(WT, name)
    ok([c for c, _ in new] == want, f"E {name}: 終了コード {want}", str([c for c, _ in new]) + " " + new[-1][1][-300:])
    if have_old:
        old = scene(OLD, name)
        ok(new == old, f"E {name}: 直す前と標準出力・終了コードが同じ",
           "\n--- 直す前\n" + "\n".join(o for _, o in old)[-1200:] + "\n--- 直した後\n" + "\n".join(o for _, o in new)[-1200:])

print("R runner.py")
EVAR, HOME = os.path.join(TMP, "evar"), os.path.join(TMP, "home")
os.makedirs(EVAR)
os.makedirs(os.path.join(HOME, "state"))
os.environ.update(NIGHT_EVAR_ROOT=EVAR, NIGHT_HOME=HOME, NIGHT_JUDGE_INTERVAL_SEC="0", PYTHONUTF8="1", NIGHT_WT_PY=WT,
                  NIGHT_CLAUDE_BIN=os.path.join(TMP, "no_claude_here.py"), NIGHT_WATCH_HOME=os.path.join(TMP, "watch_home"),
                  NIGHT_MIN_FREE_PCT="0", NIGHT_MIN_FREE_GB="0", NIGHT_USAGE_PROBE_SEC="0", NIGHT_USAGE_HOLD_PCT="0",
                  NIGHT_USAGE_WARN_PCT="0", NIGHT_STALE_DIRTY_HOURS="0")
for k in ("NIGHT_SELF_REPO", "NIGHT_MODEL", "NIGHT_LABEL", "NIGHT_RECORDS_LATER", "NIGHT_REFLOW_PREVIOUS", "NIGHT_BUSY_RETRY_SEC",
          "NIGHT_MAX_HOURS", "NIGHT_END_AT", "WT_PROJECT", "WT_WORKTREE", "WT_BRANCH", "WT_BASE"):
    os.environ.pop(k, None)
spec = importlib.util.spec_from_file_location("runner", os.path.join(NIGHT, "runner.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
runner.GATE_KEY = os.urandom(32)
runner.RUN_ID = "test-run-1"
runner.NIGHT = "2099-01-01"
runner.toast = lambda *a, **k: None

ok(runner.records_later_args() == ["--records-later"], "R1 実物の wt.py には --records-later を付けて呼ぶ")
runner.WT_PY = os.path.join(NIGHT, "tests", "wt_stub.py")
ok(runner.records_later_args() == ["--records-later"], "R1 スタブも受け取る")
if have_old:
    runner.WT_PY = OLD
    ok(runner.records_later_args() == [], "R1 対応していない wt.py（直す前）には付けない")
runner.WT_PY = WT
runner.RECORDS_LATER = False
ok(runner.records_later_args() == [], "R1 NIGHT_RECORDS_LATER=0 なら付けない")
runner.RECORDS_LATER = True

root, w = build(WT, "run", STD)
put(root, "DECISIONS.md", MAIN_WIP)
t = {"id": "t-rec", "title": "試験の票", "source": "PENDING.md", "project": "run", "kind": "do", "status": "done", "sessions": 1,
     "repo": root, "branch": "night/t1", "base": "master", "worktree": w}
ok(runner.reflow_skip_reason(t) is None, "R2 流し直しの前の検査: 記録だけの重なりは通す", str(runner.reflow_skip_reason(t)))
runner.RECORDS_LATER = False
ok("本体がまだ使用中" in str(runner.reflow_skip_reason(t)), "R2 切ってあれば今までどおり流さない")
runner.RECORDS_LATER = True
put(root, "a.txt", "a\nmain wip\n")
ok("本体がまだ使用中" in str(runner.reflow_skip_reason(t)), "R2 成果物も重なっていれば流さない")
g(["checkout", "--", "a.txt"], root)


def real_finish(t, approved=False):
    """関門なしで実物の wt.py を呼ぶ（runner.run_wt_finish の代わり。付ける引数は runner が決めたもの）。"""
    return wt(WT, "finish", t["repo"], t["branch"], *runner.records_later_args())[2]


runner.run_wt_finish = real_finish
runner.call_finish(t)
m = t.get("merge") or {}
ok(m.get("state") == "busy" and m.get("records_held") == RECORDS and bool(m.get("part_commit")) and not t.get("outcome"),
   "R3 records_held は合流待ちのまま（成果物のコミットと残した記録を覚える）", json.dumps(m, ensure_ascii=False))
ok("成果は本流に入った" in str(m.get("note")) and "本体が空いてから入る" in str(m.get("note")), "R3 票の注記に出る", str(m.get("note")))
ok(get(root, "a.txt") == b"a\nbranch\n" and get(root, "DECISIONS.md").decode("utf-8") == MAIN_WIP, "R3 成果物は本流に入り、本体の書きかけはそのまま")
ok("本体がまだ使用中" in str(runner.reflow_skip_reason(t)), "R3 記録だけが残った票は、本体が空くまで流し直さない")
runner.write_json(runner.TONIGHT, {"night": runner.night_date(), "tickets": [json.loads(json.dumps(t))]})
runner.finalize_night("試験")
t2 = runner.read_json(runner.TONIGHT, {})["tickets"][0]
oc = json.dumps(t2.get("outcome"), ensure_ascii=False)
ok("hold" in oc and "成果は本流に入った" in oc and "本体が空いてから入る" in oc, "R4 夜の締めで保留になり、理由に出る", oc)
try:
    runner.write_report()
    rep_text = ""
    for dp, _, fns in os.walk(os.path.join(HOME, "reports")):
        for fn in fns:
            rep_text += open(os.path.join(dp, fn), encoding="utf-8", errors="replace").read()
    ok("成果は本流に入った" in rep_text and "本体が空いてから入る" in rep_text, "R4 朝の報告に出る", rep_text[-600:])
except Exception as ex:  # 報告の組み立ては票以外の状態も読む。落ちたら落ちたと出す
    ok(False, "R4 朝の報告に出る", f"{type(ex).__name__}: {ex}")
g(["commit", "-q", "-m", "main decision", "--", "DECISIONS.md"], root)
runner.call_finish(t)
m = t.get("merge") or {}
ok(m.get("state") == "merged" and "records_held" not in m and bool(m.get("part_commit"))
   and "branch decision" in get(root, "DECISIONS.md").decode("utf-8") and "main decision" in get(root, "DECISIONS.md").decode("utf-8"),
   "R5 本体が空いた後の再試行で記録も入る", json.dumps(m, ensure_ascii=False))
ok("done" in json.dumps(t.get("outcome"), ensure_ascii=False), "R5 行き先は「できた」", json.dumps(t.get("outcome"), ensure_ascii=False))

rm(TMP)
print(("不合格: " + " ／ ".join(FAILS)) if FAILS else "全項目合格")
sys.exit(1 if FAILS else 0)
