"""ブラウニー — エヴァ不在の間、溜まった作業を「1作業票＝使い捨ての新しいセッション」で回し続ける。

司令塔はこの Python（LLM ではない）なので、待ちと取りまとめにトークンを使わない。
長い会話セッションに回させると往復ごとに巨大な文脈を読み直す（親コストの9割が文脈20万超の往復。
手元の実測）。ここでは各セッションを文脈15万で区切って捨てる。

流れ: 仕分けセッション（PENDING / HANDOFF §0 / inbox から今夜の作業票を作る）
      → 作業票を優先度順に1枚ずつ新しいセッションで処理（区切りが来たら新セッションで続き）
      → 作業票が尽きたら、元の文書が変わるまでトークン0で待機 → 変わったら仕分けし直す
審判官: 起動時と2時間ごとに、作業役とは別のセッションが judge/criteria.md の5項目で採点する（作業と並行）。
        虚偽が見つかった作業票は差し戻し、指摘は次の作業セッションに渡す。
合流: 成果物は git 統合の入口 `wt.py finish` を通して本流へ入れる（[E-045]）。評価役と関門は `--gate` に渡す finish_gate.py が走らせる。
      票はどれも「できた（合流済み）」「保留（許可待ち・本体使用中）」「失敗（原因つき）」のちょうど1つで終わり、台帳に kind=outcome で残る。
止まる条件: STOP ファイル（ブラウニーを止める.bat）／一晩の上限額／ウィンドウを閉じた
利用枠（[W-002]）: 5時間枠の使用率がしきい値に達したら新しいセッションを始めず、枠が戻る時刻までトークン0で待ってから再開する。
"""
import datetime, glob, hashlib, hmac, json, os, re, secrets, shutil, subprocess, sys, tempfile, time, traceback, uuid

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import usage_window  # noqa: E402
# 試運転用: NIGHT_EVAR_ROOT で走査対象を、NIGHT_HOME で状態と報告の置き場を差し替えられる（本番の文書に触らずに流れを試す）
EVAR = os.environ.get("NIGHT_EVAR_ROOT") or os.path.dirname(ROOT)
HOME = os.environ.get("NIGHT_HOME") or ROOT
STATE = os.path.join(HOME, "state")
REPORTS = os.path.join(HOME, "reports")
JUDGE_DIR = os.path.join(REPORTS, "judge")
PROMPTS = os.path.join(ROOT, "prompts")
CRITERIA = os.path.join(ROOT, "judge", "criteria.md")   # 評価基準は作業役の指示の外に置く
SETTINGS = os.path.join(ROOT, "night_settings.json")
INBOX = os.path.join(HOME, "inbox.md")
PROJECTS_FILE = os.path.join(HOME, "projects.txt")  # 今夜進めるプロジェクト。# を付けた行は対象外
LEDGER = os.path.join(STATE, "ledger.jsonl")      # 全セッションの記録（夜をまたいで残る）
TONIGHT = os.path.join(STATE, "tonight.json")      # 仕分けの出力（その夜の作業票）
RUNLOG = os.path.join(STATE, "runner.log")         # 黒いウィンドウと同じ内容（審判官が読む）
HEARTBEAT = os.path.join(STATE, "heartbeat.json")  # 今なにをしているか（30秒ごとに更新）
FEEDBACK = os.path.join(STATE, "judge_feedback.md")  # 審判官から作業役への指摘（最新）
SESSIONS = os.path.join(STATE, "sessions")         # 各セッションの生の出力
STOP = os.path.join(STATE, "STOP")
LOCK = os.path.join(STATE, "runner.lock")

BUDGET_USD = float(os.environ.get("NIGHT_BUDGET_USD", "80"))      # 一晩の上限（API 換算。Max 枠の消費の目安）
SESSION_CAP_USD = float(os.environ.get("NIGHT_SESSION_CAP_USD", "15"))  # 1セッションの上限（暴走を1本で止める）
JUDGE_CAP_USD = float(os.environ.get("NIGHT_JUDGE_CAP_USD", "5"))
CTX_LIMIT = os.environ.get("NIGHT_CTX_LIMIT", "150000")           # 区切り（フックが読む）
JUDGE_INTERVAL_SEC = int(os.environ.get("NIGHT_JUDGE_INTERVAL_SEC", str(2 * 3600)))
MAX_SESSIONS_PER_TICKET = 4
# 無人で止まらずに動き続けることを優先し、権限の確認は省いて動かす（歯止めは hooks/night_guard.py と司令塔の上限に置く）。
PERMISSION_MODE = os.environ.get("NIGHT_PERMISSION_MODE", "bypassPermissions")
MAX_TRIAGE_PER_NIGHT = int(os.environ.get("NIGHT_MAX_TRIAGE", "4"))
MODEL = os.environ.get("NIGHT_MODEL", "")  # 空なら CLI の既定。試験で安いモデルに下げるときだけ指定する
IDLE_POLL_SEC = int(os.environ.get("NIGHT_IDLE_POLL_SEC", "300"))
SESSION_TIMEOUT_SEC = 4 * 3600
# 時間切れ（1票が固まっても夜全体を止めない）。切れたら子プロセスごと止める（kill_tree）
TICKET_TIMEOUT_SEC = int(os.environ.get("NIGHT_TICKET_TIMEOUT_SEC", str(4 * 3600)))  # 1票の作業セッションの合計
TRIAGE_TIMEOUT_SEC = int(os.environ.get("NIGHT_TRIAGE_TIMEOUT_SEC", "3600"))
RESOLVE_TIMEOUT_SEC = int(os.environ.get("NIGHT_RESOLVE_TIMEOUT_SEC", "3600"))
REVIEW_TIMEOUT_SEC = int(os.environ.get("NIGHT_REVIEW_TIMEOUT_SEC", "900"))
GATE_TIMEOUT_SEC = int(os.environ.get("NIGHT_GATE_TIMEOUT_SEC", "3600"))    # wt.py finish --gate-timeout に渡す
NIGHT_GATE_STEP_SEC = max(60, GATE_TIMEOUT_SEC - REVIEW_TIMEOUT_SEC - 60)   # その中の tests/night_gate.py の分
# wt.py は本流が進むと関門を最大4回流し直すので、wt.py の持ち時間は関門4回ぶん＋余裕にする（②4）
WT_TIMEOUT_SEC = int(os.environ.get("NIGHT_WT_TIMEOUT_SEC", str(4 * GATE_TIMEOUT_SEC + 900)))
BUSY_RETRY_SEC = max(5, int(os.environ.get("NIGHT_BUSY_RETRY_SEC", "900")))  # 本体が使用中（30）だった票を再試行する間隔（下限5秒）
# 本流の未コミットの変更を「置き去り」とみなす古さ。これより古いと、待っても空かないので起動時と報告の先頭で知らせる（0＝見ない）
STALE_DIRTY_HOURS = float(os.environ.get("NIGHT_STALE_DIRTY_HOURS", "24"))
MAX_FINISH_TRIES = int(os.environ.get("NIGHT_MAX_FINISH_TRIES", "48"))  # 1票で wt.py finish を呼ぶ回数の上限
PRECHECK = os.environ.get("NIGHT_PRECHECK", "1") != "0"                  # 着手前の検査（[W-004]）。0＝見ずに始める（2026-10-08 までの動き）
PRECHECK_RECORDS = os.environ.get("NIGHT_PRECHECK_RECORDS", "0") == "1"  # 1＝記録（DECISIONS・PENDING・HANDOFF）の重なりでも始めない
PRECHECK_POLL_SEC = max(1, int(os.environ.get("NIGHT_PRECHECK_POLL_SEC", "300")))  # 始められる票が無いとき、本体が空いたかを見直す間隔
REFLOW_PREVIOUS = os.environ.get("NIGHT_REFLOW_PREVIOUS", "1") != "0"    # 前の夜に本体の使用中で保留になった票を、起動のはじめに流し直す（[W-004]）。0＝流さない
RECORDS_LATER = os.environ.get("NIGHT_RECORDS_LATER", "1") != "0"        # 本体で塞がっているのが記録だけのとき、成果物を先に本流へ入れる（wt.py finish --records-later・[W-004]）。0＝付けない
JUDGE_TIMEOUT_SEC = int(os.environ.get("NIGHT_JUDGE_TIMEOUT_SEC", "1800"))
GIT_TIMEOUT_SEC = int(os.environ.get("NIGHT_GIT_TIMEOUT_SEC", "300"))
# 夜の終わり（②2）: 既定は「時間では終わらない」（エヴァが止めるまで動く。STOP・上限・空き容量・例外の連続では止まる）。
# NIGHT_MAX_HOURS（起動からの最長時間）か NIGHT_END_AT（終わる時刻 HH:MM）を指定すると、その早い方で締めて終了する
MAX_HOURS = float(os.environ.get("NIGHT_MAX_HOURS", "0"))  # 0＝時間では終わらない
END_CLOCK = os.environ.get("NIGHT_END_AT", "")  # 空＝時刻では終わらない
END_GRACE_SEC = int(os.environ.get("NIGHT_END_GRACE_SEC", "1800"))  # 夜の終わりに走っているセッションの延長の上限
# 空き容量（②12）: 空きが「全体の NIGHT_MIN_FREE_PCT%」と「NIGHT_MIN_FREE_GB」の小さい方を下回ったら、新しい票を始めず夜を終える
MIN_FREE_PCT = float(os.environ.get("NIGHT_MIN_FREE_PCT", "12"))
MIN_FREE_GB = float(os.environ.get("NIGHT_MIN_FREE_GB", "20"))
# 利用枠（[W-002]）: 使用率は claude -p の出力（rate_limit_event）から読む。アカウント全体の値＝エヴァの別セッションの分も入る
USAGE_EVENTS = os.path.join(STATE, "usage_events.jsonl")                    # status が allowed 以外の通知と、上限で断られた結果の行（実物の形を残す）
USAGE_FILE = os.path.join(STATE, "usage.json")                              # いちばん新しい使用率（関門の中の評価役も書く）
USAGE_WARN_PCT = float(os.environ.get("NIGHT_USAGE_WARN_PCT", "70"))        # 5時間枠がこの%で知らせる（0＝知らせない）
USAGE_HOLD_PCT = float(os.environ.get("NIGHT_USAGE_HOLD_PCT", "85"))        # この%で新しいセッションを始めず、枠が戻るまで待つ（0＝上限に当たるまで走る）
USAGE_FOLLOW_PCT = float(os.environ.get("NIGHT_USAGE_FOLLOW_PCT", "97"))    # 票の途中のセッション（評価役・解消役）を起こさない%。走り終えた票は、しきい値を越えていても合流まで進める
USAGE_WEEK_HOLD_PCT = float(os.environ.get("NIGHT_USAGE_WEEK_HOLD_PCT", "95"))  # 7日枠の同じしきい値
USAGE_WAIT = os.environ.get("NIGHT_USAGE_WAIT", "1") != "0"                 # 0＝待たずに夜を終える（2026-10-01 までの動き）
USAGE_PROBE_SEC = int(os.environ.get("NIGHT_USAGE_PROBE_SEC", "600"))       # 使用率の記録がこれより古ければ、始める前に小さな1往復で読み直す（0＝読み直さない）
USAGE_PROBE_MODEL = os.environ.get("NIGHT_USAGE_PROBE_MODEL", "")           # 空＝作業と同じモデル（モデル別の枠で、読み直しだけ通るのを避ける）
USAGE_RETRY_SEC = int(os.environ.get("NIGHT_USAGE_RETRY_SEC", "600"))       # 戻る時刻が分からない上限のとき、確かめ直すまでの待ち（続けて当たると倍々に延ばす。最長60分）
USAGE_MARGIN_SEC = int(os.environ.get("NIGHT_USAGE_MARGIN_SEC", "60"))           # 戻る時刻からこの秒数だけ遅らせて再開する（ちょうどだと、まだ戻っていないことがある）
# ブラウニーが合流しないプロジェクト（③8）と、decide を合流しないゲームのプロジェクト（③6）。走査の根からの相対パス
NO_MERGE_PROJECTS = [p.strip() for p in os.environ.get(
    "NIGHT_NO_MERGE_PROJECTS", "").split(",") if p.strip()]
# ③6 は 2026-09-25 エヴァの決裁で取り下げ（ゲームの decide も今までどおり推奨案で実装して合流する）。既定は空。仕組みだけ残す
GAME_PROJECTS = [p.strip() for p in os.environ.get("NIGHT_GAME_PROJECTS", "").split(",") if p.strip()]
# 作業コピーの外への書き込みを見張る場所（③3）。試験では砂場の偽の家に向ける
WATCH_HOME = os.environ.get("NIGHT_WATCH_HOME") or os.path.expanduser("~")
BELOW_NORMAL = 0x00004000
# git 統合の入口（[E-045]）。試験ではスタブ（tests/wt_stub.py）に差し替える
WT_PY = os.environ.get("NIGHT_WT_PY") or os.path.join(os.path.expanduser("~"), ".claude", "tools", "wt.py")
GATE_SCRIPT = os.path.join(ROOT, "finish_gate.py")  # wt.py finish --gate に渡す関門（本流側＝この runner の隣のもの）
SELF_REPO = os.environ.get("NIGHT_SELF_REPO") or ROOT  # ブラウニー自身のリポジトリ（tests/・judge/ を凍結する対象）
IN_GATE = False  # finish_gate.py の中で動いているとき True（審判官を起こさない・heartbeat を書かない）

sys.stdout.reconfigure(encoding="utf-8")
# 試験用: NIGHT_CLAUDE_BIN に tests/fake_claude.py を指定すると、トークン0で配管だけを試せる
CLAUDE = os.environ.get("NIGHT_CLAUDE_BIN") or shutil.which("claude") or os.path.expanduser(r"~\.local\bin\claude.exe")


def now():
    return datetime.datetime.now()


def log(msg):
    line = f"[{now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(RUNLOG, "a", encoding="utf-8") as f:
            f.write(f"{now():%Y-%m-%d} {line}\n")
    except OSError:
        pass


def calendar_night():
    # 朝6時までは前日の夜として数える（起動したときに1回だけ決める。途中で日付が変わっても夜の名前は変えない＝②3）
    return (now() - datetime.timedelta(hours=6)).strftime("%Y-%m-%d")


NIGHT = os.environ.get("NIGHT_LABEL") or calendar_night()  # 夜の名前（報告・進捗・関門のフォルダ名）。main() が起動時に決める
RUN_ID = os.environ.get("NIGHT_RUN_ID", "")                  # 夜の回（起動ごと）。費用はこれで数える（②2）
END_TS = None                                              # 夜の終わりの時刻（main() が決める。関門の中では None）
GATE_KEY = b""                                             # 関門の情報ファイルの署名鍵（③4）。夜の回ごとに司令塔が作る


def night_date():
    return NIGHT


def toast(title, body):
    try:
        sys.path.insert(0, os.path.expanduser("~/.claude/hooks"))
        from toast import toast as _t
        _t(title, body)
    except Exception:
        pass


def keep_awake(on):
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:
        pass


def pid_alive(pid):
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
    return str(pid) in r.stdout


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def read_text(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9_-]", "-", str(s))


def dur(sec):
    sec = int(sec)
    return f"{sec // 3600}時間" if sec >= 3600 and sec % 3600 == 0 else (f"{sec // 60}分" if sec >= 60 and sec % 60 == 0 else f"{sec}秒")


def kill_tree(p):
    """子プロセスごと止める（claude.exe の下で動いているシェルやビルドを残さない）。止め切れたら True。
    親が先に終わって切り離された孫には taskkill /T が届かない（未検証の穴。②14）。"""
    if p.poll() is not None:
        return True
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        p.kill()
    except OSError:
        pass
    try:
        p.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass
    if p.poll() is None:
        log(f"  ⚠ プロセス {p.pid} を止め切れなかった")
        return False
    return True


def run_capture(cmd, cwd=None, timeout=GIT_TIMEOUT_SEC, env=None):
    """subprocess.run の代わり。出力はパイプではなく一時ファイルで受ける（孫がパイプを握ると、Windows の
    subprocess.run(timeout) は止めた後の読み取りで固まる＝②7）。時間切れなら子プロセスごと止め、returncode=-9・timed_out=True。"""
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
        try:
            p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe, env=env)
        except OSError as ex:
            return subprocess.CompletedProcess(cmd, 127, "", str(ex))
        try:
            p.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            kill_tree(p)
            timed_out = True
        fo.seek(0)
        fe.seek(0)
        r = subprocess.CompletedProcess(cmd, -9 if timed_out else p.returncode,
                                        fo.read().decode("utf-8", "replace"), fe.read().decode("utf-8", "replace"))
    r.timed_out = timed_out
    if timed_out:
        r.stderr += f"\n（{dur(timeout)} で終わらなかったので止めた）"
    return r


def wait_proc(p, deadline):
    """p が終わるまで待つ。待っている間も審判官の時刻と heartbeat を見る。deadline を過ぎたら子プロセスごと止めて False。
    偽の claude（1秒未満で終わる）で試験が遅くならないよう、見る間隔は 0.2 秒から 5 秒まで伸ばしていく。"""
    step = 0.2
    while p.poll() is None:
        if time.time() > deadline:
            kill_tree(p)
            return False
        judge_tick()
        usage_tick()
        heartbeat()
        time.sleep(min(step, max(0.05, deadline - time.time())))
        step = min(step * 2, 5)
    return True


def ledger_rows():
    rows = []
    try:
        with open(LEDGER, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return rows


def ledger_add(row):
    row.setdefault("run", RUN_ID)
    try:
        u5 = usage_window.pct(read_json(USAGE_FILE, {}), time.time())
    except Exception:
        u5 = None
    if u5 is not None:
        row.setdefault("u5", u5)  # 最後に読めた5時間枠の使用率（%）。1% あたり何ドルかを後で数えられるように残す
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def spent_tonight():
    # 作業と審判官が並行で使うので、合計は台帳から数え直す。数えるのは日付ではなく「この夜の回（起動）」（②2）
    if RUN_ID:
        return sum(float(r.get("cost") or 0) for r in ledger_rows() if r.get("run") == RUN_ID)
    return sum(float(r.get("cost") or 0) for r in ledger_rows() if r.get("night") == night_date())


def remaining_budget():
    """使ってよい残り額。並行で走っている審判官の上限も先に差し引く（②11）。"""
    left = BUDGET_USD - spent_tonight()
    if _judge.get("h") is not None and _judge["h"]["p"].poll() is None:
        left -= float(_judge["h"].get("cap") or 0)
    return left


def night_over():
    return END_TS is not None and time.time() >= END_TS


def session_end_cap():
    """セッションの時間切れの上限（夜の終わり＋延長。夜の終わりを過ぎて何時間も走らせない）。"""
    return None if END_TS is None else END_TS + END_GRACE_SEC


def disk_problem(paths=None):
    """空き容量が足りなければ理由を返す（②12）。しきい値は全体の MIN_FREE_PCT% と MIN_FREE_GB の小さい方。"""
    for p in paths or (EVAR, HOME):
        try:
            u = shutil.disk_usage(p)
        except OSError:
            continue
        need = min(u.total * MIN_FREE_PCT / 100, MIN_FREE_GB * 1024 ** 3)
        if u.free < need:
            return (f"空き容量が足りない（{p} のドライブの空き {u.free / 1024 ** 3:.1f}GB・{u.free * 100 / u.total:.1f}%。"
                    f"しきい値 {need / 1024 ** 3:.1f}GB＝全体の{MIN_FREE_PCT:g}%と{MIN_FREE_GB:g}GBの小さい方）")
    return None


def source_files():
    files = sorted(glob.glob(os.path.join(EVAR, "**", "PENDING.md"), recursive=True)
                   + glob.glob(os.path.join(EVAR, "**", "HANDOFF.md"), recursive=True))
    # BrownieProject/sandbox は試験用の砂場（本物の PENDING ではない）。本番の仕分けに拾わせない
    skip = ("排除", "_public_copies", os.path.join("GameAProject", "Live"), "node_modules", ".git",
            os.path.join("BrownieProject", "sandbox"), os.path.join("BrownieProject", "state"),  # state には作業コピー（worktrees）がある
            os.path.join("BrownieProject", "tests"))  # 試験用のひな形（tests/fixtures/ProjA/PENDING.md）を本番で拾わない
    # 走査の根からの相対パスで判定する（砂場の中で動かしたとき、砂場自身の PENDING まで外さないように）
    files = [f for f in files if not any(s in os.path.relpath(f, EVAR) for s in skip)]
    # 別セッションの作業コピー（<何か>/.claude/worktrees/…）を拾わない。パスの要素が .claude のときだけ外す（.claude_foo は巻き込まない）
    files = [f for f in files if ".claude" not in re.split(r"[\\/]", os.path.relpath(f, EVAR))]
    files = [f for f in files if project_selected(f)]
    return files + [INBOX]


def selected_projects():
    """projects.txt で選んだプロジェクト（絶対パス）。ファイルが無い・有効な行が無いときは None＝全部。
    走査の根に実在しない行は無視する（砂場の自己試験で本番の選択が効いて全部捨てないように）。"""
    try:
        lines = read_text(PROJECTS_FILE).splitlines()
    except OSError:
        return None
    dirs = [os.path.normcase(os.path.abspath(os.path.join(EVAR, l.strip().strip("/\\"))))
            for l in lines if l.strip() and not l.strip().startswith("#")]
    dirs = [d for d in dirs if os.path.isdir(d)]
    return dirs or None


def project_rules():
    """projects.txt の有効行（選んだ）と # 付きでパスの形をした行（外した）を (絶対パス, 選んだか) で返す。走査の根に実在しない行は無視。"""
    try:
        lines = read_text(PROJECTS_FILE).splitlines()
    except OSError:
        return []
    rules = []
    for l in lines:
        s = l.strip()
        on = not s.startswith("#")
        body = s.lstrip("#").strip().strip("/\\")
        if not body or not re.fullmatch(r"[\w./\\-]+", body):
            continue  # 空行・説明のコメント
        d = os.path.normcase(os.path.abspath(os.path.join(EVAR, body)))
        if os.path.isdir(d):
            rules.append((d, on))
    return rules


def project_selected(path):
    """入れ子のプロジェクトは、パスに一致する行のうちいちばん深いものに従う（親を選んで子を外せば子は除外）。
    一致する行が無ければ除外。有効行が0なら全部（selected_projects() が None）。"""
    if selected_projects() is None or not path:
        return True
    p = os.path.normcase(os.path.abspath(re.sub(r":[\d-]+$", "", str(path))))
    hits = [(len(d), on) for d, on in project_rules() if p == d or p.startswith(d + os.sep)]
    return max(hits)[1] if hits else False


# ---------------------------------------------------------------- 仕分けへ渡す手がかり（機械で拾う・トークン0）
# PENDING と HANDOFF §0 だけを見ていると、DECISIONS.md にだけ書かれた残り仕事（「ついでに直したい（未実行）」「未着手」「残り:」）と、
# TODO.md に投げられたそのプロジェクトの行を取りこぼす（[W-004]）。仕分け役に DECISIONS.md を読ませると高くつくので、
# 司令塔が行だけ抜いて `パス:行` 付きで渡し、仕分け役は要る節だけ範囲指定で読む。
LEADS_ON = os.environ.get("NIGHT_LEADS", "1") != "0"
LEADS_TAIL_LINES = int(os.environ.get("NIGHT_LEADS_TAIL_LINES", "400"))    # DECISIONS.md の末尾から見る行数
LEADS_PER_PROJECT = int(os.environ.get("NIGHT_LEADS_PER_PROJECT", "20"))   # 1プロジェクトで渡す行数の上限（新しい方を残す）
LEAD_CHARS = 140
LEAD_RE = re.compile(r"ついでに|未着手|未実行|未実装|未対応|やり残し|後回し|要対応|未解決|次の一手|残り\s*(?:\*\*)?\s*[:：]")
LEAD_NONE_RE = re.compile(r"[:：]\s*(?:\*\*)?\s*(?:なし|無し|ありません)\s*[。.]?\s*$")  # 「未解決: なし」は手がかりではない
LEAD_ID_RE = re.compile(r"\[?([A-Z]+-(?:\d{3,}|NEW)[^\s\]]*)")
TODO_PY = os.environ.get("NIGHT_TODO_PY") or os.path.join(os.path.expanduser("~"), ".claude", "tools", "todo.py")
TODO_FILE = os.path.join(EVAR, "TODO.md")


def lead_projects(files=None):
    """手がかりを拾うフォルダ＝今夜の対象のうち PENDING.md か HANDOFF.md を持つもの。
    files＝呼ぶ側がすでに持っている source_files() の結果（走査は遅いので、二度しない）。"""
    files = source_files() if files is None else files
    return sorted({os.path.dirname(f) for f in files if os.path.basename(f) in ("PENDING.md", "HANDOFF.md")})


def decision_leads(d):
    out = []
    for path in (os.path.join(d, "DECISIONS.md"), os.path.join(d, "docs", "DECISIONS.md")):
        lines = read_text(path).splitlines()
        head, start = "", max(0, len(lines) - LEADS_TAIL_LINES)
        for i, ln in enumerate(lines):
            if ln.startswith("## "):
                head = ln[3:].strip()
            if i < start or not LEAD_RE.search(ln) or LEAD_NONE_RE.search(ln):
                continue
            m = LEAD_ID_RE.match(head)
            out.append(f"- {path}:{i + 1} （{m.group(1) if m else head[:30]}） {ln.strip()[:LEAD_CHARS]}")
    return out[-LEADS_PER_PROJECT:] if LEADS_PER_PROJECT > 0 else []


def todo_leads(d):
    """TODO.md のうち、そのプロジェクトに属する開いている行（todo.py の出力のまま）。道具か TODO.md が無ければ空。"""
    rel = os.path.relpath(d, EVAR).replace("\\", "/")
    if rel == "." or not (os.path.isfile(TODO_FILE) and os.path.isfile(TODO_PY)):
        return []
    out = []
    for name in dict.fromkeys((rel, os.path.basename(d))):  # 所属はフォルダ名だけで書かれていることがある
        try:
            r = subprocess.run([sys.executable, TODO_PY, "--file", TODO_FILE, "list", "--project", name], capture_output=True,
                               encoding="utf-8", errors="replace", timeout=60, env=dict(os.environ, PYTHONUTF8="1"))
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode:
            continue
        for ln in (r.stdout or "").splitlines():
            s = ln.strip()
            if s and not re.fullmatch(r"\d+\s*件", s) and s not in out:
                out.append(s)
    return [f"- {TODO_FILE} （{rel}） {s[:LEAD_CHARS]}" for s in out[:max(0, LEADS_PER_PROJECT)]]


def leads_text(files=None):
    if not LEADS_ON:
        return ""
    out = []
    for d in lead_projects(files):
        got = decision_leads(d) + todo_leads(d)
        if got:
            out.append(f"### {d}\n" + "\n".join(got))
    return "\n".join(out)


def sources_hash():
    h = hashlib.sha1()
    files = source_files()
    for f in files:
        try:
            h.update(f"{f}:{os.path.getmtime(f)}".encode())
        except OSError:
            pass
    h.update(leads_text(files).encode("utf-8", "replace"))  # 手がかりが増えたら仕分け直す（DECISIONS.md の更新時刻では見ない＝関係ない追記で仕分け直さない）
    return h.hexdigest()


# ---------------------------------------------------------------- 今なにをしているか（審判官が読む）

_hb = {"phase": "起動", "ticket": None, "session_id": None, "since": None}
_hb_written = 0.0


def heartbeat(force=False, **kw):
    global _hb_written
    if IN_GATE:
        return
    _hb.update(kw)
    if not force and time.time() - _hb_written < 30:
        return
    _hb_written = time.time()
    try:
        write_json(HEARTBEAT, dict(_hb, at=now().isoformat(timespec="seconds"), pid=os.getpid()))
    except OSError:
        pass


# ---------------------------------------------------------------- セッションの実行

PROJECTS = os.path.expanduser(r"~\.claude\projects")


def transcript_path(cwd, sid):
    """会話記録の場所。フォルダ名は作業ディレクトリの英数字以外を '-' にしたもの（実物で確認）。
    日本語を含むパスでは外れうるので、終わった後に実物を探し直す（find_transcript）。"""
    return os.path.join(PROJECTS, re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(cwd)), sid + ".jsonl")


