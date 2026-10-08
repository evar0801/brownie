"""偽の claude -p（トークン0）。runner.py の配管だけを試すために、台本どおりにファイルを書いて JSON を返す。

runner は NIGHT_CLAUDE_BIN にこのファイルを指定すると、本物の代わりにこれを起動する。
- NIGHT_KIND=triage : EVAR 直下の各プロジェクトの PENDING.md から、未完了の行ごとに作業票を作る（「判断待ち」を含む行は decide）
- NIGHT_KIND=run    : 担当ファイルを書き、PENDING の該当行を抜き、DECISIONS に1行足してコミットし、done を書く
                      （.claims にこのセッションの行を書いて、runner が放すかも試す）
- NIGHT_KIND=decide : DECISIONS に判断待ちを足して needs_decision
- NIGHT_KIND=judge  : 指示文から出力先を拾い、5項目 pass の採点を書く
- NIGHT_KIND=review : OK（FAKE_REVIEW_NG があるか、作業票の題に [review-ng] があれば NG）
- NIGHT_KIND=resolve: 衝突の印を外して両方を残し、コミット（作業票の題に [resolve-fail] があれば何もしない）
FAKE_CLAUDE_SLEEP で1本あたりの所要秒を変えられる（既定1秒）。

PENDING の行（＝作業票の題）に書いた印で、run の振る舞いを変えられる（test_overnight.py が使う）:
  [hang]      子プロセスを1つ起こして、両方とも眠り続ける（票ごとの時間切れで子プロセスごと止まるかを試す）。
              pid は <NIGHT_HOME>/state/fake_hang_<票>.json
  [error]     毎回エラーで落ちる
  [conflict]  作業コピーの shared.txt を書き換える（本流側の書き換えは wt_stub.py の台本 human で入れる。作業セッションの最中に
              本流を書き換えると、ブラウニーは「作業コピーの外への変更」として保留にするため）
  [frozen]    tests/frozen_touch.py も書く（ブラウニー自身の凍結に触れる）
  [permission] 手前の安全な変更（docs）をコミットし、STATUS: needs_permission と「許可が要る操作」を書く
  [outside]   作業コピーの外（NIGHT_WATCH_HOME/.claude/settings.json）を書き換える
  [push]      本流のリポジトリの refs/remotes/origin/fake-push を動かす（push の痕跡）
  [protected] 作業コピーに .claude/settings.json を足してコミットする（Claude の設定に触れる差分）
  [code]      docs に加えて tool.py も書く（.md 以外の差分）
  [badprio]   仕分けがこの票の priority を文字列にする（司令塔の例外が続いたときの試験）
  [gatefail]  gate_fail.txt を書く（本流側の tests/night_gate.py がこれを見て落ちる）
  [slowgate]  slow_gate.txt を書く（本流側の tests/night_gate.py がこれを見て眠り続ける＝関門の時間切れ）
  [s0]        HANDOFF.md の §0 の本文を書き換える（wt.py が「ブランチが §0 を書いた」で弾く）
  [s0line]    DECISIONS.md に新しい節を足し、その中に「§0 へ: …」の行を書く（wt.py が §0 の管理区間へ写す）
"""
import datetime, json, os, re, subprocess, sys, time

args = sys.argv[1:]
sid = args[args.index("--session-id") + 1] if "--session-id" in args else "fake"
prompt = sys.stdin.buffer.read().decode("utf-8", errors="replace")
kind = os.environ.get("NIGHT_KIND", "")
prog = os.environ.get("NIGHT_PROGRESS", "")
evar = os.environ.get("NIGHT_EVAR_ROOT", os.getcwd())
time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "1")))

