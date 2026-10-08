"""ブラウニー（無人実行）のセッションにだけ差し込むフック。runner.py が --settings で渡す。

PreToolUse : 不可逆な操作を拒否する（exit 2）。ブラウニーは bypassPermissions で動くので、ここが唯一の歯止め。
             上位 CLAUDE.md §不可逆な操作は人間が握る — push・公開・配備・サーバーの起動停止・送信・恒久削除・
             セキュリティ設定・凍結領域への書き込み。
PostToolUse: 文脈が NIGHT_CTX_LIMIT（既定15万）を超えたら「進捗を書いて終われ」と返す。runner が新しいセッションで続きを出す。
             以後 3万増えるごとに再度返す。

ブラウニー以外のセッションでは何もしない（NIGHT_RUNNER=1 のときだけ効く）。
越えない線（git push とリモート操作・Claude の設定/フック/エージェント定義と git の全体フックへの書き込み・入れ子の claude・
作業ディレクトリの外での削除）は、NIGHT_GUARD が off でも常に止める（2026-09-25 敵対レビュー③2・③13。line_guard）。
"""
import json, os, re, sys

sys.path.insert(0, os.path.expanduser("~/.claude/tools"))

# ---- 不可逆（コマンド本文に対する検査。Bash / PowerShell 共通） ----
IRREVERSIBLE = [
    (r"\bgit\s+push\b", "git push"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard"),
    (r"\bgit\s+clean\b", "git clean"),
    (r"\bgit\s+branch\s+-D\b", "git branch -D"),
    (r"\bgit\s+stash\s+(drop|clear)\b", "git stash drop/clear"),
    (r"\bgit\s+commit\b[^\n]*--amend", "git commit --amend"),
    (r"\bgit\s+(checkout|restore)\s+(--\s|\.\s*$)", "作業ツリーの変更を捨てる git checkout/restore"),
    (r"\bgit\s+(filter-branch|filter-repo)\b", "git の履歴書き換え"),
    (r"\bgit\s+remote\s+(add|set-url|remove)\b", "git remote の変更"),
    (r"(^|[\s;&|(])(gh|glab)\s", "gh/glab（外部への発信）"),
    (r"\b(npm|pnpm|yarn)\s+publish\b", "パッケージ公開"),
    (r"\btwine\s+upload\b", "パッケージ公開"),
    (r"\brm\s+-[a-zA-Z]*[rRf]", "rm -r/-f（削除は 排除/ へ退避する）"),
    (r"\bRemove-Item\b", "Remove-Item（削除は 排除/ へ退避する）"),
    (r"\b(rmdir|rd)\s", "rmdir"),
    (r"\bdel\s+/", "del /s"),
    (r"\bClear-RecycleBin\b", "ごみ箱を空にする"),
    (r"\b(Stop-Process|taskkill)\b|(^|[\s;&|])kill\s", "プロセスの停止"),
    (r"\b(Stop|Start|Restart|Set)-Service\b", "サービスの操作"),
    (r"\bsc(\.exe)?\s+(stop|start|config|delete|create)\b", "サービスの操作"),
    (r"\bnet\s+(stop|start|user)\b", "サービス/アカウントの操作"),
    (r"\b(shutdown|Stop-Computer|Restart-Computer)\b", "PC の停止/再起動"),
    (r"\bschtasks\b|\b(Register|Unregister|Enable|Disable|Set)-ScheduledTask\b", "タスクスケジューラの変更"),
    (r"\breg(\.exe)?\s+(add|delete|import|load)\b", "レジストリの変更"),
    (r"\b(Set|New|Remove)-ItemProperty\b[^\n]*HK(LM|CU|CR|U)", "レジストリの変更"),
    (r"\b(Set|Add|Remove)-MpPreference\b", "Defender の設定変更"),
    (r"\bnetsh\b|\bSet-NetFirewall|\bNew-NetFirewall", "ネットワーク/ファイアウォールの設定変更"),
    (r"\bSet-ExecutionPolicy\b", "実行ポリシーの変更"),
    (r"\b(Invoke-Expression|iex)\b", "文字列の実行（iex）"),
    (r"\|\s*(sh|bash|pwsh|powershell|cmd)(\.exe)?\b", "ダウンロードしたものをそのまま実行"),
    (r"\b(winget|choco|scoop)\s", "ソフトウェアの導入/更新"),
    (r"\bnpm\s+(i|install)\s+(-g|--global)\b", "グローバル導入"),
    (r"\bclaude\s+(update|install|config|mcp\s+add)\b", "Claude 本体の更新/設定変更"),
    # 入れ子の claude / codex にはブラウニーのフックが付かない（--settings を渡さずに bypassPermissions で起こせる）。
    # コマンドの位置に出たもの（フルパス・引用符・& 呼び出しを含む）と、起動フラグが続くもの（subprocess の引数列など）を拒否する。
    # `~/.claude/tools/x.py` のようなパスは通す（直前が「.」なので当たらない）。
    (r"(^|[;&|(\n`{]|\b(exec|start|Start-Process|xargs|env|nohup|call)\s+|/c\s+)\s*[\"']?([^\s\"';&|]*[\\/])?"
     r"(claude|codex)(\.exe|\.cmd|\.ps1)?[\"']?(\s|$|\))", "入れ子の claude/codex（ブラウニーのフックが付かない）"),
    (r"(?<![\w.-])(claude|codex)(\.exe|\.cmd)?[\"']?\s*,?\s*[\"']?(-p|--print|--permission-mode|--dangerously|--resume|--continue|--settings)\b",
     "入れ子の claude/codex（ブラウニーのフックが付かない）"),
    (r"disableAllHooks", "フックの無効化"),
    (r"\bSend-MailMessage\b", "メール送信"),
    (r"(start|stop|restart)[_-]?server", "サーバーの起動/停止"),
    (r"\bnogui\b|valheim_server|-batchmode\s+-nographics", "実サーバーの起動"),
]

# ---- 凍結領域（書き込みを拒否。読むのは可） ----
FROZEN = [
    (r"GameAProject[\\/]+Live", "実運用のゲームサーバー一式"),
    (r"_public_copies", "公開済みリポジトリの作業コピー"),
    (r"worlds_local", "Valheim の凍結ワールド"),
    (r"My Games[\\/]+Terraria", "Terraria の凍結ワールド/プレイヤー"),
    # 設定・フック・スキル・tools（ctx_now.py はこのフックが import する）・.claude.json まで丸ごと。
    # 書き換えられると、フックを外す・フック内でコードを動かす・MCP を足すことができる。
    (r"\.claude(\.json|[\\/])", "Claude の設定・フック・スキル・tools"),
    (r"\.mcp\.json", "MCP サーバーの設定"),
    (r"BrownieProject[\\/]+(runner\.py|hooks|prompts|night_settings\.json|.*\.bat)", "ブラウニーそのもの"),
]
WRITE_VERBS = (r"(Copy-Item|Move-Item|Rename-Item|Set-Content|Add-Content|Out-File|New-Item|Clear-Content|"
               r"(^|\s)(cp|mv|tee|touch|sed\s+-i)\s|>>?|\bcopy\s|\bmove\s|\bren\s)")
# インタプリタに渡したコード片や .NET の API はファイルを書けるのに WRITE_VERBS に出てこない
# （`python -c "open('.claude/settings.local.json','w')..."`）。凍結パスと一緒に出たら書き込みとみなす。
INLINE_CODE = (r"\b(python[\d.]*|py|node|deno|ruby|perl|php)(\.exe)?\s+(-\w+\s+)*-[ce]\b|"
               r"\b(powershell|pwsh)(\.exe)?\b[^\n]*\s-(c|Command|e|EncodedCommand)\b|\[(System\.)?IO\.|"
               r"\bopen\s*\(|\.(write_text|write_bytes|unlink|rmdir)\s*\(")

# ---- 外部へ発信する/人に聞くツール ----
DENY_TOOLS = [
    (r"^AskUserQuestion$", "エヴァは不在。判断待ちは進捗ファイルに書く"),
    (r"^(Artifact|ArtifactData|ArtifactComments|RemoteTrigger|CronCreate|CronDelete|PushNotification|SendMessage|Workflow)$",
     "外部公開・常駐設定・他セッションへの発信"),
    (r"^mcp__claude-in-chrome__", "エヴァのログイン済み Chrome"),
    (r"^mcp__scheduled-tasks__|^mcp__ccd_", "アプリ/予約タスクの操作"),
    (r"(send_message|create_draft|update_draft|reply|forward|share_file|trash|delete|create_event|update_event|"
     r"respond_to_event|publish|deploy|tiktok|createJira|editJira|transitionJira|addComment|addWorklog|"
     r"createConfluence|updateConfluence|createCompass|createIssueLink|create_file|update_file|copy_file|"
     r"label_|unlabel_|mark_|untrash|create_label|update_label)", "外部サービスへの書き込み・送信"),
]


# ---- 越えない線（NIGHT_GUARD が off でも常に効く。2026-09-25 敵対レビュー③2・③13）----
# ブラウニーは bypassPermissions で動き、拒否リストは既定 off（エヴァの判断）。それでも次の4つは、実行されたら朝に取り消せないか、
# ブラウニーの歯止めそのものを外せるので、ガードの on/off に関わらず止める。止めたら作業役は needs_permission で終える（prompts/work.md）。
#  1. git push とリモート操作（remote の追加・変更、gh/glab）
#  2. Claude の設定・フック・エージェント定義（.claude/settings*・hooks/・agents/）と git の全体フック（.git-hooks/・.githooks/）への書き込み
#  3. 入れ子の claude / codex（ブラウニーのフックが付かない）
#  4. 作業ディレクトリ（フックの入力の cwd）の外での削除
GIT_OPTS = r"\bgit(\.exe)?[\"']?((\s+-[Cc]\s+(\"[^\"]*\"|'[^']*'|\S+))|(\s+--?[\w.-]+(=\S+)?))*"
LINE_CMD = [
    (GIT_OPTS + r"\s+(push|send-email|request-pull)\b", "git push（リモートへの送信）"),
    (GIT_OPTS + r"\s+remote\s+(add|set-url|remove|rm|rename|set-head|set-branches|prune)\b", "git remote の変更"),
    (GIT_OPTS + r"\s+config\b[^\n;&|]*\b(remote|url|pushurl)\.", "リモートの設定の変更"),
    (r"(^|[\s;&|(`\"'])(gh|glab)(\.exe)?\s", "gh/glab（リモートの操作）"),
] + [x for x in IRREVERSIBLE if "入れ子" in x[1]]  # 入れ子の claude / codex の2つの型（上の拒否リストと同じもの）
LINE_PATH = r"(^|[\\/\"'\s=])\.claude[\\/]+(settings[^\\/\s\"']*|hooks([\\/]|$)|agents([\\/]|$))|(^|[\\/\"'\s=])\.git-?hooks([\\/]|$|[\s\"'])"
DELETE = (r"(^|[\s;&|(`])(rm|rmdir|rd|del|erase|Remove-Item|ri|unlink|shred)(\.exe)?(?=\s|$)|shutil\.rmtree|"
          r"\bos\.(remove|unlink|rmdir|removedirs)\s*\(|\.(unlink|rmdir|rmtree)\s*\(|\[(System\.)?IO\.(File|Directory)\]::Delete")
PATH_TOKEN = re.compile(r"[A-Za-z]:[\\/][^\s\"'`;|&<>(),]*|(?<![\w.~$%])/[^\s\"'`;|&<>(),]*|~[\\/]?[^\s\"'`;|&<>(),]*|"
                        r"\$HOME[^\s\"'`;|&<>(),]*|\$env:\w+[^\s\"'`;|&<>(),]*|%\w+%[^\s\"'`;|&<>(),]*|(?<![\w.])\.\.(?=[\\/\s\"']|$)")


def _abs(tok):
    """コマンドの中のパスを Windows の絶対パスにする。決められないもの（~・環境変数・..・ドライブの無い /）は None＝外とみなす。"""
    m = re.match(r"^/([a-zA-Z])(/.*|$)", tok)
    if m:
        tok = f"{m.group(1)}:{m.group(2) or '/'}"
    if re.match(r"^[A-Za-z]:[\\/]", tok):
        return os.path.normcase(os.path.normpath(tok))
    return None


def _outside(cmd, cwd):
    """削除のコマンドが cwd の外のパスに触れているか。触れていれば、そのパス（の1つ）を返す。"""
    base = os.path.normcase(os.path.normpath(cwd)) if cwd else None
    for tok in PATH_TOKEN.findall(cmd):
        p = _abs(tok)
        if p is None or base is None:
            return tok
        try:
            if os.path.commonpath([p, base]) != base:
                return tok
        except ValueError:
            return tok
    return None


def deny_line(reason):
    sys.stderr.write(f"[ブラウニーの越えない線] 拒否: {reason}。実行しない。線の手前までをコミットし、進捗ファイルの「許可が要る操作」に"
                     "〈操作・理由・流すコマンドか差分〉を書いて `STATUS: needs_permission` で終えてください。\n")
    sys.exit(2)


def line_guard(name, ti, cwd):
    """越えない線（ガードの on/off に関わらず効く）。"""
    if name in ("Bash", "PowerShell"):
        cmd = ti.get("command") or ""
        for pat, why in LINE_CMD:
            if re.search(pat, cmd, re.IGNORECASE | re.MULTILINE):
                deny_line(why)
        writes = re.search(WRITE_VERBS, cmd, re.IGNORECASE) or re.search(INLINE_CODE, cmd, re.IGNORECASE) \
            or re.search(DELETE, cmd, re.IGNORECASE)
        if writes and re.search(LINE_PATH, cmd, re.IGNORECASE):
            deny_line("Claude の設定・フック・エージェント定義か、git の全体フックへの書き込み")
        if re.search(DELETE, cmd, re.IGNORECASE):
            out = _outside(cmd, cwd)
            if out:
                deny_line(f"作業ディレクトリ（{cwd or '不明'}）の外での削除（{out}）。消さずに 排除/ へ退避するのも、作業コピーの外なら許可が要る")
    elif name in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        if re.search(LINE_PATH, path.replace("\\", "/"), re.IGNORECASE) or re.search(LINE_PATH, path, re.IGNORECASE):
            deny_line("Claude の設定・フック・エージェント定義か、git の全体フックへの書き込み")


# ---- 常時の検査（NIGHT_GUARD の on/off に関係なく、PreToolUse で最初に走る。2026-10-01 敵対レビュー N1〜N5）----
# 方針: 作業セッションが書いてよい場所を作業コピーの中に限り、外へ出る道具と操作は常に止める（個々の抜け道の列挙より範囲を狭める）。
# コマンドは「実行する位置」（区切りの先頭の語・シェルの -File/-Command の先・& や Start-Process の先）だけを見る。
# 引用符の中・heredoc の本文・コミットの文・grep の検索語は検査しない（2回目の敵対レビュー 高1・高2・#5）。
OUT_TOOLS = {x.lower() for x in (
    "Monitor", "RemoteTrigger", "CronCreate", "CronDelete", "CronList", "ScheduleWakeup", "PushNotification",
    "EnterWorktree", "ExitWorktree", "Workflow", "Agent", "Task", "SendMessage", "TeamCreate", "Artifact",
    "ArtifactData", "ArtifactComments", "DesignSync")}
WRITE_TOOLS = ("write", "edit", "multiedit", "notebookedit")
GIT_MUT = {"commit", "add", "rm", "mv", "reset", "clean", "checkout", "restore", "switch", "stash", "merge", "rebase",
           "cherry-pick", "revert", "push", "pull", "fetch", "tag", "worktree", "update-ref", "config", "remote", "branch",
           "am", "apply", "notes", "replace", "gc", "prune", "filter-branch", "send-email", "request-pull", "init", "clone",
           "submodule", "symbolic-ref", "reflog", "maintenance", "sparse-checkout"}
KILL_WORDS = {"taskkill", "stop-process", "spps", "kill", "pkill", "killall", "stop-service", "restart-service",
              "restart-computer", "stop-computer", "shutdown", "tskill"}
SERVER_SCRIPT_RE = re.compile(r"^(server_ctl|respawnkeeper|rk-start|escalate-claude)([._-].*)?$", re.I)
SHELLS = {"powershell", "pwsh", "cmd", "bash", "sh", "zsh", "wsl"}
LAUNCHERS = {"&", ".", "call", "start", "start-process", "saps", "exec", "nohup", "env", "sudo", "xargs", "time", "command",
             "builtin", "invoke-item"}
INTERPRETERS = {"python", "python3", "py", "node", "deno", "ruby", "perl", "php"}
SERVERS_JSON = os.environ.get("NIGHT_SERVERS_JSON") or "C:/Workspace/KeeperProject/servers.json"
FIXED_SERVER_PATHS = (r"C:\Workspace\Games\GameAProject\Live", r"%USERPROFILE%\AppData\LocalLow\IronGate\Valheim",
                      r"%APPDATA%\r2modmanPlus-local")


def _expand(s):
    """環境変数（%X%・$X・$env:X）と ~ を展開する。"""
    s = re.sub(r"\$env:(\w+)", lambda m: os.environ.get(m.group(1), m.group(0)), s, flags=re.I)
    s = re.sub(r"(^|(?<=[\s\"'=(]))~(?=[\\/]|$|[\s\"'])", lambda m: m.group(1) + os.path.expanduser("~"), s)
    return os.path.expandvars(s)


def _canon(s):
    """比べるための形: 展開・/c/→c:/・区切りは /・小文字・連続の / を1つに。"""
    s = _expand(s).replace("\\", "/")
    s = re.sub(r"(^|[\s\"'=(])/([a-zA-Z])/", r"\1\2:/", s)
    return re.sub(r"/+", "/", s).lower()


def server_paths():
    paths = list(FIXED_SERVER_PATHS)
    try:
        with open(SERVERS_JSON, encoding="utf-8-sig") as f:
            paths += [p for p in json.load(f).get("servers", []) if isinstance(p, str) and p]
    except (OSError, ValueError, AttributeError):
        pass  # 読めないときは固定の3つだけで続ける
    return [_canon(p).rstrip("/") for p in paths]


def _hits_server(text):
    c = _canon(text)
    for p in server_paths():
        if re.search(re.escape(p) + r"(?![\w.-])", c):
            return p
    return None


def _resolve(p, base):
    """パスの語を絶対パスにする。決められなければ None（$(…)・未定義の変数など）。"""
    p = p.strip("\"'")
    if re.search(r"\$\(|\$\{?(PWD|OLDPWD)\b|%CD%|\$pwd\b", p, re.I):
        return None  # シェルの今の場所はフックからは分からない（#4）
    p = _expand(p)
    if re.search(r"\$|%\w+%|`", p):
        return None
    m = re.match(r"^/([a-zA-Z])(/.*|$)", p)
    if m:
        p = f"{m.group(1)}:{m.group(2) or '/'}"
    if not re.match(r"^[A-Za-z]:[\\/]", p):
        if not base or re.match(r"^[/\\~]", p):
            return None
        p = os.path.join(base, p)
    return os.path.normcase(os.path.normpath(p))


def _inside(p, root):
    """正規化したパスの要素単位の包含（兄弟の Temp_x・wt_x は外）。"""
    if not p or not root:
        return False
    r = _resolve(root, None)
    if not r:
        return False
    p = os.path.normcase(os.path.normpath(p))
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:
        return False


def _git_mutates(sub, args):
    if sub not in GIT_MUT:
        return False
    if sub == "branch":
        return any(a in ("-d", "-D", "-m", "-M", "-f", "-c", "-C", "--delete", "--move", "--force", "--copy",
                         "-u", "--unset-upstream", "--edit-description") or a.startswith("--set-upstream") for a in args)
    if sub == "tag":
        return bool(args) and not any(a in ("-l", "--list", "-v", "--verify") or a.startswith("--contains") for a in args)
    if sub == "config":
        return not any(a in ("--list", "-l") or a.startswith("--get") for a in args)
    if sub == "remote":
        return bool(args) and args[0] in ("add", "set-url", "remove", "rm", "rename", "set-head", "set-branches", "prune", "update")
    if sub == "stash":
        return not (args and args[0] in ("list", "show"))
    if sub == "worktree":
        return not (args and args[0] == "list")
    if sub == "reflog":
        return bool(args) and args[0] in ("expire", "delete")
    if sub == "submodule":
        return not (args and args[0] in ("status", "summary", "foreach"))
    return True


SPLIT_RE = re.compile(r"&&|\|\||[;|\n]|&(?=\s)")
TOKEN_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"?|'[^']*'?|[^\s\"']+")