def find_transcript(sid):
    hits = glob.glob(os.path.join(PROJECTS, "*", sid + ".jsonl"))
    return hits[0] if hits else None


def start_claude(prompt, cwd, effort, cap_usd, extra_env, label, add_dirs=()):
    """claude -p を起動して、終わるのを待たずに返す。出力はファイルに落とす（パイプだと待ちの間に詰まる）。"""
    # セッションIDをこちらで決めて渡す。動いている最中に黒いウィンドウから会話記録を開けるようにするため
    sid = str(uuid.uuid4())
    guess = transcript_path(cwd, sid)
    log(f"  {label}セッション {sid}")
    log(f"  会話記録 {guess}")
    env = dict(os.environ)
    env.update(NIGHT_RUNNER="1", NIGHT_CTX_LIMIT=CTX_LIMIT, NIGHT_HOME=HOME)
    env.update(extra_env)
    env.pop("ANTHROPIC_API_KEY", None)  # 従量課金に落ちないよう、Max の OAuth で動かす
    # stream-json にすると、途中の出力に利用枠の使用率（rate_limit_event）が流れる。最後の1行は json のときと同じ結果の行（[W-002]）
    cmd = ([sys.executable, CLAUDE] if CLAUDE.endswith(".py") else [CLAUDE]) + ["-p", "--session-id", sid, "--output-format", "stream-json", "--verbose", "--effort", effort,
           "--permission-mode", PERMISSION_MODE, "--settings", SETTINGS,
           "--add-dir", HOME, "--max-budget-usd", f"{cap_usd:.2f}"]
    if MODEL:
        cmd += ["--model", MODEL]
    for d in add_dirs:
        cmd += ["--add-dir", d]
    if PERMISSION_MODE != "bypassPermissions":
        cmd += ["--permission-prompts", "none"]  # auto などのとき、確認が要る操作は自動で拒否する
    os.makedirs(SESSIONS, exist_ok=True)
    base = os.path.join(SESSIONS, sid)
    with open(base + ".prompt.md", "w", encoding="utf-8") as f:
        f.write(prompt)
    fin = open(base + ".prompt.md", "rb")
    fout = open(base + ".out", "wb")
    ferr = open(base + ".err", "wb")
    p = subprocess.Popen(cmd, stdin=fin, stdout=fout, stderr=ferr, cwd=cwd, env=env, creationflags=BELOW_NORMAL)
    h = {"p": p, "sid": sid, "guess": guess, "t0": time.time(), "base": base, "files": (fin, fout, ferr), "cap": cap_usd, "uoff": 0}
    _usage["live"].append(h)
    return h


_limit = {"hit": False}  # どれかのセッション（作業・仕分け・評価役・解消役・審判官）が利用上限に当たった（②21）


TAIL_BYTES = 4 * 1024 * 1024


