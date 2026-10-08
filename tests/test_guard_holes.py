"""ガードの穴 N1〜N5 を塞いだことの試験（NIGHT_GUARD=off のまま）。トークン0・数秒。

    python tests/test_guard_holes.py        # 終了コード 0＝合格、1＝不合格

止める＝exit 2、通す＝exit 0。作業セッションは NIGHT_WORK_ROOT を一時ディレクトリの作業コピーにして模す。
"""
import json, os, subprocess, sys, tempfile

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
G = os.path.join(HERE, "hooks", "night_guard.py")
WT = tempfile.mkdtemp(prefix="night_wt_")
PROG = os.path.join(HERE, "state", "progress", "holes-test", "t1.md")  # TEMP の外（作らない。フックはパスだけを見る）
base = {k: v for k, v in os.environ.items() if k not in ("NIGHT_GUARD", "NIGHT_WORK_ROOT", "NIGHT_PROGRESS")}
base["NIGHT_RUNNER"] = "1"
# サーバーの登録簿は試験用の写しを渡す（その PC に実物が無くても同じ結果になる）
_SRV = os.path.join(tempfile.mkdtemp(prefix="night_srv_"), "servers.json")
with open(_SRV, "w", encoding="utf-8") as _f:
    json.dump({"servers": [r"C:\Workspace\GameProject\GameBProject\server",
                           r"C:\Workspace\GameProject\GameAProject\PackWork\packb\server"]}, _f)
base["NIGHT_SERVERS_JSON"] = _SRV
WORK = dict(base, NIGHT_WORK_ROOT=WT, NIGHT_PROGRESS=PROG, NIGHT_KIND="run")
NOWT = dict(base, NIGHT_PROGRESS=os.path.join(HERE, "state", "tonight.json"), NIGHT_KIND="triage")
OUT = "C:/Workspace/GameProject/GameBProject"


def run(tool, inp, env, cwd=WT, raw=None):
    d = raw if raw is not None else json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": inp,
                                                "session_id": "x", "cwd": cwd}, ensure_ascii=False).encode("utf-8")
    return subprocess.run([sys.executable, G], input=d, capture_output=True, env=env).returncode


cases = []  # (説明, 期待, 実際)


def check(desc, want, tool, inp, env=WORK, cwd=WT, raw=None):
    cases.append((desc, want, run(tool, inp, env, cwd, raw)))


B, P = 2, 0
# N1 コネクタ・外へ出る道具
for n in ("mcp__claude_ai_Gmail__send_message", "mcp__claude_ai_Google_Drive__trash_file", "mcp__anything__x"):
    check(f"止める {n}", B, n, {})
for n in ("Monitor", "RemoteTrigger", "CronCreate", "ScheduleWakeup", "PushNotification", "EnterWorktree", "Workflow",
          "Agent", "SendMessage", "Artifact"):
    check(f"止める {n}", B, n, {"command": "echo hi"} if n == "Monitor" else {})
# N2 名前に関係なく command を検査
check("止める 別名の道具の git push", B, "SomeShell", {"command": "git push origin master"})
check("止める Monitor 経由の git push", B, "Monitor", {"command": "git push origin master"})
# N3 作業コピーの外への書き込み
for p in ("C:/Workspace/BrownieProject/hooks/night_guard.py", os.path.expanduser("~/.claude/tools/wt.py"),
          os.path.expanduser("~/.claude/CLAUDE.md"), "C:/Workspace/DECISIONS.md", "C:/Workspace/GameProject/GameAProject/Live/x.toml"):
    for tool in ("Write", "Edit"):
        check(f"止める {tool} 外 {p}", B, tool, {"file_path": p, "content": "x"})
check("通す Write 作業コピーの中", P, "Write", {"file_path": os.path.join(WT, "docs", "a.md"), "content": "x"})
check("通す Edit 作業コピーの中（相対）", P, "Edit", {"file_path": "docs/a.md", "old_string": "a", "new_string": "b"})
check("通す Write 進捗ファイル", P, "Write", {"file_path": PROG, "content": "STATUS: done"})
check("通す Write 進捗の隣", P, "Write", {"file_path": PROG + ".scratch-1", "content": "x"})
check("通す Write %TEMP%", P, "Write", {"file_path": os.path.join(tempfile.gettempdir(), "night_x.txt"), "content": "x"})
# N4 git
for c in (f"git -C {OUT} reset --hard", f"git -C {OUT} clean -fdx", f"git -C {OUT} add -A", f"git -C {OUT} commit -m x",
          f"git -C {OUT} checkout -- .", f"git -C {OUT} stash", f"cd {OUT} && git commit -m x",
          "git -C C:/Workspace add -A && git -C C:/Workspace commit -m x", f"sh -c \"cd {OUT} && git commit -m x\"",
          f"git -C {OUT} -c alias.p=reset p --hard"):
    check(f"止める {c}", B, "Bash", {"command": c})