def _strip_bodies(cmd):
    """heredoc の本文と PowerShell の here-string を取り除く（文字列であってコマンドではない）。"""
    cmd = re.sub(r"@'\r?\n.*?\r?\n'@|@\"\r?\n.*?\r?\n\"@", "''", cmd, flags=re.S)
    out, lines, i = [], cmd.split("\n"), 0
    while i < len(lines):
        ln = lines[i]
        out.append(ln)
        tags = re.findall(r"<<-?\s*[\"']?([A-Za-z_]\w*)[\"']?", ln)
        i += 1
        for tag in tags:
            while i < len(lines) and lines[i].strip() != tag:
                i += 1
            i += 1  # 終わりの印の行
    return "\n".join(out)


def _segments(cmd):
    """引用符の外だけで区切り、各部分を語の列（引用符つきのまま）で返す。"""
    segs, buf = [], ""
    for tk in re.findall(r"\"(?:[^\"\\]|\\.)*\"?|'[^']*'?|[^\"']+", cmd):
        if tk[:1] in "\"'":
            buf += tk
        else:
            parts = SPLIT_RE.split(tk)
            buf += parts[0]
            for p in parts[1:]:
                segs.append(buf)
                buf = p
    segs.append(buf)
    return [TOKEN_RE.findall(s) for s in segs if s.strip()]