def read_tail(path, limit=TAIL_BYTES):
    """ファイルの末尾 limit バイトを文字列で返す（(文字列, ファイル全体のバイト数)）。数十MBの出力を丸ごとメモリに載せない。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - limit))
            return f.read().decode("utf-8", "replace"), size
    except OSError:
        return "", 0


def result_line(out):
    """セッションの出力から結果の行（type=result）を探す。無ければ None。
    stream-json では途中の行も JSON なので、止めたセッションの最後の行（途中の発言）を結果と取り違えない。"""
    for line in reversed(out.split("\n")):  # splitlines() は本文の中の U+2028 などでも割ってしまう
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and (d.get("type") == "result" or ("type" not in d and "result" in d)):
            return d
    return None


def finish_claude(h):
    for f in h["files"]:
        try:
            f.close()
        except OSError:
            pass
    usage_feed(h)  # 最後まで読み切る（終わり際に流れた使用率を落とさない）
    if h in _usage["live"]:
        _usage["live"].remove(h)
    (out, out_size), err = read_tail(h["base"] + ".out"), read_tail(h["base"] + ".err", 64 * 1024)[0]
    res = result_line(out)
    ended = h["p"].poll() is not None  # 止め損ねたプロセスがまだ書いているファイルは、縮めない
    if res is not None and ended and out_size > 256 * 1024:
        # stream-json の .out は1本で数MBになる（json のときは1行だった）。結果の行だけ残す。途中のやりとりは会話記録（~/.claude/projects）にある
        try:
            with open(h["base"] + ".out", "w", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(res, ensure_ascii=False) + "\n")
        except OSError:
            pass
    if res is None and ended and out_size > 256 * 1024:
        # 止めた・落ちたセッションの .out は末尾 64KB だけ残す（何をしていたかの手がかり。全部は会話記録にある）
        try:
            with open(h["base"] + ".out", "w", encoding="utf-8", newline="\n") as f:
                f.write("（先頭を切った。全体は会話記録 " + h["guess"] + "）\n" + out[-64 * 1024:])
        except OSError:
            pass
    if res is None:
        # 結果の JSON が無い（時間切れで止めた・落ちた）ときは、使った額が分からない。渡した上限を使い切ったものとして
        # 控えめに多く数える（$0 と数えると、固まる票を繰り返しても一晩の上限が効かない＝②1）
        # stream-json の途中の行（発言・道具の結果）は入れない。入れると、その中の「上限」「overloaded」を利用上限・混雑と取り違える
        plain = "\n".join(l for l in out.split("\n") if l.strip() and not l.lstrip().startswith("{"))
        res = {"is_error": True, "result": (err or plain)[-800:] or f"終了コード {h['p'].returncode}",
               "total_cost_usd": float(h.get("cap") or 0), "_cost_estimated": True}
    res["_sec"] = round(time.time() - h["t0"])
    if hit_usage_limit(res):
        _limit["hit"] = True
        usage_keep("result", {k: v for k, v in res.items() if k not in ("usage", "modelUsage", "permission_denials")}, h.get("sid"))
    elif not res.get("is_error"):
        _usage["strikes"] = 0
    res.setdefault("session_id", h["sid"])
    real = find_transcript(h["sid"])
    if real and os.path.normcase(real) != os.path.normcase(h["guess"]):
        log(f"  会話記録（実際の場所） {real}")
    n = release_claims(res.get("session_id"))
    if n:
        log(f"  このセッションが持っていたリース {n} 件を放した")
    return res


def run_claude(prompt, cwd, effort, cap_usd, extra_env, label="", deadline=None):
    """起動して終わるまで待つ。待っている間も審判官の時刻を見る。
    deadline（時刻）を過ぎたら子プロセスごと止め、res["_timeout"]=True で返す（1本が固まってもブラウニー全体は止まらない）。"""
    h = start_claude(prompt, cwd, effort, cap_usd, extra_env, label)
    heartbeat(force=True, session_id=h["sid"], since=now().isoformat(timespec="seconds"))
    end = deadline if deadline is not None else h["t0"] + SESSION_TIMEOUT_SEC
    if session_end_cap() is not None:
        end = min(end, session_end_cap())
    if not wait_proc(h["p"], end):
        res = finish_claude(h)
        res.update(is_error=True, _timeout=True,
                   result=f"{label or 'セッション'}が {dur(time.time() - h['t0'])} で終わらなかったので、子プロセスごと止めた（時間切れ）")
        log(f"  ⏱ {res['result']}")
        return res
    return finish_claude(h)


def release_claims(session_id):
    """終わったセッションが持っていた統治文書のリース（claim_gate の .claims）を放す。
    ブラウニーのセッションは使い捨てなので、終わった時点でリースに意味が無い。放さないと60分残り、
    同じプロジェクトの次の作業票が DECISIONS.md などに書けず continue を返し続ける（2026-09-24 砂場で実測）。
    消すのはこのセッションの行だけ。人のセッションの行には触らない。"""
    sid = (session_id or "")[:8]
    if not sid:
        return 0
    released = 0
    for depth in range(5):
        for path in glob.glob(os.path.join(EVAR, *(["*"] * depth), ".claims")):
            try:
                with open(path, encoding="utf-8-sig") as f:
                    lines = f.read().splitlines()
            except OSError:
                continue
            keep = [ln for ln in lines if not (not ln.startswith("#") and len(ln.split("\t")) >= 2
                                               and ln.split("\t")[1] == sid)]
            if len(keep) != len(lines):
                released += len(lines) - len(keep)
                try:
                    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
                        f.write("\n".join(keep) + "\n")
                except OSError:
                    pass
    return released


def drop_empty_claims(wt):
    """作業コピーの中の、リースの行が1つも残っていない .claims（見出しのコメント行と空行だけ）を消す。消した件数を返す。
    作業票のセッションが終わると release_claims が行を抜くので、見出しだけの .claims が作業コピーに残る。
    無視設定なので、wt.py finish と stash_untracked が 排除/ へ運び、合流のたびに 排除/worktree_*/.claims が溜まっていた
    （[E-045追記] の判断待ち①・案B。wt.py を直す案Cは根の外なのでブラウニーは触らない）。
    行が残っている .claims（まだ効いているリース）には触らない。作業コピーの外にも触らない。"""
    if not wt or not os.path.isdir(wt):
        return 0
    dropped = 0
    for depth in range(5):
        for path in glob.glob(os.path.join(wt, *(["*"] * depth), ".claims")):
            if not inside_dir(os.path.realpath(path), os.path.realpath(wt)) or not os.path.isfile(path):  # リンクの先は作業コピーの外
                continue
            try:
                with open(path, encoding="utf-8-sig") as f:
                    lines = f.read().splitlines()
                if any(ln.strip() and not ln.lstrip().startswith("#") for ln in lines):
                    continue
                os.remove(path)
                dropped += 1
            except (OSError, UnicodeDecodeError):
                continue
    if dropped:
        log(f"  作業コピーに残った空の .claims {dropped} 件を片付けた（排除/ へ運ばせない）")
    return dropped


def hit_usage_limit(res):
    if not res.get("is_error"):
        return False
    text = str(res.get("result") or "")
    return res.get("api_error_status") == 429 or bool(
        re.search(r"usage limit|rate limit|hit your limit|limit (will )?reset|上限", text, re.I))


def overloaded(res):
    return res.get("api_error_status") in (500, 502, 503, 529) or "overloaded" in str(res.get("result") or "").lower()


# ---------------------------------------------------------------- 利用枠（5時間枠・7日枠）の見張り（[W-002]）
# 2026-10-01 までは、上限に当たると夜を終えていた（03:36 に1票が打ち切られ、そのまま終了）。
# 今は、しきい値で新しいセッションを始めるのをやめ、枠が戻る時刻まで待ってから続ける。走っているセッションは止めない。

_usage = {"live": [], "tick": 0.0, "warned": set(), "hold_key": None, "retry_at": 0.0, "known": None,
          "strikes": 0, "counted": False, "probe_fail": 0, "probed_at": 0.0}


def usage_state():
    st = read_json(USAGE_FILE, {})
    return st if isinstance(st, dict) else {}


def usage_keep(kind, obj, sid=None):
    """上限・警告の実物を1行残す（どんな形で来たかを、後で fake_claude の台本と突き合わせるため）。失敗しても何もしない。"""
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(USAGE_EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": now().isoformat(timespec="seconds"), "kind": kind, "session_id": sid, "data": obj}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def usage_feed(h):
    """走っている（終わった直後の）セッションの出力から、新しく流れた使用率を記録に重ねる。
    見張りのための読み書きなので、ここの失敗でセッションの待ちや結果の取り込みを落とさない。"""
    before = h.get("uoff", 0)
    try:
        info, h["uoff"] = usage_window.scan(h["base"] + ".out", before)
        if info is None:
            return
        if info.get("status") not in (None, "allowed"):
            usage_keep("event", info, h.get("sid"))
        st = usage_window.merge(usage_state(), info, time.time(), h.get("sid"))
        os.makedirs(STATE, exist_ok=True)
        tmp = f"{USAGE_FILE}.{os.getpid()}.tmp"  # 関門の中の評価役（別プロセス）と一時ファイルを取り合わない
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, USAGE_FILE)
        usage_warn(st)
    except OSError:
        h["uoff"] = before  # 書けなかった通知は、次の回に読み直す
    except Exception:
        if not _usage.get("broken"):
            _usage["broken"] = True
            log("利用枠の読み取りで例外（以後は黙って続ける）:\n" + traceback.format_exc())


def usage_tick(force=False):
    """待っている間に呼ぶ。15秒に1回、走っているセッションの出力の続きだけを読む（トークン0）。"""
    if not force and time.time() - _usage["tick"] < 15:
        return
    _usage["tick"] = time.time()
    for h in list(_usage["live"]):
        usage_feed(h)


def usage_warn(st):
    """しきい値をまたいだら1回だけ知らせる（枠が戻るたびに、また1回）。関門の中（評価役）では知らせない。"""
    if IN_GATE:
        return
    tnow = time.time()
    for k, v in usage_window.live(st, tnow).items():
        p = v["utilization"] * 100
        hold = usage_window.threshold(k, USAGE_HOLD_PCT, USAGE_WEEK_HOLD_PCT)
        back = usage_window.clock(v["resetsAt"], tnow)
        name = usage_window.ja(k)
        if p >= hold:
            if (k, v["resetsAt"], "hold") in _usage["warned"]:
                continue
            _usage["warned"] |= {(k, v["resetsAt"], "hold"), (k, v["resetsAt"], "warn")}
            after = f"今のセッションが終わったら、{back} まで待ってから続ける" if USAGE_WAIT else "今のセッションが終わったら夜を終える"
            log(f"⚠ {name} {p:.0f}%（しきい値 {hold:g}%）。新しいセッションは始めない。{after}")
            toast(f"{name}が {p:.0f}% です", f"ブラウニーは新しい票を始めません。{after}")
        elif k == "five_hour" and 0 < USAGE_WARN_PCT <= p and (k, v["resetsAt"], "warn") not in _usage["warned"]:
            _usage["warned"].add((k, v["resetsAt"], "warn"))
            log(f"⚠ {name} {p:.0f}%（{back} に戻る）。{hold:g}% で新しいセッションを始めるのをやめる")
            toast(f"{name}が {p:.0f}% です", f"{back} に戻ります。{hold:g}% でブラウニーは新しい票を始めるのをやめます")


def usage_over(follow=False):
    """いま新しいセッションを始めない状態か（記録を見るだけ・トークン0）。{"window","pct","until"} か None。
    follow=True は票の途中のセッション用で、しきい値が高い（NIGHT_USAGE_FOLLOW_PCT）。"""
    try:
        hold = max(USAGE_HOLD_PCT, USAGE_FOLLOW_PCT) if follow else USAGE_HOLD_PCT
        week = max(USAGE_WEEK_HOLD_PCT, USAGE_FOLLOW_PCT) if follow else USAGE_WEEK_HOLD_PCT
        # 戻る時刻から USAGE_MARGIN_SEC の間は、まだ戻っていないものとして扱う（ちょうどの時刻に始めると、断られることがある）
        return usage_window.verdict(usage_state(), time.time() - USAGE_MARGIN_SEC, hold, week)
    except Exception:
        return None


def usage_probe():
    """小さな1往復（道具なし・設定なし・会話記録なし。Haiku での実測 $0.0014・約3秒）で、今の使用率を読み直す。
    True＝通った／False＝上限で断られた／None＝読み直せなかった（起動できない・時間切れ・ほかのエラー）。"""
    sid = str(uuid.uuid4())
    os.makedirs(SESSIONS, exist_ok=True)
    base = os.path.join(SESSIONS, sid)
    cmd = ([sys.executable, CLAUDE] if CLAUDE.endswith(".py") else [CLAUDE]) + [
        "-p", "--session-id", sid, "--output-format", "stream-json", "--verbose", "--effort", "low",
        "--max-budget-usd", "0.20", "--no-session-persistence", "--safe-mode", "--strict-mcp-config", "--tools", "", "--system-prompt", "ok とだけ答える"]
    if USAGE_PROBE_MODEL or MODEL:
        cmd += ["--model", USAGE_PROBE_MODEL or MODEL]
    env = dict(os.environ, NIGHT_KIND="probe")
    env.pop("ANTHROPIC_API_KEY", None)
    with open(base + ".prompt.md", "w", encoding="utf-8") as f:
        f.write("ok")
    t0 = time.time()
    try:
        with open(base + ".prompt.md", "rb") as fin, open(base + ".out", "wb") as fout, open(base + ".err", "wb") as ferr:
            p = subprocess.Popen(cmd, stdin=fin, stdout=fout, stderr=ferr, cwd=STATE, env=env, creationflags=BELOW_NORMAL)
            limit_sec = 20 if IN_GATE else 60  # 関門の中では、関門の持ち時間の余裕（60秒）を食わないように短く切る
            while p.poll() is None and time.time() - t0 < limit_sec and not (os.path.exists(STOP) and not IN_GATE):
                heartbeat()
                time.sleep(0.2)
            if p.poll() is None:
                kill_tree(p)
                log(f"  使用率の読み直しが{limit_sec}秒で終わらなかった（か「ブラウニーを止める」が押された）ので止めた")
                ledger_add({"at": now().isoformat(timespec="seconds"), "night": night_date(), "kind": "probe", "session_id": sid,
                            "cost": 0.20, "cost_estimated": True, "sec": round(time.time() - t0), "is_error": True, "result": "時間切れ"})
                return None
    except OSError as ex:
        log(f"  使用率の読み直しを起動できなかった: {ex}")
        return None
    usage_feed({"base": base, "sid": sid, "uoff": 0})
    res = result_line(read_text(base + ".out")) or {"is_error": True, "result": read_text(base + ".err")[-300:]}
    ledger_add({"at": now().isoformat(timespec="seconds"), "night": night_date(), "kind": "probe", "session_id": sid,
                "cost": float(res.get("total_cost_usd") or 0), "sec": round(time.time() - t0), "is_error": res.get("is_error"),
                "result": str(res.get("result"))[:120]})
    if hit_usage_limit(res):
        usage_keep("result", res, sid)
        return False
    if res.get("is_error"):
        return None
    for ext in (".prompt.md", ".out", ".err"):  # 通った読み直しのファイルは残さない（結果は台帳にある）
        try:
            os.remove(base + ext)
        except OSError:
            pass
    return True


def usage_refresh(max_age=None):
    """使用率の記録が max_age 秒（既定 NIGHT_USAGE_PROBE_SEC）より古ければ読み直す。読み直したらその結果（usage_probe と同じ）、
    読み直さなかったら None。前に撃ってから max_age 秒たっていなければ撃たない（通知が流れず記録が更新されなくても、続けて撃たない）。"""
    max_age = USAGE_PROBE_SEC if max_age is None else max_age
    st = usage_state()
    if USAGE_PROBE_SEC <= 0 or not st.get("at"):
        return None  # 記録が1度も無いうちは撃たない（最初のセッションが記録を作る）
    try:
        last = max(float(st["at"]), _usage["probed_at"])
    except (TypeError, ValueError):
        last = _usage["probed_at"]
    if time.time() - last <= max_age:
        return None
    _usage["probed_at"] = time.time()
    return usage_probe()


def usage_blocked(follow=False):
    """いまセッションを始めてはいけないか（待たずに答える）。記録が古ければ先に読み直す。
    審判官（follow=False）と、票の途中で起こすセッション＝評価役・解消役・合流の再試行（follow=True）が、始める直前に使う。
    follow=True は高いしきい値で見る: 85% で走り終えた票を、評価役1本のために数時間待たせない。"""
    try:
        if not _limit["hit"] and not usage_over(follow) and usage_refresh() is False:
            _limit["hit"] = True
            _usage["retry_at"] = time.time() + usage_backoff()
        return bool(_limit["hit"] or usage_over(follow))
    except Exception:
        return False


def usage_backoff():
    """上限に当たった後、戻る時刻が分からないときに待つ秒数。成功を挟まずに続けて当たるほど延ばす（10分→20分→40分→60分）。"""
    return min(max(3600, USAGE_RETRY_SEC), USAGE_RETRY_SEC * 2 ** max(0, _usage["strikes"] - 1))


def usage_clear():
    _limit["hit"] = False
    _usage.update(known=None, counted=False, retry_at=0.0, probe_fail=0)


def usage_gate(refresh=False):
    """新しいセッションを始める前の関門。始めてよければ None、待った（step をやり直す）なら "waited"、夜を終えるならその理由。
    refresh=True のときは、使用率の記録が古ければ先に読み直す（待機の後・エヴァが別のセッションで使った後に、古い数字で始めない）。
    NIGHT_USAGE_WAIT=1（既定）では、利用枠を理由に夜を終えない。"""
    tnow = time.time()
    st = usage_state()
    v = usage_over()
    unknown = lambda: {"window": None, "pct": 100, "until": _usage["retry_at"]}
    if _limit["hit"] and not USAGE_WAIT:
        return "利用上限に当たった"
    if _limit["hit"]:
        if not _usage["counted"]:
            # この当たりを数える。前の再開から成功を挟まずにまた当たったら（読み直しは通るのに本番は断られる等）、先に待つ
            _usage["counted"] = True
            _usage["strikes"] += 1
            if _usage["strikes"] >= 2:
                _usage["retry_at"] = tnow + usage_backoff()
        if v is not None:
            _usage["known"] = v["until"]  # 断られた枠の戻る時刻が分かっている。その時刻（＋余裕）を過ぎたら、そのまま再開する
        elif _usage["known"] and tnow >= _usage["known"] + USAGE_MARGIN_SEC:
            usage_clear()
        elif tnow < _usage["retry_at"]:
            v = unknown()
        else:
            # 戻る時刻が分からない（通知が流れなかった・記録が読めない）: 読み直して確かめる
            ok = usage_probe() if USAGE_PROBE_SEC > 0 else True
            v = usage_over()
            if ok is None:
                _usage["probe_fail"] += 1
            if v is None and (ok or _usage["probe_fail"] >= 3):
                usage_clear()  # 通った（読み直しが3回続けてできないときは、本番のセッションで確かめる）
            elif v is None:
                _usage["retry_at"] = time.time() + usage_backoff()
                v = unknown()
    elif v is not None and v["window"] and usage_refresh(max(3600, USAGE_PROBE_SEC)) is not None:
        v = usage_over()  # 長い待ち（7日枠は数日）の間は1時間に1回読み直す。記録が間違っていた・枠が早く戻ったら、待ちをやめる
    elif v is None and refresh:
        if usage_refresh() is False:
            _limit["hit"] = True
            _usage["retry_at"] = time.time() + usage_backoff()
            return "waited"  # 次の step が上の道（上限に当たった）で待つ
        v = usage_over()
    if v is None:
        if _usage["hold_key"] is not None:
            label = usage_window.label(usage_state(), time.time())
            log("利用枠が戻ったので続ける" + (f"（{label}）" if label else ""))
            _usage["hold_key"] = None
        return None
    name = usage_window.ja(v["window"]) if v["window"] else "利用枠"
    what = f"{name} {v['pct']}%" if v["window"] else "利用上限に当たった（戻る時刻は分からない）"
    if not USAGE_WAIT:
        return f"{what} に達した（NIGHT_USAGE_WAIT=0 なので、待たずに終える）"
    wake = v["until"] + (USAGE_MARGIN_SEC if v["window"] else 0)
    back = usage_window.clock(wake)
    key = (v["window"], v["until"] if v["window"] else None)
    if _usage["hold_key"] != key:
        _usage["hold_key"] = key
        tail = "まで新しいセッションを始めずに待つ" if v["window"] else "にもう一度確かめる（以後は間隔を延ばしながら確かめる）"
        log(f"⏸ {what}。{back} {tail}（待機中はトークン0。止めるなら「ブラウニーを止める」）")
        toast("ブラウニーは利用枠が戻るのを待っています", f"{what}。{back} に" + ("再開します" if v["window"] else "もう一度確かめます"))
        ledger_add({"at": now().isoformat(timespec="seconds"), "night": night_date(), "kind": "usage_hold",
                    "window": v["window"], "pct": v["pct"], "until": datetime.datetime.fromtimestamp(wake).isoformat(timespec="seconds")})
    heartbeat(force=True, phase=f"待機（{what}・{back} に再開）", ticket=None, session_id=None)
    sleep_watch(max(1, min(IDLE_POLL_SEC, wake - time.time())))
    return "waited"


# ---------------------------------------------------------------- 審判官（作業と並行で走る）

_judge = {"h": None, "next_at": 0.0, "no": 0, "started_at": None, "md": None, "json": None, "seen": 0}


def judge_progress_count():
    """今夜の回（RUN_ID）の work・finish の行の数（審判官を起こすかの判断に使う）。"""
    return sum(1 for r in ledger_rows() if r.get("kind") in ("work", "finish") and (not RUN_ID or r.get("run") == RUN_ID))


def judge_has_pending():
    d = read_json(TONIGHT, None) or {}
    return any(t.get("status") == "pending" for t in d.get("tickets", []))


def judge_worth_waking():
    """前回の審判官を起こした後に作業が進んだ（work・finish の行が増えた）か、pending の票がある（停滞も見る）ときだけ起こす。
    待機中に同じ状態を何度も採点させない（2026-09-26 は起動16秒後に起きて全項目 n/a だった）。"""
    return judge_progress_count() > _judge["seen"] or judge_has_pending()


def judge_tick(final=False):
    """2時間ごと（作業が進んだか pending があるときだけ）に審判官を起こし、終わっていたら結果を取り込む。作業セッションの待ちの間からも呼ばれる。"""
    j = _judge
    if IN_GATE:
        return  # 関門（finish_gate.py）の中では審判官を起こさない。審判官は司令塔の1本だけが持つ
    if j["h"] is not None:
        if j["h"]["p"].poll() is None:
            if time.time() - j["h"]["t0"] <= JUDGE_TIMEOUT_SEC:
                return
            log(f"⚖ 審判官 {j['no']}回目が {dur(JUDGE_TIMEOUT_SEC)} で終わらなかったので、子プロセスごと止める（②5）")
            kill_tree(j["h"]["p"])
        judge_collect()
    if final or JUDGE_INTERVAL_SEC <= 0 or time.time() < j["next_at"] or os.path.exists(STOP) or night_over() or _limit["hit"]:
        return
    if usage_over():
        return  # 利用枠がしきい値以上の間は起こさない（[W-002]）
    if remaining_budget() < JUDGE_CAP_USD:
        return  # 審判官の上限ぶんが残っていなければ起こさない（②11）
    if not judge_worth_waking():
        return  # 前回から何も進んでおらず pending も無い＝採点する材料が無い（空回りさせない）
    if usage_blocked():
        return  # 起こす直前に、利用枠を読み直して確かめる（古い記録のまま始めない）
    j["seen"] = judge_progress_count()
    j["next_at"] = time.time() + JUDGE_INTERVAL_SEC
    j["no"] += 1
    j["started_at"] = now()
    os.makedirs(JUDGE_DIR, exist_ok=True)
    stamp = f"{night_date()}_{now():%H%M}"
    j["md"] = os.path.join(JUDGE_DIR, f"{stamp}.md")
    j["json"] = os.path.join(JUDGE_DIR, f"{stamp}.json")
    last = sorted(glob.glob(os.path.join(JUDGE_DIR, f"{night_date()}_*.md")))
    tpl = read_text(os.path.join(PROMPTS, "judge.md"))
    rep = {"{CRITERIA}": CRITERIA, "{NIGHT}": night_date(), "{JUDGE_NO}": str(j["no"]), "{ROOT}": ROOT,
           "{EVAR}": EVAR, "{RUNLOG}": RUNLOG, "{LEDGER}": LEDGER, "{TONIGHT}": TONIGHT, "{HEARTBEAT}": HEARTBEAT,
           "{PROGRESS_DIR}": os.path.join(STATE, "progress", night_date()),
           "{REPORT}": os.path.join(REPORTS, f"{night_date()}.md"), "{LOCK}": LOCK, "{SETTINGS}": SETTINGS,
           "{PREV_JUDGE}": last[-1] if last else "（今夜はまだ無い）", "{OUT_MD}": j["md"], "{OUT_JSON}": j["json"]}
    prompt = tpl
    for k, v in rep.items():
        prompt = prompt.replace(k, v)
    log(f"⚖ 審判官 {j['no']}回目を起動（作業と並行。次は {JUDGE_INTERVAL_SEC // 60}分後）")
    j["h"] = start_claude(prompt, HOME, "medium", JUDGE_CAP_USD,
                          {"NIGHT_PROGRESS": j["json"], "NIGHT_KIND": "judge", "NIGHT_CTX_LIMIT": "10000000"},
                          "審判官", add_dirs=(EVAR,))


def judge_collect():
    j = _judge
    res = finish_claude(j["h"])
    j["h"] = None
    verdict = read_json(j["json"], None)
    cost = float(res.get("total_cost_usd") or 0)
    items = (verdict or {}).get("items", [])
    summary = " ".join(f"{it.get('id')}={it.get('verdict')}" for it in items) or "判定ファイルが無い"
    ledger_add({"at": now().isoformat(timespec="seconds"), "night": night_date(), "kind": "judge",
                "judge_no": j["no"], "session_id": res.get("session_id"), "cost": cost, "sec": res.get("_sec"),
                "is_error": res.get("is_error") or verdict is None, "verdict": summary, "report": j["md"],
                "result": str(res.get("result"))[:300]})
    log(f"⚖ 審判官 {j['no']}回目: {summary}  ${cost:.2f}  {res.get('_sec')}秒  → {j['md']}")
    if not verdict:
        return
    if production():
        add_judge_fails_to_pending(verdict, j["md"])
    fb = (verdict.get("feedback") or "").strip()
    if fb:
        with open(FEEDBACK, "w", encoding="utf-8") as f:
            f.write(f"（審判官 {j['no']}回目・{now():%m/%d %H:%M}。詳細 {j['md']}）\n\n{fb}\n")
    apply_reopen(verdict)


def apply_reopen(verdict):
    """虚偽・根拠なしで差し戻された作業票は、1回だけ作り直させる（同じ票で差し戻しを繰り返さない）。
    時間切れの票・合流済みの票・合流を待っている票・作業コピーを作れなかった票は作り直さない（②9）。持ち時間は通算のまま（②10）。"""
    reopen = {r.get("ticket"): r.get("reason", "") for r in verdict.get("reopen", []) if r.get("ticket")}
    if not reopen:
        return 0
    d = read_json(TONIGHT, {"tickets": []})
    n = 0
    for t in d.get("tickets", []):
        if t.get("id") not in reopen or t.get("status") == "pending" or t.get("judge_reopened"):
            continue
        state = (t.get("merge") or {}).get("state")
        if t.get("status") == "timeout" or state in ("merged", "busy") or t.get("no_revive"):
            log(f"⚖ 差し戻しを見送った: {t['id']}（{t.get('status')}・{state}）— {reopen[t['id']][:120]}")
            continue
        t.update(status="pending", judge_reopened=True, judge_note=reopen[t["id"]])
        t["sessions"] = min(t.get("sessions", 0), MAX_SESSIONS_PER_TICKET - 1)
        for k in ("outcome", "merge"):  # 作り直すので、行き先（できた／保留／失敗）も決め直す
            t.pop(k, None)
        n += 1
        log(f"⚖ 差し戻し: {t['id']} — {reopen[t['id']][:120]}")
    write_json(TONIGHT, d)
    return n


# ---------------------------------------------------------------- 仕分け

def triage():
    # 絶対パスで渡す。相対パスだと、砂場で動かしたとき仕分け役が親の CLAUDE.md を辿って本番のプロジェクトを読んだ（2026-09-25 Haiku で実測）
    files = source_files()
    pend = [f for f in files if f.endswith("PENDING.md")]
    seen = {}
    for r in ledger_rows():
        if r.get("ticket"):
            seen[r["ticket"]] = f"- {r['ticket']}: {r.get('title','')}（出典 {r.get('source','')}）→ {r.get('status','')}"
    tpl = read_text(os.path.join(PROMPTS, "triage.md"))
    leads = leads_text(files)
    prompt = (tpl.replace("{TONIGHT}", TONIGHT).replace("{INBOX}", INBOX).replace("{EVAR}", EVAR)
                 .replace("{PENDING_LIST}", "、".join(pend))
                 .replace("{LEADS}", leads or "（無い）")
                 .replace("{LEDGER}", "\n".join(list(seen.values())[-200:]) or "（まだ無い）"))
    sel = selected_projects()
    if sel:
        prompt += ("\n\n## 今夜の対象プロジェクト（エヴァが projects.txt で選んだ）\n"
                   "次の配下だけから作業票を作る（HANDOFF も含む）。inbox に書かれたものはこの限りでない。\n"
                   + "\n".join(f"- {d}" for d in sel))
    moved = os.path.exists(TONIGHT)
    if moved:
        os.replace(TONIGHT, TONIGHT + ".prev")
    log(f"仕分けセッションを起動（PENDING / HANDOFF §0 / inbox を読む。機械が拾った手がかり {sum(l.startswith('- ') for l in leads.splitlines())} 行）")
    heartbeat(force=True, phase="仕分け", ticket=None)
    prev = read_json(TONIGHT + ".prev", {}) if os.path.exists(TONIGHT + ".prev") else {}
    res = run_claude(prompt, EVAR, "medium", max(0.5, min(8.0, remaining_budget())), {"NIGHT_PROGRESS": TONIGHT, "NIGHT_KIND": "triage"}, "仕分け",
                     deadline=time.time() + TRIAGE_TIMEOUT_SEC)
    ledger_add({"at": now().isoformat(timespec="seconds"), "night": night_date(), "kind": "triage",
                "session_id": res.get("session_id"), "cost": res.get("total_cost_usd", 0), "sec": res.get("_sec"),
                "is_error": res.get("is_error"), "result": str(res.get("result"))[:300]})
    data = read_json(TONIGHT, None)
    if data is None:
        log(f"仕分けが作業票を書けなかった: {str(res.get('result'))[:200]}")
        if moved and prev.get("night") == night_date():
            # この呼び出しで退避した今夜の票を戻す。戻さないと tonight.json が無いままになり、合流待ちの票の再試行も夜の締めも票を見失う。
            # 前の夜の票・いつのものか分からない .prev は戻さない（戻すと、step が今夜の名前を付けて今夜の票として動かしてしまう）
            try:
                shutil.copyfile(TONIGHT + ".prev", TONIGHT)
                log("  前の作業票（tonight.json.prev）を戻した")
            except OSError as ex:
                log(f"  前の作業票を戻せなかった: {ex}")
        return res, []
    # 走査の根の外を指す作業票・見送りは機械的に捨てる（指示だけでは破られる。砂場の試験が本番を書き換えないための本命の歯止め）
    def inside(p):
        try:
            return bool(p) and os.path.commonpath([os.path.normcase(os.path.abspath(p)), os.path.normcase(EVAR)]) == os.path.normcase(EVAR)
        except ValueError:
            return False
    raw = data.get("tickets", [])
    tickets = [t for t in raw if inside(t.get("project_dir"))]
    for t in raw:
        if t not in tickets:
            log(f"  走査の根 {EVAR} の外を指す作業票を捨てた: {t.get('id')}（{t.get('project_dir')}）")
    # projects.txt で外したプロジェクトの作業票は捨てる。inbox に本人が書いたものは選択より優先する
    kept = []
    for t in tickets:
        if project_selected(t.get("project_dir")) or "inbox" in str(t.get("source", "")).lower():
            kept.append(t)
        else:
            data.setdefault("skipped", []).append({"source": t.get("source"), "title": t.get("title"),
                                                   "reason": "projects.txt で今夜の対象から外したプロジェクト"})
            log(f"  選択外のプロジェクトの作業票を見送りへ回した: {t.get('id')}（{t.get('project_dir')}）")
    tickets = kept
    def inside_any(src):
        p = re.sub(r":[\d-]+$", "", str(src or ""))
        return inside(p) or (bool(p) and os.path.normcase(os.path.abspath(p)).startswith(os.path.normcase(os.path.abspath(HOME))))
    data["skipped"] = [s for s in data.get("skipped", []) if inside_any(s.get("source"))]
    for t in tickets:
        t.setdefault("status", "pending")
        t.setdefault("sessions", 0)
    # 同じ夜の再仕分けで、終わった作業票と見送りを引き継ぐ。上書きすると朝の報告から消える（2026-09-24 砂場で実測）
    # 同じ ID の票を仕分けがもう一度出しても、今夜はやり直さない（前の票を残す）。上書きすると、合流待ち（本体使用中）や
    # 保留の票が行き先の決まらないまま消える。やり直しは revive_leftovers と審判官の差し戻しが受け持つ
    if prev.get("night") == night_date():
        old = [x for x in prev.get("tickets", []) if x.get("status") != "pending"]
        old_ids = {x.get("id") for x in old}
        for t in tickets:
            if t.get("id") in old_ids:
                log(f"  今夜すでに扱った作業票 {t.get('id')} を仕分けがもう一度出したので、やり直さない")
        tickets = old + [t for t in tickets if t.get("id") not in old_ids]
        srcs = {s.get("source") for s in data.get("skipped", [])}
        data["skipped"] = [s for s in prev.get("skipped", []) if s.get("source") not in srcs] + data.get("skipped", [])
    data["tickets"] = tickets
    write_json(TONIGHT, data)
    new = [t for t in tickets if t.get("status") == "pending"]
    log(f"新しい作業票 {len(new)} 枚（run {sum(t.get('kind')=='run' for t in new)} / "
        f"decide {sum(t.get('kind')=='decide' for t in new)}）、今夜の済み {len(tickets) - len(new)} 枚、見送り {len(data.get('skipped', []))} 件")
    return res, tickets


# ---------------------------------------------------------------- ブランチ（作業票ごとに作業コピーを分け、終わったら wt.py finish で本流へ）
# 2026-09-25 エヴァの判断「ブランチ＋自動マージ」。作業票ごとに git worktree とブランチ night/<夜>/<票> を作る。
# done / needs_decision で終わったら、git 統合の入口 wt.py finish（[E-045]）を通して本流へ入れる。合流の中身（本流の取り込み・
# -NEW の振り直し・§0 への差し込み・本体の早送り・作業コピーの片付け）は wt.py が持つ。ブラウニーが持つのは、
# 関門と評価役（--gate に渡す finish_gate.py）・衝突したときの解消役（1票1回）・本体が使用中（30）のときの再試行だけ。
# 独立した git リポジトリを持たないプロジェクト（根が Workspace の統治リポジトリになるもの）は今までどおり直接書く。

WORKTREES = os.path.join(STATE, "worktrees")
FINISH_DIR = os.path.join(STATE, "finish")  # wt.py finish の生の出力（1回ごと）
GATE_DIR = os.path.join(STATE, "gate")      # 関門に渡す作業票の情報と、評価役・関門の結果（票ごと。評価役を1票1回にするための記憶）
# 作業役がこの終わり方をした票だけ合流に進む。needs_permission は線の手前までの安全な変更だけを合流し、票は保留にする（③1）
OK_STATUS = ("done", "needs_decision", "needs_permission")
OUTCOME_JA = {"done": "できた", "hold": "保留", "failed": "失敗"}


GIT_ENV = {"PYTHONUTF8": "1", "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no",
           "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}


def git(args, cwd, timeout=None):
    # quotePath=false: 日本語名を "\346..." で出させない（名前の一覧は names() で -z 区切りに読む）
    # PYTHONUTF8=1: マージドライバ（merge_md.py）の stderr を UTF-8 にする
    # 無人なので、エディタ・資格情報の入力・標準入力を待たない。時間切れ付き（②6）。出力はファイルで受ける（②7）
    return run_capture(["git", "-c", "core.quotePath=false", *args], cwd=cwd, timeout=timeout or GIT_TIMEOUT_SEC,
                       env=dict(os.environ, **GIT_ENV))


def names(args, cwd):
    """ファイル名の一覧を返す git 呼び出し（NUL 区切り。空白や日本語を含む名前でも割れない）。"""
    return [x for x in git([*args, "-z"], cwd).stdout.split("\0") if x]


def joined(r):
    return (r.stdout.rstrip("\n") + "\n" + r.stderr).strip()


def checked_out_at(root, branch):
    """branch をチェックアウトしている作業コピーのパス（無ければ None）。"""
    path = None
    for ln in git(["worktree", "list", "--porcelain"], root).stdout.splitlines():
        if ln.startswith("worktree "):
            path = ln[9:]
        elif ln == f"branch refs/heads/{branch}":
            return os.path.normpath(path)
    return None


def same_path(a, b):
    return bool(a) and bool(b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def inside_dir(p, root):
    try:
        r = os.path.normcase(os.path.abspath(root))
        return os.path.commonpath([os.path.normcase(os.path.abspath(p)), r]) == r
    except ValueError:
        return False


def git_marker_between(d):
    """d から走査の根（含まない）までに .git があるか。git が動かなくても「git リポジトリである」ことは分かる。"""
    cur = os.path.abspath(d)
    while inside_dir(cur, EVAR) and not same_path(cur, EVAR):
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


class NotUsable(Exception):
    """git リポジトリなのに作業コピーを作れない（直接書きに落とさない＝③7）"""


def repo_root(d):
    """プロジェクトの独立した git リポジトリの根。git の無いプロジェクトなら None（直接書く）。
    git リポジトリなのに git が答えない（壊れた・所有者の確認で止まる等）ときは NotUsable を投げる（③7）。"""
    if not d or not os.path.isdir(d):
        return None
    r = git(["rev-parse", "--show-toplevel"], d)
    if r.returncode != 0 or not r.stdout.strip():
        marker = git_marker_between(d)
        if marker:
            raise NotUsable(f"{marker} は git リポジトリだが git が答えない: {joined(r)[-200:]}")
        return None
    root = os.path.normpath(r.stdout.strip())
    if same_path(root, EVAR):
        return None  # 統治リポジトリ（サブフォルダを追跡しない）＝ブランチを切っても意味が無い
    if not inside_dir(root, EVAR):
        # 走査の根より上のリポジトリ（砂場が BrownieProject の中にあるとき等）。そのプロジェクト自身のリポジトリではないので、
        # ここに作業コピーを作ると別のリポジトリ（ブラウニー自身など）を書き換える。直接書く扱いにする
        return None
    return root


def stale_dirty():
    """選んだプロジェクトの本流（本体の作業ツリー）に置き去りになっている未コミットの変更。
    [(リポジトリの根, [(相対パス, 最後に書かれた時刻。消されたままなら None)])] を返す。無ければ []。

    wt.py finish は、合流が書くパスに本体の未コミットの変更があると 30（本体が使用中）を返す。別のセッションが書いている
    途中なら待てば空くが、何日も前の変更は待っても空かない（2026-10-01 に GameBProject で、6日前の変更のために
    3票が計92回の再試行のあと保留になった）。その見分けを、ファイルが最後に書かれた時刻で付ける。
    見るのは追跡しているファイルだけ（追跡外は server/phase.txt のような稼働の状態が常にあり、毎晩鳴ってしまう）。
    消されたままのファイルは古さが分からないので、同じリポジトリに古い変更があるときだけ一緒に出す。"""
    if STALE_DIRTY_HOURS <= 0:
        return []
    cutoff = time.time() - STALE_DIRTY_HOURS * 3600
    out, seen = [], set()
    for d in selected_projects() or []:
        try:
            root = repo_root(d)
        except NotUsable:
            continue
        if not root or os.path.normcase(root) in seen:
            continue
        seen.add(os.path.normcase(root))
        toks = names(["status", "--porcelain", "--untracked-files=no"], root)
        old, gone, i = [], [], 0
        while i < len(toks):
            xy, rel = toks[i][:2], toks[i][3:]
            i += 2 if "R" in xy or "C" in xy else 1  # 名前の変更・複写は、元の名前がもう1つ続く
            try:
                m = os.path.getmtime(os.path.join(root, rel))
            except OSError:
                gone.append((rel, None))
                continue
            if m < cutoff:
                old.append((rel, datetime.datetime.fromtimestamp(m)))
        if old:
            out.append((root, sorted(old, key=lambda x: x[1]) + gone))
    return out


def stale_dirty_lines(found):
    """stale_dirty() の結果を、報告とログに出す行にする。"""
    L = []
    for root, files in found:
        dated = [m for _, m in files if m]
        L.append(f"- `{root}`: {len(files)} 件（いちばん古いのは {min(dated):%m/%d %H:%M}・新しいのは {max(dated):%m/%d %H:%M}）")
        L += [f"  - `{rel}`" + (f"（{m:%m/%d %H:%M}）" if m else "（消されたまま）") for rel, m in files[:8]]
        if len(files) > 8:
            L.append(f"  - ほか {len(files) - 8} 件（`git -C \"{root}\" status` で全部見える）")
    return L


# 記録（統治文書）。仕分けの契約で、出典の PENDING.md・DECISIONS.md はほぼ全部の票の files に入る（prompts/triage.md）
RECORD_DOCS = ("decisions.md", "pending.md", "handoff.md")


def ticket_paths(t, root):
    """作業票の files を [(リポジトリの根からの相対パス（/ 区切り）, フォルダか)] にする。根の外と根そのものは捨てる。
    実物の files は絶対パスで、後ろに「（退避先）」のような説明が付いたり、「…/dev/Everheim/ 配下の〜」とフォルダを指したりする。"""
    out = []
    base = t.get("project_dir") or root
    for raw in t.get("files") or []:
        s = str(raw).split("（")[0].strip()
        m = re.search(r"[\\/]\s", s)  # 区切りの直後に空白＝そこまでがフォルダで、後ろは説明
        if m:
            s = s[:m.start() + 1]
        if not s:
            continue
        p = os.path.normpath(s if os.path.isabs(s) else os.path.join(base, s))
        if not inside_dir(p, root) or same_path(p, root):
            continue
        out.append((os.path.relpath(p, root).replace("\\", "/"), s.endswith(("/", "\\")) or os.path.isdir(p)))
    return out


def main_dirty(root):
    """本体の未コミットの変更 {相対パス: 追跡しているか}。名前の変更は新旧の両方を入れる。無視設定のファイルは入らない。"""
    toks = names(["status", "--porcelain", "--untracked-files=all"], root)
    out, i = {}, 0
    while i < len(toks):
        xy, rel = toks[i][:2], toks[i][3:]
        out[rel] = xy != "??"
        i += 1
        if ("R" in xy or "C" in xy) and i < len(toks):  # 名前の変更・複写は、元の名前がもう1つ続く
            out[toks[i]] = True
            i += 1
    return out


def precheck_clash(t, cache=None):
    """着手前の検査（[W-004]）。まだ始めていない票の担当ファイルのうち、本体に未コミットの変更があるもの（相対パス）を返す。
    wt.py finish は、合流が書くパスに本体の未コミットが重なると 30（本体が使用中）を返す。重なったまま始めると、古い版を元に
    作業して合流できずに終わる（2026-10-01 の3票は計92回の再試行のあと保留になった）。だから始める前に見て、空くまで後へ回す。
    見ないもの: もう始めた票（ブランチがある）・git の無いプロジェクト・記録（RECORD_DOCS）。記録は仕分けの契約でほぼ全部の票の
    files に入るので、見ると、別のセッションが居るプロジェクトの票が1枚も始まらなくなる（NIGHT_PRECHECK_RECORDS=1 で見る）。
    フォルダで書かれた担当は、追跡しているファイルの変更だけを見る（追跡外は退避先や生成物が常にあり、いつまでも空かない）。"""
    if not PRECHECK or t.get("branch") or not t.get("files"):
        return []
    try:
        root = repo_root(t.get("project_dir"))
    except NotUsable:
        return []  # 作業コピーを作れない票は、ensure_worktree が失敗にする
    if not root:
        return []
    cache = {} if cache is None else cache
    key = os.path.normcase(root)
    if key not in cache:
        cache[key] = main_dirty(root)
    hit = set()
    for rel, is_dir in ticket_paths(t, root):
        if not PRECHECK_RECORDS and os.path.basename(rel).lower() in RECORD_DOCS:
            continue
        r = os.path.normcase(rel)
        for d, tracked in cache[key].items():
            dn = os.path.normcase(d)
            if dn == r or (is_dir and tracked and dn.startswith(r.rstrip("\\/") + os.sep)):
                hit.add(d)
    return sorted(hit)


def pick_startable(todo):
    """優先順に並んだ pending の票から、今始められる最初の1枚を返す（無ければ None）。
    担当ファイルが本体で使用中の票には t["waiting"] を付けて後へ回し、空いたら外す（同じ夜のうちに始める）。"""
    cache = {}
    for t in todo:
        clash = precheck_clash(t, cache)
        prev = t.get("waiting") if isinstance(t.get("waiting"), dict) else None
        if not clash:
            if prev:
                log(f"▶ {t['id']} の担当ファイルが本体で空いたので始める")
                t.pop("waiting", None)
            return t
        if not prev or prev.get("files") != clash:
            log(f"⏸ {t['id']} は始めない。担当ファイルに本体の未コミットの変更が重なっている（{'、'.join(clash[:5])}"
                + (f" ほか {len(clash) - 5} 件" if len(clash) > 5 else "") + "）。空いたら始める")
            t["waiting"] = {"why": "main_dirty", "files": clash,
                            "since": (prev or {}).get("since") or now().isoformat(timespec="seconds")}
            save_ticket(t)
    return None


def remap(value, src, dst):
    """作業票の中の本流のパスを、作業コピーのパスへ読み替える（本流を直接書かせないため）。"""
    if isinstance(value, str):
        for s in {src, src.replace("\\", "/")}:
            value = re.sub(re.escape(s), dst.replace("\\", "\\\\"), value, flags=re.I)
        return value
    if isinstance(value, list):
        return [remap(v, src, dst) for v in value]
    if isinstance(value, dict):
        return {k: remap(v, src, dst) for k, v in value.items()}
    return value


def ensure_worktree(t):
    """作業票の作業コピーを用意して (本流の根, 作業コピーの根) を返す。直接書くのは git の無いプロジェクトだけ（None）。
    git リポジトリなのに作業コピーを作れないときは "cannot" を返し、t["merge"] に理由を置く（直接書きに落とさない＝③7）。"""
    if t.get("branch") and os.path.isdir(t.get("worktree") or ""):
        return t["repo"], t["worktree"]

    def cannot(why, **kw):
        t["merge"] = dict(kw, state="no_worktree", note="作業コピーを作れない: " + why)
        log(f"  {t['merge']['note']}")
        return "cannot"

    try:
        root = repo_root(t.get("project_dir"))
    except NotUsable as ex:
        return cannot(str(ex))
    if not root:
        t["merge"] = {"state": "direct", "note": "独立した git リポジトリが無いので直接書いた"}
        return None
    base = git(["symbolic-ref", "--short", "HEAD"], root).stdout.strip()
    if not base:
        return cannot(f"本流 {root} が detached HEAD（どのブランチへ合流させるか決められない）", repo=root)
    branch = t.get("branch") or f"night/{night_date()}/{t['id']}"
    wt = os.path.join(WORKTREES, night_date(), re.sub(r"[^A-Za-z0-9_-]", "-", t["id"]))
    os.makedirs(os.path.dirname(wt), exist_ok=True)
    exists = git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], root).returncode == 0
    r = git(["worktree", "add", wt, branch] if exists else ["worktree", "add", "-b", branch, wt, "HEAD"], root)
    if r.returncode != 0 and (other := checked_out_at(root, branch)):
        # ブランチが既に別の作業コピー（前に残したもの）でチェックアウトされている。本流へ直接書くには落とさない
        if wt_ok(other) and not link_problem(other):
            t.update(repo=root, base=base, branch=branch, worktree=other)
            log(f"  ブランチ {branch} は既に作業コピー {other} にあるので、それを使う")
            return root, other
        return cannot(f"ブランチ {branch} が使えない作業コピー {other} でチェックアウトされたまま", branch=branch, repo=root, base=base)
    if r.returncode != 0:
        return cannot(f"worktree add に失敗: {joined(r)[-200:]}", repo=root, base=base)
    # claim_gate の .claims を作業コピーのコミットに混ぜない（info/exclude は本流と共通）
    common = git(["rev-parse", "--git-common-dir"], root).stdout.strip()
    excl = os.path.join(common if os.path.isabs(common) else os.path.join(root, common), "info", "exclude")
    try:
        if ".claims" not in read_text(excl).split():
            os.makedirs(os.path.dirname(excl), exist_ok=True)
            with open(excl, "a", encoding="utf-8") as f:
                f.write("\n.claims\n")
    except OSError:
        pass
    t.update(repo=root, base=base, branch=branch, worktree=wt)
    log(f"  ブランチ {branch}（本流 {base}）作業コピー {wt}")
    return root, wt


# ブラウニーが自分（BrownieProject）を直すときの凍結。tests/ と審判官の基準はブラウニー自身には直させない
# （作業役が自分の採点基準や関門を書き換えられると、評価基準を外に置いた意味が無くなる。2026-09-25 エヴァの判断）
FROZEN_FOR_NIGHT = ("tests/", "judge/", "prompts/judge.md")
# どのリポジトリでも、Claude やフックの振る舞いを変えるファイルに触れた差分は保留にする（③5）
PROTECTED_RE = re.compile(r"(^|/)\.claude/(settings[^/]*|hooks/.*|agents/.*)$|(^|/)\.git-?hooks/|(^|/)\.mcp\.json$", re.I)


def protected_paths(changed):
    return [c for c in changed if PROTECTED_RE.search(c.replace("\\", "/"))]


# 2026-10-01（N3）: ブラウニー自身のリポジトリは、判断の記録（HANDOFF・PENDING・DECISIONS）以外に触れた差分を全部保留にする。
# runner.py・finish_gate.py・choose_projects.py・hooks/・prompts/・judge/・tests/・night_settings.json・*.bat を作業役が書き換えて
# 自動で合流させると、歯止めそのものを外せる。FROZEN_FOR_NIGHT（tests/・judge/・prompts/judge.md）はこの中に含まれる。
SELF_DOCS_OK = ("HANDOFF.md", "PENDING.md", "DECISIONS.md")


def self_hold_paths(changed):
    return [c for c in changed if c.replace("\\", "/").removeprefix("./") not in SELF_DOCS_OK]


def project_in(t, rels):
    """作業票のプロジェクトが、走査の根からの相対パス rels のどれかの配下か。"""
    p = t.get("project_dir") or ""
    return any(inside_dir(p, os.path.join(EVAR, r)) for r in rels) if p else False


def production():
    return os.path.normcase(os.path.abspath(HOME)) == os.path.normcase(ROOT) and not CLAUDE.endswith(".py")


def add_judge_fails_to_pending(verdict, md):
    """審判官の fail を BrownieProject/PENDING.md に1行ずつ足し、ブラウニーの改善ループの種にする（同じ夜・同じ項目は1回だけ）。"""
    pend = os.path.join(ROOT, "PENDING.md")
    body = read_text(pend)
    lines = []
    for it in verdict.get("items", []):
        if it.get("verdict") != "fail":
            continue
        key = f"審判官 {night_date()} {it.get('id')}"
        if key in body:
            continue
        note = " ".join(str(it.get("note", "")).split())[:300]
        lines.append(f"- [ ] {key} fail: {note}（詳細 `{os.path.relpath(md, ROOT)}`。直すのはブラウニーの仕組みの側。tests/ と judge/ は触らない）")
    if not lines:
        return
    with open(pend, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    git(["add", "--", "PENDING.md"], ROOT)
    # pathspec で PENDING.md だけに絞る（共有の index に他のセッションが積んだ分を混ぜない＝③10）
    git(["commit", "-qm", f"night: 審判官の fail を PENDING に {len(lines)} 件足した", "--", "PENDING.md"], ROOT)
    _self_heads.add(git(["rev-parse", "HEAD"], ROOT).stdout.strip())  # 自分のコミットを「外の変更」と取り違えない
    log(f"⚖ 審判官の fail {len(lines)} 件を BrownieProject/PENDING.md に足した（ブラウニーの改善の種）")


def has_markers(path):
    """衝突の印（ドライバが落ちたときの `<<<<<<< ours (merge_md.py failed…` も含む）が残っているか。
    UTF-8 として読めないファイルでも落ちずに調べる（ドライバが落ちる典型がそれ）。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return re.search(r"^(<{7}|>{7})", f.read(), re.M) is not None
    except OSError:
        return False


