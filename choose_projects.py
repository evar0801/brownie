"""ブラウニーを始める前に、進めるプロジェクトを番号で選ぶ（「ブラウニーを始める.bat」から呼ばれる）。

終了コード: 0＝選べた（runner を起動してよい）／1＝選ばれていない・中断（runner を起動しない）。
候補は runner.py の source_files() と同じ除外をかけたうえで、PENDING.md か HANDOFF.md を持つディレクトリ。
選んだ結果は projects.txt に書き戻す（選んだ候補は # なし、選ばなかった候補と候補に無い既存の行は # 付きで残す）。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
EVAR = os.environ.get("NIGHT_EVAR_ROOT") or os.path.dirname(ROOT)
HOME = os.environ.get("NIGHT_HOME") or ROOT
PROJECTS_FILE = os.path.join(HOME, "projects.txt")

# runner.py の source_files() と同じ（走査の根からの相対パスに含まれていたら外す）
SKIP = ("排除", "_public_copies", os.path.join("GameAProject", "Live"), "node_modules", ".git",
        os.path.join("BrownieProject", "sandbox"), os.path.join("BrownieProject", "state"),
        os.path.join("BrownieProject", "tests"))
PENDING_ITEM = re.compile(r"^## \[[A-Za-z]+-\d+\]")
PATH_LIKE = re.compile(r"^[\w./\\-]+$")

HEADER = [
    "# ブラウニーで進めるプロジェクト。行頭に # を付けた行は対象外。",
    "# 「ブラウニーを始める」を押すと選択画面が出て、選んだ結果でこのファイルを書き直す（選ばなかったものは # 付きで残る）。",
    "# 有効な行が1つも無いときは、ブラウニーは始めない（全プロジェクトには倒れない）。",
    "# inbox.md に書いたものは、ここで外していても実行する。",
    "# 次にブラウニーを始めたとき（仕分けのとき）に読み直すので、走っている最中に書き換えても今夜の票には効かない。",
]


def skipped(rel):
    # 別セッションの作業コピー（<何か>/.claude/worktrees/…）も外す（パスの要素が .claude のときだけ）
    return any(s in rel for s in SKIP) or ".claude" in re.split(r"[\\/]", rel)


def candidates():
    found = set()
    for dirpath, dirnames, filenames in os.walk(EVAR):
        rel_dir = os.path.relpath(dirpath, EVAR)
        dirnames[:] = [d for d in dirnames if not skipped(os.path.normpath(os.path.join(rel_dir, d)))]
        if rel_dir == "." or skipped(rel_dir):
            # 根の直下の PENDING/HANDOFF は総本山の文書でプロジェクトではない（runner は拾うが選ぶ単位にならない）
            continue
        if any(f in filenames for f in ("PENDING.md", "HANDOFF.md")):
            found.add(rel_dir.replace("\\", "/"))
    return sorted(found)


def pending_count(rel):
    try:
        with open(os.path.join(EVAR, rel, "PENDING.md"), encoding="utf-8") as f:
            return sum(1 for ln in f if PENDING_ITEM.match(ln))
    except OSError:
        return 0


def read_lines():
    try:
        with open(PROJECTS_FILE, encoding="utf-8") as f:
            return f.read().splitlines()
    except OSError:
        return []


def norm(p):
    return p.strip().strip("/\\").replace("\\", "/")


def active(lines):
    return [norm(l) for l in lines if l.strip() and not l.strip().startswith("#")]


def parse(text, n):
    """番号の並びを解釈する。だめなら (None, 理由)。"""
    toks = text.split()
    bad = [t for t in toks if not t.isdigit()]
    if bad:
        return None, f"数字以外が混ざっています: {' '.join(bad)}"
    nums = [int(t) for t in toks]
    out = [x for x in nums if not 1 <= x <= n]
    if out:
        return None, f"存在しない番号です: {' '.join(map(str, out))}（1〜{n}）"
    seen = []
    for x in nums:
        if x not in seen:
            seen.append(x)
    return seen, None


def write_back(cands, chosen, lines):
    kept = []
    for l in lines:
        s = l.strip()
        if not s:
            continue
        body = norm(s.lstrip("#"))
        if not PATH_LIKE.match(body) or body in cands or body in kept:
            continue  # 説明のコメント・候補の行は書き直す
        kept.append(body)
    out = list(HEADER)
    out += [c if c in chosen else "# " + c for c in cands]
    out += ["# " + k for k in kept]
    with open(PROJECTS_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")


def main():
    lines = read_lines()
    prev = active(lines)
    cands = candidates()
    print("ブラウニーで進めるプロジェクトを選んでください。")
    for i, c in enumerate(cands, 1):
        print(f"  {i:2d}. {c}  PENDING {pending_count(c)}件" + ("  ← 前回" if c in prev else ""))
    print("番号をスペース区切りで入力（Enter だけ＝前回と同じ）:")
    while True:
        try:
            text = input("> ")
        except (EOFError, KeyboardInterrupt):
            print("\n選べないので始めません。")
            return 1
        if not text.strip():
            chosen = [c for c in cands if c in prev]
            if not chosen:
                print("プロジェクトが選ばれていないので始めません。")
                return 1
            break
        nums, why = parse(text, len(cands))
        if why:
            print(why + "。もう一度入力してください。")
            continue
        chosen = [cands[x - 1] for x in nums]
        if not chosen:
            print("プロジェクトが選ばれていないので始めません。")
            return 1
        break
    write_back(cands, set(chosen), lines)
    print(f"→ {'・'.join(chosen)} だけで始めます（inbox.md に書いたものは選択に関係なく実行します）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