def _uq(tk):
    return tk[1:-1] if len(tk) >= 2 and tk[0] == tk[-1] and tk[0] in "\"'" else tk.strip("\"'")


def _base(w):
    b = re.split(r"[\\/]", _uq(w))[-1].lower()
    return re.sub(r"\.(exe|cmd|bat|ps1|com)$", "", b)


def _exec_views(toks, depth=0):
    """1つの区切りから〈実行する語の位置, 語の列〉を取り出す。シェルの -Command 等は入れ子として返す。
    戻り値: (execs, nested) — execs は [(実行する語, その後ろの語の列)]、nested は入れ子で検査する文字列の列。"""
    execs, nested = [], []
    i = 0
    while i < len(toks) and (re.fullmatch(r"[A-Za-z_]\w*=\S*", toks[i]) or toks[i] in ("(", "{", "!")):
        i += 1  # 先頭の環境変数の代入（GIT_DIR=X）は git_guard が別に読む
    if i >= len(toks):
        return execs, nested
    w = re.sub(r"^[({`!]+|^\$\(", "", toks[i])
    b = _base(w)
    rest = toks[i + 1:]
    if b in LAUNCHERS or w in ("&", "."):
        nxt = [t for t in rest if not t.startswith("-")]
        if nxt:
            k = rest.index(nxt[0])
            e2, n2 = _exec_views(rest[k:], depth)
            return [(w, rest)] + e2, n2
        return [(w, rest)], nested
    execs.append((w, rest))
    if b in ("invoke-expression", "iex"):
        nested.append(" ".join(_uq(t) for t in rest))
    elif b in SHELLS:
        j = 0
        while j < len(rest):
            o = _uq(rest[j]).lower()
            if o in ("-file", "-f") and j + 1 < len(rest):
                execs.append((rest[j + 1], rest[j + 2:]))
                break
            if o in ("-command", "-c", "/c", "/k", "-encodedcommand", "-ec"):
                if o in ("-encodedcommand", "-ec"):
                    raise ValueError("エンコードしたコマンド（-EncodedCommand）は検査できない")
                nested.append(" ".join(_uq(t) for t in rest[j + 1:]))
                break
            if not o.startswith(("-", "/")):
                execs.append((rest[j], rest[j + 1:]))  # `bash script.sh`・`pwsh x.ps1`
                break
            j += 1
    return execs, nested