def wt_ok(wt):
    """作業コピーが自分自身の git を持っているか。.git が無いと、その中で打つ git は上へ辿って別のリポジトリ
    （BrownieProject 本体など）を操作してしまうので、作業コピーで git を打つ前に必ずこれで確かめる。"""
    if not os.path.isdir(wt) or not os.path.exists(os.path.join(wt, ".git")):
        return False
    top = git(["rev-parse", "--show-toplevel"], wt).stdout.strip()
    return bool(top) and os.path.normcase(os.path.realpath(top)) == os.path.normcase(os.path.realpath(wt))


NO_GIT = "作業コピーに自分の .git が無い（消えたか壊れた）ので、その中では git を打たなかった"


def _is_junction(path):
    f = getattr(os.path, "isjunction", None)  # Python 3.12 から
    if f:
        return f(path)
    import ctypes
    a = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    return a != 0xFFFFFFFF and bool(a & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def links_inside(root):
    """root の下の junction・シンボリックリンク（辿らずに探す。os.path.islink は junction を拾わないことがある）。"""
    found, stack = [], [root]
    while stack:
        for e in os.scandir(stack.pop()):
            if e.is_symlink() or _is_junction(e.path):
                found.append(e.path)
            elif e.is_dir(follow_symlinks=False):
                stack.append(e.path)
    return found


def link_problem(wt):
    """作業コピーに触ってはいけない理由（パスの途中か中身に junction・シンボリックリンクがある）。無ければ None。
    `git add -A` は junction を辿ってリンク先の中身をコミットし、再帰削除はリンク先まで消す。wt.py finish と同じ確かめ方。"""
    try:
        if os.path.normcase(os.path.realpath(wt)) != os.path.normcase(os.path.abspath(wt)):
            return f"作業コピーへのパスの途中に junction かシンボリックリンクがある（{wt}）"
        links = links_inside(wt)
    except Exception as ex:  # 合流の後で落ちないよう、何が起きても「残す」に倒す
        return f"作業コピーの中を調べられなかった（{type(ex).__name__}: {ex}）"
    if links:
        return "作業コピーの中に junction かシンボリックリンクがある: " + ", ".join(links[:5])
    return None


def committed_links(root, *rev):
    """rev の差分でコミットされたシンボリックリンク（mode 120000）のパス。作業役が自分でリンクをコミットした場合に備える。"""
    toks = git(["diff", "--raw", "-z", "--no-renames", *rev], root).stdout.split("\0")
    found, i = [], 0
    while i < len(toks):
        if toks[i].startswith(":") and i + 1 < len(toks):
            if toks[i][1:].split()[1] == "120000":
                found.append(toks[i + 1])
            i += 2
        else:
            i += 1
    return found


# ---------------------------------------------------------------- 作業コピーの外の見張り（③3）
# 関門は git の差分しか見ないので、作業コピーの外（Claude の設定・フック・エージェント定義・git の全体フック・本体の作業ツリー・
# リモート）への書き込みは見えない。フックは変えずに、司令塔が作業セッションの前後で指紋を比べる。夜は他のセッションも
# 動きうるので、誤検出は「保留」で受ける（合流させないだけで、ブランチは残る）。

_self_heads = set()  # 司令塔が自分で積んだコミット（審判官の fail の追記）。外の変更と取り違えない
WATCH_SKIP_DIRS = {".git", "__pycache__", ".ctx_gate_state", "node_modules"}  # 動くたびに書かれるもの（2026-09-25 実物で確認）
WATCH_SKIP_EXT = (".log", ".pyc", ".tmp")
# 関門フック spawn_gate.py が自分で書く状態ファイル（<セッション>.json と .lock）の置き場。どのセッションの道具の呼び出しでも
# 書かれるので、数えると全部の票が保留になる（2026-10-08 の夜 2026-10-07 で2票とも）。名前ではなく場所で外す
# （hooks の直下のこの1つだけ。同じ名前のフォルダが別の場所にあれば見張る）。
WATCH_SKIP_HOOK_DIRS = {".spawn_gate_state"}


def watch_files():
    c = os.path.join(WATCH_HOME, ".claude")
    hooks = os.path.join(c, "hooks")
    out = sorted(glob.glob(os.path.join(c, "settings*.json")))
    for d in (hooks, os.path.join(c, "agents"), os.path.join(WATCH_HOME, ".git-hooks")):
        for root, dirs, files in os.walk(d):
            dirs[:] = sorted(x for x in dirs if x not in WATCH_SKIP_DIRS and not (root == hooks and x in WATCH_SKIP_HOOK_DIRS))
            out += [os.path.join(root, f) for f in sorted(files) if not f.lower().endswith(WATCH_SKIP_EXT)]
    return out


def outside_snapshot(repos):
    """作業セッションの前後で比べる指紋。"""
    h = {}
    for f in watch_files():
        try:
            with open(f, "rb") as fh:
                h["file:" + f] = hashlib.sha1(fh.read()).hexdigest()
        except OSError:
            h["file:" + f] = "unreadable"
    for r in dict.fromkeys(os.path.normpath(x) for x in repos if x and os.path.isdir(x)):
        refs = git(["for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes"], r).stdout
        conf = git(["config", "--get-regexp", r"^remote\."], r).stdout
        h["remotes:" + r] = hashlib.sha1((refs + conf).encode()).hexdigest()
        h["head:" + r] = git(["rev-parse", "HEAD"], r).stdout.strip()
        h["status:" + r] = hashlib.sha1(git(["status", "--porcelain", "--untracked-files=all"], r).stdout.encode()).hexdigest()
    return h


def outside_changes(before, after):
    """前後の指紋の違いを、人が読める言葉で返す（無ければ空）。司令塔自身のコミットは除く。"""
    words = {"file": "{} が書き換わった", "remotes": "{} のリモート（refs/remotes か remote の設定）が変わった＝push かリモート操作の痕跡",
             "head": "{} の本体の HEAD が動いた", "status": "{} の本体の作業ツリー（git status）が変わった"}
    out = []
    for k in sorted(set(before) | set(after)):
        if before.get(k) == after.get(k):
            continue
        kind, _, what = k.partition(":")
        if kind == "head" and after.get(k) in _self_heads:
            continue
        if kind == "status" and after.get("head:" + what) in _self_heads and before.get("head:" + what) != after.get("head:" + what):
            continue
        out.append(words[kind].format(what))
    return out


def watch_repos(t):
    """見張る本体：作業票のリポジトリと、ブラウニー自身のリポジトリ。"""
    repos = [t.get("repo")]
    top = git(["rev-parse", "--show-toplevel"], ROOT)
    if top.returncode == 0 and top.stdout.strip():
        repos.append(top.stdout.strip())
    return repos


def watched(t, fn):
    """fn（セッション）の前後で作業コピーの外を比べ、変わっていたら t["outside"] に足す。"""
    repos = watch_repos(t)
    before = outside_snapshot(repos)
    try:
        return fn()
    finally:
        changes = outside_changes(before, outside_snapshot(repos))
        if changes:
            t.setdefault("outside", [])
            t["outside"] += [c for c in changes if c not in t["outside"]]
            log("  ⚠ 作業コピーの外への変更を検出: " + " ／ ".join(changes[:5]))


# ---------------------------------------------------------------- 関門（wt.py finish --gate → finish_gate.py → gate_main）
# 関門の情報ファイル（state/gate/<夜>/<票>.json）は作業役にも書ける場所にある。夜の回ごとの秘密鍵で署名し（③4）、
# 鍵は wt.py と finish_gate を呼ぶときの環境変数だけで渡す（claude のセッションには渡さない）。署名が合わない情報は使わない。

def gate_file(t):
    return t.get("gate_file") or os.path.join(GATE_DIR, night_date(), safe_name(t["id"]) + ".json")


def sign_gate(d, key):
    body = json.dumps({k: v for k, v in d.items() if k != "mac"}, ensure_ascii=False, sort_keys=True)
    return hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()


def read_gate(path, key):
    """(中身, 問題)。key があるときは署名を確かめ、合わなければ (None, 理由)。"""
    d = read_json(path, None)
    if not isinstance(d, dict):
        return None, "読めない"
    if key and not hmac.compare_digest(str(d.get("mac", "")), sign_gate(d, key)):
        return None, "署名が合わない（司令塔の外で書き換えられた）"
    return d, None


def write_gate(path, d, key):
    if key:
        d["mac"] = sign_gate(d, key)
    else:
        d.pop("mac", None)
    write_json(path, d)


def cmdline(parts):
    """wt.py の --gate に渡すコマンド行。区切りは / にする（シェルが \\ を食う実装でも壊れないように）。空白などを含むものだけ引用符で囲む。
    % と " と改行は、cmd.exe で展開・分割されて別物になるので受け付けない（②20）。"""
    out = []
    for x in parts:
        x = str(x).replace("\\", "/")
        if re.search(r'[%"\r\n!]', x):
            raise ValueError(f"コマンド行に安全に載せられない文字（% \" ! 改行）を含む: {x}")
        out.append(f'"{x}"' if not x or re.search(r'[\s&|<>^()]', x) else x)
    return " ".join(out)


def gate_command(t, approved=False):
    return cmdline([sys.executable, GATE_SCRIPT] + (["--approved"] if approved else []) + [gate_file(t)])


def approve_cmd(t, approved=False):
    """保留を通すときのコマンド（朝の報告に載せる）。approved=True は凍結と評価役を飛ばす（エヴァが「はい」と言った分）。
    関門の試験・リンク・署名の確かめは飛ばさない。--approved はブラウニーが動いている間は関門が受け付けない（③9）。"""
    try:
        return cmdline([sys.executable, WT_PY, "finish", t["repo"], t["branch"], "--gate", gate_command(t, approved),
                        "--gate-timeout", str(GATE_TIMEOUT_SEC)])
    except ValueError as ex:
        return f"（コマンドを作れない: {ex}）"


def write_gate_file(t, attempt):
    """関門（別のプロセス）に渡す作業票の情報。評価役の結果もここに残るので、再試行しても評価役は1票1回で済む（③12 で最大2回）。"""
    path = gate_file(t)
    t["gate_file"] = path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    d, err = read_gate(path, GATE_KEY) if os.path.exists(path) else ({}, None)
    if err:
        log(f"  ⚠ 関門の情報ファイル {path} の{err}。中の記憶（評価役・関門の結果）は捨てて作り直す")
        d = {}
    d.update(ticket={k: t.get(k) for k in ("id", "title", "goal", "source", "project_dir", "done_check", "kind")},
             repo=t["repo"], worktree=t["worktree"], branch=t["branch"], base=t["base"], attempt=attempt,
             env={"NIGHT_HOME": HOME, "NIGHT_EVAR_ROOT": EVAR, "NIGHT_CLAUDE_BIN": CLAUDE, "NIGHT_MODEL": MODEL,
                  "NIGHT_SELF_REPO": SELF_REPO, "NIGHT_PERMISSION_MODE": PERMISSION_MODE, "NIGHT_BUDGET_USD": str(BUDGET_USD),
                  "NIGHT_REVIEW_TIMEOUT_SEC": str(REVIEW_TIMEOUT_SEC), "NIGHT_GATE_TIMEOUT_SEC": str(GATE_TIMEOUT_SEC),
                  "NIGHT_LABEL": NIGHT, "NIGHT_RUN_ID": RUN_ID, "NIGHT_WATCH_HOME": WATCH_HOME})
    write_gate(path, d, GATE_KEY)
    return path


def runner_alive():
    """ブラウニーの司令塔が動いているか（ロックの pid が生きている／heartbeat が5分以内）。"""
    try:
        pid = int(open(LOCK).read().strip())
        if pid != os.getpid() and pid_alive(pid):
            return True
    except (OSError, ValueError):
        pass
    hb = read_json(HEARTBEAT, {})
    try:
        age = (now() - datetime.datetime.fromisoformat(hb.get("at", ""))).total_seconds()
        return age < 300 and not str(hb.get("phase", "")).startswith("終了")
    except (TypeError, ValueError):
        return False


def review_verdict(t, repo, wt, base, branch, files):
    if usage_blocked(follow=True):
        return {"verdict": "limit", "line": "利用枠が残っていないので、評価役を起こさなかった"}
    return review_verdict_run(t, repo, wt, base, branch, files)


def review_verdict_run(t, repo, wt, base, branch, files):
    """評価役：合流の直前に、差分が既存の決裁と食い違っていないかを別セッションで見る（2026-09-25 エヴァ案・[E-043追記11] で残す）。
    {"verdict": OK|NG|unreadable|limit|outside, "line": 最後の1行} を返す。"""
    tpl = read_text(os.path.join(PROMPTS, "review.md"))
    view = remap({k: t.get(k) for k in ("id", "title", "goal", "source", "project_dir")}, repo, wt)
    prompt = (tpl.replace("{WT}", wt).replace("{BASE}", base).replace("{BRANCH}", branch)
                 .replace("{FILES}", "\n".join(f"- `{f}`" for f in files[:80]))
                 .replace("{TICKET}", json.dumps(view, ensure_ascii=False, indent=1)))
    log("  評価役を起動（差分が既存の決裁と食い違っていないか）")
    before = outside_snapshot([repo])  # 評価役は読むだけのはず。前後で作業コピーの外が変わったら判定を使わない（③3）
    res = run_claude(prompt, wt, "medium", max(0.3, min(1.5, remaining_budget())),
                     {"NIGHT_KIND": "review", "NIGHT_PROGRESS": os.path.join(STATE, "review_last.md")}, "評価役",
                     deadline=time.time() + REVIEW_TIMEOUT_SEC)
    outside = outside_changes(before, outside_snapshot([repo]))
    last = ([ln.strip() for ln in str(res.get("result") or "").splitlines() if ln.strip()] or [""])[-1]
    cost = float(res.get("total_cost_usd") or 0)
    ledger_add({"at": now().isoformat(timespec="seconds"), "night": night_date(), "kind": "review", "ticket": t.get("id"),
                "session_id": res.get("session_id"), "cost": cost, "sec": res.get("_sec"), "cost_estimated": res.get("_cost_estimated"),
                "is_error": res.get("is_error"), "result": last[:300]})
    log(f"  評価役: {last[:160]}  ${cost:.2f}")
    if outside:
        return {"verdict": "outside", "line": "評価役の間に作業コピーの外が変わった: " + " ／ ".join(outside[:3])}
    if hit_usage_limit(res):
        return {"verdict": "limit", "line": "評価役が利用上限に当たって走れなかった"}
    if res.get("is_error") or not re.match(r"^(OK|NG)\b", last):
        log("  評価役の判定が読めなかったので、止めずに通す（朝に revert できる）")
        return {"verdict": "unreadable", "line": last}
    return {"verdict": "NG" if last.startswith("NG") else "OK", "line": last}


def run_night_gate(gate, wt, base, branch):
    """本流側の tests/night_gate.py を作業コピーに向けて流す。(通ったか, 出力の末尾) を返す。固まったら子プロセスごと止める。"""
    os.makedirs(STATE, exist_ok=True)
    out = os.path.join(STATE, f"night_gate_{uuid.uuid4().hex[:8]}.out")
    # 出力の置き場もここに書く（関門が固まって wt.py に止められると、この後の行は出ない。出力のファイルは残る）
    log(f"  関門の試験を流す（{gate}。出力は {out}）")
    with open(out, "wb") as fo:
        p = subprocess.Popen([sys.executable, gate, wt, base, branch], cwd=wt, stdout=fo, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL)
        ok = wait_proc(p, time.time() + NIGHT_GATE_STEP_SEC)
    lines = [ln for ln in read_text(out).strip().splitlines() if ln.strip()]
    try:
        os.remove(out)
    except OSError:
        pass
    for ln in lines[-6:]:
        log("    " + ln)
    if not ok:
        return False, lines[-5:] + [f"関門の試験が {dur(NIGHT_GATE_STEP_SEC)} で終わらなかった（子プロセスごと止めた）"]
    return p.returncode == 0, lines[-6:]


def gate_main(path, approved=False, key=b""):
    """finish_gate.py の本体。wt.py finish が作業コピーの中で呼ぶ（WT_PROJECT・WT_WORKTREE・WT_BRANCH・WT_BASE）。
    終了コード 0＝通す、1＝通さない。結果は情報ファイルの results[attempt] に kind と理由で残す（司令塔が 20 の中身を見分けるため）。
    kind: pass／hold＝エヴァの許可で通せる（凍結・保護したファイルに触れた・評価役が止めた）／fail＝直さないと通らない。
    key（夜の回の秘密鍵）が無いとき（朝に手で流す「通すなら」）は、情報ファイルの記憶（評価役・関門の結果・env）を信じない。"""
    global IN_GATE
    IN_GATE = True
    d, err = read_gate(path, key)
    if d is None:
        print(f"関門: 不合格 — 作業票の情報ファイル {path} が{err}", flush=True)
        return 1
    trusted = bool(key)
    if not trusted:
        d = {k: d.get(k) for k in ("ticket", "repo", "worktree", "branch", "base", "attempt", "gate_file")}
    t = d.get("ticket") or {}
    repo = d.get("repo") or os.environ.get("WT_PROJECT", "")
    wt = os.environ.get("WT_WORKTREE") or d.get("worktree") or os.getcwd()
    base = os.environ.get("WT_BASE") or d.get("base") or "master"
    branch = os.environ.get("WT_BRANCH") or d.get("branch") or "HEAD"
    attempt = d.get("attempt") or "manual"

    def verdict(kind, reason, **extra):
        # 鍵の無い手作業（朝の「通すなら」）では情報ファイルに書き戻さない。書くと署名が外れ、その夜の記憶（評価役の結果）が消える
        if trusted:
            d.setdefault("results", {})[attempt] = dict(extra, kind=kind, reason=reason, approved=approved,
                                                         at=now().isoformat(timespec="seconds"))
            write_gate(path, d, key)
        print(f"関門: {({'pass': '合格', 'hold': '保留（エヴァの許可待ち）'}).get(kind, '不合格')} — {reason}", flush=True)
        return 0 if kind == "pass" else 1

    try:
        if approved and runner_alive():
            return verdict("fail", "ブラウニーの司令塔が動いている間は --approved（凍結と評価役を飛ばす許可）を受け付けない。朝にエヴァが流す")
        if not wt_ok(wt):
            return verdict("fail", NO_GIT)
        # 判定はすべて、実際に本流へ入る中身（本流を取り込んだ後の木）に対して行う
        links = committed_links(wt, f"{base}...HEAD")
        if links:
            return verdict("fail", "シンボリックリンクがコミットされている（" + "、".join(links[:5]) + "）ので合流しない")
        changed = names(["diff", "--name-only", f"{base}...HEAD"], wt)
        if not approved:
            prot = protected_paths(changed)
            if prot:
                return verdict("hold", "Claude・フックの設定に触れた（" + "、".join(prot[:5]) + "）ので、エヴァの確認待ちにした")
            if same_path(repo, SELF_REPO):
                touched = self_hold_paths(changed)
                if touched:
                    return verdict("hold", "ブラウニー自身（司令塔・フック・プロンプト・審判官の基準・関門の試験など。"
                                   + "、".join(touched[:5]) + "）に触れたので、エヴァの確認待ちにした")
        gate = os.path.join(repo, "tests", "night_gate.py")
        if repo and os.path.isfile(gate):
            tree = git(["rev-parse", "HEAD^{tree}"], wt).stdout.strip()
            cached = d.setdefault("tree_cache", {}).get(tree) if trusted else None
            if cached is None:
                ok, lines = run_night_gate(gate, wt, base, branch)
                cached = d["tree_cache"][tree] = {"ok": ok, "lines": lines}
                if trusted:
                    write_gate(path, d, key)
            else:
                log("  関門の試験は同じ中身ですでに流した（結果を使い回す）")
            if not cached["ok"]:
                return verdict("fail", "関門の試験に落ちた: " + " ／ ".join(cached["lines"][-3:]))
        if not approved and changed:
            # 評価役の結果は〈票＋ブランチが変えた差分〉で覚える。解消役などで差分が変わったら1回だけ走り直す（1票最大2回＝③12）
            dh = hashlib.sha1(git(["diff", "-U0", f"{base}...HEAD"], wt).stdout.encode()).hexdigest()
            rv = d.get("review") if trusted else None
            if rv is None or (rv.get("diff") != dh and rv.get("count", 1) < 2):
                if BUDGET_USD - spent_tonight() - JUDGE_CAP_USD < 0.3:
                    return verdict("hold", "一晩の上限の残りが少なく、評価役を起こせなかった（評価なしでは合流しない）")
                got = review_verdict(t, repo, wt, base, branch, changed)
                if got.get("verdict") == "limit":
                    # 結果は覚えない（覚えると、枠が戻った後の再試行でも評価役を呼ばずに保留になる）。回数だけ別に数える
                    d["limit_count"] = d.get("limit_count", 0) + 1
                    if trusted:
                        write_gate(path, d, key)
                    if d["limit_count"] > 3:
                        return verdict("hold", f"評価役が利用上限で {d['limit_count']} 回走れなかったので、これ以上は起こさない（評価なしでは合流しない）")
                    rv = got
                else:
                    rv = d["review"] = dict(got, diff=dh, count=(rv or {}).get("count", 0) + 1, at=now().isoformat(timespec="seconds"))
                    if trusted:
                        write_gate(path, d, key)
            elif rv.get("diff") != dh:
                log("  差分が変わったが、評価役は1票2回まで。前の判定を使う")
            else:
                log(f"  評価役はこの差分ですでに走った（{rv.get('verdict')}）ので、もう呼ばない")
            if rv.get("verdict") == "limit":
                return verdict("hold", rv.get("line", ""), usage_limit=True)
            if rv.get("verdict") == "outside":
                return verdict("hold", rv.get("line", ""))
            if rv.get("verdict") == "NG":
                return verdict("hold", "評価役が止めた: " + str(rv.get("line", ""))[3:].strip(" :："))
        return verdict("pass", "関門を通った" + ("（エヴァの許可で、凍結と評価役は飛ばした）" if approved else ""))
    except Exception as ex:
        log("関門の中で例外:\n" + traceback.format_exc())
        return verdict("fail", f"関門の中で例外 {type(ex).__name__}: {ex}")


# ---------------------------------------------------------------- 合流（wt.py finish）と、票の行き先（できた／保留／失敗）

def wt_usable():
    """wt.py が finish --gate と --gate-timeout に対応しているか（[E-045] より前の wt.py だと、全部の票が合流できずに夜が終わる）。"""
    r = run_capture([sys.executable, WT_PY, "finish", "--help"], timeout=120)
    text = r.stdout + r.stderr
    return r.returncode == 0 and "--gate" in text and "--gate-timeout" in text


_WT_FLAGS = {}


def records_later_args():
    """wt.py finish に足す `--records-later`（[W-004]）。本体で塞がっているのが記録（DECISIONS.md・PENDING.md・HANDOFF.md）だけのとき、
    成果物を先に本流へ入れさせる。切ってあるとき（NIGHT_RECORDS_LATER=0）と、wt.py が対応していないときは空（古い wt.py でも夜が回る）。"""
    if not RECORDS_LATER:
        return []
    if WT_PY not in _WT_FLAGS:
        r = run_capture([sys.executable, WT_PY, "finish", "--help"], timeout=120)
        _WT_FLAGS[WT_PY] = r.returncode == 0 and "--records-later" in (r.stdout + r.stderr)
    return ["--records-later"] if _WT_FLAGS[WT_PY] else []


def set_outcome(t, kind, cause, how=None, night=None):
    """票の行き先を決めて台帳に残す（kind: done|hold|failed）。同じ票に後から別の行き先が付いたら（再開・差し戻し）、最後の行が有効。"""
    t["outcome"] = {"kind": kind, "cause": cause, "how": how, "at": now().isoformat(timespec="seconds")}
    ledger_add({"at": t["outcome"]["at"], "night": night or night_date(), "kind": "outcome", "ticket": t.get("id"),
                "title": t.get("title"), "outcome": kind, "cause": cause, "how": how, "status": t.get("status"),
                "merge": (t.get("merge") if isinstance(t.get("merge"), dict) else {}).get("state")})
    log(f"  行き先: {OUTCOME_JA.get(kind, kind)} — {str(cause)[:160]}")


def status_cause(t):
    """作業役の終わり方から、失敗の原因を1行で。"""
    st = t.get("status")
    m = t.get("merge") or {}
    head = {"timeout": f"時間切れ（1票の持ち時間 {dur(TICKET_TIMEOUT_SEC)}・通算）。子プロセスごと止めた",
            "error": "エラーで止まった: " + str(t.get("last_result") or "")[:200],
            "gave_up": f"区切りを {MAX_SESSIONS_PER_TICKET} 回重ねても終わらなかった",
            "blocked": "自力で進めなかった" + (f"（{m.get('note')}）" if m.get("note") else "")}.get(st, f"{st} で終わった")
    if t.get("revived"):
        head += f"（同じ夜に {t['revived']} 回再開した後）"
    one = section(read_text(progress_path(t)), "ひとことで").replace("\n", " ")[:200]
    return head + (f" ／ 進捗: {one}" if one else "")


def permission_detail(t):
    """needs_permission の票の〈操作・理由・流すコマンドか差分〉（進捗ファイルの「許可が要る操作」節）。"""
    body = section(read_text(progress_path(t)), "許可が要る操作")
    lines = [" ".join(ln.split()) for ln in body.splitlines() if ln.strip()]
    return " ／ ".join(lines)[:700] or "（進捗ファイルに「許可が要る操作」の節が無い。進捗ファイルを見る）"


def diff_how(t):
    return f'git -C "{t["repo"]}" diff {t["base"]}...{t["branch"]} --stat'


def stash_untracked(t):
    """作業コピーの追跡外と無視設定のファイルを 排除/ へ移す（作業コピーを外すと消えるため＝③11）。移した先を返す。"""
    wt = t["worktree"]
    items = names(["ls-files", "--others", "--exclude-standard", "--directory"], wt)
    items += names(["ls-files", "--others", "--ignored", "--exclude-standard", "--directory"], wt)
    items = [x.rstrip("/") for x in dict.fromkeys(items) if x.strip("/")]
    if not items:
        return None
    dest = os.path.join(EVAR, "排除", "night_worktrees", night_date(), safe_name(t["id"]) + f"_{now():%H%M%S}")
    for rel in items:
        src = os.path.join(wt, rel)
        if not os.path.lexists(src):
            continue
        dst = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
    log(f"  作業コピーの追跡外のファイル {len(items)} 件を {dest} へ退避した")
    return dest


def remove_worktree(t):
    """作業コピーを外す（ブランチは記録として残す）。追跡外・無視設定のファイルは先に 排除/ へ移し、外すのは wt.py finish --discard
    （使えなければ --force なしの git worktree remove）。リンクがあるときや .git が無いときは触らない（③11・②12）。"""
    wt = t.get("worktree")
    if not wt or not os.path.isdir(wt):
        return
    m = t.setdefault("merge", {})
    why = link_problem(wt) or (None if wt_ok(wt) else NO_GIT)
    if why:
        m["worktree_kept"] = why + "。作業コピーを消さずに残した"
        return
    drop_empty_claims(wt)
    try:
        dest = stash_untracked(t)
        if dest:
            m["stashed"] = dest
    except (OSError, shutil.Error) as ex:
        m["worktree_kept"] = f"追跡外のファイルを退避できなかった（{ex}）ので、作業コピーを消さずに残した"
        return
    r = run_capture([sys.executable, WT_PY, "finish", t["repo"], t["branch"], "--discard"], timeout=300,
                    env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"))
    rep = next((ln[len("WT-REPORT "):] for ln in reversed(r.stdout.splitlines()) if ln.startswith("WT-REPORT ")), None)
    try:
        m["discard"] = json.loads(rep).get("status") if rep else f"WT-REPORT なし（{r.returncode}）"
    except ValueError:
        m["discard"] = f"WT-REPORT が読めない（{r.returncode}）"
    if os.path.isdir(wt):
        r2 = git(["worktree", "remove", wt], t["repo"])
        if r2.returncode != 0:
            m["worktree_kept"] = f"作業コピーを外せなかった（wt.py --discard: {joined(r)[-120:]} ／ git: {joined(r2)[-120:]}）"
            log(f"  {m['worktree_kept']}")


def revert_how(root, before, head, tip0):
    """合流を戻すコマンド。合流の形（取り込みの合流コミットか、早送りだけか）で変わるので、実物から決める。"""
    if not before or not head or before == head:
        return f'確かめ方 `git -C "{root}" log --oneline -5`'
    parents = git(["rev-list", "--parents", "-n", "1", head], root).stdout.split()[1:]
    if len(parents) == 2:
        if before in parents:
            k = parents.index(before) + 1  # 合流の直前の本流が親の何番目か（そこを mainline にして戻す）
            return f'戻すなら `git -C "{root}" revert -m {k} {head[:9]}`'
        side = [bool(tip0) and git(["merge-base", "--is-ancestor", tip0, p], root).returncode == 0 for p in parents]
        if side.count(True) == 1:
            return f'戻すなら `git -C "{root}" revert -m {side.index(False) + 1} {head[:9]}`'
    elif not git(["rev-list", "--merges", f"{before}..{head}"], root).stdout.strip() \
            and git(["merge-base", "--is-ancestor", before, head], root).returncode == 0:
        return f'戻すなら `git -C "{root}" revert --no-edit {before[:9]}..{head[:9]}`'
    return f'戻すときは形を見てから `git -C "{root}" log --graph --oneline {before[:9]}..{head[:9]}`'


def index_lock_note(root):
    """wt.py を途中で止めた後、本体に index.lock が残っていれば知らせる（他のセッションのロックかもしれないので消さない＝②4）。"""
    p = git(["rev-parse", "--git-path", "index.lock"], root).stdout.strip()
    p = p if os.path.isabs(p) else os.path.join(root, p)
    return f" ⚠ 本体に index.lock が残っている（{p}。他のセッションのロックかもしれないので消していない）" if p and os.path.exists(p) else ""


def run_wt_finish(t, approved=False):
    """wt.py finish を1回呼ぶ。標準出力の最後の `WT-REPORT {json}` を読んで dict で返す（code・status・reason…）。
    呼び出し全体に時間切れを付け、切れたら子プロセス（関門・評価役も）ごと止めて code="timeout" を返す。"""
    attempt = uuid.uuid4().hex[:12]
    drop_empty_claims(t.get("worktree"))  # wt.py は作業コピーの無視設定のファイルを 排除/ へ運ぶので、その前に空の .claims を片付ける
    write_gate_file(t, attempt)
    try:
        cmd = [sys.executable, WT_PY, "finish", t["repo"], t["branch"], "--gate", gate_command(t, approved),
               "--gate-timeout", str(GATE_TIMEOUT_SEC)] + records_later_args()
    except ValueError as ex:
        return {"code": 2, "status": "abnormal", "reason": str(ex), "_gate": None}
    if t.get("last_session"):
        cmd += ["--session", str(t["last_session"])]
    os.makedirs(FINISH_DIR, exist_ok=True)
    base = os.path.join(FINISH_DIR, f"{night_date()}_{safe_name(t['id'])}_{attempt}")
    log(f"  合流の入口 wt.py finish を呼ぶ（{t['branch']}）")
    t0 = time.time()
    deadline = t0 + WT_TIMEOUT_SEC
    if session_end_cap() is not None:
        deadline = min(deadline, max(t0 + 60, session_end_cap()))
    # 署名鍵は wt.py（とその中の関門）にだけ渡す。ブラウニーのセッションの環境変数には入れない（③4）
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", NIGHT_GATE_KEY=GATE_KEY.hex())
    with open(base + ".out", "wb") as fo, open(base + ".err", "wb") as fe:
        p = subprocess.Popen(cmd, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL, cwd=HOME, creationflags=BELOW_NORMAL, env=env)
        finished = wait_proc(p, deadline)
    out, err = read_text(base + ".out"), read_text(base + ".err")
    rep = None
    for line in reversed(out.splitlines()):
        if line.startswith("WT-REPORT "):
            try:
                rep = json.loads(line[len("WT-REPORT "):])
                break
            except ValueError:
                continue
    if not finished:
        rep = {"code": "timeout", "status": "timeout",
               "reason": f"wt.py finish が {dur(time.time() - t0)} で終わらなかったので、子プロセスごと止めた（時間切れ）" + index_lock_note(t["repo"])}
    elif not isinstance(rep, dict):
        tail = " ／ ".join((out + "\n" + err).strip().splitlines()[-3:])[:300]
        rep = {"code": 2, "status": "abnormal", "reason": f"WT-REPORT の行が無かった（終了コード {p.returncode}）: {tail}"}
    elif rep.get("code") != p.returncode:
        log(f"  wt.py の終了コード {p.returncode} と WT-REPORT の code {rep.get('code')} が違う。WT-REPORT を採る")
    # 本物の wt.py は関門の出力を <作業コピーの git dir>/wt-gate.log に書く。作業コピーを外すと消えるので、今のうちに手元へ写す
    m_log = re.search(r"log: (\S*wt-gate\.log)", str(rep.get("reason") or ""))
    if m_log and os.path.isfile(m_log.group(1)):
        try:
            shutil.copyfile(m_log.group(1), base + ".gate.log")
            rep["reason"] = str(rep["reason"]).replace(m_log.group(1), base + ".gate.log")
        except OSError:
            pass
    gd, gerr = read_gate(gate_file(t), GATE_KEY)
    gate = (gd or {}).get("results", {}).get(attempt)
    if gerr:
        rep["reason"] = f"{rep.get('reason') or ''} ／ 関門の情報ファイルの{gerr}".strip(" ／")
    rep["_gate"] = gate
    rep["_review"] = (gd or {}).get("review")
    if (gate or {}).get("usage_limit"):
        _limit["hit"] = True
        # 関門の中で決めた待ちは司令塔に伝わらない。すぐ読み直して通ると、60秒後の再試行でまた断られる（1票3回をすぐ使い切る）
        _usage["retry_at"] = max(_usage["retry_at"], time.time() + usage_backoff())
    ledger_add({"at": now().isoformat(timespec="seconds"), "night": night_date(), "kind": "finish", "ticket": t["id"],
                "code": rep.get("code"), "status": rep.get("status"), "reason": str(rep.get("reason") or "")[:300],
                "gate": (gate or {}).get("kind"), "sec": round(time.time() - t0), "out": base + ".out"})
    log(f"  wt.py finish → {rep.get('code')} {rep.get('status')}" + (f"（{str(rep.get('reason'))[:160]}）" if rep.get("reason") else ""))
    return rep


def resolve_in_worktree(t):
    """wt.py finish が 10（本当の衝突）を返したとき、作業コピーで本流を取り込み、衝突したら解消役のセッションに直させる。
    (解消できたか, 出力) を返す。解消役は1票に1回だけ（コスパ。2026-09-25 エヴァ「衝突は自動で解消」）。
    衝突が起きるのは作業コピーの中だけ（本体の作業ツリーは触らない）。解消役の前後も作業コピーの外を見張る（③3）。"""
    root, wt, base, branch = t["repo"], t["worktree"], t["base"], t["branch"]
    if not os.path.isdir(wt) or not wt_ok(wt):
        return False, NO_GIT
    r = git(["merge", "--no-edit", base], wt)
    out = joined(r)
    if r.returncode == 0:
        log(f"  本流 {base} を作業コピーに取り込んだ（今度は衝突しなかった）")
        return True, out
    files = names(["diff", "--name-only", "--diff-filter=U"], wt)
    if not files:
        git(["merge", "--abort"], wt)
        log(f"  作業コピーへの取り込みに失敗（衝突以外）: {out.strip()[-200:]}")
        return False, out + "\n衝突以外で取り込みに失敗"
    t["resolver_used"] = True
    log(f"  衝突 {len(files)} 件を解消役に回す: {', '.join(files[:5])}")
    tpl = read_text(os.path.join(PROMPTS, "resolve.md"))
    view = remap({k: t.get(k) for k in ("id", "title", "goal", "done_check", "project_dir")}, root, wt)
    prompt = (tpl.replace("{WT}", wt).replace("{BASE}", base).replace("{BRANCH}", branch)
                 .replace("{FILES}", "\n".join(f"- `{f}`" for f in files))
                 .replace("{TICKET}", json.dumps(view, ensure_ascii=False, indent=1)))
    res = watched(t, lambda: run_claude(
        prompt, wt, "medium", max(0.5, min(3.0, remaining_budget())),
        {"NIGHT_KIND": "resolve", "NIGHT_PROGRESS": os.path.join(STATE, "resolve_last.md"), "NIGHT_WORK_ROOT": wt}, "解消役",
        deadline=time.time() + RESOLVE_TIMEOUT_SEC))
    if not wt_ok(wt):
        left, markers, ok = [], [], False
        out += "\n" + NO_GIT
    else:
        left = names(["diff", "--name-only", "--diff-filter=U"], wt)
        markers = [f for f in files if has_markers(os.path.join(wt, f))]
        if not left and not markers and git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], wt).returncode == 0:
            git(["add", "-A"], wt)  # ブラウニーの作業コピーの中（1票1コピー）
            git(["commit", "-qm", f"night: 本流 {base} を取り込み、衝突を解消（解消役）"], wt)
        ok = not left and not markers and git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], wt).returncode != 0
        if not ok:
            git(["merge", "--abort"], wt)
            what = [f"未解消のファイル {', '.join(left[:5])}"] if left else []
            what += [f"衝突の印（{', '.join(markers[:5])}）"] if markers else []
            out += "\n解消役の後も" + ("・".join(what) + "が残った" if what else "マージが終わっていなかった")
    ledger_add({"at": now().isoformat(timespec="seconds"), "night": night_date(), "kind": "resolve", "ticket": t["id"],
                "session_id": res.get("session_id"), "cost": float(res.get("total_cost_usd") or 0), "sec": res.get("_sec"),
                "cost_estimated": res.get("_cost_estimated"), "is_error": res.get("is_error"),
                "status": "resolved" if ok else "unresolved", "files": files, "markers": markers, "unmerged": left})
    log(f"  解消役: {'解消できた' if ok else '解消できなかった'}  ${float(res.get('total_cost_usd') or 0):.2f}"
        + (f"（印が残った: {', '.join(markers[:5])}）" if markers else ""))
    return ok, out