for c in ("git add a.txt", "git commit -m x", f"git -C {WT} commit -m x"):
    check(f"通す 作業コピーで {c}", P, "Bash", {"command": c})
for c in ("git log", "git status", "git diff", "git show", f"git -C {OUT} log --oneline", f"cd {OUT} && git status"):
    check(f"通す {c}", P, "Bash", {"command": c})
for c in ("git commit -m x", "git add a.txt", "git reset --hard", "git clean -fd", "git checkout -- a", "git restore a",
          "git stash", "git merge x", "git rebase x", "git revert HEAD", "git push", "git branch -d x", "git branch -D x",
          "git tag v1", "git worktree add ../x", "git update-ref refs/heads/x HEAD", "git config user.name x"):
    check(f"止める 作業コピー無しで {c}", B, "Bash", {"command": c}, env=NOWT, cwd="C:/Workspace")
for c in ("git log", "git status", "git diff", "git config --get user.name", "git branch", "git tag -l"):
    check(f"通す 作業コピー無しで {c}", P, "Bash", {"command": c}, env=NOWT, cwd="C:/Workspace")
# N5 実運用サーバー
for c in ("taskkill /F /IM java.exe", "Stop-Process -Name valheim_server", "kill 1234", "Stop-Service x", "sc stop x",
          "net stop x", "shutdown /s", r"powershell -File C:\Workspace\GameProject\GameBProject\tools\server_ctl.ps1 stop",
          r"powershell -File C:\Workspace\KeeperProject\respawnkeeper.ps1", "cmd /c rk-start.bat",
          r"pwsh C:\x\escalate-claude.ps1"):
    check(f"止める {c}", B, "PowerShell", {"command": c})
srv = r"C:\Workspace\GameProject\GameBProject\server"
for c in (rf"Copy-Item a.dll {srv}\BepInEx\plugins\ ", r"Get-ChildItem C:\Workspace\GameProject\GameAProject\Live",
          r"ls /c/Workspace/GameProject/GameAProject/PackWork/packb/server/mods",
          r"Copy-Item x $env:USERPROFILE\AppData\LocalLow\IronGate\Valheim\worlds_local",
          r"dir %APPDATA%\r2modmanPlus-local\Valheim"):
    check(f"止める サーバーの場所 {c}", B, "PowerShell", {"command": c})
check("止める Write サーバーの場所（作業コピー無しでも）", B, "Write",
      {"file_path": r"C:\Workspace\GameProject\GameAProject\Live\cfg\a.toml", "content": "x"}, env=NOWT)
for tool, key in (("Read", "file_path"), ("Grep", "path"), ("Glob", "path")):
    check(f"通す {tool} サーバーの場所", P, tool, {key: srv, "pattern": "x"})
# 壊れた入力
check("止める tool_input が配列", B, "Bash", None, raw=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                                                      "tool_input": [1, 2]}).encode())
check("通す JSON でない（どのイベントか分からない。Stop を終われなくしない＝#12）", P, "Bash", None, raw=b"not json")
check("通す 配列の入力（イベントが分からない）", P, "Bash", None, raw=b"[1,2]")

# ---- 2回目の敵対レビュー: 誤検出（高1・高2・#4・#5）は通す ----
HEREDOC_COMMIT = "git commit -F - <<'EOF'\nnight: taskkill と Stop-Process を直した\n\ngit -C C:/Workspace/GameProject/GameBProject reset --hard\nEOF"
for c in ("git add respawnkeeper.ps1", "git diff tools/server_ctl.ps1", "pwsh -File tests/server_ctl.Tests.ps1",
          "grep -n server_ctl docs/", "grep -rn kill src", "pytest -k kill", "git log --grep shutdown",
          'git commit -m "night: shutdown 手順を直した"', "git commit -m 'night: taskkill の説明を直した'", HEREDOC_COMMIT,
          "cat <<EOF > a.md\ntaskkill /F /IM java.exe\nEOF", "rg -n Stop-Process .", "Select-String -Pattern shutdown a.md",
          'findstr /s "taskkill" *.md', "echo escalate-claude", "git commit -m 'night: git -C C:/x reset --hard の説明'",
          "cd $wt && git add a.txt", "cd $(git rev-parse --show-toplevel) && git commit -m x", "git -C $PWD add a.txt",
          'cmd /c "cd /d %CD% && git status"', "git -C %CD% add a.txt"):
    check(f"通す 誤検出 {c[:60]!r}", P, "Bash", {"command": c})