# 利用枠の台本（test_usage_window.py が使う）。FAKE_USAGE_SCRIPT＝JSON のファイル {"by_kind": {"run": [項目, …]}, "default": 項目}。
# 呼ばれるたびに、自分の NIGHT_KIND の列の先頭を1つ取り出して（列が空なら default を）、本物と同じ形の rate_limit_event を流す。
# 項目＝{"u": 5時間枠の使用率 0〜1, "resetsAt": 戻る時刻（秒）, "limit": true なら上限で断られた結果を返して終わる, "bare": true なら通知を流さない}
_us = os.environ.get("FAKE_USAGE_SCRIPT")
if _us and os.path.isfile(_us):
    _sc = json.load(open(_us, encoding="utf-8"))
    _col = (_sc.get("by_kind") or {}).get(kind) or []
    _it = _col.pop(0) if _col else _sc.get("default")
    json.dump(_sc, open(_us, "w", encoding="utf-8"))
    if _it:
        if not _it.get("bare"):
            print(json.dumps({"type": "rate_limit_event", "session_id": sid, "rate_limit_info": {
                "status": "rejected" if _it.get("limit") else "allowed", "resetsAt": _it["resetsAt"], "rateLimitType": "five_hour",
                "unifiedWindows": {"five_hour": {"utilization": _it["u"], "resetsAt": _it["resetsAt"]},
                                   "seven_day": {"utilization": 0.01, "resetsAt": _it["resetsAt"] + 600000}}}}), flush=True)
        if _it.get("limit"):
            print(json.dumps({"type": "result", "subtype": "error", "is_error": True, "total_cost_usd": 0, "num_turns": 1, "session_id": sid,
                              "result": "You've hit your usage limit. Your limit will reset at 5am"}))
            sys.exit(0)


def git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, encoding="utf-8").stdout.strip()