def hold_outside(t):
    """作業コピーの外が変わった票は、合流せず保留（③3）。ブランチと作業コピーは残す。"""
    m = t.setdefault("merge", {})
    m.update(state="held", note="作業コピーの外への変更を検出: " + " ／ ".join(t["outside"][:5]))
    how = "確かめてから、採るなら " + (f"`{approve_cmd(t)}`" if t.get("branch") else "（ブランチ無し）")
    set_outcome(t, "hold", m["note"], how=how)


def finish_ticket(t):
    """作業役のセッションが終わった後の後始末。作業票がまだ続く（pending）なら何もしない。
    成果物は wt.py finish を通して本流へ入れ、t["merge"] と t["outcome"]（できた／保留／失敗のどれか1つ）を決める。
    本体（他セッションと共有の作業ツリー）には、ブラウニーは git で書き込まない（参照と、作業コピー・ブランチの片付けだけ）。"""
    if t.get("status") == "pending":
        return
    if not t.get("branch"):
        m = t.get("merge") or {}
        if m.get("state") == "no_worktree":
            set_outcome(t, "failed", m.get("note", "作業コピーを作れない"))
        elif t.get("outside"):
            hold_outside(t)
        elif t.get("status") == "needs_permission" and m.get("state") == "direct":
            set_outcome(t, "hold", "許可が要る操作の手前で止めた（本流に直接書いた。関門は通していない）", how="許可が要る操作: " + permission_detail(t))
        elif t.get("status") in OK_STATUS and m.get("state") == "direct":
            set_outcome(t, "done", "本流に直接書いた（独立した git リポジトリが無いので、合流の入口と関門は通していない）")
        else:
            set_outcome(t, "failed", status_cause(t))
        return
    root, wt, branch, base = t["repo"], t["worktree"], t["branch"], t["base"]
    m = t["merge"] = {"branch": branch, "base": base, "repo": root}
    # 2026-09-25 エヴァの決裁（案A）: junction・リンクは書きかけのコミットより前に確かめ、見つかったら何もせず残す
    stop = (link_problem(wt) or (None if wt_ok(wt) else NO_GIT)) if os.path.isdir(wt) else None
    if stop:
        m.update(state="danger", note=stop,
                 worktree_kept=stop + "。作業コピーもブランチも消さずに残した（リンクはリンクそのものを rmdir で外してから消す）")
        set_outcome(t, "failed", stop, how=f"⚠ そのまま合流しない。中身を確かめる `{diff_how(t)}`")
        return
    if os.path.isdir(wt) and git(["status", "--porcelain"], wt).stdout.strip():
        git(["add", "-A"], wt)  # ブラウニーの作業コピーは1票1コピー（[E-043追記9] の例外）。wt.py は未コミットを弾くので先に固める
        git(["commit", "-qm", f"night: {t['id']} の書きかけ（司令塔がコミット）"], wt)
    if t.get("outside"):
        hold_outside(t)
        return
    r = git(["rev-list", "--count", f"{base}..{branch}"], root)
    if r.returncode != 0:
        m.update(state="abnormal", note=f"ブランチと本流を比べられなかった: {joined(r)[-200:]}")
        set_outcome(t, "failed", m["note"], how=f"確かめ方 `{diff_how(t)}`")
        return
    if r.stdout.strip() == "0":
        m.update(state="no_changes", note="ブランチに変更が無かった（ブランチは消した）")
        remove_worktree(t)
        if not m.get("worktree_kept"):
            git(["branch", "-d", branch], root)  # 変更が無いので -d で消える。消えなければ残す（③11）
        if t.get("status") == "needs_permission":
            set_outcome(t, "hold", "許可が要る操作の手前で止めた（手前の変更は無かった）", how="許可が要る操作: " + permission_detail(t))
        elif t.get("status") in OK_STATUS:
            set_outcome(t, "failed", f"{t.get('status')} と報告したが、ブランチに変更が無かった（成果物なし）")
        else:
            set_outcome(t, "failed", status_cause(t))
        return
    if t.get("status") not in OK_STATUS:
        m.update(state="not_merged", note=f"{t.get('status')} なので合流しなかった（ブランチは残した）")
        remove_worktree(t)
        set_outcome(t, "failed", status_cause(t), how=f"確かめ方 `{diff_how(t)}`")
        return
    # 合流しないプロジェクト（③8）と、ゲームのプロジェクトの decide（③6）は、ブランチに作るまでで保留
    if project_in(t, NO_MERGE_PROJECTS):
        files = names(["diff", "--name-only", f"{base}...{branch}"], root)
        code = [f for f in files if not f.lower().endswith(".md")]
        m.update(state="held", note="このプロジェクトはブラウニーが合流しない（CLAUDE.md が実装を禁じる／所有者がエヴァでない）。ブランチに残した"
                 + (f"。⚠ 差分に .md 以外が入っている: {'、'.join(code[:8])}" if code else ""))
        set_outcome(t, "hold", m["note"], how=f"中身を確かめる `{diff_how(t)}` ／ 採るなら `{approve_cmd(t)}`")
        return
    if t.get("kind") == "decide" and project_in(t, GAME_PROJECTS):
        m.update(state="held", note="ゲームのプロジェクトの decide（数値・名前などの設計判断）は、推奨案をブランチに作るところまで。合流はエヴァが決める")
        set_outcome(t, "hold", m["note"], how=f"採るなら `{approve_cmd(t)}` ／ 中身 `{diff_how(t)}`")
        return
    call_finish(t)