def _check_exec(word, args):
    b = _base(word)
    name = re.split(r"[\\/]", _uq(word))[-1]
    if b in KILL_WORDS:
        deny_line(f"プロセス・サービスの停止や PC の停止（{name}。実運用サーバーを落としうる）")
    low = [_uq(a).lower() for a in args]
    if b == "wmic" and any(a in ("terminate", "delete") for a in low):
        deny_line("wmic によるプロセスの停止")
    if b == "sc" and low[:1] and low[0] in ("stop", "delete"):
        deny_line("サービスの停止・削除（sc）")
    if b == "net" and low[:1] and low[0] == "stop":
        deny_line("サービスの停止（net stop）")
    if SERVER_SCRIPT_RE.match(name) and not re.search(r"\.tests?\.ps1$", name, re.I):
        deny_line(f"実運用サーバーの操作スクリプト（{name}）")
    if b in INTERPRETERS and any(re.search(r"\bos\.kill\b|\.Kill\s*\(|\bsignal\.SIG", a) for a in args):
        deny_line("コード片からのプロセスの停止")


def _git_target(toks, cur, base_cwd):
    """git の区切りを読んで (サブコマンド, 引数, 対象) を返す。git でなければ None。"""
    env_dir = None
    i = 0
    while i < len(toks) and re.fullmatch(r"[A-Za-z_]\w*=\S*", toks[i]):
        k, v = toks[i].split("=", 1)
        if k.upper() in ("GIT_DIR", "GIT_WORK_TREE"):
            env_dir = _resolve(v, cur)
        i += 1
    words = [_uq(t) for t in toks[i:]]
    while words and words[0] in ("&", "exec", "command", "(", "{"):
        words = words[1:]
    if not words or not re.fullmatch(r"[({`]*(.*[\\/])?git(\.exe)?", words[0], re.I):
        return None
    target = env_dir or cur
    aliases, j = {}, 1
    while j < len(words) and words[j].startswith("-"):
        w = words[j]
        if w == "-C" and j + 1 < len(words):
            target = _resolve(words[j + 1], target) or base_cwd
            j += 2
        elif w == "-c" and j + 1 < len(words):
            m = re.match(r"alias\.([^=]+)=(.*)", words[j + 1], re.I)
            if m:
                aliases[m.group(1).lower()] = m.group(2)
            j += 2
        elif w in ("--git-dir", "--work-tree", "--namespace", "--exec-path") and j + 1 < len(words):
            if w in ("--git-dir", "--work-tree"):
                target = _resolve(words[j + 1], cur) or base_cwd
            j += 2
        elif w.startswith(("--git-dir=", "--work-tree=")):
            target = _resolve(w.split("=", 1)[1], cur) or base_cwd
            j += 1
        else:
            j += 1
    if j >= len(words):
        return None
    sub, args = words[j].lower(), words[j + 1:]
    if sub in aliases:
        al = aliases[sub].strip()
        if al.startswith("!"):
            deny_line(f"git の別名でシェルを動かす（{al[:40]}）")
        parts = al.split()
        sub, args = (parts[0].lower() if parts else ""), parts[1:] + args
    return sub, args, target


