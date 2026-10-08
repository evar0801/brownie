"""ブラウニーの砂場試験を1回まわす。出力は BrownieProject/sandbox/<日付_時刻>_<名前>/ に残す（あとで報告を機械的に比べるため）。

    python tests/sandbox.py <名前> [--fake] [--model claude-haiku-4-5-20251001] [--budget 8] [--ctx 150000]
                                  [--judge 180|0] [--max-triage 1] [--stop-after-idle]

安く試す順: まず --fake（偽の claude・トークン0）で配管を確かめ、指示文を変えたときだけ本物を --model で安いモデルに下げて1本。

中身: tests/fixtures/ProjA を evar/ProjA に複製して git init → NIGHT_EVAR_ROOT / NIGHT_HOME をそこへ向けて runner.py を起動。
合流の入口は、既定で wt.py のスタブ（tests/wt_stub.py）。本物で試すなら --wt ~/.claude/tools/wt.py（または NIGHT_SANDBOX_WT_PY）。
--stop-after-idle を付けると、作業票が尽きて待機に入った時点で STOP を置き、停止まで見届けて終わる。
本番の仕分けは BrownieProject/sandbox を走査しない（runner.py の source_files）。
"""
import argparse, datetime, os, shutil, subprocess, sys, time
sys.stdout.reconfigure(encoding="utf-8")

NIGHT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(NIGHT, "tests", "fixtures", "ProjA")

ap = argparse.ArgumentParser()
ap.add_argument("name")
ap.add_argument("--budget", default="8")
ap.add_argument("--ctx", default="150000")
ap.add_argument("--judge", default="7200", help="審判官の間隔（秒）。0 で審判官なし")
ap.add_argument("--fake", action="store_true", help="偽の claude（tests/fake_claude.py）で配管だけ試す。トークン0")
ap.add_argument("--model", default="", help="本物を使うときのモデル（例 claude-haiku-4-5-20251001）")
ap.add_argument("--max-triage", default="4")
ap.add_argument("--stop-after-idle", action="store_true")
ap.add_argument("--wt", default="", help="合流の入口 wt.py のパス。既定は NIGHT_SANDBOX_WT_PY、無ければスタブ（tests/wt_stub.py）")
a = ap.parse_args()
# 本物の wt.py（~/.claude/tools/wt.py）で試すときは --wt か NIGHT_SANDBOX_WT_PY で渡す。既定はスタブ（本物の作りかけに左右されない）
WT = a.wt or os.environ.get("NIGHT_SANDBOX_WT_PY") or os.path.join(NIGHT, "tests", "wt_stub.py")

run = os.path.join(NIGHT, "sandbox", f"{datetime.datetime.now():%Y-%m-%d_%H%M%S}_{a.name}")
proj = os.path.join(run, "evar", "ProjA")
home = os.path.join(run, "home")
shutil.copytree(FIXTURE, proj)
# 題材の vendor/argparse.py は同梱しない（CPython 本体のファイル）。手元の Python から複写する
_vendor = os.path.join(proj, "vendor", "argparse.py")
if not os.path.isfile(_vendor):
    os.makedirs(os.path.dirname(_vendor), exist_ok=True)
    shutil.copyfile(argparse.__file__, _vendor)
os.makedirs(home)
for c in (["git", "init", "-q"], ["git", "config", "user.name", "night-sandbox"], ["git", "config", "user.email", "sandbox@local"],
          ["git", "add", "-A"], ["git", "commit", "-qm", "init"]):
    subprocess.run(c, cwd=proj, check=True, capture_output=True)
# 本番と同じく統治文書のマージドライバ（merge_md.py）を登録する。有れば、衝突の試験はドライバ越しの衝突になる
# 本物の wt.py を試すときは、その隣の install_md_merge.py（＝同じ版の merge_md.py）を使う
INSTALL = os.path.join(os.path.dirname(os.path.abspath(WT)), "install_md_merge.py")
if not os.path.isfile(INSTALL):
    INSTALL = os.path.join(os.path.expanduser("~"), ".claude", "tools", "install_md_merge.py")
if os.path.isfile(INSTALL):
    # install_md_merge.py は .claude の下を飛ばす（作業コピーの中で流すとき砂場がそこに入る）ので、同じ設定を同じ関数で書く
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location("install_md_merge", INSTALL)
    imm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(imm)
    for k, v in (("merge.evarmd.name", "Workspace governance docs"), ("merge.evarmd.driver", f'python "{imm.TOOL}" %O %A %B %P'),
                 ("merge.evarmd.recursive", "text")):
        subprocess.run(["git", "config", k, v], cwd=proj, check=True, capture_output=True)
    info = pathlib.Path(proj, ".git", "info")
    info.mkdir(exist_ok=True)
    imm.rewrite(info / "attributes", [f"{f} merge=evarmd" for f in imm.ATTRS])
    imm.rewrite(info / "exclude", imm.EXCLUDES)
print(f"砂場: {run}", flush=True)

env = dict(os.environ, NIGHT_EVAR_ROOT=os.path.join(run, "evar"), NIGHT_HOME=home, NIGHT_BUDGET_USD=a.budget,
           NIGHT_CTX_LIMIT=a.ctx, NIGHT_JUDGE_INTERVAL_SEC=a.judge, NIGHT_MAX_TRIAGE=a.max_triage, NIGHT_MODEL=a.model, PYTHONUTF8="1",
           NIGHT_WT_PY=WT, NIGHT_WATCH_HOME=os.path.join(run, "watch_home"),  # 作業コピーの外の見張りは砂場の偽の家に向ける
           NIGHT_MIN_FREE_PCT=os.environ.get("NIGHT_MIN_FREE_PCT", "0"), NIGHT_MIN_FREE_GB=os.environ.get("NIGHT_MIN_FREE_GB", "0"))
for k in ("NIGHT_SELF_REPO", "WT_STUB_SCRIPT", "WT_STUB_LOG", "WT_PROJECT", "WT_WORKTREE", "WT_BRANCH", "WT_BASE"):
    env.pop(k, None)  # 関門の中から流したときに、外側の試験や wt.py の値を持ち込まない
if a.fake:
    env["NIGHT_CLAUDE_BIN"] = os.path.join(NIGHT, "tests", "fake_claude.py")
console = open(os.path.join(run, "console.log"), "w", encoding="utf-8")
p = subprocess.Popen([sys.executable, "-u", os.path.join(NIGHT, "runner.py")], env=env, stdout=console, stderr=subprocess.STDOUT)
runlog = os.path.join(home, "state", "runner.log")
stopped = False
while p.poll() is None:
    time.sleep(5)
    if a.stop_after_idle and not stopped and "作業票が尽きた" in (open(runlog, encoding="utf-8").read() if os.path.exists(runlog) else ""):
        open(os.path.join(home, "state", "STOP"), "w").close()
        stopped = True
        print("作業票が尽きたので STOP を置いた", flush=True)
console.close()
print(f"runner 終了（exit {p.returncode}）。報告: {os.path.join(home, 'reports')}", flush=True)
