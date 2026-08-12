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
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SELF_VERSION = "2.3.0"
UPDATE_REPO_RAW = "https://raw.githubusercontent.com/novaongats/h3-video-tool/main"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
OUTPUT_DIR = os.path.join(ROOT_DIR, "outputs")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
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
POD_GPU = "NVIDIA H100 80GB HBM3"
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
        "gpuTypeIds": [POD_GPU],
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


def compose_prompt(p):
    """フォームの各欄から、MiniMax公式プロンプトガイド準拠の形式に組み立てる。
    公式形式: 本文（Shot構成・話者ID・<d>タグのセリフ） + overall_soundscape + non_diegetic_music
    """
    mix = p.get("mix", "auto")
    parts = [p.get("prompt", "").strip()]

    d = p.get("dialogue", "").strip()
    if d:
        v = p.get("voice", "").strip()
        who = f"登場人物 (S1)（{v}）" if v else "登場人物 (S1)"
        parts.append(f"{who} がカメラに向かってはっきりと話す。口の動きはセリフに正確に同期する："
                     f"<d>[Japanese] {d}</d>")

    if mix in ("no_speech", "silent"):
        parts.append("この動画では誰も一切話さない。セリフ、ナレーション、実況、ボイスオーバー、"
                     "歌声、人の声を絶対に入れない。")
    elif mix == "voice_first":
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
    try:
        ensure_pod_running(cfg)

        mode = params.get("mode", "t2v")
        wf_file = {"i2v": "wf_i2v.json", "flf2v": "wf_i2v.json", "r2v": "wf_r2v.json"}.get(mode, "wf_t2v.json")
        with open(os.path.join(BASE_DIR, wf_file), "r", encoding="utf-8") as f:
            wf = json.load(f)

        # ノードIDがモードで異なる
        ids = {"prompt": "105:104", "dur": "105:111", "seed": "105:15", "steps": "105:9"}
        if mode == "r2v":
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
            unet_id = "127" if mode == "r2v" else "105:6"
            wf["300"] = {"inputs": {"model": [unet_id, 0], "reuse_threshold": 0.2,
                                    "start_percent": 0.15, "end_percent": 0.95, "verbose": False},
                         "class_type": "EasyCache", "_meta": {"title": "EasyCache高速化"}}
            for nid, node in wf.items():
                if nid == "300":
                    continue
                m = node.get("inputs", {}).get("model")
                if isinstance(m, list) and m and m[0] == unet_id:
                    node["inputs"]["model"] = ["300", 0]

        if mode == "r2v":
            import base64
            refs = params.get("ref_images") or []
            if not refs:
                raise RunPodError("参照画像が選択されていません（1〜4枚）")
            set_job(state="uploading", message="参照画像をアップロード中…")
            for i, ref in enumerate(refs[:4]):
                blob = base64.b64decode(ref["b64"])
                up = comfy_upload_image(cfg, ref.get("name") or f"ref{i}.png", blob)
                name = up.get("name")
                sub = up.get("subfolder") or ""
                node_id = str(200 + i)
                wf[node_id] = {"inputs": {"image": (sub + "/" + name) if sub else name},
                               "class_type": "LoadImage", "_meta": {"title": f"参照画像{i + 1}"}}
                wf["136"]["inputs"][f"ref_images.ref_image_{i}"] = [node_id, 0]
            pics = "、".join(f"<Picture {i + 1}>" for i in range(len(refs[:4])))
            prompt_text = (f"参照画像（{pics}）に写っている人物・キャラクターと完全に同一の外見"
                           f"（顔、髪型、体型、服装）を維持して登場させる。\n\n" + prompt_text)

        wf[ids["prompt"]]["inputs"]["prompt" if mode != "r2v" else "value"] = prompt_text

        set_job(state="generating", message="動画を生成中…（5秒動画で約5分。初回はさらに+2分）")
        res = http_json(cfg["comfy_url"] + "/prompt", method="POST",
                        body={"prompt": wf, "client_id": "h3tool"}, timeout=60)
        pid = res.get("prompt_id")
        if not pid:
            raise RunPodError("生成リクエストが受け付けられませんでした: " + json.dumps(res, ensure_ascii=False)[:300])

        deadline = time.time() + 2400
        entry = None
        while time.time() < deadline:
            time.sleep(8)
            try:
                hist = comfy_get(cfg, f"/history/{pid}", timeout=30)
            except Exception:
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
        })
        set_job(state="done", message="完成！", video=local_name, error=None)
    except Exception as e:
        set_job(state="error", message="", error=str(e))


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
            self._send(200, {"has_key": bool(cfg.get("api_key")), "auto_stop": cfg.get("auto_stop", True)})
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
                runpod_call(cfg, f"/pods/{cfg['pod_id']}/stop", method="POST")
                self._send(200, {"ok": True})
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