def check_command(cmd, cwd, depth=0):
    """実行する位置だけを見て、停止・サーバーの操作スクリプト・状態を変える git を止める。"""
    if depth > 3:
        raise ValueError("入れ子が深すぎる")
    root = os.environ.get("NIGHT_WORK_ROOT") or None
    base_cwd = cwd or os.getcwd()
    cur = base_cwd
    for toks in _segments(_strip_bodies(cmd)):
        words = [_uq(t) for t in toks]
        first = _base(words[0]) if words else ""
        if first in ("cd", "pushd", "set-location", "sl", "chdir", "push-location"):
            nxt = [w for w in words[1:] if not w.startswith("-") and w.lower() != "/d"]
            cur = (_resolve(nxt[0], cur) if nxt else None) or base_cwd  # 決められないときはフックの cwd（#4）
            continue
        execs, nested = _exec_views(toks)
        for w, args in execs:
            _check_exec(w, args)
        for n in nested:
            check_command(n, cur, depth + 1)
        g = _git_target(toks, cur, base_cwd)
        if not g:
            continue
        sub, args, target = g
        if not _git_mutates(sub, args):
            continue
        if root is None:
            deny_line(f"このセッション（作業コピーを持たない）での状態を変える git（git {sub}）")
        if not _inside(target, root):
            deny_line(f"作業コピー（{root}）の外のリポジトリへの状態を変える git（git {sub}・対象 {target}）")


