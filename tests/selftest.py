"""ブラウニーの自己試験（トークン0・約30秒）。runner が本番に入る前と、ブラウニーが自分を直した作業票をマージする前（関門）に流す。

    python tests/selftest.py            # 終了コード 0＝合格、1＝不合格。何が落ちたかを標準出力に書く

中身:
  1. 偽の claude で砂場を1回（仕分け → run → decide → 審判官 → 再仕分け → STOP）流し、結果の形を確かめる
  2. tests/test_guard.py（ガードの拒否リストと壊れた入力の扱い）
このファイルの場所から辿るので、ブランチの作業コピーの中で流せば、その作業コピーの runner を試す。
"""
import glob, json, os, subprocess, sys

sys.stdout.reconfigure(encoding="utf-8")
NIGHT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = dict(os.environ, FAKE_CLAUDE_SLEEP="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
for k in ("NIGHT_EVAR_ROOT", "NIGHT_HOME", "NIGHT_CLAUDE_BIN", "NIGHT_MODEL", "NIGHT_RUNNER"):
    env.pop(k, None)
fails = []


def check(ok, what):
    print(("  ok   " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        fails.append(what)


print("自己試験 1/2: 偽の claude で砂場を流す", flush=True)
p = subprocess.run([sys.executable, os.path.join(NIGHT, "tests", "sandbox.py"), "selftest", "--fake", "--judge", "4",
                    "--max-triage", "2", "--stop-after-idle"], env=env, capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=600)
run = next((ln.split("砂場: ", 1)[1].strip() for ln in p.stdout.splitlines() if ln.startswith("砂場: ")), None)
check(p.returncode == 0 and run is not None, f"砂場スクリプトが最後まで動いた（exit {p.returncode}）")
if run:
    state = os.path.join(run, "home", "state")
    log = open(os.path.join(state, "runner.log"), encoding="utf-8").read() if os.path.exists(os.path.join(state, "runner.log")) else ""
    rows = [json.loads(l) for l in open(os.path.join(state, "ledger.jsonl"), encoding="utf-8")] if os.path.exists(os.path.join(state, "ledger.jsonl")) else []
    work = [r for r in rows if r.get("kind") == "work"]
    judges = [r for r in rows if r.get("kind") == "judge"]
    console = open(os.path.join(run, "console.log"), encoding="utf-8").read()
    # 構文エラーでは "Traceback" の行が出ないので、"〜Error:" の行も拾う（2026-09-25 関門の試験で素通りした）
    check("Traceback" not in log + console and not any(ln.split(":")[0].endswith("Error") for ln in console.splitlines()),
          "例外が出ていない")
    check(any(r.get("status") == "done" and r.get("merge") == "merged" for r in work), "run が done になり、本流へマージされた")
    check(any(r.get("status") == "needs_decision" and r.get("merge") == "merged" for r in work), "decide が needs_decision になり、本流へマージされた")
    check(not any(r.get("status") in ("error", "gave_up") for r in work), "error / gave_up が無い")
    check(len(judges) >= 1 and not any(r.get("is_error") for r in judges), f"審判官が走り、採点を書けた（{len(judges)}回）")
    check(sum(r.get("kind") == "triage" for r in rows) >= 2, "作業の後に再仕分けが走った")
    check("ブラウニーを終了: 「ブラウニーを止める」が押された" in log, "STOP で止まった")
    proj = os.path.join(run, "evar", "ProjA")
    pend = open(os.path.join(proj, "PENDING.md"), encoding="utf-8").read()
    check("argparse" not in pend, "run の項目が PENDING から抜けた")
    wt = subprocess.run(["git", "worktree", "list"], cwd=proj, capture_output=True, text=True).stdout.strip().splitlines()
    check(len(wt) == 1, f"作業コピーが残っていない（{len(wt)}件）")
    check(not os.path.exists(os.path.join(proj, ".claims")) or all(l.startswith("#") or not l.strip() for l in open(os.path.join(proj, ".claims"), encoding="utf-8-sig")),
          "書き込み予約が放されている")
    reports = sorted(glob.glob(os.path.join(run, "home", "reports", "*.md")))
    one = next((p for p in reports if not p.endswith("_詳細.md")), "")
    full = next((p for p in reports if p.endswith("_詳細.md")), "")
    body = open(one, encoding="utf-8").read() if one else ""
    heads = [ln for ln in body.splitlines() if ln.startswith("## ")]
    check([h.split("（")[0] for h in heads[:3]] == ["## できたこと", "## 保留中の許可", "## 失敗"],
          f"朝の報告の1枚目が「できたこと・保留中の許可・失敗」の3節で始まる（{heads[:3]}）")
    detail = open(full, encoding="utf-8").read() if full else ""
    check(all(k in detail for k in ("## 一覧", "## ⚖ 審判官の採点", "ブランチ:", "## 時系列")), "報告の全文に一覧・採点・ブランチ・時系列が載った")
    outs = {}
    for r in rows:
        if r.get("kind") == "outcome":
            outs[r.get("ticket")] = r.get("outcome")
    ids = {r.get("ticket") for r in work}
    check(ids and all(outs.get(i) in ("done", "hold", "failed") for i in ids), f"どの票にも行き先（できた・保留・失敗）が付いた（{outs}）")
    check(any(r.get("kind") == "finish" and r.get("code") == 0 for r in rows), "合流は wt.py finish（既定はスタブ）を通った")

print("自己試験 2/2: ガード試験", flush=True)
g = subprocess.run([sys.executable, os.path.join(NIGHT, "tests", "test_guard.py")], capture_output=True, text=True, encoding="utf-8", errors="replace")
out = g.stdout
check("mismatch: []" in out and "False" not in out, "ガード試験がすべて期待どおり")

print(("合格" if not fails else f"不合格 {len(fails)} 件: " + " ／ ".join(fails)) + (f"（砂場 {run}）" if run else ""), flush=True)
sys.exit(1 if fails else 0)