def claim(proj, name):
    until = (datetime.datetime.now() + datetime.timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M")
    with open(os.path.join(proj, ".claims"), "a", encoding="utf-8-sig") as f:
        f.write(f"{name}\t{sid[:8]}\t{datetime.datetime.now():%Y-%m-%dT%H:%M}\t{until}\t\n")


result = "OK"
if kind == "triage":
    tickets = []
    for proj in sorted(os.listdir(evar)):
        pend = os.path.join(evar, proj, "PENDING.md")
        if not os.path.isfile(pend):
            continue
        for i, line in enumerate(open(pend, encoding="utf-8").read().splitlines(), 1):
            if not line.startswith("- [ ]"):
                continue
            dec = "判断待ち" in line
            tid = f"{proj.lower()}-{'decide' if dec else 'run'}-{i}"
            tickets.append({"id": tid, "project_dir": os.path.join(evar, proj), "title": line[6:40], "source": f"{pend}:{i}",
                            "kind": "decide" if dec else "run", "goal": "偽の作業", "files": [f"docs/{tid}.md", "PENDING.md", "DECISIONS.md"],
                            "inputs": "なし", "read_scope": "必読: PENDING.md", "done_check": f"test -f docs/{tid}.md",
                            "effort": "low", "priority": "x" if "[badprio]" in line else 3, "why": "偽の仕分け"})
    json.dump({"tickets": tickets, "skipped": []}, open(prog, "w", encoding="utf-8"), ensure_ascii=False)
    result = f"OK run={sum(t['kind'] == 'run' for t in tickets)} decide={sum(t['kind'] == 'decide' for t in tickets)} skipped=0"
elif kind == "resolve" and "[resolve-fail]" in prompt:
    result = "unresolved: 偽の解消役が解けなかった（[resolve-fail]）"
elif kind == "resolve":
    # 衝突の印を外して両方を残す（解消役の台本）
    cwd = os.getcwd()
    for f in git("diff", "--name-only", "--diff-filter=U", cwd=cwd).split():
        p = os.path.join(cwd, f)
        body = open(p, encoding="utf-8").read()
        body = re.sub(r"^(<{7}|={7}|>{7}).*\r?\n", "", body, flags=re.M)
        open(p, "w", encoding="utf-8").write(body)
    git("add", "-A", cwd=cwd)
    git("commit", "-q", "--no-edit", cwd=cwd)
    result = "resolved"
elif kind == "review":
    # 評価役の台本：FAKE_REVIEW_NG があれば止め、無ければ通す
    result = "NG: 偽の評価役が止めた（fake）" if os.environ.get("FAKE_REVIEW_NG") or "[review-ng]" in prompt else "OK"
elif kind in ("run", "decide"):
    t = json.loads(re.search(r"```json\r?\n(.*?)\r?\n```", prompt, re.S).group(1))
    proj = t["project_dir"]
    home = os.environ.get("NIGHT_HOME", ".")
    # 出典の行を題で探す（同じプロジェクトの前の票が行を抜いて合流すると、行番号がずれるため）
    pend_path = os.path.join(proj, "PENDING.md")
    plines = open(pend_path, encoding="utf-8").read().splitlines() if os.path.isfile(pend_path) else []
    hit = next((i for i, l in enumerate(plines, 1) if l.startswith("- [ ] ") and l[6:40] == t["title"]), None)
    text = plines[hit - 1] if hit else t["title"]
    # 本物のセッションでも全体フックがログを書く（~/.claude/hooks/*.log）。ブラウニーの見張りはこれを外の変更と数えない
    hook_log = os.path.join(os.environ.get("NIGHT_WATCH_HOME", ""), ".claude", "hooks", "hook.log")
    if os.environ.get("NIGHT_WATCH_HOME") and os.path.isfile(hook_log):
        with open(hook_log, "a", encoding="utf-8") as fh:
            fh.write(f"{t['id']} のセッション\n")
    # 関門フック（spawn_gate.py）も、道具の呼び出しのたびに状態ファイルを書く（~/.claude/hooks/.spawn_gate_state/<セッション>.json と .lock）。
    # 見張りがこれを外の変更と数えると、全部の票が保留になる（2026-10-08 の本番で起きた）
    gate_state = os.path.join(os.path.dirname(hook_log), ".spawn_gate_state")
    if os.environ.get("NIGHT_WATCH_HOME") and os.path.isdir(gate_state):
        with open(os.path.join(gate_state, f"{t['id']}.json"), "w", encoding="utf-8") as fh:
            json.dump({"ticket": t["id"], "pid": os.getpid()}, fh)
        open(os.path.join(gate_state, f"{t['id']}.json.lock"), "w").close()
    if "[hang]" in text:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
        json.dump({"fake": os.getpid(), "child": child.pid},
                  open(os.path.join(home, "state", f"fake_hang_{t['id']}.json"), "w", encoding="utf-8"))
        time.sleep(3600)
    if "[error]" in text:
        sys.stderr.write("偽のエラーで落ちた（[error]）\n")
        sys.exit(1)
    marker = os.path.join(home, "state", f"fake_once_{t['id']}")
    if not os.path.exists(marker) and ((kind == "run" and os.environ.get("FAKE_BLOCK_ONCE")) or (kind == "decide" and os.environ.get("FAKE_ERROR_ONCE"))):
        open(marker, "w").close()
        if kind == "run":
            open(prog, "w", encoding="utf-8").write("STATUS: blocked\n## ひとことで\n判断が要るので止まった（偽）\n")
            print(json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0, "num_turns": 1, "session_id": sid, "result": "STATUS: blocked"}))
        else:
            sys.stderr.write("偽のエラーで落ちた\n")
        sys.exit(0 if kind == "run" else 1)
    line_no = hit or int(t["source"].rsplit(":", 1)[1])
    claim(proj, "DECISIONS.md")
    dec_path = os.path.join(proj, "DECISIONS.md")
    if kind == "run":
        os.makedirs(os.path.join(proj, "docs"), exist_ok=True)
        open(os.path.join(proj, "docs", f"{t['id']}.md"), "w", encoding="utf-8").write("偽の成果物\n")
        pend = os.path.join(proj, "PENDING.md")
        lines = open(pend, encoding="utf-8").read().splitlines()
        item = lines[line_no - 1]
        lines[line_no - 1] = None
        open(pend, "w", encoding="utf-8").write("\n".join(x for x in lines if x is not None) + "\n")
        open(dec_path, "a", encoding="utf-8").write(f"- 完了（ブラウニー）: {item[6:60]}\n")
        if "[frozen]" in text:
            os.makedirs(os.path.join(proj, "tests"), exist_ok=True)
            open(os.path.join(proj, "tests", "frozen_touch.py"), "w", encoding="utf-8").write("# ブラウニーが凍結に触れた（偽）\n")
        if "[conflict]" in text:
            open(os.path.join(proj, "shared.txt"), "w", encoding="utf-8").write(f"night {t['id']}\n")
        if "[protected]" in text:
            os.makedirs(os.path.join(proj, ".claude"), exist_ok=True)
            open(os.path.join(proj, ".claude", "settings.json"), "w", encoding="utf-8").write('{"permissions": {"allow": ["Bash(*)"]}}\n')
        if "[code]" in text:
            open(os.path.join(proj, "tool.py"), "w", encoding="utf-8").write("print('偽の実装')\n")
        if "[gatefail]" in text:
            open(os.path.join(proj, "gate_fail.txt"), "w", encoding="utf-8").write("関門が落ちる印\n")
        if "[slowgate]" in text:
            open(os.path.join(proj, "slow_gate.txt"), "w", encoding="utf-8").write("関門が固まる印\n")
        hp = os.path.join(proj, "HANDOFF.md")
        if "[s0]" in text and os.path.isfile(hp):
            body = open(hp, encoding="utf-8").read().replace("今の話", "ブラウニーが書き換えた §0")
            open(hp, "w", encoding="utf-8", newline="\n").write(body)
        if "[s0line]" in text:
            open(dec_path, "a", encoding="utf-8").write(f"\n## ブラウニーのメモ {t['id']}\n- §0 へ: {t['id']} の結果を §0 に載せる\n")
        status = "needs_permission" if "[permission]" in text else "done"
    else:
        open(dec_path, "a", encoding="utf-8").write(f"## 判断待ち: {t['title']}\n- A / B / C（偽）\n")
        status = "needs_decision"
    git("add", "-A", cwd=proj)
    git("commit", "-qm", f"night: {t['id']}", cwd=proj)
    if "[outside]" in text:
        wh = os.path.join(os.environ.get("NIGHT_WATCH_HOME", home), ".claude")
        os.makedirs(wh, exist_ok=True)
        with open(os.path.join(wh, "settings.json"), "a", encoding="utf-8") as fh:
            fh.write('\n{"ブラウニーが書いた": true}\n')
    if "[push]" in text:
        main = os.path.dirname(os.path.abspath(os.path.join(proj, git("rev-parse", "--git-common-dir", cwd=proj))))
        git("update-ref", "refs/remotes/origin/fake-push", git("rev-parse", "HEAD", cwd=proj), cwd=main)
    h = git("rev-parse", "--short", "HEAD", cwd=proj)
    perm = ("## 許可が要る操作\n- 操作: `git push origin master`\n- 理由: 配布する版を上げるため（根拠 PENDING.md:3）\n"
            "- 流すコマンドか差分: `git -C <本流> push origin master`\n") if status == "needs_permission" else ""
    open(prog, "w", encoding="utf-8").write(
        f"STATUS: {status}\n## ひとことで\n偽の作業票 {t['id']} を処理した（コミット {h}）。\n## なぜやったか\n配管の試験。\n"
        f"## やったこと\n1. docs を書いた 根拠: {h}\n## 確かめたこと\n`git log -1` → {h}\n## 判断した所と理由\nなし\n"
        f"## エヴァに見てほしい所\nなし\n## 未解決と次の企画書\nなし\n{perm}## エヴァがやること\nなし\n")
    result = f"STATUS: {status}"
elif kind == "judge":
    out_md = re.search(r"`([^`]+\.md)`（エヴァが朝読む", prompt).group(1)
    out_json = re.search(r"`([^`]+\.json)`（runner が読む", prompt).group(1)
    ids = ["boot", "truthful", "evidence", "proposal", "progress"]
    json.dump({"judge_no": 0, "at": datetime.datetime.now().isoformat(),
               "items": [{"id": i, "name": i, "verdict": "pass", "note": "偽の採点", "evidence": []} for i in ids],
               "reopen": [], "feedback": ""}, open(out_json, "w", encoding="utf-8"), ensure_ascii=False)
    open(out_md, "w", encoding="utf-8").write("# 偽の審判官\n全項目 pass（配管の試験）\n")
    result = "judge OK"

print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0, "num_turns": 1,
                  "session_id": sid, "result": result}, ensure_ascii=False))