def always_guard(d):
    raw_name = d.get("tool_name") or ""
    ti = d.get("tool_input")
    if ti is None:
        ti = {}
    if not isinstance(raw_name, str) or not isinstance(ti, dict):
        raise ValueError("tool_name / tool_input の形が想定外")
    name = raw_name.strip().lower()
    cwd = d.get("cwd") or None
    if name.startswith("mcp__") or name in OUT_TOOLS:
        deny_line(f"ブラウニーのセッションでは〈外へ出る道具〉（{raw_name.strip()}）を使わない。必要なら進捗ファイルの"
                  "『許可が要る操作』に書いて needs_permission で終える")
    cmd = ti.get("command")
    if isinstance(cmd, str) and cmd:
        if name not in ("bash", "powershell"):
            line_guard("Bash", ti, cwd)  # 道具の名前に関係なくシェルの検査に通す（N2）
        hit = _hits_server(cmd)
        if hit:
            deny_line(f"実運用サーバーの場所（{hit}）に触れるコマンド。読むだけなら Read・Grep・Glob の道具を使う")
        check_command(cmd, cwd)
    if name in WRITE_TOOLS:
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        if not isinstance(path, str):
            raise ValueError("file_path が文字列でない")
        if path and _hits_server(path):
            deny_line(f"実運用サーバーの場所への書き込み（{path}）")
        root = os.environ.get("NIGHT_WORK_ROOT")
        if root:
            p = _resolve(path, cwd or os.getcwd()) if path else None
            prog = os.environ.get("NIGHT_PROGRESS")
            okdirs = [root] + ([os.path.dirname(os.path.abspath(prog))] if prog else [])
            okdirs += [x for x in (os.environ.get("TEMP"), os.environ.get("TMP")) if x]
            try:
                import tempfile
                okdirs.append(tempfile.gettempdir())
            except Exception:
                pass
            if not any(_inside(p, x) for x in okdirs):
                deny_line(f"作業コピー（{root}）の外への書き込み（{path}）。ほかのリポジトリや C:\\Workspace 直下の文書も書かない")


