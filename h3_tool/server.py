# -*- coding: utf-8 -*-
"""H3かんたん動画メーカー - ローカルサーバー
Python標準ライブラリのみで動作。RunPodのON/OFFとMiniMax H3動画生成を簡単UIで提供する。
"""
import json
import mimetypes
import os
import random
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SELF_VERSION = "3.1.0"
UPDATE_REPO_RAW = "https://raw.githubusercontent.com/novaongats/h3-video-tool/main"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
OUTPUT_DIR = os.path.join(ROOT_DIR, "outputs")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ELEMENTS_DIR = os.path.join(BASE_DIR, "elements")   # エレメント（登録した人物・場所など）の画像置き場
ELEMENTS_FILE = os.path.join(BASE_DIR, "elements.json")
PORT = 8787

DEFAULT_CONFIG = {
    "api_key": "",
    "pod_id": "",      # 起動時にNetwork Volumeから自動発見される
    "comfy_url": "",
    "auto_stop": True,
}


def self_update():
    """起動時にGitHubの最新版を確認し、更新があればファイルを差し替える。更新したらTrue。"""
    try:
        info = http_json(UPDATE_REPO_RAW + "/version.json", timeout=10)
        remote_v = str(info.get("version", ""))

        def vtup(v):
            try:
                return tuple(int(x) for x in str(v).split("."))
            except ValueError:
                return (0,)
        # リモートがローカルより新しい場合のみ更新（開発中のローカルを巻き戻さない）
        if not remote_v or vtup(remote_v) <= vtup(SELF_VERSION):
            return False
        for rel in info.get("files", []):
            if ".." in rel or rel.startswith("/") or rel.startswith("\\"):
                continue
            url = UPDATE_REPO_RAW + "/" + urllib.parse.quote(rel)
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            local = os.path.join(ROOT_DIR, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(local), exist_ok=True)
            with open(local, "wb") as f:
                f.write(data)
        print(f"自動アップデート: {SELF_VERSION} → {remote_v} を適用しました")
        return True
    except Exception as e:
        print("アップデート確認をスキップ:", e)
        return False

os.makedirs(OUTPUT_DIR, exist_ok=True)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 H3Tool/1.0")


