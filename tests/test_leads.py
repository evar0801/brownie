"""仕分けへ渡す手がかり（runner.leads_text）の試験。トークン0。使い捨てのフォルダを %TEMP% の下に作る。

    python tests/test_leads.py     # 終了コード 0＝合格、1＝不合格

1 DECISIONS.md の「ついでに直したい（未実行）」「未着手」「残り:」→ 拾う（`パス:行` と節の ID が付く）
2 「未解決: なし」→ 拾わない ／ 末尾から決めた行数より前の行 → 拾わない
3 選んでいないプロジェクト・PENDING も HANDOFF も無いフォルダ → 拾わない
4 1プロジェクトの上限を超えたら新しい方を残す ／ 長い行は切る
5 TODO.md のそのプロジェクトの行が入る（道具には --file と --project が渡る）／ 道具が失敗したら入れない
6 NIGHT_LEADS=0 → 空 ／ 手がかりが増えると sources_hash が変わり、関係ない追記では変わらない
7 仕分けの指示に手がかりが差し込まれ、置き換えの印が残らない
"""
import importlib.util, os, shutil, sys, tempfile

sys.stdout.reconfigure(encoding="utf-8")
NIGHT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="night_leads_")
EVAR = os.path.join(TMP, "evar")
HOME = os.path.join(TMP, "home")
os.makedirs(EVAR)
os.makedirs(HOME)
os.environ.update(NIGHT_EVAR_ROOT=EVAR, NIGHT_HOME=HOME, NIGHT_JUDGE_INTERVAL_SEC="0", PYTHONUTF8="1",
                  NIGHT_WT_PY=os.path.join(NIGHT, "tests", "wt_stub.py"), NIGHT_MIN_FREE_PCT="0", NIGHT_MIN_FREE_GB="0")
for k in ("NIGHT_SELF_REPO", "NIGHT_MODEL", "NIGHT_LABEL", "NIGHT_LEADS", "NIGHT_LEADS_TAIL_LINES", "NIGHT_LEADS_PER_PROJECT",
          "NIGHT_TODO_PY"):
    os.environ.pop(k, None)