def deny(reason):
    sys.stderr.write(f"[ブラウニーガード] 拒否: {reason}。不可逆・外部発信は手前まで準備し、進捗ファイルの「エヴァがやること」に書いて先へ進んでください。\n")
    sys.exit(2)


def guard_on():
    # 設計: 無人で止まらずに動き続けることを優先し、下の拒否リストは既定では効かせない（常時の検査 always_guard は別に必ず走る）。
    # 拒否リストは残し、NIGHT_GUARD=on のときだけ効かせる（既定は off）。
    return os.environ.get("NIGHT_GUARD", "off").lower() == "on"


def pre_tool_use(d):
    try:
        always_guard(d)  # 常時の検査（N1〜N5）。例外が出たら拒否する（fail closed）
    except SystemExit:
        raise
    except BaseException as e:
        deny_line(f"常時の検査が例外で止まった（{type(e).__name__}: {str(e)[:120]}）。安全側で拒否した")
    name = d.get("tool_name") or ""
    ti = d.get("tool_input") or {}
    # 評価役は読むだけ（off でも効く）。書き換えると、評価した差分とマージする差分が別物になる
    if os.environ.get("NIGHT_KIND") == "review" and name.strip().lower() in ("write", "edit", "multiedit", "notebookedit"):
        deny("評価役はファイルを書き換えない（読むだけ）")
    # 安全のためではなく動作のための拒否（off でも効く）: 不在なので聞くと答えが返らない
    if name == "AskUserQuestion":
        deny("AskUserQuestion — エヴァは不在。判断待ちは進捗ファイルに書く")
    line_guard(name, ti, d.get("cwd"))  # 越えない線は NIGHT_GUARD の on/off に関わらず効く（③2・③13）
    if not guard_on():
        return
    for pat, why in DENY_TOOLS:
        if re.search(pat, name):
            deny(f"{name} — {why}")
    if name in ("Bash", "PowerShell"):
        cmd = ti.get("command") or ""
        for pat, why in IRREVERSIBLE:
            if re.search(pat, cmd, re.IGNORECASE | re.MULTILINE):
                deny(why)
        if re.search(WRITE_VERBS, cmd, re.IGNORECASE) or re.search(INLINE_CODE, cmd, re.IGNORECASE):
            for pat, why in FROZEN:
                if re.search(pat, cmd, re.IGNORECASE):
                    deny(f"凍結領域への書き込み（{why}）")
    elif name in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        for pat, why in FROZEN:
            if re.search(pat, path, re.IGNORECASE):
                deny(f"凍結領域への書き込み（{why}）")
        if re.search(r"disableAllHooks", json.dumps(ti, ensure_ascii=False)):
            deny("フックの無効化を書き込もうとした")


