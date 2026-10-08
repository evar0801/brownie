"""試験が嘘の合格を出さないことを確かめる（トークン0）。

    python tests/sabotage_overnight.py [--only V1,V9]
    終了コード 0＝無改変の版は合格し、壊した版はどれも不合格になった／1＝どれかが期待と違った

ブラウニーの一式を %TEMP% に複製し、1か所ずつわざと壊して、その版に効く試験を流す。
壊した版ごとに、落ちた項目（FAIL の行）を出す。固まる版は試験の壁時計の上限で落ちる。1本ずつ順に流す（並列にしない）。
全部で20分以上かかる。一部だけなら --only。
"""
import json, os, shutil, subprocess, sys, tempfile

sys.stdout.reconfigure(encoding="utf-8")
NIGHT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ONLY = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else None
OV = ["tests/test_overnight.py"]
R = "runner.py"

VARIANTS = [  # (名前, 説明, [(ファイル, 壊す前, 壊した後)], 流す試験, 壁時計の上限)
    ("V0", "無改変（複製の仕組みそのものが合格することの確かめ）", [], OV, 400),
    ("V1", "票ごとの時間切れを無効にした", [(R, "deadline=time.time() + left_sec))", "deadline=time.time() + 10 ** 6))")], OV, 200),
    ("V2", "時間切れで子プロセスを残す（直下のプロセスだけ止める）",
     [(R, 'subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, stdin=subprocess.DEVNULL, timeout=60)', "pass")], OV, 400),
    ("V3", "夜の終わりに行き先を決めない（放置票が出る）", [(R, 'finalize_night(reason or "終了")  # 行き先の決まっていない票を残さない', "pass")], OV, 400),
    ("V4", "wt.py の呼び出しの時間切れを無効にした", [(R, "deadline = t0 + WT_TIMEOUT_SEC\n", "deadline = t0 + 10 ** 6\n")], OV, 200),
    ("V5", "30（本体が使用中）を再試行せず異常扱いにした", [(R, "    if code == 30:\n", "    if code == 30 and False:\n")], OV, 400),
    ("V6", "報告の節「保留中の許可」を落とした", [(R, 'f"## 保留中の許可（{len(by[\'hold\'])}）"', 'f"## 保留（{len(by[\'hold\'])}）"')], OV, 400),
    ("V7", "10（衝突）で解消役を呼ばない",
     [(R, "            ok, out = resolve_in_worktree(t)\n            if t.get(\"outside\"):", "            ok, out = False, \"壊した版\"\n            if t.get(\"outside\"):")], OV, 400),
    ("V8", "評価役の結果を覚えない（再試行のたびに評価役を呼ぶ）", [(R, '            rv = d.get("review") if trusted else None\n', "            rv = None\n")], OV, 400),
    # 2026-09-25 敵対レビュー（③越えない線・②暴走）の差し戻しで足した歯止め
    ("V9", "③3 作業コピーの外の見張りを外した", [(R, "    out = []\n    for k in sorted(set(before) | set(after)):", "    return []\n    out = []\n    for k in sorted(set(before) | set(after)):")], OV, 400),
    ("V10", "③1 許可待ち（needs_permission）を「できた」にした",
     [(R, '        if t.get("status") == "needs_permission":\n            set_outcome(t, "hold", "許可が要る操作の手前で止めた（手前までの',
       '        if False:\n            set_outcome(t, "hold", "許可が要る操作の手前で止めた（手前までの')], OV, 400),
    ("V11", "②1 結果の無いセッションを $0 と数える", [(R, '"total_cost_usd": float(h.get("cap") or 0), "_cost_estimated": True}', '"total_cost_usd": 0}')], OV, 400),
    ("V12", "②2 夜の終わりを無効にした", [(R, "    return END_TS is not None and time.time() >= END_TS", "    return False")],
     ["tests/test_night_limits.py", "--only", "L1"], 200),
    ("V13", "③4 関門の情報ファイルの署名を確かめない（関門と司令塔の両方）",
     [("finish_gate.py", "    if not hmac.compare_digest(", "    if False and not hmac.compare_digest("),
      (R, "    if key and not hmac.compare_digest(", "    if False and not hmac.compare_digest(")],
     ["tests/test_merge_flow.py", "--only", "s1"], 200),
    ("V14", "②8 司令塔の例外で夜全体が落ちる（包まない）",
     [(R, "            except Exception:\n                # 仕分け・報告・再開・保存などの例外で夜全体を終えない",
       "            except ZeroDivisionError:\n                # 仕分け・報告・再開・保存などの例外で夜全体を終えない")],
     ["tests/test_night_limits.py", "--only", "L4"], 200),
    ("V15", "③8 合流しないプロジェクト（NIGHT_NO_MERGE_PROJECTS）を合流させる",
     [(R, "    if project_in(t, NO_MERGE_PROJECTS):\n        files =", "    if False:\n        files =")], OV, 400),
    ("V16", "③7 作業コピーを作れない git リポジトリで直接書きに落とす",
     [(R, '        return cannot(f"本流 {root} が detached HEAD（どのブランチへ合流させるか決められない）", repo=root)',
       '        t["merge"] = {"state": "direct", "note": "壊した版"}\n        return None')],
     ["tests/test_merge_flow.py", "--only", "s3"], 200),
    ("V17", "②12 空き容量を見ない", [(R, "    for p in paths or (EVAR, HOME):", "    for p in ():")], ["tests/test_night_limits.py", "--only", "L3"], 200),
    # 2026-09-25 エヴァの決裁: ③6 は取り下げ（ゲームの decide も合流する）。既定を戻すと落ちることを確かめる
    ("V18", "③6 の既定を戻した（ゲームの decide を合流させない）",
     [(R, 'os.environ.get("NIGHT_GAME_PROJECTS", "")', 'os.environ.get("NIGHT_GAME_PROJECTS", "ProjP,s11game")')], OV, 400),
    # 越えない線のガード（hooks/night_guard.py の line_guard。NIGHT_GUARD が off でも効く）
    ("V19", "③2・③13 線のガードを呼ばない", [("hooks/night_guard.py", '    line_guard(name, ti, d.get("cwd"))', "    pass")],
     ["tests/test_lineguard.py"], 120),
    ("V20", "③13 作業ディレクトリの外での削除を見ない", [("hooks/night_guard.py", "            out = _outside(cmd, cwd)", "            out = None")],
     ["tests/test_lineguard.py"], 120),
    ("V21", "③2 線の拒否リストから git push を外した",
     [("hooks/night_guard.py", '    (GIT_OPTS + r"\\s+(push|send-email|request-pull)\\b", "git push（リモートへの送信）"),\n', "")],
     ["tests/test_lineguard.py"], 120),
    # 2026-10-01 ガードの穴 N1〜N5 を塞いだ常時の検査と、ブラウニー自身の差分の保留
    ("V22", "N1〜N5 常時の検査（always_guard）を呼ばない",
     [("hooks/night_guard.py", "        always_guard(d)  # 常時の検査（N1〜N5）", "        pass  # 常時の検査（N1〜N5）")],
     ["tests/test_guard_holes.py"], 200),
    ("V23", "N3 ブラウニー自身の差分を保留にしない",
     [(R, '    return [c for c in changed if c.replace("\\\\", "/").removeprefix("./") not in SELF_DOCS_OK]',
       "    return []")],
     ["tests/test_guard_holes.py"], 200),
]