spec = importlib.util.spec_from_file_location("runner", os.path.join(NIGHT, "runner.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
fails = []


def check(ok, what):
    print(("  ok   " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        fails.append(what)


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


A, B, C = (os.path.join(EVAR, n) for n in ("ProjA", "ProjB", "Deep", ))
C = os.path.join(C, "ProjC")
OLD = "## A-001 古い節\n- **ついでに直したい（未実行）**：古すぎる行\n" + "- 埋め草\n" * 30
DEC_A = (OLD + "## A-002 新しい節（2026-10-08）\n- 本文\n- **ついでに直したい（未実行）**：`a.py` の表記ゆれ\n"
         "- 未解決: なし\n- **未解決**：なし。\n## [A-002追記] 続き\n- **未着手**: 試験を足す\n- **残り**: 実機での確認\n- 関係ない行\n")
put(os.path.join(A, "PENDING.md"), "# PENDING\n")
put(os.path.join(A, "DECISIONS.md"), DEC_A)
put(os.path.join(B, "HANDOFF.md"), "# HANDOFF\n")
put(os.path.join(B, "DECISIONS.md"), "## B-001 節\n- **未着手**: B の仕事\n")
put(os.path.join(C, "HANDOFF.md"), "# HANDOFF\n")
put(os.path.join(C, "docs", "DECISIONS.md"), "## C-001 節\n- 後回しにした: C の仕事\n")
put(os.path.join(EVAR, "NoDocs", "DECISIONS.md"), "## N-001 節\n- **未着手**: 文書の無いフォルダ\n")
put(runner.PROJECTS_FILE, "ProjA\nDeep/ProjC\n# ProjB\n")
runner.LEADS_TAIL_LINES = 12

print("1-3 拾う行・拾わない行")
txt = runner.leads_text()
da = os.path.join(A, "DECISIONS.md")
n = DEC_A.splitlines()
check(f"- {da}:{n.index('- **ついでに直したい（未実行）**：`a.py` の表記ゆれ') + 1} （A-002） " in txt, "「ついでに直したい（未実行）」をパス:行と節の ID 付きで拾う")
check(f"{da}:{n.index('- **未着手**: 試験を足す') + 1} （A-002追記） - **未着手**: 試験を足す" in txt, "追記の節の「未着手」を拾う")
check("**残り**: 実機での確認" in txt, "「残り:」を拾う")
check("なし" not in txt, "「未解決: なし」「**未解決**：なし。」は拾わない")
check("古すぎる行" not in txt and "関係ない行" not in txt, "末尾から決めた行数より前の行と、語の無い行は拾わない")
check("B の仕事" not in txt, "選んでいないプロジェクトは拾わない")
check("文書の無いフォルダ" not in txt, "PENDING も HANDOFF も無いフォルダは拾わない")
check(f"### {C}" in txt and os.path.join(C, "docs", "DECISIONS.md") + ":2 （C-001）" in txt, "入れ子のプロジェクトの docs/DECISIONS.md も拾う")

print("4 上限と長さ")
runner.LEADS_PER_PROJECT = 2
got = runner.decision_leads(A)
check(len(got) == 2 and "試験を足す" in got[0] and "実機での確認" in got[1], "上限を超えたら新しい方の2行を残す")
runner.LEADS_PER_PROJECT = 20
put(os.path.join(B, "DECISIONS.md"), "## B-002 節\n- **未着手**: " + "長" * 400 + "\n")
check(all(len(l) < 140 + len(B) + 60 for l in runner.decision_leads(B)), "長い行は切る")

print("5 TODO.md")
stub = os.path.join(TMP, "todo_stub.py")
put(stub, "import sys\nopen(sys.argv[0] + '.args', 'a', encoding='utf-8').write(' '.join(sys.argv[1:]) + '\\n')\n"
          "p = sys.argv[sys.argv.index('--project') + 1]\n"
          "if p == 'ProjX': sys.exit(3)\n"
          "print('T-0007 やるべき ' + p + ' の宿題' if p in ('ProjA', 'Deep/ProjC') else '0件')\n")
runner.TODO_PY = stub
check(runner.todo_leads(A) == [], "TODO.md が無ければ道具を呼ばない")
put(runner.TODO_FILE, "# TODO\n")
ta = runner.todo_leads(A)
check(len(ta) == 1 and "T-0007 やるべき ProjA の宿題" in ta[0] and runner.TODO_FILE in ta[0], "そのプロジェクトの行が入る")
args = open(stub + ".args", encoding="utf-8").read()
check(f"--file {runner.TODO_FILE} list --project ProjA" in args, "道具に --file と --project が渡る")
tc = runner.todo_leads(C)
check(len(tc) == 1 and "Deep/ProjC の宿題" in tc[0], "入れ子は相対パスとフォルダ名の両方で引き、「0件」の行は入れない")
check(runner.todo_leads(os.path.join(EVAR, "ProjX")) == [], "道具が失敗したら入れない")
check(runner.todo_leads(EVAR) == [], "走査の根そのものは引かない")
check("ProjA の宿題" in runner.leads_text(), "手がかりの本文に TODO の行が入る")

print("6 切り替えと仕分け直し")
h0 = runner.sources_hash()
put(da, DEC_A + "- 関係ない追記\n")
check(runner.sources_hash() == h0, "関係ない追記では sources_hash が変わらない")
put(da, DEC_A + "- **未着手**: 増えた仕事\n")
check(runner.sources_hash() != h0, "手がかりが増えると sources_hash が変わる")
runner.LEADS_ON = False
check(runner.leads_text() == "", "NIGHT_LEADS=0 なら空")
runner.LEADS_ON = True

print("7 仕分けの指示")
seen = []
runner.run_claude = lambda prompt, *a, **k: (seen.append(prompt) or {"result": "stub", "session_id": "s", "total_cost_usd": 0})
os.makedirs(runner.STATE, exist_ok=True)
runner.triage()
p = seen[0] if seen else ""
check("## 機械が拾った手がかり" in p and "増えた仕事" in p and "ProjA の宿題" in p, "仕分けの指示に手がかりが入る")
check("{LEADS}" not in p and "{PENDING_LIST}" not in p, "置き換えの印が残らない")
runner.LEADS_ON = False
seen.clear()
runner.triage()
check(bool(seen) and "（無い）" in seen[0] and "増えた仕事" not in seen[0], "切ったときは「（無い）」になる")

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("合格" if not fails else f"不合格 {len(fails)} 件: " + " / ".join(fails)))
sys.exit(1 if fails else 0)