def post_tool_use(d):
    limit = int(os.environ.get("NIGHT_CTX_LIMIT", "150000"))
    path = d.get("transcript_path") or ""
    if not path or not os.path.exists(path):
        return
    try:
        from ctx_now import last_usage
        ctx, _ = last_usage(path)
    except Exception:
        return
    if not isinstance(ctx, int) or ctx < limit:
        return
    home = os.environ.get("NIGHT_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    flag_dir = os.path.join(home, "state", "ctxflags")
    os.makedirs(flag_dir, exist_ok=True)
    flag = os.path.join(flag_dir, (d.get("session_id") or "unknown") + ".txt")
    last = 0
    try:
        last = int(open(flag, encoding="utf-8").read().strip() or 0)
    except (OSError, ValueError):
        pass
    if last and ctx < last + 30_000:
        return
    open(flag, "w", encoding="utf-8").write(str(ctx))
    progress = os.environ.get("NIGHT_PROGRESS", "進捗ファイル")
    print(json.dumps({
        "decision": "block",
        "reason": (f"[ブラウニー] 区切りです。文脈が {ctx:,} トークン（上限 {limit:,}）に達しました。"
                   f"今の作業を安全な地点で止め、{progress} を `STATUS: continue` で更新し"
                   "（次のセッションが最初に読むだけで続きに入れるよう、済んだこと・次の一手・注意点を具体的に）、"
                   "git リポジトリなら区切りのローカルコミットをしてから、これ以上ツールを使わずに終了してください。"),
    }, ensure_ascii=False))


def stop(d):
    """終わり方を縛る。砂場で decide が「リースを見張る」と書いて continue を3本続けた（2026-09-24）ので、
    continue は区切りの合図（ctxflags）を受けたセッションだけに許し、decide は needs_decision / blocked でしか終われなくする。
    公式仕様で Stop フックの差し戻しは連続8回で打ち切られるので、無限には回らない。"""
    progress = os.environ.get("NIGHT_PROGRESS")
    kind = os.environ.get("NIGHT_KIND", "")
    # 仕分けセッションの NIGHT_PROGRESS は tonight.json（1行目は `{`）。縛ると31回差し戻した（2026-09-24 砂場）
    if not progress or kind not in ("run", "decide"):
        return
    try:
        first = open(progress, encoding="utf-8").readline()
    except OSError:
        first = ""
    # needs_permission＝越えない線（push・配備・設定変更など）の手前で止めた（2026-09-25 敵対レビュー③1）
    m = re.match(r"\s*STATUS:\s*(done|continue|blocked|needs_decision|needs_permission)\b", first)
    reason = None
    if not m:
        reason = f"{progress} の1行目に `STATUS: done|continue|blocked|needs_decision|needs_permission` が無い。進捗ファイルの形どおりに書いてから終了してください。"
    elif m.group(1) == "continue":
        home = os.environ.get("NIGHT_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        flagged = os.path.exists(os.path.join(home, "state", "ctxflags", (d.get("session_id") or "unknown") + ".txt"))
        # 2026-09-25〜 decide も推奨案で実装まで進めるので、区切りの後の continue は run と同じく許す
        if not flagged:
            reason = ("continue は区切りの合図を受けたときだけ使える。外の状態（リース・プロセス・人の操作）を待っているなら "
                      "`STATUS: blocked` にして何に止められたかを書き、まだ自分で進められるなら作業を続けてください。")
    if reason:
        print(json.dumps({"decision": "block", "reason": "[ブラウニー] " + reason}, ensure_ascii=False))


def main():
    if os.environ.get("NIGHT_RUNNER") != "1":
        return
    # 既定の CP932 で読むと日本語パスで解釈に失敗し、素通りになる（2026-09-24 実測で文字化け）。
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        d = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    except ValueError:
        d = None
    if not isinstance(d, dict):
        # どのイベントか分からない。Stop で exit 2 を返すと終われなくなるので、off なら通す（2回目の敵対レビュー #12）
        if guard_on():
            deny("入力が JSON オブジェクトでない。安全側で拒否した")
        return
    ev = d.get("hook_event_name")
    if ev == "PreToolUse":
        try:
            pre_tool_use(d)
        except SystemExit:
            raise
        except BaseException as e:  # PreToolUse の中の例外は拒否する（fail closed）
            deny(f"ガード自身が例外で止まった（{type(e).__name__}: {str(e)[:120]}）。安全側で拒否した")
    elif ev == "PostToolUse":
        try:
            post_tool_use(d)
        except Exception:
            pass  # 区切りの通知が出ないだけ。ツールは既に実行済みなので止める意味がない
    elif ev == "Stop":
        try:
            stop(d)
        except Exception:
            pass  # 縛れないだけ。runner 側の MAX_SESSIONS_PER_TICKET が最後の歯止め


if __name__ == "__main__":
    # exit 2 以外で終わると、フックは「通した」ことになる。例外で落ちたら拒否側に倒す（レビュー指摘 #2）。
    try:
        main()
    except SystemExit:
        raise
    except BaseException as e:
        if not guard_on():
            sys.exit(0)  # イベントが分からないまま落ちた（PreToolUse の例外は main の中で拒否済み）
        try:
            deny(f"ガード自身が例外で止まった（{type(e).__name__}: {str(e)[:120]}）。安全側で拒否した")
        except SystemExit:
            raise
        except BaseException:
            os._exit(2)
