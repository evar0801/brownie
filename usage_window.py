"""利用枠（5時間枠・7日枠）の使用率を、claude -p の出力（--output-format stream-json）から読む（[W-002]）。

claude は応答のたびにサーバーから枠の使用率を受け取り、変わると出力に次の1行を流す（v2.1.281 の実物で確認）:
  {"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1790953800,"rateLimitType":"five_hour",
   "unifiedWindows":{"five_hour":{"utilization":0.04,"resetsAt":1790953800},"seven_day":{"utilization":0.01,"resetsAt":1791525600}}}}
使用率はアカウント全体の値なので、エヴァが別のセッションで使った分も入っている。

ここにあるのは読み取りと判定だけ（ファイルを書かない・プロセスを起こさない）。待つ・知らせるは runner.py がやる。
"""
import datetime, json

JA = {"five_hour": "5時間枠", "seven_day": "7日枠"}


def ja(window):
    return JA.get(window) or (f"7日枠（{window[len('seven_day_'):]}）" if str(window).startswith("seven_day_") else str(window or "利用枠"))


def _num(v):
    """有限の数か（NaN・無限大・真偽値は数として扱わない）。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) < 1e15


def _sec(t):
    """戻る時刻を秒にそろえる（ミリ秒で来たら秒に直す）。"""
    return float(t) / 1000 if t > 1e11 else float(t)


# 窓の長さ（秒）。これより先の戻る時刻は記録として信じない（単位の取り違え・壊れた記録で、何日も待ち続けないように）
SPAN = {"five_hour": 5 * 3600}
SPAN_OTHER = 7 * 86400


def scan(path, offset=0):
    """path を offset から読み、(最後の rate_limit_info か None, 次に読む位置) を返す。
    書きかけの最終行は読まずに次回へ回す。読めなければ (None, offset)。"""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return None, offset
    cut = data.rfind(b"\n")
    if cut < 0:
        return None, offset
    info = None
    for line in data[:cut].split(b"\n"):
        if b"rate_limit_event" not in line:
            continue
        try:
            d = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        # いちばん外側の type だけを見る（道具の結果の中に同じ文字列が出ても拾わない）
        if isinstance(d, dict) and d.get("type") == "rate_limit_event" and isinstance(d.get("rate_limit_info"), dict):
            info = d["rate_limit_info"]
    return info, offset + cut + 1


def windows_of(info):
    """rate_limit_info → {窓の名前: {"utilization": 0〜1, "resetsAt": 秒}}。
    status が rejected（上限で断られた）の窓は、使用率が載っていなくても 100% として返す。"""
    out = {}
    uw = info.get("unifiedWindows")
    if isinstance(uw, dict):
        for k, v in uw.items():
            if not isinstance(v, dict):
                continue
            reset = v.get("resetsAt", v.get("resets_at"))
            if _num(v.get("utilization")) and _num(reset):
                out[k] = {"utilization": float(v["utilization"]), "resetsAt": _sec(reset)}
    k = info.get("rateLimitType")
    if k and _num(info.get("resetsAt")):
        if info.get("status") == "rejected":
            out[k] = {"utilization": max(1.0, out.get(k, {}).get("utilization", 1.0)), "resetsAt": _sec(info["resetsAt"])}
        elif k not in out and _num(info.get("utilization")):
            out[k] = {"utilization": float(info["utilization"]), "resetsAt": _sec(info["resetsAt"])}
    return out


def merge(state, info, now, session_id=None):
    """今の記録 state に新しい rate_limit_info を重ねた記録を返す。戻る時刻を過ぎた窓は落とす。"""
    wins = live(state, now)
    wins.update(windows_of(info))
    return {"at": now, "status": info.get("status"), "session_id": session_id,
            "windows": {k: v for k, v in wins.items() if now < v["resetsAt"] <= now + SPAN.get(k, SPAN_OTHER) + 3600}}


def live(state, now):
    """まだ戻っていない窓だけ（戻る時刻を過ぎた窓は 0% に戻ったものとして扱う）。"""
    wins = state.get("windows") if isinstance(state, dict) else None
    out = {}
    for k, v in (wins.items() if isinstance(wins, dict) else ()):
        if (isinstance(v, dict) and _num(v.get("utilization")) and _num(v.get("resetsAt"))
                and now < v["resetsAt"] <= now + SPAN.get(k, SPAN_OTHER) + 3600):
            out[k] = {"utilization": float(v["utilization"]), "resetsAt": float(v["resetsAt"])}
    return out


def pct(state, now, window="five_hour"):
    """使用率（%・整数）。分からなければ None。"""
    v = live(state, now).get(window)
    return None if v is None else int(round(v["utilization"] * 100))


def threshold(window, hold_pct, week_hold_pct):
    """その窓で「新しいセッションを始めない」しきい値（%）。0 以下は「しきい値では待たない」＝上限に当たったとき（100%）だけ。"""
    if window not in ("five_hour", "seven_day"):
        return 100.0  # モデル別などほかの窓は、使っていないモデルの分かもしれない。実際に断られたとき（100%）だけ待つ
    t = hold_pct if window == "five_hour" else week_hold_pct
    return 100.0 if t <= 0 else min(100.0, t)


def verdict(state, now, hold_pct, week_hold_pct):
    """しきい値以上の窓があれば {"window", "pct", "until"}（複数なら戻るのがいちばん遅い窓）。無ければ None。"""
    over = [{"window": k, "pct": int(round(v["utilization"] * 100)), "until": v["resetsAt"]}
            for k, v in live(state, now).items()
            if v["utilization"] * 100 >= threshold(k, hold_pct, week_hold_pct)]
    return max(over, key=lambda o: o["until"]) if over else None


def clock(ts, now=None):
    """戻る時刻の表示。今日なら HH:MM、別の日なら M/D HH:MM。"""
    d = datetime.datetime.fromtimestamp(ts)
    today = datetime.datetime.fromtimestamp(now).date() if now is not None else datetime.datetime.now().date()
    return f"{d:%H:%M}" if d.date() == today else f"{d.month}/{d.day} {d:%H:%M}"


def label(state, now):
    """「5時間枠 62%（05:30 に戻る）・7日枠 12%」。分からなければ空文字。"""
    wins = live(state, now)
    parts = []
    for k in sorted(wins, key=lambda k: (k != "five_hour", k)):
        v = wins[k]
        p = f"{ja(k)} {int(round(v['utilization'] * 100))}%"
        parts.append(p + (f"（{clock(v['resetsAt'], now)} に戻る）" if k == "five_hour" else ""))
    return "・".join(parts)