# 止めるものは引き続き止める（実行する位置）
for c in ("Get-Process java | Stop-Process", "& taskkill /F /IM java.exe", "Start-Process taskkill -ArgumentList '/F'",
          r"& C:\Workspace\GameProject\GameBProject\tools\server_ctl.ps1 stop", "bash -c 'kill 1234'", 'cmd /c "taskkill /IM java.exe"',
          'pwsh -Command "Stop-Process -Name valheim_server"', "Invoke-Expression 'shutdown /s'", "ls; shutdown /s",
          "echo x\ntaskkill /IM java.exe", r". .\rk-start.bat", "pwsh -File C:/x/respawnkeeper.ps1",
          'python -c "import os; os.kill(1, 9)"', 'bash -c "cd C:/Workspace/GameProject/GameBProject && git commit -m x"',
          r'cmd /c "cd /d C:\Workspace\GameProject\GameBProject && git commit -m x"'):
    check(f"止める 実行位置 {c[:60]!r}", B, "PowerShell", {"command": c})
# ---- 守る側の境界 ----
check("止める 道具名の大文字 MCP__x__y", B, "MCP__x__y", {})
check("止める 道具名の前後の空白 ' monitor '", B, " monitor ", {})
check("止める 道具名 ' Write ' の外への書き込み", B, " Write ", {"file_path": "C:/Workspace/DECISIONS.md", "content": "x"})
TMP = tempfile.gettempdir()
check("止める %TEMP% の兄弟 Temp_x", B, "Write", {"file_path": TMP.rstrip("\\/") + "_x/a.txt", "content": "x"})
WT2 = "C:/Workspace/BrownieProject/state/worktrees/holes/wt"  # TEMP の外の作業コピー（作らない。フックはパスだけを見る）
W2 = dict(WORK, NIGHT_WORK_ROOT=WT2)
check("止める 作業コピーの兄弟 wt_x", B, "Write", {"file_path": WT2 + "_x/a.txt", "content": "x"}, env=W2, cwd=WT2)
check("止める .. で作業コピーの外", B, "Write", {"file_path": WT2 + "/docs/../../x.txt", "content": "x"}, env=W2, cwd=WT2)
check("通す .. でも作業コピーの中", P, "Write", {"file_path": WT2 + "/docs/../a.txt", "content": "x"}, env=W2, cwd=WT2)
check("通す /c/ 形式・大文字・区切りの違い", P, "Write",
      {"file_path": "/c/WORKSPACE/brownieproject\\STATE/worktrees/holes/wt/a.txt", "content": "x"}, env=W2, cwd=WT2)
check("止める /c/ 形式で外", B, "Write", {"file_path": "/c/Workspace/DECISIONS.md", "content": "x"}, env=W2, cwd=WT2)
check("止める 進捗の兄弟ディレクトリ", B, "Write", {"file_path": os.path.dirname(PROG) + "_x/t1.md", "content": "x"})
for c in (f"git --git-dir={OUT}/.git commit -m x", f"git --work-tree={OUT} add -A", f"git -C {WT} -C {OUT} commit -m x",
          "git -C C:/Workspace -C GameBProject reset --hard", f"GIT_DIR={OUT}/.git git commit -m x",
          f"GIT_WORK_TREE={OUT} git add -A"):
    check(f"止める git の対象 {c[:60]}", B, "Bash", {"command": c})
check("通す -C を2回重ねて作業コピーの中", P, "Bash",
      {"command": f"git -C {os.path.dirname(WT)} -C {os.path.basename(WT)} add a.txt"})

# runner 側: ブラウニー自身の差分の保留
sys.path.insert(0, HERE)
try:
    import runner
    hold = ["runner.py", "finish_gate.py", "choose_projects.py", "hooks/night_guard.py", "prompts/work.md", "judge/x.md",
            "tests/test_guard.py", "night_settings.json", "start.bat"]
    for c in hold:
        cases.append((f"runner 保留 {c}", True, bool(runner.self_hold_paths([c]))))
    cases.append(("runner 保留しない HANDOFF・PENDING・DECISIONS だけ", False,
                  bool(runner.self_hold_paths(["HANDOFF.md", "PENDING.md", "DECISIONS.md"]))))
except Exception as e:
    cases.append((f"runner を import できない（{type(e).__name__}: {e}）", True, False))

fails = [c for c in cases if c[1] != c[2]]
for d, w, g in cases:
    print(("ok " if w == g else "FAIL ") + d + ("" if w == g else f"（期待 {w}・実際 {g}）"))
print(f"{len(cases)} 件中 不合格 {len(fails)} 件")
sys.exit(1 if fails else 0)
