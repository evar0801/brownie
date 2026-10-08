"""本流に置き去りの未コミットの変更の検出（runner.stale_dirty）の試験。トークン0。使い捨てのリポジトリを %TEMP% の下に作る。

    python tests/test_stale_dirty.py     # 終了コード 0＝合格、1＝不合格

1 きれいな本流 → 何も出ない・報告に節が無い
2 古い変更（3日前）→ 出る ／ 今書いたばかりの変更 → 出ない（別のセッションが作業中かもしれない）
3 追跡していない古いファイル → 出ない ／ 選んでいないプロジェクトの古い変更 → 出ない
4 消されたままのファイル → 古い変更があるリポジトリでだけ一緒に出る
5 日本語のファイル名が割れない ／ 報告の先頭（「できたこと」より前）に節が出る
6 NIGHT_STALE_DIRTY_HOURS=0 → 見ない
"""
import datetime, importlib.util, os, shutil, subprocess, sys, tempfile, time

sys.stdout.reconfigure(encoding="utf-8")
NIGHT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="night_stale_dirty_")
EVAR = os.path.join(TMP, "evar")
HOME = os.path.join(TMP, "home")
os.makedirs(EVAR)
os.makedirs(HOME)
os.environ.update(NIGHT_EVAR_ROOT=EVAR, NIGHT_HOME=HOME, NIGHT_JUDGE_INTERVAL_SEC="0", PYTHONUTF8="1",
                  NIGHT_WT_PY=os.path.join(NIGHT, "tests", "wt_stub.py"), NIGHT_STALE_DIRTY_HOURS="24",
                  NIGHT_MIN_FREE_PCT="0", NIGHT_MIN_FREE_GB="0")
for k in ("NIGHT_SELF_REPO", "NIGHT_MODEL", "NIGHT_LABEL"):
    os.environ.pop(k, None)

spec = importlib.util.spec_from_file_location("runner", os.path.join(NIGHT, "runner.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
fails = []


def check(ok, what):
    print(("  ok   " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        fails.append(what)


def sh(args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def age(path, days):
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def make_repo(name, files):
    root = os.path.join(EVAR, name)
    for rel, text in files.items():
        write(os.path.join(root, rel), text)
    sh(["git", "init", "-q", "-b", "master"], root)
    sh(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "add", "-A"], root)
    sh(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "init"], root)
    return root


def found():
    return {os.path.basename(root): [rel for rel, _ in files] for root, files in runner.stale_dirty()}


def report():
    return open(runner.write_report(datetime.datetime.now(), None), encoding="utf-8").read()


try:
    a = make_repo("ProjA", {"a.txt": "1\n", "docs/資料/一覧 表.md": "1\n", "gone.txt": "1\n", "fresh.txt": "1\n"})
    b = make_repo("ProjB", {"b.txt": "1\n", "gone.txt": "1\n"})
    c = make_repo("ProjC", {"c.txt": "1\n"})
    write(os.path.join(HOME, "projects.txt"), "ProjA\nProjB\n# ProjC\n")

    check(found() == {}, "1 きれいな本流では何も出ない")
    check("置き去り" not in report(), "1 きれいな本流では報告に節が無い")

    write(os.path.join(a, "a.txt"), "2\n")
    age(os.path.join(a, "a.txt"), 3)
    write(os.path.join(a, "fresh.txt"), "2\n")
    check(found() == {"ProjA": ["a.txt"]}, f"2 3日前の変更は出て、今書いた変更は出ない（{found()}）")

    write(os.path.join(a, "untracked_old.txt"), "x\n")
    age(os.path.join(a, "untracked_old.txt"), 9)
    write(os.path.join(c, "c.txt"), "2\n")
    age(os.path.join(c, "c.txt"), 9)
    check(found() == {"ProjA": ["a.txt"]}, f"3 追跡していない古いファイルと、選んでいないプロジェクトは出ない（{found()}）")

    os.remove(os.path.join(a, "gone.txt"))
    os.remove(os.path.join(b, "gone.txt"))
    check(found() == {"ProjA": ["a.txt", "gone.txt"]}, f"4 消されたままは、古い変更があるリポジトリでだけ出る（{found()}）")

    write(os.path.join(a, "docs", "資料", "一覧 表.md"), "2\n")
    age(os.path.join(a, "docs", "資料", "一覧 表.md"), 6)
    check(found().get("ProjA") == ["docs/資料/一覧 表.md", "a.txt", "gone.txt"], f"5 日本語と空白の名前が割れず、古い順に並ぶ（{found()}）")
    text = report()
    check("## ⚠ 本流に置き去りの未コミットの変更（1 リポジトリ）" in text and "`docs/資料/一覧 表.md`" in text
          and "（消されたまま）" in text, "5 報告に節・ファイル名・消されたままが出る")
    check(0 < text.find("置き去り") < text.find("## できたこと"), "5 節は「できたこと」より前にある")
    check("fresh.txt" not in text and "ProjC" not in text, "5 今書いた変更と選んでいないプロジェクトは報告に出ない")

    runner.STALE_DIRTY_HOURS = 0
    check(runner.stale_dirty() == [] and "置き去り" not in report(), "6 NIGHT_STALE_DIRTY_HOURS=0 なら見ない")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'不合格' if fails else '合格'}: {len(fails)} 件が落ちた" if fails else "\n合格")
sys.exit(1 if fails else 0)
