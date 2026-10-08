"""night_settings.json を、この PC の Python とこのフォルダの場所で作る（最初に1回）。

runner.py は --settings でこのファイルを Claude Code に渡し、hooks/night_guard.py を差し込む。
フックの Python が見つからないとフックが素通りになるので、runner.py は始める前にこのファイルを確かめる。
"""
import json, os, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
cmd = '"%s" "%s"' % (sys.executable.replace("\\", "/"), os.path.join(ROOT, "hooks", "night_guard.py").replace("\\", "/"))
hook = [{"type": "command", "command": cmd}]
doc = {"disableAllHooks": False,
       "hooks": {"PreToolUse": [{"matcher": "*", "hooks": hook}],
                 "PostToolUse": [{"matcher": "*", "hooks": hook}],
                 "Stop": [{"hooks": hook}]}}
path = os.path.join(ROOT, "night_settings.json")
with open(path, "w", encoding="utf-8") as f:
    json.dump(doc, f, ensure_ascii=False, indent=2)
print("wrote", path)