def call_finish(t, approved=False):
    """wt.py finish を呼び、終了コードで行き先を決める。
    0＝合流した（できた。needs_permission は保留）／10＝本当の衝突 → 解消役を1回だけ起こしてもう一度 finish（それでも駄目なら失敗）
    20＝検査で弾いた（関門が「許可待ち」と言った分は保留、それ以外は失敗）／30＝本体が使用中 → 後で再試行（夜の終わりまで 30 なら保留）
    2・時間切れ・WT-REPORT が読めない＝失敗（本体の状態を確かめる警告つき）"""
    m = t.setdefault("merge", {"branch": t["branch"], "base": t["base"], "repo": t["repo"]})
    root = t["repo"]
    if m.get("tries", 0) >= MAX_FINISH_TRIES:  # ②13
        m.update(state="held", note=f"wt.py finish を {m['tries']} 回呼んでも合流できなかったので、これ以上は呼ばない")
        set_outcome(t, "hold", m["note"], how=f"通すなら `{approve_cmd(t)}`")
        return
    before = git(["rev-parse", "HEAD"], root).stdout.strip()
    tip0 = git(["rev-parse", "--verify", "-q", t["branch"]], root).stdout.strip()
    rep = run_wt_finish(t, approved)
    code, gate = rep.get("code"), rep.get("_gate") or {}
    m["tries"] = m.get("tries", 0) + 1
    for k in ("renumbered", "shifted"):
        if rep.get(k):
            m[k] = rep[k]
    if rep.get("_review"):
        t["review"] = str(rep["_review"].get("line") or rep["_review"].get("verdict") or "")[:300]
    reason = str(rep.get("reason") or "").strip()
    if code == 0 and rep.get("status") != "discarded":
        head = str(rep.get("head") or git(["rev-parse", "HEAD"], root).stdout.strip())
        m.pop("next_at", None)
        m.pop("note", None)
        m.pop("records_held", None)  # 後から入る記録も入った（先に入った成果物のコミットは part_commit に残す）
        m.update(state="merged", commit=head[:9], how=revert_how(root, before, head, tip0))
        if t.get("status") == "needs_permission":
            set_outcome(t, "hold", "許可が要る操作の手前で止めた（手前までの安全な変更は合流済み）",
                        how="許可が要る操作: " + permission_detail(t) + " ／ " + m["how"])
        else:
            set_outcome(t, "done", "合流の入口（wt.py finish）と関門を通って本流へ入った" + (f"（{m['tries']}回目で）" if m["tries"] > 1 else ""),
                        how=m["how"])
        return
    if code == 10:
        if t.get("resolver_used"):
            note = "解消役の後も衝突した（解消役は1票に1回まで）: " + reason[:300]
        elif os.path.exists(STOP):
            note = "衝突したが「ブラウニーを止める」が押されていたので、解消役を起こさなかった: " + reason[:300]
        elif remaining_budget() < 0.5:
            note = "衝突したが、一晩の上限の残りが少なく解消役を起こせなかった: " + reason[:300]
        elif usage_blocked(follow=True):
            if USAGE_WAIT and m.get("limit_tries", 0) < 3:
                m.update(state="busy", why="limit", limit_tries=m.get("limit_tries", 0) + 1,
                         note="衝突したが、利用枠がしきい値以上なので解消役を起こさなかった（枠が戻ってから再試行する）",
                         next_at=time.time() + min(BUSY_RETRY_SEC, 60))
                t.pop("outcome", None)
                log("  衝突したが利用枠がしきい値以上。枠が戻ってから合流し直す")
                return
            # 解消役は1票に1回。断られると分かっているときに使わせない。失敗にもしない（ブランチと作業コピーは残す）
            m.update(state="held", note="衝突したが、利用枠が戻らず解消役を起こせなかった: " + reason[:300])
            set_outcome(t, "hold", m["note"], how=f"利用枠が戻ってから合流し直す。確かめ方 `{diff_how(t)}`")
            return
        else:
            ok, out = resolve_in_worktree(t)
            if t.get("outside"):
                hold_outside(t)
                return
            if ok:
                log("  解消できたので、もう一度 wt.py finish を呼ぶ")
                return call_finish(t, approved)
            md = [ln for ln in out.splitlines() if ln.startswith("merge_md:")]  # ドライバの言い分（落ちた等）は切らずに残す
            note = "衝突を解消役でも解けなかった: " + " ／ ".join(md + out.strip().splitlines()[-3:])[:600]
        m.update(state="conflict", note=note)
        remove_worktree(t)
        set_outcome(t, "failed", note, how=f"確かめ方 `{diff_how(t)}`")
        return
    if code == 20:
        if gate.get("kind") == "hold" and gate.get("usage_limit") and USAGE_WAIT and m.get("limit_tries", 0) < 3:
            # 評価役が利用上限で走れなかっただけ。保留で確定させず、枠が戻ってから合流し直す（1票3回まで。[W-002]）
            m.update(state="busy", why="limit", limit_tries=m.get("limit_tries", 0) + 1,
                     note="評価役が利用上限で走れなかったので、今は合流しなかった（枠が戻ってから再試行する）: " + str(gate.get("reason") or "")[:200],
                     next_at=time.time() + min(BUSY_RETRY_SEC, 60))
            t.pop("outcome", None)
            log("  評価役が利用上限で走れなかった。枠が戻ってから合流し直す")
            return
        if gate.get("kind") == "hold":
            m.update(state="held", note=gate.get("reason") or reason)
            set_outcome(t, "hold", m["note"], how=f"通すなら `{approve_cmd(t, approved=True)}`")
        else:
            m.update(state="check_failed", note=(gate.get("reason") if gate.get("kind") == "fail" else "") or reason or "検査で弾かれた（理由なし）")
            remove_worktree(t)
            set_outcome(t, "failed", "検査で弾かれた: " + m["note"], how=f"確かめ方 `{diff_how(t)}`")
        return
    if code == 30:
        m.pop("why", None)
        if rep.get("status") == "records_held":
            # 塞いでいたのは記録だけ。成果物は本流に入り、記録はブランチに残った（wt.py finish --records-later・[W-004]）
            head = str(rep.get("head") or git(["rev-parse", "HEAD"], root).stdout.strip())
            m.update(records_held=[str(f) for f in rep.get("files") or []], part_commit=head[:9],
                     part_how=revert_how(root, before, head, tip0))
            log(f"  成果は本流に入った（{head[:9]}）。記録（{'、'.join(m['records_held'])}）はブランチに残した")
        note = "本体が使用中だったので、今は合流しなかった（同じ夜のうちに再試行する）"
        if m.get("records_held"):
            note = (f"成果は本流に入った（{m.get('part_commit')}）。記録（{'、'.join(m['records_held'])}）は本体が空いてから入る"
                    "（同じ夜のうちに再試行する）")
        m.update(state="busy", note=note, next_at=time.time() + BUSY_RETRY_SEC)
        t.pop("outcome", None)
        log(f"  本体が使用中（30）。{dur(BUSY_RETRY_SEC)}後に再試行する")
        return
    m.update(state="abnormal", note=f"合流の入口が異常終了した（{code}）: {reason[:400]}")
    set_outcome(t, "failed", m["note"], how=f'⚠ 先に本体の状態を確かめる `git -C "{root}" status` ／ `{diff_how(t)}`')


def save_ticket(t):
    """作業票1枚の状態を tonight.json に書き戻す（他の票・審判官の差し戻しは変えない）。"""
    d = read_json(TONIGHT, {"tickets": []})
    d["tickets"] = [t if x.get("id") == t.get("id") else x for x in d.get("tickets", [])]
    write_json(TONIGHT, d)


def busy_tickets():
    d = read_json(TONIGHT, {})
    if d.get("night") != night_date():
        return []  # 前の夜の票は finalize_previous が締める。今夜の名前で合流させ直さない
    return [t for t in d.get("tickets", []) if (t.get("merge") or {}).get("state") == "busy"]


def next_busy_due():
    due = [t["merge"].get("next_at", 0) for t in busy_tickets()]
    return min(due) if due else None


def ticket_crashed(t, ex):
    """司令塔の中で例外が出たら、その票だけ失敗にして次の票へ進む（1票の不具合で夜全体を止めない）。"""
    log("司令塔の例外（この票だけ失敗にして次へ進む）:\n" + traceback.format_exc())
    t["last_result"] = f"司令塔の例外 {type(ex).__name__}: {ex}"
    if t.get("status") == "pending":
        t["status"] = "error"
    t["no_revive"] = True
    m = t.setdefault("merge", {})
    if m.get("state") == "busy":
        m["state"] = "abnormal"
    set_outcome(t, "failed", f"司令塔の例外で止まった（{type(ex).__name__}: {str(ex)[:200]}）。ブラウニーの不具合なので runner.log を見る")


def review_remembered(t):
    """この票の今の差分に、評価役の結果がもう残っているか（残っていれば、合流の再試行はセッションを起こさない）。"""
    try:
        gd = read_gate(gate_file(t), GATE_KEY)[0] or {}
        rv = gd.get("review") or {}
        if not rv.get("diff") or not t.get("worktree") or not os.path.isdir(t["worktree"]):
            return False
        dh = hashlib.sha1(git(["diff", "-U0", f"{t['base']}...HEAD"], t["worktree"]).stdout.encode()).hexdigest()
        return rv["diff"] == dh or rv.get("count", 1) >= 2
    except Exception:
        return False


def retry_busy(force=False, no_session_only=False):
    """本体が使用中（30）・利用枠待ちで合流できなかった票を、間隔を空けて同じ夜のうちに再試行する。試した枚数を返す。
    no_session_only=True は、利用枠が塞がっている間に呼ぶ形（評価役の結果が残っていて、セッションを起こさずに済む票だけ）。"""
    n = 0
    for t in busy_tickets():
        if os.path.exists(STOP) or night_over():
            break
        if not force and time.time() < t["merge"].get("next_at", 0):
            continue
        if not force and not review_remembered(t):
            if no_session_only or usage_blocked(follow=True):
                continue  # 合流の関門が評価役を起こす票は、利用枠が戻るまで再試行しない（step の先頭が待つ）
        why = "利用枠が戻るのを待たせていた" if t["merge"].get("why") == "limit" else "本体の使用中で待たせていた"
        log(f"↻ {why} {t['id']} をもう一度合流させる（{t['merge'].get('tries', 1)}回試した後）")
        heartbeat(force=True, phase="合流の再試行", ticket=t["id"])
        try:
            call_finish(t)
        except Exception as ex:
            ticket_crashed(t, ex)
        save_ticket(t)
        n += 1
    if n:
        write_report()
    return n


def finalize_night(reason, path=None, previous=False):
    """夜の終わりに、行き先の決まっていない票を1枚も残さない（途中・未着手は失敗、本体の使用中のままは保留）。
    previous=True は前の夜の回の締め（合流待ちも「保留」に確定させ、今夜は触らない）。"""
    path = path or TONIGHT
    d = read_json(path, None)
    if not d or (not previous and d.get("night") != night_date()):
        return
    night = d.get("night") or night_date()
    unstarted = []
    for t in d.get("tickets", []):
        m = t.get("merge") if isinstance(t.get("merge"), dict) else {}  # 旧形式・壊れた票で落ちない
        w = t.get("waiting") if isinstance(t.get("waiting"), dict) else None
        if t.get("status") == "pending" and w and not t.get("sessions") and not t.get("branch"):
            # 着手前の検査で一度も始められなかった票は、失敗ではなく見送りに理由つきで載せる（次の夜の仕分けで拾い直される）
            unstarted.append(t)
            d.setdefault("skipped", []).append({
                "source": t.get("source"), "title": t.get("title"), "ticket": t.get("id"), "why": "main_dirty", "files": w.get("files") or [],
                "reason": "担当ファイルに本体の未コミットの変更が重なっていたので始めなかった（" + "、".join((w.get("files") or [])[:5])
                          + f"。{reason}）。本体をコミットするか退避すると、次の夜の仕分けで拾い直される"})
            log(f"  見送り: {t.get('id')} — 担当ファイルが本体で使用中のまま夜が終わった")
        elif t.get("status") == "pending":
            set_outcome(t, "failed", f"{'途中' if t.get('sessions') else '未着手'}のまま夜が終わった（{reason}）。次の夜の仕分けで拾い直される",
                        night=night)
        elif m.get("state") == "busy":
            how = f"通すなら `{approve_cmd(t)}`"
            if t.get("status") == "needs_permission":
                how = "許可が要る操作: " + permission_detail(t) + " ／ " + how
            cause = "利用枠が戻らなかった" if m.get("why") == "limit" else "本体が使用中だった"
            text = f"夜の終わりまで{cause}ので合流しなかった（{m.get('tries', 1)}回試した。ブランチと作業コピーは残した）"
            if m.get("records_held") and m.get("why") != "limit":
                text = (f"成果は本流に入った（{m.get('part_commit')}）。記録（{'、'.join(m['records_held'])}）は本体が空いてから入る"
                        f"（夜の終わりまで本体が使用中だった。{m.get('tries', 1)}回試した。記録はブランチと作業コピーに残した）")
            set_outcome(t, "hold", text + (f"（{reason}）" if previous else ""), how=how, night=night)
            if previous:
                m["state"] = "held"
        elif not t.get("outcome"):
            raw = t.get("merge")
            ms = raw.get("state") if isinstance(raw, dict) else None
            if ms in ("merged", "no_changes"):
                set_outcome(t, "done", "合流済み（行き先の記録が無かったので、合流の記録から決めた）", night=night)
            elif ms == "direct" and t.get("status") == "done":
                set_outcome(t, "done", "本流に直接書いて終わった（行き先の記録が無かったので、合流の記録から決めた）", night=night)
            elif previous:
                set_outcome(t, "failed", "行き先の記録が無い票（旧形式か強制終了）。ブランチと進捗ファイルを確かめる", night=night)
            else:
                set_outcome(t, "failed", "司令塔が行き先を決められなかった（ブラウニーの不具合。ブランチと進捗ファイルを確かめる）", night=night)
    if unstarted:
        d["tickets"] = [t for t in d["tickets"] if not any(t is u for u in unstarted)]
    write_json(path, d)


def reflow_skip_reason(t):
    """前の夜の合流待ちの票を、起動のはじめに流し直さない理由（None＝流す）。許可が要る票と、本体がまだ塞がっている票は流さない。
    本体の重なりは wt.py を呼ぶ前に見る（新しい回は関門の署名鍵が変わり、呼ぶたびに評価役のセッションが起きるため）。"""
    if t.get("status") == "needs_permission":
        return "許可が要る操作を残した票なので、自動では流さない"
    if t.get("outside"):
        return "作業コピーの外への変更を検出した票なので、自動では流さない"
    if project_in(t, NO_MERGE_PROJECTS) or (t.get("kind") == "decide" and project_in(t, GAME_PROJECTS)):
        return "合流をエヴァが決める票なので、自動では流さない"
    root, branch, base, wt = t.get("repo"), t.get("branch"), t.get("base"), t.get("worktree")
    if not (root and branch and base and wt) or not os.path.isdir(root) or not os.path.isdir(wt):
        return "作業コピーが残っていない"
    if git(["rev-parse", "--verify", "-q", branch], root).returncode != 0:
        return "ブランチが残っていない"
    dirty = {os.path.normcase(d) for d in main_dirty(root)}
    changed = names(["diff", "--name-only", f"{base}...{branch}"], root)
    hit = [f for f in changed if os.path.normcase(f) in dirty]

    def is_record(f):
        return os.path.basename(f).lower() in RECORD_DOCS
    if hit and all(is_record(f) for f in hit) and not all(is_record(f) for f in changed) and records_later_args():
        hit = []  # 塞いでいるのが記録だけなら流す。wt.py が成果物だけ先に本流へ入れる（--records-later・[W-004]）
    if hit:
        return "本体がまだ使用中（" + "、".join(hit[:5]) + (f" ほか {len(hit) - 5} 件" if len(hit) > 5 else "") + "）"
    if usage_blocked(follow=True):
        return "利用枠がしきい値以上で、関門の評価役を起こせない"
    return None


def reflow_previous(d):
    """前の夜に本体の使用中（30）で合流できなかった票を、起動のはじめに1回だけ流し直す（[W-004]）。合流した枚数を返す。
    流すのは許可が要らない票だけ（reflow_skip_reason）。利用枠待ちだった票（why=limit）は対象にしない。
    票は前の夜の名前のまま扱う（台帳の行き先・関門の情報・進捗ファイルは前の夜のもの）。今夜の報告には台帳の reflow の行から載せる。"""
    global NIGHT
    cands = [t for t in d.get("tickets", []) if isinstance(t.get("merge"), dict)
             and t["merge"].get("state") == "busy" and t["merge"].get("why") != "limit"]
    if not REFLOW_PREVIOUS or not cands:
        return 0
    tonight_name, prev = NIGHT, d.get("night") or NIGHT
    log(f"前の夜（{prev}）に本体の使用中で合流できなかった票が {len(cands)} 枚ある。流し直せるかを見る")
    n = 0
    NIGHT = prev
    try:
        for t in cands:
            if os.path.exists(STOP):
                break
            m = t["merge"]
            try:
                why = reflow_skip_reason(t)
            except Exception as ex:  # 見るだけの所で落ちても、起動を止めない（票は今までどおり保留になる）
                why = f"流し直せるかを確かめられなかった（{type(ex).__name__}: {str(ex)[:120]}）"
            if why:
                result = "skipped"
                log(f"  {t.get('id')} は流し直さない: {why}")
            else:
                log(f"↻ 前の夜の {t['id']} を流し直す（{m.get('tries', 1)}回試した後）")
                heartbeat(force=True, phase="前の夜の合流の流し直し", ticket=t["id"])
                try:
                    call_finish(t)
                except Exception as ex:
                    ticket_crashed(t, ex)
                save_ticket(t)
                result = {"merged": "merged", "busy": "busy"}.get(m.get("state"), "other")
                why = "" if result == "merged" else str(m.get("note") or (t.get("outcome") or {}).get("cause") or "")[:300]
                n += result == "merged"
            ledger_add({"at": now().isoformat(timespec="seconds"), "night": tonight_name, "from_night": prev, "kind": "reflow",
                        "ticket": t.get("id"), "title": t.get("title"), "source": t.get("source"), "status": t.get("status"),
                        "result": result, "note": why, "commit": m.get("commit") if result == "merged" else None})
    finally:
        NIGHT = tonight_name
    return n


def finalize_previous():
    """前の夜の回が締めずに終わっていたら（強制終了など）、その票に行き先を付けてから今夜を始める（行き先の無い票を捨てない＝②3）。
    締める前に、本体の使用中で合流できなかった票を1回だけ流し直す（reflow_previous）。"""
    d = read_json(TONIGHT, None)
    if not d or d.get("night") == night_date():
        return
    try:
        reflow_previous(d)
    except Exception:  # 流し直しで落ちても、前の夜の締めと今夜の起動は止めない
        log("前の夜の票の流し直しで例外:\n" + traceback.format_exc())
    d = read_json(TONIGHT, None) or d
    left = [t for t in d.get("tickets", []) if not t.get("outcome") or (t.get("merge") if isinstance(t.get("merge"), dict) else {}).get("state") == "busy"]
    if left:
        log(f"前の夜（{d.get('night')}）の票 {len(left)} 枚に行き先が無かったので、締めてから始める")
        finalize_night("前の夜の回が締めずに終わった", TONIGHT, previous=True)


# ---------------------------------------------------------------- 作業票1枚を1セッションで

def progress_path(t):
    d = os.path.join(STATE, "progress", night_date())
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, re.sub(r"[^A-Za-z0-9_-]", "-", t["id"]) + ".md")


def read_status(path):
    try:
        first = open(path, encoding="utf-8").readline()
    except OSError:
        return None
    m = re.match(r"\s*STATUS:\s*(done|continue|blocked|needs_decision|needs_permission)\b", first)
    return m.group(1) if m else None


def judge_feedback_for(t):
    parts = []
    if t.get("judge_note"):
        parts.append(f"**この作業票は審判官に差し戻された。** 理由: {t['judge_note']}\n"
                     "進捗ファイルの主張をディスクと照合し直し、食い違いを直してから STATUS を付け直すこと。")
    if t.get("retry_note"):
        parts.append(f"**司令塔から:** {t['retry_note']}")
    fb = read_text(FEEDBACK).strip()
    if fb:
        parts.append(fb)
    return "\n\n".join(parts) or "（まだ無い）"


MAX_REVIVE_PER_TICKET = 2  # 打ち切り・エラー・止まった作業票を、その夜のうちに再開する回数の上限（コスパのため）


