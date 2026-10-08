"""ブラウニーの関門。git 統合の入口 `wt.py finish --gate "<このコマンド>"` から、作業コピーの中で呼ばれる。

    python finish_gate.py [--approved] <state/gate/<夜>/<票>.json>
    終了コード 0＝合流してよい、1＝合流しない（理由は標準出力の最後の行と、情報ファイルの results に残す）

中身（runner.gate_main）: --approved ならブラウニーが動いていないこと → 作業コピーに .git があるか → リンクのコミット
→ Claude・フックの設定（.claude/settings・hooks・agents、.githooks、.mcp.json）に触れたか（保留）→ ブラウニー自身の tests/・judge/ に触れたか（保留）
→ 本流側の tests/night_gate.py（あれば。同じ中身なら結果を使い回す）→ 評価役（差分ごとに1回・1票最大2回）。
--approved は、エヴァが「はい」と言った保留を通すとき。凍結と評価役を飛ばす（関門の試験とリンクの確かめは飛ばさない）。
ブラウニーの司令塔が動いている間は --approved を受け付けない（ブラウニーが自分で「はい」を押せないように）。

情報ファイルは、夜の回ごとの秘密鍵（環境変数 NIGHT_GATE_KEY。司令塔が wt.py を呼ぶときだけ渡す）で署名されている。
署名が合わない情報は使わない。鍵が無いとき（朝に手で流す）は、情報ファイルの記憶（評価役・関門の結果）と env を信じない。
"""
import json, os, sys

args = sys.argv[1:]
approved = "--approved" in args
paths = [a for a in args if not a.startswith("--")]
key_hex = os.environ.pop("NIGHT_GATE_KEY", "")  # この先で起こす評価役のセッションに鍵を渡さない
if not paths:
    print("関門: 使い方 python finish_gate.py [--approved] <情報ファイル>", flush=True)
    sys.exit(2)
try:
    key = bytes.fromhex(key_hex) if key_hex else b""
except ValueError:
    key = b""
try:
    with open(paths[0], encoding="utf-8") as f:
        info = json.load(f)
except (OSError, ValueError) as ex:
    print(f"関門: 不合格 — 作業票の情報ファイル {paths[0]} が読めない（{ex}）", flush=True)
    sys.exit(1)
if key:
    import hashlib, hmac
    body = json.dumps({k: v for k, v in info.items() if k != "mac"}, ensure_ascii=False, sort_keys=True)
    if not hmac.compare_digest(str(info.get("mac", "")), hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()):
        print(f"関門: 不合格 — 作業票の情報ファイル {paths[0]} の署名が合わない（司令塔の外で書き換えられた）", flush=True)
        sys.exit(1)
    # 署名が合ったときだけ、司令塔の設定（NIGHT_HOME など）を戻す。runner の定数は import のときに環境変数から決まる
    for k, v in (info.get("env") or {}).items():
        if v not in (None, ""):
            os.environ[k] = str(v)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner  # noqa: E402

sys.exit(runner.gate_main(paths[0], approved, key))
