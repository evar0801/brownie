# PENDING

- [ ] vendor/argparse.py の全クラスについて、定義しているメソッドを全部列挙し、各メソッドの役割を本体を読んだうえで日本語1行で書いた docs/argparse_methods.md を作る。形式はクラスごとに `## クラス名`、その下に1メソッド1行で `- メソッド名: 役割`。完了判定: `grep -c '^## ' docs/argparse_methods.md` が vendor/argparse.py の `grep -c '^class '` と一致し、`grep -c '^- ' docs/argparse_methods.md` が 120 以上。
- [ ] 設定の読み込み先を環境変数と設定ファイルのどちらを優先にするか決めたい（エヴァの判断待ち。Python 3.12 標準ライブラリだけで実装できることが条件）。