def revive_leftovers():
    """新しい作業票が無く時間が余っているとき、打ち切り・エラー・止まった作業票を、残したブランチの続きから再開する。
    2026-09-25 エヴァ「次の夜といわずに、その日から時間が余るなら実装に移らせたい」。再開した枚数を返す。
    時間切れ・許可待ち（needs_permission）・作業コピーを作れない票・外への変更を検出した票は再開しない。
    持ち時間は通算のまま、評価役の結果も消さない（差分が変われば関門が1回だけ走り直す＝②10）。"""
    d = read_json(TONIGHT, {"tickets": []})
    n = 0
    for t in d.get("tickets", []):
        if (t.get("status") in ("gave_up", "error", "blocked") and t.get("revived", 0) < MAX_REVIVE_PER_TICKET
                and not t.get("no_revive") and not t.get("outside")
                and t.get("worked_sec", 0) < TICKET_TIMEOUT_SEC):
            prev = t["status"]
            t.update(status="pending", sessions=0, revived=t.get("revived", 0) + 1,
                     retry_note=f"前回は {prev} で終わった。ブランチ `{(t.get('merge') or {}).get('branch') or t.get('branch') or '（新規）'}` と"
                                "進捗ファイルに残した続きから進める。同じ所で止まらないよう、推奨案で進めて done を目指すこと。")
            for k in ("outcome", "resolver_used"):
                t.pop(k, None)
            n += 1
            log(f"↻ 再開: {t['id']}（前回 {prev}・{t['revived']}回目）")
    if n:
        write_json(TONIGHT, d)
    return n


# 作業役に見せない（司令塔の内部状態）
INTERNAL_KEYS = ("status", "sessions", "cost", "judge_note", "judge_reopened", "repo", "base", "branch", "worktree", "merge", "gate",
                 "blocked_retried", "revived", "retry_note", "resolver_used", "reviewed", "review", "outcome", "deadline",
                 "last_result", "last_session", "worked_sec", "outside", "no_revive", "gate_file", "limit_requeued", "waiting")


def work(t, remaining):
    prog = progress_path(t)
    t["sessions"] += 1
    tpl = read_text(os.path.join(PROMPTS, "work.md"))
    ticket_view = {k: v for k, v in t.items() if k not in INTERNAL_KEYS}
    wt = ensure_worktree(t)
    if wt == "cannot":
        # git リポジトリなのに作業コピーを作れない。直接書きには落とさず、セッションも起こさずに失敗にする（③7）
        t.update(status="error", no_revive=True, last_result=t["merge"]["note"])
        finish_ticket(t)
        ledger_add({"at": now().isoformat(timespec="seconds"), "night": night_date(), "kind": "work", "ticket": t["id"],
                    "title": t.get("title"), "source": t.get("source"), "status": "error", "merge": "no_worktree",
                    "session_no": t["sessions"], "cost": 0, "result": t["merge"]["note"][:300]})
        return {"is_error": False, "total_cost_usd": 0, "result": t["merge"]["note"]}, "error"
    if wt:
        ticket_view = remap(ticket_view, *wt)
    no_merge = project_in(t, NO_MERGE_PROJECTS) or (t.get("kind") == "decide" and project_in(t, GAME_PROJECTS))
    note = (f"ブランチ `{t['branch']}` の作業コピー（`{t['worktree']}`）。本流 `{t['base']}`（`{t['repo']}`）へは、"
            "作業票が done か needs_decision で終わったら、司令塔が git 統合の入口（wt.py finish）を通して入れる。"
            "ブランチの切り替え・マージ・本流の直接編集はしない。**HANDOFF.md の §0 は書かない**（書いたブランチは合流の入口に弾かれる。"
            "§0 に載せたいことは、DECISIONS.md の本文に「§0 へ: 〜」の1行で残す）。" if wt
            else "本流に直接書く（このプロジェクトは独立した git リポジトリではない）。")
    if no_merge:
        note += ("\n**この票はブラウニーが合流しない**（実装が禁じられたプロジェクト・所有者が別のプロジェクト・ゲームの設計判断）。"
                 "推奨案はブランチに作るまで。採るかどうかは朝にエヴァが決める。" if wt else
                 "\n**この票は実装しない**（ゲームの設計判断・実装が禁じられたプロジェクトで、ブランチも無い）。選択肢と推奨までを書いて needs_decision で終える。")
    prompt = (tpl.replace("{TICKET}", json.dumps(ticket_view, ensure_ascii=False, indent=1))
                 .replace("{PROGRESS}", prog).replace("{SESSION_NO}", str(t["sessions"]))
                 .replace("{JUDGE_FEEDBACK}", judge_feedback_for(t))
                 .replace("{BRANCH_NOTE}", note))
    cwd = ticket_view.get("project_dir") if os.path.isdir(ticket_view.get("project_dir") or "") else None
    # 書いてよい場所（N3・N4）: 作業コピーがあればその根、無ければ（git の無いプロジェクト）project_dir。
    # project_dir が無い・存在しない票は、C:\Workspace 全体を作業場所にしないよう、セッションを起こさずに失敗にする
    work_root = t.get("worktree") if wt else cwd
    if not cwd or not work_root or same_path(work_root, EVAR):
        msg = "作業場所（project_dir）が無いか存在しない・または Workspace 全体なので、作業させなかった"
        t.update(status="error", no_revive=True, last_result=msg)
        finish_ticket(t)
        ledger_add({"at": now().isoformat(timespec="seconds"), "night": night_date(), "kind": "work", "ticket": t["id"],
                    "title": t.get("title"), "source": t.get("source"), "status": "error", "merge": "no_workdir",
                    "session_no": t["sessions"], "cost": 0, "result": msg})
        return {"is_error": False, "total_cost_usd": 0, "result": msg}, "error"
    cap = min(SESSION_CAP_USD, remaining)
    log(f"▶ {t['id']}（{t.get('kind')}・effort {t.get('effort','medium')}・{t['sessions']}本目）{t.get('title','')}")
    heartbeat(force=True, phase="作業", ticket=t["id"])
    left_sec = TICKET_TIMEOUT_SEC - t.get("worked_sec", 0)  # 1票の持ち時間は、区切り・再開・差し戻しをまたいで通算（②10）
    if left_sec <= 0:
        res = {"is_error": True, "_timeout": True, "total_cost_usd": 0, "_sec": 0,
               "result": f"1票の持ち時間 {dur(TICKET_TIMEOUT_SEC)} を使い切ったので、次のセッションを起こさなかった"}
    else:
        res = watched(t, lambda: run_claude(prompt, cwd, t.get("effort") or "medium", cap,
                                            {"NIGHT_PROGRESS": prog, "NIGHT_KIND": t.get("kind") or "",
                                             "NIGHT_WORK_ROOT": work_root},
                                            deadline=time.time() + left_sec))
    t["worked_sec"] = t.get("worked_sec", 0) + int(res.get("_sec") or 0)
    cost = float(res.get("total_cost_usd") or 0)
    t["cost"] = round(t.get("cost", 0) + cost, 3)
    t["last_result"] = str(res.get("result"))[:300]
    t["last_session"] = res.get("session_id")
    st = read_status(prog)
    if res.get("_timeout"):
        st = "timeout"  # 進捗に何が書いてあっても、固まった票は合流させない（書きかけはブランチに残す）
    elif res.get("is_error") and st is None:
        st = "error"
    elif st is None:
        st = "continue"  # 進捗を書かずに終わった＝もう1本だけ試す
    if st == "continue" and (t["sessions"] >= MAX_SESSIONS_PER_TICKET or t.get("outside")):
        st = "gave_up" if not t.get("outside") else "blocked"
    t["status"] = "pending" if st == "continue" else st
    if (hit_usage_limit(res) and USAGE_WAIT and not res.get("_timeout") and st in ("error", "continue", "gave_up")
            and t.get("limit_requeued", 0) < 3):
        # 上限で断られたセッションは作業していない。失敗にも1本にも数えず、枠が戻ってから同じ票をやり直す（1票3回まで。[W-002]）
        t["limit_requeued"] = t.get("limit_requeued", 0) + 1
        t["sessions"] = max(0, t["sessions"] - 1)
        st, t["status"] = "limit", "pending"
        log("  利用上限で断られたので、この票は枠が戻ってからやり直す")
    t.pop("judge_note", None)
    t.pop("retry_note", None)
    # 2026-09-25 エヴァ「自力で進めない、をなくしたい」。ブランチの上なので、止まったら1回だけ推奨案で進めるよう差し戻す。
    # 越えない線（許可が要る操作）で止まった needs_permission には効かせない（③1）
    if st == "blocked" and not t.get("blocked_retried") and t["sessions"] < MAX_SESSIONS_PER_TICKET and not t.get("outside"):
        t.update(status="pending", blocked_retried=True,
                 retry_note="前のセッションは blocked で止まった（理由は進捗ファイル）。ここはブランチの作業コピーで、朝にエヴァが revert で戻せる。"
                            "判断が要る所は推奨案を選んで進め、選んだ理由と戻し方を「判断した所と理由」に書くこと。"
                            "越えない線（push・配備・リモート操作・設定やフックや権限の変更・削除）に当たったら、"
                            "実行せず、手前までの安全な変更をコミットして `STATUS: needs_permission` で終えること。")
        log(f"  blocked だったので、推奨案で進めるよう1回だけ差し戻す")
    finish_ticket(t)
    ledger_add({"at": now().isoformat(timespec="seconds"), "night": night_date(), "kind": "work",
                "ticket": t["id"], "title": t.get("title"), "source": t.get("source"), "status": st,
                "merge": (t.get("merge") or {}).get("state") if t["status"] != "pending" else None,
                "session_no": t["sessions"], "session_id": res.get("session_id"), "cost": cost, "sec": res.get("_sec"),
                "cost_estimated": res.get("_cost_estimated"), "turns": res.get("num_turns"), "is_error": res.get("is_error"),
                "outside": t.get("outside"), "result": str(res.get("result"))[:300]})
    log(f"  → {st}  ${cost:.2f}" + ("（上限額で数えた）" if res.get("_cost_estimated") else "") + f"  {res.get('_sec')}秒  {res.get('num_turns')}往復")
    heartbeat(force=True, phase="次の作業票へ", ticket=None, session_id=None)
    return res, st


# ---------------------------------------------------------------- 朝の報告
# 1枚目（reports/<夜>.md）は5分で読める形：冒頭から「できたこと」「保留中の許可」「失敗」の3節だけ。
# 長い全文（進捗ファイル全文・審判官の採点・時系列・見送り）は reports/<夜>_詳細.md へ回す。

STATUS_JA = {"needs_permission": "🔒 許可待ち", "needs_decision": "🗳 決裁待ち", "blocked": "⛔ 止まった", "error": "⚠ エラー", "gave_up": "⌛ 打ち切り",
             "timeout": "⏱ 時間切れ", "done": "✅ 完了", "pending": "… 未着手・途中"}
GROUPS = {"needs_permission": "🔒 越えない線の手前で止めた（許可待ち）", "needs_decision": "🗳 決裁待ち（朝に一言で決められる形にしてある）", "blocked": "⛔ 自力で進めなかった",
          "error": "⚠ エラーで止まった", "gave_up": "⌛ 区切りを4回重ねても終わらなかった", "timeout": "⏱ 時間切れで止めた",
          "done": "✅ 完了", "pending": "… 未着手・途中"}
ORDER = {"needs_permission": 0, "needs_decision": 1, "blocked": 2, "error": 3, "gave_up": 4, "timeout": 5, "done": 6, "pending": 7}
MERGE_JA = {"merged": "✅ 合流済み（wt.py finish）", "no_changes": "変更なし（ブランチは消した）", "not_merged": "⏸ 合流せずブランチを残した",
            "conflict": "⚠ 衝突を解けずブランチを残した", "check_failed": "⚠ 検査で弾かれた", "held": "⏸ 保留（エヴァの許可待ち）",
            "busy": "⏸ 本体が使用中で合流を待っている", "abnormal": "⚠ 合流の入口が異常終了", "danger": "⚠ リンクか .git の異常で触らずに残した",
            "direct": "本流に直接書いた", "kept": "⏸ ブランチのまま残した", "no_worktree": "⚠ 作業コピーを作れなかった（直接は書いていない）"}

# wt.py finish（~/.claude/tools/wt.py の rep.out と remove_worktree）と git がよく返す英語の理由 → 朝の報告の1枚目に出す日本語の一言。
# 上から順に置き換え、表に無い英語はそのまま残す。台帳・t["merge"]・全文ファイルには原文を残す（[E-045追記] の判断待ち②）。
# 理由は切り詰められて届くことがある（note は 300〜600 字）ので、末尾の括弧は閉じていなくても拾う。
_PAREN = r"(?:\s*\([^)]*\)?)?"
REASON_JA = [
    # 関門（⑥）
    (r"the gate failed \(exit (-?\d+)\); log: (\S+)", r"関門に落ちた（終了コード \1。記録 \2）"),
    (r"the gate timed out after (\d+)s \(its process tree was killed\); log: (\S+)", r"関門が \1 秒で終わらず、子プロセスごと止めた（時間切れ。記録 \2）"),
    (r"the gate made commits or moved HEAD in the worktree" + _PAREN, "関門が作業コピーでコミットしたか HEAD を動かした（関門は読むだけの約束）"),
    (r"the gate changed tracked files in the worktree" + _PAREN, "関門が作業コピーの追跡中のファイルを書き換えた（関門は読むだけの約束）"),
    # 作業コピーとブランチの検査（20）
    (r"uncommitted tracked changes in (\S+) -- commit them first", r"作業コピー \1 に未コミットの変更がある"),
    (r"(\S+) has uncommitted tracked changes\. Commit them, or ask the human\.", r"作業コピー \1 に未コミットの変更がある（エヴァに聞く）"),
    (r"the branch changed HANDOFF section 0" + _PAREN, "ブランチが HANDOFF の §0 を書いていた（§0 は書かず、DECISIONS に「§0 へ:」で残す）"),
    (r"the worktree has untracked or ignored files where (\S+) brings tracked files" + _PAREN + r"(?:\s*--[^;／]*)?",
     r"作業コピーの追跡外のファイルが、本流 \1 の持ち込むファイルと同じ場所にある（作業コピーの外へ移してから合流し直す）"),
    (r"the merge was refused in the worktree: ", "作業コピーで本流の取り込みが拒否された: "),
    (r"cannot number: ", "仮番号に本番号を振れなかった: "),
    # 本当の衝突（10）
    (r"real conflict while merging (\S+) into the branch" + _PAREN, r"本流 \1 を取り込むと本当の衝突が起きた（作業コピーの中で取り消した。本体は無傷）"),
    # 本体が使用中（30）
    (r"the main checkout is in the middle of (\w+) \(someone else's operation\)", r"本体が他の人の git 操作（\1）の途中だった"),
    (r"something is staged in the main checkout" + _PAREN, "本体に誰かがステージしたものがある（ブランチのものではないので触らない）"),
    (r"the main checkout has uncommitted, untracked or ignored files on paths this merge writes" + _PAREN,
     "本体の、この合流が書き込む場所に他のセッションの書きかけがある（触らずに待つ）"),
    (r"the fast-forward was refused by local changes in the main checkout", "本体の書きかけに阻まれて、早送りできなかった"),
    (r"the main checkout kept moving during (\d+) attempts; run finish again", r"本体が動き続けて、\1 回とも合流できなかった"),
    (r"fast-forward refused: ", "早送りが拒否された: "),
    # 合流の後の片付けと、異常終了（2）
    (r"merged into (\S+) at (\S+), but the cleanup failed: ", r"本流 \1 へは合流した（\2）が、作業コピーの片付けに失敗した: "),
    (r"merged into (\S+) at (\S+), but branch -d failed: ", r"本流 \1 へは合流した（\2）が、ブランチを消せなかった: "),
    (r"branch -d failed: ", "ブランチを消せなかった: "),
    (r"git worktree remove failed: ", "作業コピーを外せなかった（git worktree remove）: "),
    (r"could not move (\S+) \(([^)]*)\)\.", r"追跡外の \1 を 排除/ へ移せなかった（\2）。"),
    (r"Moved so far \((\d+)\) are listed in (\S+?);", r"そこまでに移した \1 件の一覧は \2。"),
    (r"the worktree was NOT removed\.", "作業コピーは外していない。"),
    (r"junction/symlink inside the worktree -- nothing was changed\.[^:]*: ",
     "作業コピーの中にリンク（ジャンクション）があるので何も変えなかった。リンクそのものを rmdir で外してからやり直す: "),
    (r"(\S+) is locked \(([^)]*)\)\. Ask the human\.", r"作業コピー \1 がロックされている（\2。エヴァに聞く）"),
    (r"the worktree (\S+) is not usable: ", r"作業コピー \1 が使えない: "),
    (r'main checkout is on "([^"]*)", not master/main\. Ask the human\.', r"本体が master/main ではなく「\1」にいる（エヴァに聞く）"),
    (r"branch (\S+) not found", r"ブランチ \1 が見つからない"),
    (r"(\S+) is the branch checked out in (\S+)", r"\1 は本体 \2 でチェックアウト中のブランチ"),
    (r"(\S+) has no worktree; the merge of (\S+) into it needs one", r"\1 に作業コピーが無い（本流 \2 を取り込むのに要る）"),
    (r"(\S+) is not the top of a git work tree", r"\1 は git の作業ツリーの根ではない"),
    (r"bad arguments: ", "引数が不正: "),
    # 取り込みで git が返す文（解消役・拒否の理由に混ざる）
    (r"(?:error: )?Your local changes to the following files would be overwritten by merge:", "取り込むと上書きされる書きかけがある:"),
    (r"Please commit your changes or stash them before you merge\.", ""),
    (r"Merge with strategy \w+ failed\.", "取り込みに失敗した。"),
    (r"\bAborting\b", "取り込みを中止した"),
    (r"(?:warning: in the working copy of )?'?[^'\s／]*'?, LF will be replaced by CRLF the next time Git touches it", ""),
]
_REASON_JA = [(re.compile(p), ja) for p, ja in REASON_JA]


def ja_reason(text):
    """朝の報告の1枚目に出す前に、wt.py と git の英語の理由を REASON_JA で日本語の一言に置き換える。"""
    s = str(text or "")
    for rx, ja in _REASON_JA:
        s = rx.sub(ja, s)
    return " ／ ".join(x.strip() for x in s.split("／") if x.strip())  # 置き換えで空になった「／」の区切りを詰める


_started = None


def section(body, name):
    m = re.search(rf"^##\s*{name}[^\n]*\n(.*?)(?=^##\s|\Z)", body, re.S | re.M)
    return m.group(1).strip() if m else ""


def one_liner(t):
    body = read_text(progress_path(t))
    one = section(body, "ひとことで") or section(body, "要約") or ""
    return " ".join(one.split())[:160]


def fmt_item(x):
    return x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)


def write_report(started=None, reason=None):
    """朝の報告の1枚目。上から「できたこと」「保留中の許可」「失敗」。全文は write_details が別ファイルに書く。
    行き先の理由は ja_reason で日本語の一言にして出す（原文は全文ファイルと台帳に残る）。"""
    started = started or _started or now()
    os.makedirs(REPORTS, exist_ok=True)
    data = read_json(TONIGHT, {"tickets": [], "skipped": []})
    tickets = data.get("tickets", [])
    spent = spent_tonight()
    by = {k: [t for t in tickets if (t.get("outcome") or {}).get("kind") == k] for k in ("done", "hold", "failed")}
    running = [t for t in tickets if (t.get("outcome") or {}).get("kind") not in by]
    details = write_details(started, reason)
    judges = [r for r in ledger_rows() if r.get("night") == night_date() and r.get("kind") == "judge"]
    L = [f"# ブラウニーの報告 {night_date()}", "",
         f"- {started:%m/%d %H:%M}〜{now():%m/%d %H:%M}" + (f" ／ 停止理由: **{reason}**" if reason else "（稼働中）")
         + f" ／ API 換算 ${spent:.2f}（上限 ${BUDGET_USD:.0f}）",
         f"- 作業票 {len(tickets)} 枚: できた {len(by['done'])} ／ 保留 {len(by['hold'])} ／ 失敗 {len(by['failed'])}"
         + (f" ／ まだ途中 {len(running)}" if running else ""),
         f"- 全文（進捗ファイル・審判官の採点・時系列）は `{os.path.basename(details)}`"
         + (f"。審判官の最新: {judges[-1].get('verdict')}" if judges else "")]
    holds = [r for r in ledger_rows() if r.get("kind") == "usage_hold" and (r.get("run") == RUN_ID if RUN_ID else r.get("night") == night_date())]
    usage_now = usage_window.label(usage_state(), time.time())
    if usage_now or holds:
        L.append(f"- 利用枠: {usage_now or '（記録なし）'}"
                 + (f" ／ 枠が戻るのを待った回数 {len(holds)}（" + "、".join(
                     f"{str(r.get('at'))[11:16]} {r.get('pct')}% → {str(r.get('until'))[11:16]}" for r in holds[-6:]) + "）" if holds else ""))
    try:
        stale = stale_dirty()
    except Exception:  # 知らせるための検出で、報告そのものを落とさない
        stale = []
        log("置き去りの変更の検出で例外:\n" + traceback.format_exc())
    if stale:
        L += ["", f"## ⚠ 本流に置き去りの未コミットの変更（{len(stale)} リポジトリ）", "",
              f"最後に書かれてから {STALE_DIRTY_HOURS:g} 時間以上たった未コミットの変更が、本流の作業ツリーにある。"
              "**これらのファイルを書く票は、何回待っても合流できずに保留になる。**"
              "中身を見て、コミットするか `排除/` へ退避すると通る。", ""]
        L += stale_dirty_lines(stale)
    L += ["", f"## できたこと（{len(by['done'])}）", ""]
    for i, t in enumerate(by["done"], 1):
        m, o = t.get("merge") or {}, t["outcome"]
        L.append(f"{i}. **{t.get('title')}** `{t['id']}` — {one_liner(t) or ja_reason(o.get('cause'))}")
        extra = []
        if m.get("commit"):
            extra.append(f"本流 `{m['commit']}`")
        if m.get("state") == "direct":
            extra.append("本流に直接書いた（検査なし）")
        if t.get("kind") == "decide" or t.get("status") == "needs_decision":
            extra.append("🗳 推奨案で進めた。残すか戻すかを朝に決める")
        # wt.py の WT-REPORT: renumbered＝仮番号 X-NEW を振った（④）、shifted＝重なった番号をマージドライバが後ろへずらした
        for k, ja in (("renumbered", "仮番号を振った"), ("shifted", "番号がずれた（ほかのファイルに書いた古い番号はエヴァが直す）")):
            if m.get(k):
                extra.append(f"{ja}: " + "、".join(fmt_item(x) for x in m[k][:5]))
        if o.get("how"):
            extra.append(o["how"])
        if extra:
            L.append("   - " + " ／ ".join(extra))
    if not by["done"]:
        L.append("- なし")
    L += ["", f"## 保留中の許可（{len(by['hold'])}）", ""]
    if by["hold"]:
        L += [f"全部通すときの一言: **「{night_date()} の保留 1〜{len(by['hold'])} を全部はい」**"
              "（Claude が下の「通すなら」を番号順に流す。一部だけなら番号で言う）", ""]
        for i, t in enumerate(by["hold"], 1):
            L.append(f"{i}. **{t.get('title')}** `{t['id']}` — {ja_reason(t['outcome'].get('cause'))}")
            if t["outcome"].get("how"):
                L.append(f"   - {t['outcome']['how']}")
    else:
        L.append("- なし")
    L += ["", f"## 失敗（{len(by['failed'])}）", ""]
    for i, t in enumerate(by["failed"], 1):
        L.append(f"{i}. **{t.get('title')}** `{t['id']}` — 原因: {ja_reason(t['outcome'].get('cause'))}")
        if t["outcome"].get("how"):
            L.append(f"   - {t['outcome']['how']}")
        if (t.get("merge") or {}).get("worktree_kept"):
            L.append(f"   - ⚠ {ja_reason(t['merge']['worktree_kept'])}")
    if not by["failed"]:
        L.append("- なし")
    unstarted = [s for s in data.get("skipped", []) if s.get("why") == "main_dirty"]
    if unstarted:
        L += ["", f"## 始めなかった票（{len(unstarted)}。担当ファイルに本体の未コミットの変更が重なっていた）", "",
              "古い版を元に作業して合流できずに終わるのを避けるため、セッションを起こしていない。"
              "本体をコミットするか `排除/` へ退避すると、次の夜の仕分けで拾い直される。", ""]
        L += [f"- {s.get('title')} `{s.get('ticket')}` — " + "、".join(f"`{f}`" for f in (s.get("files") or [])[:5])
              + (f" ほか {len(s['files']) - 5} 件" if len(s.get("files") or []) > 5 else "") for s in unstarted]
    reflow = {r.get("ticket"): r for r in ledger_rows() if r.get("kind") == "reflow" and r.get("night") == night_date()}
    if reflow:
        got = sum(r.get("result") == "merged" for r in reflow.values())
        L += ["", f"## 前の夜から流し直した票（{len(reflow)}。本流に入った {got}）", "",
              "前の夜に本体が使用中で合流できなかった票を、起動のはじめに1回だけ流し直した。"
              "入らなかった票の通し方は、その夜の報告の「保留中の許可」にある。", ""]
        L += [f"- {'✅' if r.get('result') == 'merged' else '⏸'} {r.get('title')} `{r.get('ticket')}`（{r.get('from_night')} の票）— "
              + (f"本流 `{r.get('commit')}` に入った" if r.get("result") == "merged" else f"入らなかった: {ja_reason(r.get('note') or '')}")
              for r in reflow.values()]
    if running:
        L += ["", f"## まだ途中（{len(running)}。夜の終わりに上の3つのどれかへ入る）", ""]
        L += [f"- {t.get('title')} `{t['id']}` — {STATUS_JA.get(t.get('status'), t.get('status'))}"
              + (f"・{MERGE_JA.get((t.get('merge') or {}).get('state'), '')}" if (t.get("merge") or {}).get("state") else "")
              + ("・⏸ 担当ファイルが本体で使用中（" + "、".join((t["waiting"].get("files") or [])[:3]) + "）。空いたら始める"
                 if isinstance(t.get("waiting"), dict) else "")
              for t in running]
    path = os.path.join(REPORTS, f"{night_date()}.md")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")
    return path