def http_json(url, method="GET", headers=None, body=None, timeout=30):
    data = None
    hdrs = {"User-Agent": UA}
    hdrs.update(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {"_raw": raw.decode("utf-8", "replace")}


# ---------- RunPod API ----------

RUNPOD_BASE = "https://rest.runpod.io/v1"


class RunPodError(Exception):
    pass


def runpod_call(cfg, path, method="GET", body=None):
    if not cfg.get("api_key"):
        raise RunPodError("APIキーが未設定です")
    url = RUNPOD_BASE + path
    try:
        return http_json(url, method=method,
                         headers={"Authorization": "Bearer " + cfg["api_key"]},
                         body=body, timeout=60)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        if e.code == 401:
            raise RunPodError("APIキーが正しくありません（401）")
        raise RunPodError(f"RunPod APIエラー {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RunPodError(f"RunPodに接続できません: {e.reason}")


NETWORK_VOLUME_ID = "qqvwtszok9"  # 日本(AP-JP-1) minimax-h3-jp
POD_IMAGE = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
# 優先順。H100が満席のときは同じDC内のH200($4.59/h)に自動フォールバック
POD_GPUS = ["NVIDIA H100 80GB HBM3", "NVIDIA H200"]
POD_GPU = POD_GPUS[0]
POD_START_CMD = ["bash", "-c", "(sleep 15 && /workspace/start_comfyui.sh) & exec /start.sh"]


def resolve_pod(cfg):
    """Network Volumeが繋がったPodを探して設定を同期する（複数PC間の共有対応）。
    見つかればそのPodのid/URLを返し、config.jsonも更新する。無ければNone。"""
    d = runpod_call(cfg, "/pods")
    pods = d if isinstance(d, list) else d.get("pods", [])
    ours = [p for p in pods if p.get("networkVolumeId") == NETWORK_VOLUME_ID]
    if not ours:
        return None
    # RUNNINGを優先、無ければ先頭
    ours.sort(key=lambda p: 0 if p.get("desiredStatus") == "RUNNING" else 1)
    pod = ours[0]
    pid = pod["id"]
    if cfg.get("pod_id") != pid:
        cfg["pod_id"] = pid
        cfg["comfy_url"] = f"https://{pid}-8188.proxy.runpod.net"
        save_config(cfg)
    return pod


def create_replacement_pod(cfg):
    """空きのある別マシンに新しいPodを作る。古いPodは削除する。"""
    old_id = cfg.get("pod_id")
    body = {
        "name": "minimax-h3-pod-auto",
        "imageName": POD_IMAGE,
        "gpuTypeIds": POD_GPUS,
        "gpuCount": 1,
        "cloudType": "SECURE",
        "networkVolumeId": NETWORK_VOLUME_ID,
        "volumeMountPath": "/workspace",
        "containerDiskInGb": 30,
        "ports": ["8888/http", "8188/http", "22/tcp"],
        "dockerStartCmd": POD_START_CMD,
    }
    new_pod = runpod_call(cfg, "/pods", method="POST", body=body)
    new_id = new_pod.get("id")
    if not new_id:
        raise RunPodError("Podの作り直しに失敗しました: " + json.dumps(new_pod)[:200])
    cfg["pod_id"] = new_id
    cfg["comfy_url"] = f"https://{new_id}-8188.proxy.runpod.net"
    save_config(cfg)
    if old_id and old_id != new_id:
        try:
            runpod_call(cfg, f"/pods/{old_id}", method="DELETE")
        except Exception:
            pass  # 消せなくても実害はない（停止中Podは課金されない）
    return new_pod


BALANCE_CACHE = {"t": 0.0, "data": None}


def get_balance(cfg):
    """RunPodの残高を取得（60秒キャッシュ）。失敗時はNone。"""
    now = time.time()
    if now - BALANCE_CACHE["t"] < 60:
        return BALANCE_CACHE["data"]
    try:
        q = json.dumps({"query": "query { myself { clientBalance currentSpendPerHr } }"}).encode()
        req = urllib.request.Request(
            "https://api.runpod.io/graphql", data=q,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + cfg["api_key"], "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())["data"]["myself"]
        BALANCE_CACHE["t"] = now
        BALANCE_CACHE["data"] = {"usd": round(float(d["clientBalance"]), 2),
                                 "spend_per_hr": float(d.get("currentSpendPerHr") or 0)}
    except Exception:
        BALANCE_CACHE["t"] = now - 50  # 失敗時は10秒後に再試行
    return BALANCE_CACHE["data"]


def pod_status(cfg):
    if not cfg.get("pod_id"):
        raise RunPodError("Pod未設定")  # 呼び出し側でresolve_podにフォールバックする
    d = runpod_call(cfg, f"/pods/{cfg['pod_id']}")
    return d.get("desiredStatus", "UNKNOWN")


def comfy_alive(cfg, timeout=8):
    try:
        req = urllib.request.Request(cfg["comfy_url"] + "/system_stats",
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


# ---------- ComfyUI API ----------

def comfy_get(cfg, path, timeout=30):
    return http_json(cfg["comfy_url"] + path, timeout=timeout)


def comfy_upload_image(cfg, filename, blob):
    boundary = "----H3Boundary" + uuid.uuid4().hex
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "input.png"
    ctype = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    parts = []
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{safe_name}\"\r\nContent-Type: {ctype}\r\n\r\n".encode())
    parts.append(blob)
    parts.append(f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        cfg["comfy_url"] + "/upload/image", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def comfy_progress(cfg):
    """ComfyUIのログから現在のサンプリング進捗(step, total)を取得。取れなければNone。"""
    try:
        req = urllib.request.Request(cfg["comfy_url"] + "/internal/logs/raw",
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            text = r.read().decode("utf-8", "replace")
        hits = re.findall(r"(\d+)/(\d+) \[", text)
        for cur, tot in reversed(hits):
            if 5 <= int(tot) <= 40:  # ステップ数の範囲だけ拾う（他の進捗表示と区別）
                return int(cur), int(tot)
    except Exception:
        pass
    return None


def friendly_comfy_error(e):
    """ComfyUIの400エラー本文を読み、人間に分かる日本語メッセージへ変換する"""
    try:
        detail = json.loads(e.read().decode("utf-8", "replace"))
    except Exception:
        return f"【設定エラー】生成サーバーがリクエストを拒否しました（HTTP {e.code}）"
    msgs = []
    for nid, info in (detail.get("node_errors") or {}).items():
        ct = info.get("class_type", "")
        raw = "; ".join(err.get("details") or err.get("message", "") for err in info.get("errors", []))[:150]
        if ct == "LoadVideo":
            msgs.append("【動画エラー】元動画を読み込めませんでした。MP4形式に変換して再試行してください"
                        "（iPhoneの.MOV等は非対応。スマホは設定→カメラ→フォーマットを「互換性優先」に）")
        elif ct == "LoadImage":
            msgs.append("【画像エラー】画像を読み込めませんでした。JPG/PNG形式で再選択してください")
        elif ct == "ResolutionSelector":
            msgs.append(f"【画面サイズエラー】画面の形の指定に問題があります（{raw}）")
        else:
            msgs.append(f"【設定エラー】{ct}: {raw}")
    if not msgs:
        err = detail.get("error") or {}
        msgs.append("【設定エラー】" + (err.get("message") if isinstance(err, dict) else str(err))[:200])
    return " / ".join(msgs[:3])


VIDEO_OK_EXT = (".mp4", ".webm", ".mkv")


def pick_aspect(cfg, ratio_label):
    """object_infoから有効なaspect_ratio選択肢を取得し、希望比率に最も近いものを返す"""
    want = {"16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1.0}.get(ratio_label, 16 / 9)
    fallback = {"16:9": "16:9 (Widescreen)", "9:16": "9:16 (Portrait Widescreen)",
                "1:1": "1:1 (Square)"}.get(ratio_label, "16:9 (Widescreen)")
    try:
        info = comfy_get(cfg, "/object_info/ResolutionSelector")
        spec = info["ResolutionSelector"]["input"]["required"]["aspect_ratio"]
        # 旧形式: [[options], {...}] / 新形式: ["COMBO", {"options": [...]}]
        if isinstance(spec[0], list):
            choices = spec[0]
        elif spec[0] == "COMBO" and isinstance(spec[1], dict):
            choices = spec[1]["options"]
        else:
            return fallback
        if not choices:
            return fallback
    except Exception:
        return fallback
    best, best_diff = choices[0], 1e9
    for c in choices:
        m = re.match(r"\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)", c)
        if not m:
            continue
        r = float(m.group(1)) / float(m.group(2))
        diff = abs(r - want)
        if diff < best_diff:
            best, best_diff = c, diff
    return best


# ---------- 生成ジョブ管理 ----------

JOB = {
    "state": "idle",   # idle / starting_pod / waiting_comfy / uploading / generating / downloading / done / error / stopping_pod
    "message": "",
    "started_at": None,
    "video": None,
    "error": None,
    "seed": None,
}
JOB_LOCK = threading.Lock()

HISTORY_PATH = os.path.join(BASE_DIR, "history.json")
HISTORY_LOCK = threading.Lock()


def load_history():
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def append_history(record):
    with HISTORY_LOCK:
        hist = load_history()
        hist.append(record)
        hist = hist[-500:]  # 直近500件まで保持
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=1)


def load_elements():
    """登録済みエレメント（人物・場所などの参照素材）の一覧を読む"""
    if os.path.exists(ELEMENTS_FILE):
        try:
            with open(ELEMENTS_FILE, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_elements(items):
    with open(ELEMENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def set_job(**kw):
    with JOB_LOCK:
        JOB.update(kw)


def ensure_pod_running(cfg):
    pod = resolve_pod(cfg)  # 他のPCがPodを作り直していても自動で追従する
    st = pod.get("desiredStatus") if pod else "MISSING"
    if st != "RUNNING":
        set_job(state="starting_pod", message="GPUサーバーを起動しています…（1〜2分）")
        started = False
        if pod:
            attempts = 3
            for i in range(attempts):
                try:
                    runpod_call(cfg, f"/pods/{cfg['pod_id']}/start", method="POST")
                    started = True
                    break
                except RunPodError as e:
                    if "not enough free GPUs" not in str(e):
                        raise
                    if i < attempts - 1:
                        set_job(state="starting_pod",
                                message=f"GPUの空き待ち中…（{i + 1}/{attempts}回目）")
                        time.sleep(90)
        if not started:
            # このマシンは満席 → 別マシンにPodを自動で作り直す
            set_job(state="starting_pod",
                    message="このマシンは満席のため、空いている別マシンでサーバーを作り直しています…（1〜3分）")
            try:
                create_replacement_pod(cfg)
            except RunPodError as e:
                if "not enough free GPUs" in str(e) or "no longer any instances" in str(e).lower():
                    raise RunPodError(
                        "データセンター全体でGPUの空きがありません。"
                        "30分〜数時間おいて再試行してください。")
                raise
    set_job(state="waiting_comfy", message="動画生成エンジンの準備中…（1〜2分）")
    deadline = time.time() + 420
    while time.time() < deadline:
        if comfy_alive(cfg):
            return
        time.sleep(6)
    raise RunPodError("エンジンが時間内に起動しませんでした。もう一度お試しください。")


def find_video_in_history(hist_entry):
    for out in hist_entry.get("outputs", {}).values():
        if not isinstance(out, dict):
            continue
        for v in out.values():
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, dict) and str(item.get("filename", "")).lower().endswith(".mp4"):
                        return item
    return None


def norm_tag(s):
    """@名前の表記ゆれ吸収用の正規化（大文字小文字・全角半角・カタカナ→ひらがな）"""
    s = unicodedata.normalize("NFKC", s).lower()
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def replace_at_tags(text, mapping):
    """@名前 を正式タグへ置換する。mapping=[(生の名前, 置換後), …]。
    表記ゆれ（ひらがな/カタカナ/大小文字）でも同じ長さなら一致とみなす。"""
    entries = sorted(((name, norm_tag(name), rep) for name, rep in mapping),
                     key=lambda x: -len(x[0]))
    out = []
    i = 0
    while i < len(text):
        if text[i] == "@":
            for name, nname, rep in entries:
                seg = text[i + 1:i + 1 + len(name)]
                if len(seg) == len(name) and norm_tag(seg) == nname:
                    out.append(rep)
                    i += 1 + len(name)
                    break
            else:
                out.append(text[i])
                i += 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def compose_prompt(p):
    """フォームの各欄から、MiniMax公式プロンプトガイド準拠の形式に組み立てる。
    公式形式: 本文（Shot構成・話者ID・<d>タグのセリフ） + overall_soundscape + non_diegetic_music
    """
    mix = p.get("mix", "auto")
    parts = [p.get("prompt", "").strip()]

    d = p.get("dialogue", "").strip()
    if d:
        v = p.get("voice", "").strip()
        # 「@名前：セリフ」形式の行が並んでいれば会話モード、「ナレーション：」は画面外の声として扱う
        lines = [ln.strip() for ln in d.splitlines() if ln.strip()]
        conv = []
        for ln in lines:
            m = re.match(r"^@?([^：:]{1,10})[：:]\s*(.+)$", ln)
            if m and (ln.startswith("@") or m.group(1).strip() in ("ナレーション", "ナレーター") or len(lines) > 1):
                conv.append((m.group(1).strip(), m.group(2).strip()))
            else:
                conv = None
                break
        if conv:
            sid = {}
            out_lines = []
            for name, text in conv:
                if name in ("ナレーション", "ナレーター"):
                    out_lines.append("ナレーション（画面外の声。映像内の誰の口も動かさない）："
                                     f"<d>[Japanese] {text}</d>")
                else:
                    if name not in sid:
                        sid[name] = f"S{len(sid) + 1}"
                    out_lines.append(f"{name} ({sid[name]}) が話す。口の動きをこのセリフに正確に同期："
                                     f"<d>[Japanese] {text}</d>")
            block = "会話・音声:\n" + "\n".join(out_lines)
            if v:
                block += f"\n声の指定: {v}"
            parts.append(block)
        else:
            who = f"登場人物 (S1)（{v}）" if v else "登場人物 (S1)"
            parts.append(f"{who} がカメラに向かってはっきりと話す。口の動きはセリフに正確に同期する："
                         f"<d>[Japanese] {d}</d>")

    if mix in ("no_speech", "silent"):
        parts.append("この動画では誰も一切話さない。セリフ、ナレーション、実況、ボイスオーバー、"
                     "歌声、人の声を絶対に入れない。")
    else:
        if not d:
            parts.append("指示していないセリフ・ナレーション・実況ボイスを勝手に追加しない。"
                         "もし人物が自然に声を発する場合は必ず日本語のみ（中国語・英語の音声は禁止）。")
        if mix == "voice_first":
            parts.append("音のバランス：セリフを最優先で明瞭に。他の音は控えめの音量。")

    if p.get("no_text", True):
        parts.append("画面内に文字・字幕・ロゴ・透かしを一切出さない。")

    # 公式の音声2区画（環境音・効果音 / 画面外の音楽）
    se = p.get("se", "").strip()
    soundscape = se if se else "シーンに合った自然な環境音と、動きに伴う物理的な音。"
    parts.append("overall_soundscape: " + soundscape)

    bgm = p.get("bgm", "").strip()
    if mix in ("no_bgm", "silent"):
        music = "N/A"
    elif bgm:
        music = bgm + ("（音量は控えめ）" if mix == "bgm_low" else "")
    else:
        music = "N/A"
    parts.append("non_diegetic_music: " + music)

    return "\n\n".join(x for x in parts if x)


def run_generation(params, image_blob, image_name):
    cfg = load_config()
    src_video_path = None  # 部分編集で「元動画の音声を使う」とき、元動画の一時保存先
    try:
        mode = params.get("mode", "t2v")

        # ---- 事前チェック（サーバーを起動する前に安く失敗させる） ----
        if mode in ("i2v", "flf2v") and not image_blob:
            raise RunPodError("【入力不足】画像が選択されていません")
        if mode == "flf2v" and not params.get("last_image_b64"):
            raise RunPodError("【入力不足】「最後の画像」が選択されていません")
        # ---- エレメント・絵コンテの読み込みとエンジン切替 ----
        element_ids = params.get("element_ids") or []
        elements = [e for e in load_elements() if e.get("id") in element_ids] if element_ids else []
        storyboard = (params.get("storyboard_images") or [])[:6]
        if (elements or storyboard) and mode in ("i2v", "flf2v"):
            raise RunPodError("【非対応】「画像を動かす」「始点→終点」はAIの仕様上エレメント・絵コンテを併用できません。"
                              "「文章から」「人物固定」「部分編集」をご利用ください")
        if (elements or storyboard) and mode == "t2v":
            # エレメント・絵コンテ使用時は参照対応エンジン（人物固定と同じ）で生成する
            mode = "r2v"

        if mode == "r2v" and not (params.get("ref_images") or []) and not elements and not storyboard:
            raise RunPodError("【入力不足】参照画像が選択されていません（エレメントを選ぶか、画像を1〜4枚選択）")
        if mode == "edit":
            if not params.get("ref_video_b64"):
                raise RunPodError("【入力不足】編集する元動画が選択されていません")
            vname = (params.get("ref_video_name") or "").lower()
            if vname and not vname.endswith(VIDEO_OK_EXT):
                raise RunPodError(
                    "【動画形式エラー】この動画形式は使えません（対応: MP4/WebM/MKV）。"
                    "iPhoneの.MOV動画などはMP4に変換してから選択してください"
                    "（無料の変換サイトやスマホアプリでOK）")

        ensure_pod_running(cfg)
        wf_file = {"i2v": "wf_i2v.json", "flf2v": "wf_i2v.json",
                   "r2v": "wf_r2v.json", "edit": "wf_r2v.json"}.get(mode, "wf_t2v.json")
        with open(os.path.join(BASE_DIR, wf_file), "r", encoding="utf-8") as f:
            wf = json.load(f)

        # ノードIDがモードで異なる
        ids = {"prompt": "105:104", "dur": "105:111", "seed": "105:15", "steps": "105:9"}
        if mode in ("r2v", "edit"):
            ids = {"prompt": "138", "dur": "132", "seed": "129", "steps": "124"}

        prompt_text = compose_prompt(params)
        wf[ids["dur"]]["inputs"]["value"] = float(params.get("seconds", 5))
        seed_in = str(params.get("seed", "")).strip()
        seed = int(seed_in) if seed_in.isdigit() else random.randint(0, 2**48)
        wf[ids["seed"]]["inputs"]["noise_seed"] = seed
        set_job(seed=seed)
        try:
            wf["115"]["inputs"]["megapixels"] = float(params.get("quality_mp", 0.4))
        except (TypeError, ValueError):
            pass
        try:
            wf[ids["steps"]]["inputs"]["steps"] = max(10, min(30, int(params.get("steps", 20))))
        except (TypeError, ValueError):
            pass
        wf["115"]["inputs"]["aspect_ratio"] = pick_aspect(cfg, params.get("aspect", "16:9"))

        if mode in ("i2v", "flf2v"):
            if not image_blob:
                raise RunPodError("画像が選択されていません")
            set_job(state="uploading", message="画像をアップロード中…")
            up = comfy_upload_image(cfg, image_name or "input.png", image_blob)
            name = up.get("name")
            sub = up.get("subfolder") or ""
            wf["114"]["inputs"]["image"] = (sub + "/" + name) if sub else name

        if mode == "flf2v":
            import base64
            last_b64 = params.get("last_image_b64")
            if not last_b64:
                raise RunPodError("「最後の画像」が選択されていません")
            up2 = comfy_upload_image(cfg, params.get("last_image_name") or "last.png",
                                     base64.b64decode(last_b64))
            name2 = up2.get("name")
            sub2 = up2.get("subfolder") or ""
            wf["116"] = {"inputs": {"image": (sub2 + "/" + name2) if sub2 else name2},
                         "class_type": "LoadImage", "_meta": {"title": "最後の画像"}}
            wf["105:104"]["inputs"]["last_frame"] = ["116", 0]

        if params.get("fast_mode"):
            # EasyCache高速化ノードをモデルの直後に差し込む
            unet_id = "127" if mode in ("r2v", "edit") else "105:6"
            wf["300"] = {"inputs": {"model": [unet_id, 0], "reuse_threshold": 0.2,
                                    "start_percent": 0.15, "end_percent": 0.95, "verbose": False},
                         "class_type": "EasyCache", "_meta": {"title": "EasyCache高速化"}}
            for nid, node in wf.items():
                if nid == "300":
                    continue
                m = node.get("inputs", {}).get("model")
                if isinstance(m, list) and m and m[0] == unet_id:
                    node["inputs"]["model"] = ["300", 0]

        if mode in ("r2v", "edit"):
            import base64
            manual_refs = (params.get("ref_images") or [])[:4]
            # エレメントの画像 + その場で選んだ画像 を1つの参照リストに統合する
            all_refs = []       # [{"fname":…, "blob":…}]
            elem_slots = []     # (エレメント, [参照リスト上のindex,…])
            for el in elements:
                idxs = []
                for fn in el.get("images", [])[:3]:
                    fp = os.path.join(ELEMENTS_DIR, fn)
                    if os.path.exists(fp):
                        with open(fp, "rb") as f:
                            all_refs.append({"fname": fn, "blob": f.read()})
                        idxs.append(len(all_refs) - 1)
                if idxs:
                    elem_slots.append((el, idxs))
            manual_slots = []   # (@名札, 参照リスト上のindex)
            for ref in manual_refs:
                all_refs.append({"fname": ref.get("name") or "ref.png", "blob": base64.b64decode(ref["b64"])})
                manual_slots.append(((ref.get("tag") or "").strip().lstrip("@"), len(all_refs) - 1))
            sb_slots = []       # 絵コンテ: カット番号順の参照リスト上のindex
            if mode == "r2v":
                for sb in storyboard:
                    all_refs.append({"fname": sb.get("name") or "cut.png", "blob": base64.b64decode(sb["b64"])})
                    sb_slots.append(len(all_refs) - 1)
            if mode == "r2v" and not all_refs:
                raise RunPodError("参照画像が選択されていません（エレメントを選ぶか、画像を1〜4枚選択）")
            if len(all_refs) > 8:
                raise RunPodError(f"参照画像が多すぎます（エレメント含め合計8枚まで。現在{len(all_refs)}枚）。"
                                  "エレメントや画像を減らしてください")
            # 参照画像を縮小せず高解像度のまま渡す（顔の一致度が大きく向上する公式推奨設定）
            wf["136"]["inputs"]["ref_image_size"] = "max"
            set_job(state="uploading", message="参照素材をアップロード中…")
            for i, ref in enumerate(all_refs):
                up = comfy_upload_image(cfg, ref["fname"], ref["blob"])
                name = up.get("name")
                sub = up.get("subfolder") or ""
                node_id = str(200 + i)
                wf[node_id] = {"inputs": {"image": (sub + "/" + name) if sub else name},
                               "class_type": "LoadImage", "_meta": {"title": f"参照画像{i + 1}"}}
                wf["136"]["inputs"][f"ref_images.ref_image_{i}"] = [node_id, 0]
            pics = "、".join(f"<Picture {i + 1}>" for i in range(len(all_refs)))

            # --- Seedance流：素材ごとに役割を宣言し、「@名前」で本文から呼べるようにする ---
            ELEM_ROLE = {"人物": "本文に登場するこの人物の顔・髪型・体型・雰囲気は、この画像と完全に同一にする",
                         "場所": "シーンの場所・背景として、この画像の場所を正確に再現する",
                         "物": "この物・衣装を形・色・質感まで正確に再現して登場させる",
                         "スタイル": "映像全体の画風・色調・質感をこの画像の雰囲気に合わせる"}
            defs = []
            tag_map = []
            for el, idxs in elem_slots:
                pstr = "、".join(f"<Picture {i + 1}>" for i in idxs)
                role = ELEM_ROLE.get(el.get("type") or "人物", "この画像を参照素材として使う")
                memo = f"。補足情報: {el['memo']}" if el.get("memo") else ""
                defs.append(f"「{el['name']}」（{el.get('type', '人物')}）= {pstr}。{role}{memo}")
                tag_map.append((el["name"], f"「{el['name']}」"))
            for t, idx in manual_slots:
                if t:
                    tag_map.append((t, f"<Picture {idx + 1}>"))
            # 絵コンテ: 各カットの構図指定として役割宣言し、@カット1 でも呼べるようにする
            if sb_slots:
                sec = float(params.get("seconds", 5))
                per = max(2, round(sec / len(sb_slots)))
                for k, idx in enumerate(sb_slots):
                    defs.append(f"カット{k + 1} = <Picture {idx + 1}>。このカットの構図・カメラアングル・"
                                f"人物や物の配置はこの絵コンテ画像に従う（絵柄・画風はコピーしない）")
                    tag_map.append((f"カット{k + 1}", f"<Picture {idx + 1}>"))
                prompt_text = (f"絵コンテ指定: 動画を{len(sb_slots)}個のカットで順番に構成する。"
                               f"各カットは約{per}秒。カットの構図は上記の絵コンテ定義に従い、"
                               "本文に時間指定があればそちらを優先する。\n\n" + prompt_text)
            if mode == "edit":
                tag_map += [("元の動画", "<Video 1>"), ("元動画", "<Video 1>")]
            # @名前 → 正式タグ（ひらがな/カタカナ・大文字小文字の表記ゆれも吸収）
            prompt_text = replace_at_tags(prompt_text, tag_map)
            # @なしの「参照画像」「元動画」という普通の言葉も正式タグに自動変換して確実に紐付ける
            if all_refs:
                prompt_text = prompt_text.replace("参照画像", pics)
            if mode == "edit":
                prompt_text = prompt_text.replace("元の動画", "<Video 1>").replace("元動画", "<Video 1>")
            if defs:
                prompt_text = "参照素材の役割定義:\n- " + "\n- ".join(defs) + "\n\n" + prompt_text

        if mode == "r2v" and not elem_slots and not sb_slots:
            prompt_text = (f"参照画像（{pics}）に写っている人物・キャラクターと完全に同一の外見"
                           f"（顔、髪型、体型、服装）を維持して登場させる。\n\n" + prompt_text)

        if mode == "edit":
            ref_video_b64 = params.get("ref_video_b64")
            if not ref_video_b64:
                raise RunPodError("編集する元動画が選択されていません")
            set_job(state="uploading", message="元動画をアップロード中…（サイズにより数十秒）")
            src_video_bytes = base64.b64decode(ref_video_b64)
            if params.get("keep_audio"):
                # 完成後に元動画の音声を貼り直すため、元動画を一時保存しておく
                src_video_path = os.path.join(OUTPUT_DIR, "_src_audio_tmp.mp4")
                with open(src_video_path, "wb") as f:
                    f.write(src_video_bytes)
            vup = comfy_upload_image(cfg, params.get("ref_video_name") or "ref_video.mp4",
                                     src_video_bytes)
            vname = vup.get("name")
            vsub = vup.get("subfolder") or ""
            wf["210"] = {"inputs": {"file": (vsub + "/" + vname) if vsub else vname},
                         "class_type": "LoadVideo", "_meta": {"title": "元動画"}}
            # ref_videosは「コマ画像に分解した形(IMAGE)」を要求するため変換を挟む
            wf["211"] = {"inputs": {"video": ["210", 0]},
                         "class_type": "GetVideoComponents", "_meta": {"title": "動画をコマに分解"}}
            wf["136"]["inputs"]["ref_videos.ref_video_0"] = ["211", 0]
            if all_refs:
                target = (f"人物の指定された部分だけを{pics}の人物に置き換える。顔立ち・目鼻・輪郭・肌の色は、"
                          f"クリップの最初から最後まで一貫して{pics}に完全一致させること。"
                          f"<Video 1>に映っている元の人物の顔は一切残さず、使用しない。"
                          f"(Replace only the specified parts of the person with the character shown in {pics}. "
                          f"Match the new face exactly to {pics} throughout the entire clip. "
                          f"Do not keep or reuse the original person's face from <Video 1>.)")
            else:
                target = "変更内容は以下の指示文に正確に従う。"
            # 元動画で口が動いていると、AIが意味不明な言語の音声を後付けしてしまう対策。
            # セリフ指定があればそれを、「セリフなし」指定なら無音を、どちらも無ければ自然な日本語を優先させる
            d_edit = (params.get("dialogue") or "").strip()
            mix_edit = params.get("mix", "auto")
            if d_edit or mix_edit in ("no_speech", "silent"):
                voice_rule = ""
            else:
                voice_rule = ("音声の規則: <Video 1>の人物が口を動かしている場合は、その口の動きに合わせて"
                              "自然で意味の通る日本語だけを話させる。中国語・英語・実在しない言語の発話は"
                              "絶対に禁止。口が動いていない場合は誰も話さず、環境音のみとする。")
            prompt_text = ("[video editing + attribute transfer] <Video 1>を演技とシーンのマスターとする。"
                           "動き、カメラワーク、構図、シーンの進行、タイミング、背景、照明はすべて<Video 1>から"
                           f"継承し、新しい動きを追加しない。{target}{voice_rule}\n\n"
                           "変更の指示: " + prompt_text)

        wf[ids["prompt"]]["inputs"]["prompt" if mode not in ("r2v", "edit") else "value"] = prompt_text

        # 送信前の安全網: 仮置き文字(PLACEHOLDER)が残っていたら、設定の書き込み漏れなので送信せず止める
        leftovers = [nid for nid, node in wf.items()
                     for v in node.get("inputs", {}).values()
                     if isinstance(v, str) and "PLACEHOLDER" in v]
        if leftovers:
            raise RunPodError("【内部エラー】設定がワークフローに反映されていません（ノード"
                              + "、".join(leftovers) + "）。ツールの不具合のため送信前に中止しました。"
                              "開発担当に連絡してください")

        set_job(state="generating", message="動画を生成中…（5秒動画で約5分。初回はさらに+2分）")
        try:
            res = http_json(cfg["comfy_url"] + "/prompt", method="POST",
                            body={"prompt": wf, "client_id": "h3tool"}, timeout=60)
        except urllib.error.HTTPError as e:
            raise RunPodError(friendly_comfy_error(e))
        pid = res.get("prompt_id")
        if not pid:
            raise RunPodError("生成リクエストが受け付けられませんでした: " + json.dumps(res, ensure_ascii=False)[:300])

        deadline = time.time() + 2400
        entry = None
        fails = 0
        last_step = 0
        while time.time() < deadline:
            time.sleep(8)
            # 進捗の実況: 順番待ち→ステップ進捗→経過時間 の順で分かるものを表示
            try:
                q = comfy_get(cfg, "/queue", timeout=10)
                pending_ids = [x[1] for x in (q.get("queue_pending") or [])]
                if pid in pending_ids:
                    set_job(message=f"順番待ち中…（あなたの前に{pending_ids.index(pid) + len(q.get('queue_running') or [])}件）")
                else:
                    prog = comfy_progress(cfg)
                    if prog and prog[0] >= last_step:
                        cur, tot = prog
                        last_step = cur
                        pct = int(cur / tot * 100)
                        bar = "▓" * (pct // 10) + "░" * (10 - pct // 10)
                        set_job(message=f"動画を生成中… ステップ {cur}/{tot}（{pct}%） {bar}")
                    else:
                        mins = int((time.time() - (JOB.get('started_at') or time.time())) // 60)
                        set_job(message=f"動画を生成中…（経過{mins}分。モデル読み込み中か仕上げ処理中です）")
            except Exception:
                pass
            try:
                hist = comfy_get(cfg, f"/history/{pid}", timeout=30)
                fails = 0
            except Exception:
                fails += 1
                if fails >= 5:  # 約40秒連続で応答なし → サーバーの生死を確認
                    try:
                        if pod_status(cfg) != "RUNNING":
                            raise RunPodError(
                                "【中断】生成の途中でサーバーが停止しました（クラウド側の障害、"
                                "または誰かが停止した可能性）。動画は次回サーバー起動時に回収できる"
                                "場合があります。もう一度生成するか、Claudeに相談してください")
                    except RunPodError as pod_err:
                        if "中断" in str(pod_err):
                            raise
                    fails = 0
                continue
            if pid in hist:
                entry = hist[pid]
                st = entry.get("status", {})
                if st.get("completed") or st.get("status_str") == "success":
                    break
                if st.get("status_str") == "error":
                    raise RunPodError("生成中にエラーが発生しました。プロンプトや秒数を変えてお試しください。")
        if not entry:
            raise RunPodError("生成がタイムアウトしました")

        vid = find_video_in_history(entry)
        if not vid:
            raise RunPodError("動画ファイルが見つかりませんでした")

        set_job(state="downloading", message="動画をPCに保存中…")
        from urllib.parse import urlencode
        q = urlencode({"filename": vid["filename"], "subfolder": vid.get("subfolder", ""), "type": vid.get("type", "output")})
        local_name = time.strftime("%Y%m%d_%H%M%S") + "_" + re.sub(r"[^A-Za-z0-9._-]", "_", vid["filename"])
        local_path = os.path.join(OUTPUT_DIR, local_name)
        dl_req = urllib.request.Request(cfg["comfy_url"] + "/view?" + q,
                                        headers={"User-Agent": UA})
        with urllib.request.urlopen(dl_req, timeout=300) as r, open(local_path, "wb") as f:
            f.write(r.read())

        # 部分編集の「元動画の音声をそのまま使う」: AIの作った音声を元動画の音声に差し替える
        audio_note = ""
        if mode == "edit" and src_video_path and os.path.exists(src_video_path):
            import shutil
            import subprocess
            set_job(message="元動画の音声を貼り付け中…")
            if shutil.which("ffmpeg"):
                tmp_out = local_path + ".audio.mp4"
                try:
                    r = subprocess.run(
                        ["ffmpeg", "-y", "-v", "error", "-i", local_path, "-i", src_video_path,
                         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
                         "-shortest", tmp_out],
                        capture_output=True, timeout=180)
                    if r.returncode == 0 and os.path.exists(tmp_out) and os.path.getsize(tmp_out) > 0:
                        os.replace(tmp_out, local_path)
                        audio_note = "（音声は元動画のまま）"
                    else:
                        audio_note = "（注意：音声の貼り付けに失敗したためAIの音声のままです）"
                except Exception:
                    audio_note = "（注意：音声の貼り付けに失敗したためAIの音声のままです）"
                finally:
                    if os.path.exists(tmp_out):
                        try:
                            os.remove(tmp_out)
                        except OSError:
                            pass
            else:
                audio_note = "（注意：このPCに音声処理ソフトffmpegが無いためAIの音声のままです）"

        if params.get("auto_stop"):
            # 他の人（別PC）の生成がキューに残っていたら停止しない
            others_busy = False
            try:
                q = comfy_get(cfg, "/queue", timeout=15)
                others_busy = bool(q.get("queue_running")) or bool(q.get("queue_pending"))
            except Exception:
                pass
            if others_busy:
                set_job(message="完成！（他の生成が動作中のためサーバーは停止しません）", video=local_name)
            else:
                set_job(state="stopping_pod", message="サーバーを自動停止しています…", video=local_name)
                try:
                    runpod_call(cfg, f"/pods/{cfg['pod_id']}/stop", method="POST")
                except Exception:
                    pass

        append_history({
            "time": time.strftime("%Y-%m-%d %H:%M"),
            "video": local_name,
            "seed": seed,
            "mode": mode,
            "prompt": params.get("prompt", ""),
            "dialogue": params.get("dialogue", ""),
            "voice": params.get("voice", ""),
            "bgm": params.get("bgm", ""),
            "se": params.get("se", ""),
            "mix": params.get("mix", "auto"),
            "seconds": params.get("seconds", 5),
            "aspect": params.get("aspect", "16:9"),
            "quality_mp": params.get("quality_mp", "0.4"),
            "steps": params.get("steps", "20"),
            "fast": bool(params.get("fast_mode")),
            "keep_audio": bool(params.get("keep_audio")),
            "elements": [e.get("name") for e in elements],
            "element_ids": element_ids,
        })
        set_job(state="done", message="完成！" + audio_note, video=local_name, error=None)
    except RunPodError as e:
        set_job(state="error", message="", error=str(e))
    except urllib.error.HTTPError as e:
        set_job(state="error", message="",
                error=f"【通信エラー】サーバーが応答を拒否しました（HTTP {e.code}）。もう一度お試しください")
    except urllib.error.URLError as e:
        set_job(state="error", message="",
                error=f"【接続エラー】サーバーに接続できませんでした（{getattr(e, 'reason', e)}）。ネット接続とサーバー状態を確認してください")
    except TimeoutError:
        set_job(state="error", message="", error="【タイムアウト】処理が時間内に終わりませんでした。もう一度お試しください")
    except Exception as e:
        set_job(state="error", message="", error=f"【不明なエラー】{type(e).__name__}: {e}")
    finally:
        if src_video_path and os.path.exists(src_video_path):
            try:
                os.remove(src_video_path)
            except OSError:
                pass


# ---------- HTTPハンドラ ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        cfg = load_config()
        if path == "/":
            with open(os.path.join(BASE_DIR, "index.html"), "r", encoding="utf-8") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/api/config":
            self._send(200, {"has_key": bool(cfg.get("api_key")), "auto_stop": cfg.get("auto_stop", True),
                             "version": SELF_VERSION})
        elif path == "/api/status":
            out = {"job": dict(JOB)}
            if not cfg.get("api_key"):
                out["pod"] = "NO_KEY"
            else:
                out["balance"] = get_balance(cfg)
                try:
                    out["pod"] = pod_status(cfg)
                except RunPodError:
                    # Podが作り直されてIDが変わった可能性 → ボリュームから探し直す
                    try:
                        pod = resolve_pod(cfg)
                        out["pod"] = pod.get("desiredStatus", "UNKNOWN") if pod else "MISSING"
                    except RunPodError as e:
                        out["pod"] = "ERROR"
                        out["pod_error"] = str(e)
            self._send(200, out)
        elif path == "/api/history":
            self._send(200, {"history": list(reversed(load_history()))})
        elif path == "/api/elements":
            self._send(200, {"elements": load_elements()})
        elif path == "/manual":
            mp = os.path.join(ROOT_DIR, "説明書.html")
            if os.path.exists(mp):
                with open(mp, "r", encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            else:
                self._send(404, {"error": "説明書が見つかりません"})
        elif path.startswith("/elements/"):
            name = os.path.basename(path[len("/elements/"):])
            fp = os.path.join(ELEMENTS_DIR, name)
            if os.path.exists(fp):
                ctype = "image/png" if name.lower().endswith(".png") else "image/jpeg"
                with open(fp, "rb") as f:
                    self._send(200, f.read(), ctype)
            else:
                self._send(404, {"error": "not found"})
        elif path == "/api/videos":
            files = sorted((f for f in os.listdir(OUTPUT_DIR) if f.lower().endswith(".mp4")), reverse=True)
            self._send(200, {"videos": files[:30]})
        elif path.startswith("/videos/"):
            name = os.path.basename(path[len("/videos/"):])
            fp = os.path.join(OUTPUT_DIR, name)
            if os.path.exists(fp):
                with open(fp, "rb") as f:
                    self._send(200, f.read(), "video/mp4")
            else:
                self._send(404, {"error": "not found"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        cfg = load_config()
        try:
            if path == "/api/setkey":
                data = json.loads(raw)
                key = data.get("api_key", "").strip()
                if not key:
                    self._send(400, {"error": "APIキーが空です"})
                    return
                cfg["api_key"] = key
                try:
                    # キーの有効性確認を兼ねて、共有ボリュームから現在のPodを自動発見する
                    pod = resolve_pod(cfg)
                    st = pod.get("desiredStatus", "UNKNOWN") if pod else "MISSING"
                except RunPodError as e:
                    self._send(400, {"error": str(e)})
                    return
                save_config(cfg)
                self._send(200, {"ok": True, "pod": st})
            elif path == "/api/clear_error":
                if JOB["state"] == "error":
                    set_job(state="idle", message="", error=None)
                self._send(200, {"ok": True})
            elif path == "/api/start":
                if JOB["state"] in ("starting_pod", "waiting_comfy", "uploading", "generating", "downloading", "stopping_pod"):
                    self._send(409, {"error": "処理が進行中です"})
                    return

                def manual_start():
                    c = load_config()
                    try:
                        ensure_pod_running(c)
                        set_job(state="idle", message="サーバー起動完了", error=None)
                    except Exception as e:
                        set_job(state="error", error=str(e))

                set_job(state="starting_pod", message="サーバーを起動しています…",
                        started_at=time.time(), error=None)
                threading.Thread(target=manual_start, daemon=True).start()
                self._send(200, {"ok": True})
            elif path == "/api/stop":
                # 誰かの生成が動いている間は停止を拒否する（共有利用の事故防止）
                try:
                    if comfy_alive(cfg):
                        q = comfy_get(cfg, "/queue", timeout=10)
                        busy = len(q.get("queue_running") or []) + len(q.get("queue_pending") or [])
                        if busy:
                            self._send(409, {"error": (
                                f"いま誰かが動画を生成中のため停止しませんでした（実行中＋順番待ち: {busy}件）。"
                                "生成が終わるのを待つか、生成した人の「自動停止」に任せてください")})
                            return
                except RunPodError:
                    raise
                except Exception:
                    pass  # キューを確認できない場合（エンジン起動前など）は通常どおり停止できる
                runpod_call(cfg, f"/pods/{cfg['pod_id']}/stop", method="POST")
                self._send(200, {"ok": True})
            elif path == "/api/elements":
                # エレメント登録: {name, type, memo, images: [{name, b64}, ...]}
                import base64
                data = json.loads(raw)
                ename = (data.get("name") or "").strip().lstrip("@")
                if not ename:
                    self._send(400, {"error": "エレメントの名前を入力してください"})
                    return
                images = data.get("images") or []
                if not images:
                    self._send(400, {"error": "画像を1枚以上選択してください"})
                    return
                items = load_elements()
                if any(x.get("name") == ename for x in items):
                    self._send(400, {"error": f"「{ename}」という名前は登録済みです。別の名前にするか、先に削除してください"})
                    return
                os.makedirs(ELEMENTS_DIR, exist_ok=True)
                eid = "el" + time.strftime("%Y%m%d%H%M%S")
                fnames = []
                for i, img in enumerate(images[:3]):
                    ext = ".png" if (img.get("name") or "").lower().endswith(".png") else ".jpg"
                    fn = f"{eid}_{i}{ext}"
                    with open(os.path.join(ELEMENTS_DIR, fn), "wb") as f:
                        f.write(base64.b64decode(img["b64"]))
                    fnames.append(fn)
                items.append({"id": eid, "name": ename, "type": data.get("type") or "人物",
                              "memo": (data.get("memo") or "").strip(), "images": fnames})
                save_elements(items)
                self._send(200, {"ok": True, "elements": items})
            elif path == "/api/elements/update":
                # エレメント編集: {id, name, type, memo, images?(あれば全差し替え)}
                import base64
                data = json.loads(raw)
                items = load_elements()
                target = next((x for x in items if x.get("id") == data.get("id")), None)
                if not target:
                    self._send(404, {"error": "エレメントが見つかりません"})
                    return
                ename = (data.get("name") or "").strip().lstrip("@")
                if not ename:
                    self._send(400, {"error": "エレメントの名前を入力してください"})
                    return
                if any(x.get("name") == ename and x.get("id") != target["id"] for x in items):
                    self._send(400, {"error": f"「{ename}」という名前は別のエレメントで使用中です"})
                    return
                target["name"] = ename
                target["type"] = data.get("type") or target.get("type") or "人物"
                target["memo"] = (data.get("memo") or "").strip()
                new_images = data.get("images")
                if new_images:
                    for fn in target.get("images", []):
                        fp = os.path.join(ELEMENTS_DIR, fn)
                        if os.path.exists(fp):
                            try:
                                os.remove(fp)
                            except OSError:
                                pass
                    os.makedirs(ELEMENTS_DIR, exist_ok=True)
                    fnames = []
                    for i, img in enumerate(new_images[:3]):
                        ext = ".png" if (img.get("name") or "").lower().endswith(".png") else ".jpg"
                        fn = f"{target['id']}_{i}{ext}"
                        with open(os.path.join(ELEMENTS_DIR, fn), "wb") as f:
                            f.write(base64.b64decode(img["b64"]))
                        fnames.append(fn)
                    target["images"] = fnames
                save_elements(items)
                self._send(200, {"ok": True, "elements": items})
            elif path == "/api/elements/delete":
                data = json.loads(raw)
                items = load_elements()
                keep = []
                for x in items:
                    if x.get("id") == data.get("id"):
                        for fn in x.get("images", []):
                            fp = os.path.join(ELEMENTS_DIR, fn)
                            if os.path.exists(fp):
                                try:
                                    os.remove(fp)
                                except OSError:
                                    pass
                    else:
                        keep.append(x)
                save_elements(keep)
                self._send(200, {"ok": True, "elements": keep})
            elif path == "/api/generate":
                if JOB["state"] in ("starting_pod", "waiting_comfy", "uploading", "generating", "downloading", "stopping_pod"):
                    self._send(409, {"error": "生成が進行中です"})
                    return
                data = json.loads(raw)
                cfg["auto_stop"] = bool(data.get("auto_stop", True))
                save_config(cfg)
                image_blob = None
                image_name = data.get("image_name")
                if data.get("image_b64"):
                    import base64
                    image_blob = base64.b64decode(data["image_b64"])
                set_job(state="starting_pod", message="準備中…", started_at=time.time(), video=None, error=None)
                threading.Thread(target=run_generation, args=(data, image_blob, image_name), daemon=True).start()
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})
        except RunPodError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": str(e)})


def main():
    # 起動時の自動アップデート（更新があれば新しい自分で再起動）
    if os.environ.get("H3_NO_UPDATE") != "1" and self_update():
        os.environ["H3_NO_UPDATE"] = "1"  # 再起動ループ防止
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        # すでに起動済み → 画面だけ開いて終了
        print("すでに起動しています。ブラウザで画面を開きます。")
        webbrowser.open(f"http://localhost:{PORT}")
        time.sleep(2)
        return
    url = f"http://localhost:{PORT}"
    print("=" * 46)
    print("  H3かんたん動画メーカー 起動中")
    print(f"  ブラウザで {url} を開いてください")
    print("  （このウィンドウは閉じないでください）")
    print("=" * 46)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
