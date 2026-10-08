"""ブラウニーが自分（BrownieProject）を直した作業票の関門。runner が本流側のこのファイルを、作業コピーに向けてマージの前に流す。

    python <本流>/tests/night_gate.py <作業コピー> <本流のブランチ> <作業票のブランチ>
    終了コード 0＝マージしてよい、1＝マージしない（理由を標準出力に書く）

1. 作業コピーの tests/selftest.py（偽の claude・トークン0）
2. 指示文（prompts/*.md）が変わっていれば、作業コピーで Haiku の砂場を1本（$0.3〜0.7 程度）
   Haiku は仕分けの質が低い（2026-09-25 実測で decide を見送りに入れた）ので、見るのは「流れが最後まで通るか」だけ。
tests/ と審判官の基準に触れた変更は、runner 側がこの関門より前に止める（ここでは見ない）。
"""
import json, os, subprocess, sys

sys.stdout.reconfigure(encoding="utf-8")
wt, base, branch = sys.argv[1], sys.argv[2], sys.argv[3]
env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
for k in ("NIGHT_EVAR_ROOT", "NIGHT_HOME", "NIGHT_CLAUDE_BIN", "NIGHT_MODEL", "NIGHT_RUNNER", "NIGHT_PROGRESS", "NIGHT_KIND"):
    env.pop(k, None)


def run(cmd, timeout):
    return subprocess.run(cmd, cwd=wt, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


s = run([sys.executable, os.path.join(wt, "tests", "selftest.py")], 900)
print(s.stdout.strip().splitlines()[-1] if s.stdout.strip() else s.stderr[-300:])
if s.returncode != 0:
    print("関門: 自己試験に落ちたのでマージしない")
    sys.exit(1)

changed = run(["git", "diff", "--name-only", f"{base}...{branch}"], 60).stdout.split()
if any(c.startswith("prompts/") for c in changed):
    print("指示文が変わったので Haiku の砂場を1本流す")
    h = run([sys.executable, os.path.join(wt, "tests", "sandbox.py"), "gate_haiku", "--model", "claude-haiku-4-5-20251001",
             "--judge", "0", "--max-triage", "1", "--budget", "2", "--stop-after-idle"], 1800)
    sb = next((ln.split("砂場: ", 1)[1].strip() for ln in h.stdout.splitlines() if ln.startswith("砂場: ")), None)
    ledger = os.path.join(sb, "home", "state", "ledger.jsonl") if sb else ""
    rows = [json.loads(l) for l in open(ledger, encoding="utf-8")] if ledger and os.path.exists(ledger) else []
    work = [r for r in rows if r.get("kind") == "work"]
    ok = h.returncode == 0 and work and not any(r.get("is_error") or r.get("status") == "error" for r in rows)
    print(f"Haiku: 作業 {len(work)} 本、" + "、".join(f"{r.get('ticket')}={r.get('status')}" for r in work)
          + f"、計 ${sum(float(r.get('cost') or 0) for r in rows):.2f}")
    if not ok:
        print("関門: Haiku の砂場が最後まで通らなかったのでマージしない")
        sys.exit(1)
print("関門: 合格")