def write_details(started, reason=None):
    """報告の全文（一覧 → 番号ずれ → 残した作業コピー → 審判官の採点 → 作業票ごとの全文 → 時系列 → 見送り）。
    作業票ごとの節は進捗ファイルをそのまま載せる（抜粋にすると根拠と企画書が落ちる）。"""
    data = read_json(TONIGHT, {"tickets": [], "skipped": []})
    rows = [r for r in ledger_rows() if r.get("night") == night_date()]
    spent = spent_tonight()
    tickets = sorted(data.get("tickets", []), key=lambda t: (ORDER.get(t.get("status"), 9), t.get("priority", 3)))
    counts = {}
    for t in tickets:
        counts[t.get("status")] = counts.get(t.get("status"), 0) + 1
    L = [f"# ブラウニーの報告 {night_date()}（全文）", "",
         f"- 開始 {started:%m/%d %H:%M} ／ 最終更新 {now():%m/%d %H:%M}" + (f" ／ **停止理由: {reason}**" if reason else "（稼働中）"),
         f"- 使った量: API 換算 **${spent:.2f}** / 上限 ${BUDGET_USD:.0f}（セッション {sum(r.get('kind') in ('triage', 'work', 'judge', 'review', 'resolve') for r in rows)} 本。内訳: 仕分け "
         f"{sum(r.get('kind') == 'triage' for r in rows)}・作業 {sum(r.get('kind') == 'work' for r in rows)}・審判官 "
         f"{sum(r.get('kind') == 'judge' for r in rows)}・評価役 {sum(r.get('kind') == 'review' for r in rows)}・解消役 "
         f"{sum(r.get('kind') == 'resolve' for r in rows)}）",
         "- 作業票: " + (" ／ ".join(f"{STATUS_JA.get(k, k)} {v}" for k, v in sorted(counts.items(), key=lambda kv: ORDER.get(kv[0], 9))) or "なし"),
         "", "## 一覧", "",
         "| 行き先 | 状態 | 作業票 | ひとことで | 本数・費用 |", "|---|---|---|---|---|"]
    for t in tickets:
        one = (one_liner(t) or "（進捗ファイルなし）").replace("|", "｜")
        L.append(f"| {OUTCOME_JA.get((t.get('outcome') or {}).get('kind'), 'まだ途中')} | {STATUS_JA.get(t.get('status'), t.get('status'))} | "
                 f"{t.get('title')} `{t['id']}` | {one} | {t.get('sessions', 0)}本 ${t.get('cost', 0):.2f} |")
    renum = [(t, k, x) for t in tickets for k in ("renumbered", "shifted") for x in ((t.get("merge") or {}).get(k) or [])]
    if renum:
        L += ["", "## 🔢 合流の入口が番号を振った・ずらした票", ""]
        L += [f"- {t.get('title')} `{t['id']}`: {'仮番号を振った' if k == 'renumbered' else '番号がずれた'} {fmt_item(x)}" for t, k, x in renum]
        if any(k == "shifted" for _, k, _ in renum):
            L.append("- ずれたのは DECISIONS の中だけ。ほかのファイルやコミットメッセージに書いた古い番号はエヴァが直す必要がある。")
    kept_wt = [t for t in tickets if (t.get("merge") or {}).get("worktree_kept")]
    if kept_wt:
        L += ["", "## ⚠ 消さずに残した作業コピー", ""]
        L += [f"- {t.get('title')} `{t['id']}`: {t['merge']['worktree_kept']}" for t in kept_wt]
    # 審判官
    judges = [r for r in rows if r.get("kind") == "judge"]
    L += ["", "## ⚖ 審判官の採点（作業役とは別のセッションが judge/criteria.md で採点）", ""]
    if not judges:
        L.append("- まだ走っていない")
    for r in judges:
        L.append(f"- {r.get('at', '')[11:16]} {r.get('judge_no')}回目: **{r.get('verdict')}** ／ ${float(r.get('cost') or 0):.2f} ／ "
                 f"詳細 `{os.path.relpath(r.get('report') or '', HOME)}`")
    if judges:
        v = read_json(os.path.splitext(judges[-1].get("report") or "")[0] + ".json", None)
        if v:
            L += ["", "最新の採点の中身:", ""]
            for it in v.get("items", []):
                L.append(f"- **{it.get('id')}**（{it.get('name', '')}）: {it.get('verdict')} — {str(it.get('note', '')).strip()[:400]}")
            if v.get("reopen"):
                L.append("- 差し戻し: " + " ／ ".join(f"`{x.get('ticket')}` {x.get('reason', '')[:120]}" for x in v["reopen"]))
    # 作業票ごとの全文
    cur = None
    for t in tickets:
        if t.get("status") != cur:
            cur = t.get("status")
            L += ["", f"## {GROUPS.get(cur, cur)}", ""]
        prog = progress_path(t)
        sessions = [r for r in rows if r.get("ticket") == t["id"] and r.get("kind") == "work"]
        o = t.get("outcome") or {}
        L.append(f"### {t.get('title')}  `{t['id']}`")
        L.append(f"- 行き先: **{OUTCOME_JA.get(o.get('kind'), 'まだ途中')}** — {o.get('cause', '')}")
        L.append(f"- 種別 {t.get('kind')} ／ プロジェクト `{os.path.relpath(t.get('project_dir') or EVAR, EVAR)}` ／ 出典 `{t.get('source')}`")
        L.append(f"- 作業票の目標: {t.get('goal', '')}")
        L.append(f"- 完了判定: {t.get('done_check', '')}")
        L.append(f"- 仕分けが選んだ理由: {t.get('why', '')}")
        L.append(f"- {t.get('sessions', 0)}本 ${t.get('cost', 0):.2f} ／ 進捗ファイル `{prog}`"
                 + (" ／ ⚖ 審判官に差し戻された" if t.get("judge_reopened") else ""))
        m = t.get("merge") or {}
        if m:
            L.append(f"- ブランチ: {MERGE_JA.get(m.get('state'), m.get('state'))}" + (f" `{m.get('branch')}`" if m.get("branch") else "")
                     + (f" → 本流 `{m['commit']}`" if m.get("commit") else "")
                     + (f"（wt.py finish {m['tries']}回）" if m.get("tries") else "")
                     + (f" ／ {m['note']}" if m.get("note") else ""))
            if o.get("how"):
                L.append(f"  - {o['how']}")
            if t.get("review"):
                L.append(f"  - 評価役: {t['review']}")
            if m.get("worktree_kept"):
                L.append(f"  - ⚠ {m['worktree_kept']}")
            if m.get("stashed"):
                L.append(f"  - 作業コピーを外す前に、追跡外のファイルを `{m['stashed']}` へ退避した")
            if t.get("outside"):
                L.append("  - ⚠ 作業コピーの外への変更: " + " ／ ".join(t["outside"][:5]))
        for r in sessions:
            L.append(f"  - {r.get('session_no')}本目 {r.get('at', '')[11:16]} → {r.get('status')}（{r.get('sec')}秒・{r.get('turns')}往復・"
                     f"${float(r.get('cost') or 0):.2f}）会話記録ID `{r.get('session_id')}`")
        body = read_text(prog)
        if body:
            body = re.sub(r"^STATUS:[^\n]*\n", "", body)
            body = re.sub(r"^## ", "#### ", body, flags=re.M)
            L += ["", body.strip()]
        L.append("")
    # 時系列
    L += ["", "## 時系列（台帳から）", ""]
    for r in rows:
        what = {"triage": "仕分け", "work": f"作業 {r.get('ticket')}", "judge": f"審判官 {r.get('judge_no')}回目",
                "review": f"評価役 {r.get('ticket')}", "resolve": f"解消役 {r.get('ticket')}",
                "finish": f"wt.py finish {r.get('ticket')}", "outcome": f"行き先 {r.get('ticket')}"}.get(r.get("kind"), r.get("kind"))
        res = (r.get("outcome") or r.get("status") or r.get("verdict") or ("エラー" if r.get("is_error") else "OK"))
        if r.get("kind") == "finish":
            res = f"{r.get('code')} {r.get('status')}"
        L.append(f"- {r.get('at', '')[11:19]} {what} → {res}"
                 + (f"（{r.get('sec')}秒・${float(r.get('cost') or 0):.2f}）" if r.get("kind") not in ("outcome", "finish") else ""))
    if data.get("skipped"):
        L += ["", "## 見送り（仕分けで対象外にしたもの）", ""]
        L += [f"- {s.get('title')}（`{s.get('source')}`）— {s.get('reason')}" for s in data["skipped"]]
    path = os.path.join(REPORTS, f"{night_date()}_詳細.md")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")
    return path


# ---------------------------------------------------------------- 本体

def sleep_watch(sec):
    """待機中も審判官の時刻と STOP・夜の終わりを見る。"""
    end = time.time() + sec
    while time.time() < end and not os.path.exists(STOP) and not night_over():
        judge_tick()
        usage_tick()
        heartbeat()
        time.sleep(min(5, max(0.2, end - time.time())))


def compute_end(started):
    """夜の終わり＝起動から MAX_HOURS 時間と、次の END_CLOCK の早い方（②2）。どちらも無効（既定）なら None＝止めるまで。"""
    ends = []
    if MAX_HOURS > 0:
        ends.append(started + datetime.timedelta(hours=MAX_HOURS))
    if END_CLOCK:
        hh, mm = (int(x) for x in END_CLOCK.split(":"))
        e = started.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if e <= started:
            e += datetime.timedelta(days=1)
        ends.append(e)
    return min(ends).timestamp() if ends else None


def preflight():
    """始めてよいかを確かめる。だめなら理由をログに書いて False。"""
    if production() and selected_projects() is None:
        log("進めるプロジェクトが選ばれていないので始めない（projects.txt に有効な行が無い。「ブラウニーを始める」の選択画面で選ぶ）")
        toast("ブラウニーを始めませんでした", "進めるプロジェクトが選ばれていません。「ブラウニーを始める」で番号を選んでください")
        return False
    # フックの python が見つからないとフックは素通りになる（exit 2 以外＝通す）。見つからなければ始めない。
    hook_cmds = [h["command"] for ev in read_json(SETTINGS, {}).get("hooks", {}).values() for m in ev for h in m["hooks"]]
    exes = [re.match(r'\s*"([^"]+)"', c) for c in hook_cmds]
    if not hook_cmds or not all(m and os.path.isfile(m.group(1)) for m in exes):
        log(f"ブラウニーフックの python が見つからないので始めない: {hook_cmds}")
        return False
    for need in ((CRITERIA, os.path.join(PROMPTS, "judge.md")) if JUDGE_INTERVAL_SEC > 0 else ()):
        if not os.path.isfile(need):
            log(f"審判官の基準か指示が無いので始めない: {need}")
            return False
    # 合流の入口が使えないと、全部の票が合流できずに作業コピーだけが溜まる（空き容量を食う）。使えなければ始めない
    if not os.path.isfile(WT_PY) or not wt_usable():
        log(f"合流の入口 {WT_PY} が `finish --gate` `--gate-timeout` に対応していないので始めない（[E-045] の wt.py が入るまで待つ。"
            "試験のときは NIGHT_WT_PY で差し替える）")
        toast("ブラウニーを始めませんでした", "合流の入口 wt.py が finish --gate に対応していません。BrownieProject/state/runner.log を見てください")
        return False
    disk = disk_problem()
    if disk:
        log(f"{disk}ので始めない（NIGHT_MIN_FREE_PCT・NIGHT_MIN_FREE_GB で変えられる）")
        toast("ブラウニーを始めませんでした", disk)
        return False
    if production() and os.environ.get("NIGHT_SKIP_SELFTEST") != "1":
        log("自己試験（偽の claude・トークン0）を流してから始める")
        # パイプではなくファイルで受け、固まったら子孫ごと止める（②7）
        r = run_capture([sys.executable, os.path.join(ROOT, "tests", "selftest.py")], timeout=900,
                        env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"))
        for ln in r.stdout.strip().splitlines():
            log("  " + ln)
        if r.returncode != 0:
            toast("ブラウニーを始めませんでした", "自己試験に落ちました。BrownieProject/state/runner.log を見てください")
            log("自己試験に落ちたので、本番を始めない" + ("（時間切れ）" if getattr(r, "timed_out", False) else ""))
            return False
    return True


def step(st, started):
    """ブラウニーの1歩。止めるときは理由を返す（続けるなら None）。"""
    if os.path.exists(STOP):
        return "「ブラウニーを止める」が押された"
    if night_over():
        return f"夜の終わり（{datetime.datetime.fromtimestamp(END_TS):%m/%d %H:%M}）に達した"
    if BUDGET_USD - spent_tonight() < 1.0:
        return f"一晩の上限 ${BUDGET_USD:.0f} に達した（残り $1 未満）"
    disk = disk_problem()
    if disk:
        return disk
    if _limit["hit"] or usage_over():
        retry_busy(no_session_only=bool(_limit["hit"] or usage_over(follow=True)))  # 待つ前に、セッションの要らない合流の再試行だけは進める（本体が空いた票を夜の終わりまで待たせない）
    gate = usage_gate()  # 利用枠がしきい値以上・上限に当たった後は、ここで枠が戻るまで待つ（[W-002]）
    if gate:
        return None if gate == "waited" else gate
    if END_TS is None:
        # 夜の終わりが無いと何日も走るので、暦の日付が変わったら仕分けの回数を戻す（夜の名前・予算の単位は変えない）
        day = calendar_night()
        if st.setdefault("day", day) != day:
            log(f"日付が変わった（{st['day']} → {day}）ので、仕分けの回数を戻して仕分けし直す")
            st.update(day=day, triaged=0, last_hash=None)
    judge_tick()
    if retry_busy():
        return None  # 本体の使用中（30）で待たせていた票の再試行（間隔が来たものだけ）
    data = read_json(TONIGHT, None)
    fresh = data is not None and data.get("night") == night_date()
    todo = [t for t in (data or {}).get("tickets", []) if t.get("status") == "pending"] if fresh else []
    if not todo:
        h = sources_hash()
        if st["triaged"] >= MAX_TRIAGE_PER_NIGHT or h == st["last_hash"]:
            if revive_leftovers():
                return None  # 新しい作業票が無く時間が余っているので、残り物をブランチの続きから再開する
            write_report(started)
            due = next_busy_due()
            wait = IDLE_POLL_SEC if due is None else max(1, min(IDLE_POLL_SEC, int(due - time.time()) + 1))
            log(f"作業票が尽きた。元の文書が変わるまで待機（{dur(IDLE_POLL_SEC)}おきに更新時刻だけ確認・トークン0）"
                + (f"。本体の使用中で合流を待たせている票 {len(busy_tickets())} 枚は {dur(wait)}後に再試行" if due is not None else ""))
            heartbeat(force=True, phase="待機（作業票が尽きた）", ticket=None, session_id=None)
            sleep_watch(wait)
            if st["triaged"] >= MAX_TRIAGE_PER_NIGHT and sources_hash() != h:
                log(f"仕分けは今夜 {MAX_TRIAGE_PER_NIGHT} 回に達したので、再仕分けはしない")
            st["last_hash"] = h if st["triaged"] >= MAX_TRIAGE_PER_NIGHT else st["last_hash"]
            return None
        gate = usage_gate(refresh=True)
        if gate:
            return None if gate == "waited" else gate
        res, tickets = triage()
        if hit_usage_limit(res) and st.get("triage_limited", 0) < 3:
            # 上限で断られた仕分けは回数に数えない（数えると、4回断られただけでその夜は仕分けをしなくなる）。数えないのは3回まで
            st["triage_limited"] = st.get("triage_limited", 0) + 1
            _limit["hit"] = True
            return None  # last_hash を進めない＝次の step の先頭（usage_gate）で枠が戻るのを待ってから、仕分けをやり直す
        st["triaged"] += 1
        if hit_usage_limit(res):
            _limit["hit"] = True
            return None
        st["last_hash"] = sources_hash()
        d = read_json(TONIGHT, {"tickets": []})
        d["night"] = night_date()
        write_json(TONIGHT, d)
        return None
    if remaining_budget() < 1.0:
        sleep_watch(30)  # 走っている審判官の上限ぶんを空けて待つ（②11）
        return None
    gate = usage_gate(refresh=True)
    if gate:
        return None if gate == "waited" else gate
    todo.sort(key=lambda t: (t.get("priority", 3), t.get("sessions", 0)))
    t = pick_startable(todo)
    if t is None:
        # 残りの票は全部、担当ファイルが本体で使用中（着手前の検査・[W-004]）。セッションを起こさず、空くのを待つ
        ids = sorted(x["id"] for x in todo)
        if st.get("precheck_wait") != ids:
            st["precheck_wait"] = ids
            log(f"始められる票が無い（{len(ids)} 枚とも担当ファイルが本体で使用中）。{dur(PRECHECK_POLL_SEC)}おきに本体を見直す（トークン0）")
            write_report(started)
        due = next_busy_due()
        heartbeat(force=True, phase="待機（担当ファイルが本体で使用中）", ticket=None, session_id=None)
        sleep_watch(PRECHECK_POLL_SEC if due is None else max(1, min(PRECHECK_POLL_SEC, int(due - time.time()) + 1)))
        return None
    st.pop("precheck_wait", None)
    try:
        res, stt = work(t, remaining_budget())
    except Exception as ex:  # 1票の不具合で夜全体を止めない
        ticket_crashed(t, ex)
        res, stt = {"is_error": False, "total_cost_usd": 0, "result": ""}, "error"
    # 作業票の状態を書き戻す（仕分け結果の他の票・審判官の差し戻しは変えない）
    save_ticket(t)
    write_report(started)
    if hit_usage_limit(res):
        _limit["hit"] = True
        return None  # 次の step の先頭（usage_gate）で、枠が戻るのを待つか夜を終えるかを決める
    if overloaded(res):
        log("API が混雑している。10分待ってから続ける")
        sleep_watch(600)
    return None


MAX_CONSECUTIVE_ERRORS = 3


def main():
    os.makedirs(STATE, exist_ok=True)
    # ロックは自己試験より前に取る（2本同時に起動したとき、両方が自己試験を流してから走り出さないように＝②17）
    if os.path.exists(LOCK):
        try:
            pid = int(open(LOCK).read().strip())
            if pid_alive(pid):
                log(f"ブラウニーはすでに動いています（PID {pid}）。止めるなら「ブラウニーを止める」を押してください。")
                return
        except ValueError:
            pass
    open(LOCK, "w").write(str(os.getpid()))
    try:
        if not preflight():
            return
        run_night()
    finally:
        try:
            if int(open(LOCK).read().strip()) == os.getpid():
                os.remove(LOCK)
        except (OSError, ValueError):
            pass


def run_night():
    global _started, NIGHT, RUN_ID, END_TS, GATE_KEY
    if os.path.exists(STOP):
        os.remove(STOP)
    if not os.path.exists(INBOX):
        open(INBOX, "w", encoding="utf-8").write("# ブラウニーの受付箱\n\nブラウニーに回したいことを1行ずつ。処理されたら仕分け役が作業票にする（この行は消さなくてよい）。\n\n")
    started = _started = now()
    NIGHT = os.environ.get("NIGHT_LABEL") or calendar_night()  # 夜の名前は起動時に1回だけ決める（②3）
    RUN_ID = f"{started:%Y%m%dT%H%M%S}-{os.getpid()}"         # 費用はこの夜の回で数える（②2）
    END_TS = compute_end(started)
    GATE_KEY = secrets.token_bytes(32)                          # 関門の情報ファイルの署名鍵（③4）。セッションには渡さない
    finalize_previous()
    keep_awake(True)
    toast("ブラウニーを開始しました", f"上限 ${BUDGET_USD:.0f}。止めるときは「ブラウニーを止める」")
    log(f"ブラウニーを開始（PID {os.getpid()}・夜 {NIGHT}・回 {RUN_ID}）。上限 API換算 ${BUDGET_USD:.0f}、"
        f"区切り 文脈{int(CTX_LIMIT):,}、権限 {PERMISSION_MODE}、ガード {os.environ.get('NIGHT_GUARD', 'off')}、"
        + (f"審判官 {JUDGE_INTERVAL_SEC // 60}分ごと" if JUDGE_INTERVAL_SEC > 0 else "審判官なし")
        + f"、1票の持ち時間 {dur(TICKET_TIMEOUT_SEC)}、合流の入口 {WT_PY}"
        + (f"、夜の終わり {datetime.datetime.fromtimestamp(END_TS):%m/%d %H:%M}" if END_TS else "、夜の終わり なし（止めるまで）")
        + (f"、モデル {MODEL}" if MODEL else "") + ("、偽の claude" if CLAUDE.endswith(".py") else ""))
    log(f"利用枠: 5時間枠 {USAGE_WARN_PCT:g}% で知らせ、{USAGE_HOLD_PCT:g}%（7日枠は {USAGE_WEEK_HOLD_PCT:g}%）で新しいセッションを始めるのをやめて"
        + ("枠が戻るまで待つ" if USAGE_WAIT else "夜を終える")
        + (f"。いま {usage_window.label(usage_state(), time.time())}" if usage_window.live(usage_state(), time.time()) else ""))
    heartbeat(force=True, phase="起動")
    try:
        stale = stale_dirty()
    except Exception:  # 知らせるための検出で、夜を止めない
        stale = []
        log("置き去りの変更の検出で例外:\n" + traceback.format_exc())
    if stale:
        n = sum(len(files) for _, files in stale)
        log(f"⚠ 本流に置き去りの未コミットの変更が {n} 件ある（{STALE_DIRTY_HOURS:g} 時間以上前）。"
            "これらのファイルを書く票は合流できずに保留になる。コミットするか 排除/ へ退避すると通る")
        for ln in stale_dirty_lines(stale):
            log("  " + ln)
        toast("本流に置き去りの変更があります",
              "、".join(os.path.basename(root) for root, _ in stale) + f" に {n} 件。ここを書く票は合流できません。報告の先頭を見てください")
    judge_tick()  # 起動時の審判官（進んだ作業も pending も無ければ、条件を満たすまで起こさない）
    reason, st, errors = None, {"triaged": 0, "last_hash": None}, 0
    try:
        while True:
            try:
                reason = step(st, started)
                errors = 0
            except Exception:
                # 仕分け・報告・再開・保存などの例外で夜全体を終えない。続けて落ちるなら夜を終える（②8）
                errors += 1
                log(f"司令塔の例外（{errors}回続いた）:\n" + traceback.format_exc())
                if errors >= MAX_CONSECUTIVE_ERRORS:
                    reason = f"司令塔の例外が {MAX_CONSECUTIVE_ERRORS} 回続いた（runner.log を見る）"
                else:
                    sleep_watch(10)
            if reason:
                break
    except KeyboardInterrupt:
        reason = "ウィンドウで中断された"
    finally:
        keep_awake(False)
        # 走っている審判官は、STOP のときは最後まで待つ（最長15分）。夜の終わり・中断・例外の連続のときは止める（②2・②5）
        if _judge["h"] is not None:
            if reason and reason.startswith("「ブラウニーを止める」"):
                log("審判官の採点が終わるのを待ってから終了する（最長15分）")
                end = time.time() + 900
                while _judge["h"]["p"].poll() is None and time.time() < end:
                    time.sleep(5)
            if _judge["h"]["p"].poll() is None:
                log("審判官を止める")
                kill_tree(_judge["h"]["p"])
            try:
                judge_collect()
            except Exception:
                log("審判官の取り込みで例外:\n" + traceback.format_exc())
        try:
            finalize_night(reason or "終了")  # 行き先の決まっていない票を残さない
        except Exception:
            log("夜の締めで例外:\n" + traceback.format_exc())
        heartbeat(force=True, phase=f"終了（{reason}）", ticket=None, session_id=None)
        try:
            path = write_report(started, reason or "終了")
        except Exception:
            path = "（報告を書けなかった）"
            log("報告を書くときに例外:\n" + traceback.format_exc())
        log(f"ブラウニーを終了: {reason}。報告 {path}")
        toast("ブラウニーを終了しました", f"{reason}。${spent_tonight():.2f} 使用。報告: BrownieProject/reports/{night_date()}.md")


if __name__ == "__main__":
    main()