def copy_tree(dst):
    ign = shutil.ignore_patterns("sandbox", "state", "reports", "__pycache__", ".git")
    for name in ("runner.py", "finish_gate.py", "night_settings.json"):
        shutil.copy2(os.path.join(NIGHT, name), dst)
    for d in ("prompts", "judge", "hooks", "tests"):
        shutil.copytree(os.path.join(NIGHT, d), os.path.join(dst, d), ignore=ign)


def kill_leftovers(sandbox_root):
    """壊した版が残した偽の claude の子プロセス（[hang] の眠り）を片付ける。"""
    for root, _, files in os.walk(sandbox_root):
        for f in files:
            if f.startswith("fake_hang_") and f.endswith(".json"):
                try:
                    pids = json.load(open(os.path.join(root, f), encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                for pid in pids.values():
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)


results, bad = [], []
for name, what, patches, test, wall in VARIANTS:
    if ONLY and name not in ONLY:
        continue
    dst = tempfile.mkdtemp(prefix=f"night_sabotage_{name}_")
    copy_tree(dst)
    broken_ok = True
    for fname, old, new in patches:
        fp = os.path.join(dst, fname)
        src = open(fp, encoding="utf-8").read()
        if src.count(old) != 1:
            print(f"{name}: 壊す場所が {fname} に1か所で見つからない（{src.count(old)}か所）。試験の前提が崩れた", flush=True)
            broken_ok = False
            break
        open(fp, "w", encoding="utf-8", newline="\n").write(src.replace(old, new))
    if not broken_ok:
        bad.append(name)
        continue
    args = [sys.executable, os.path.join(dst, test[0]), *test[1:]] + (["--wall", str(wall)] if test[0].endswith("test_overnight.py") else [])
    print(f"{name} {what}: {' '.join(test)} を流す（上限 {wall}秒）", flush=True)
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=wall + 300)
        out, code = r.stdout, r.returncode
    except subprocess.TimeoutExpired as ex:
        out = ex.stdout.decode("utf-8", "replace") if isinstance(ex.stdout, bytes) else (ex.stdout or "")
        code = "timeout"
    kill_leftovers(os.path.join(dst, "sandbox"))
    failed = [ln.strip()[5:] for ln in out.splitlines() if ln.strip().startswith("FAIL ")]
    ok_n = sum(1 for ln in out.splitlines() if ln.strip().startswith("ok "))
    expect_pass = not patches
    good = (code == 0 and not failed) if expect_pass else (code != 0 and bool(failed))
    results.append((name, what, code, ok_n, failed, good))
    print(f"  → 終了コード {code}・ok {ok_n}・FAIL {len(failed)} … {'期待どおり' if good else '期待と違う'}", flush=True)
    for f in failed[:8]:
        print(f"     FAIL {f[:150]}", flush=True)
    if not good:
        bad.append(name)
    else:
        shutil.rmtree(dst, ignore_errors=True)

print("\n| 版 | 壊したもの | 終了コード | ok | FAIL | 判定 |\n|---|---|---|---|---|---|")
for name, what, code, ok_n, failed, good in results:
    print(f"| {name} | {what} | {code} | {ok_n} | {len(failed)} | {'期待どおり' if good else '⚠ 期待と違う'} |")
print(("合格" if not bad else f"不合格: {bad}"), flush=True)
sys.exit(1 if bad else 0)
