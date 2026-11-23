"""Assemble silent picture-book shots and resume work with the selected video service."""
import hashlib
import json
import math
import re
import time
import uuid

from PIL import Image

import providers as api

MODELS = {"heygen": api.HEYGEN, "minimax": api.VIDEO}
LABELS = {"heygen": "HeyGen", "minimax": "MiniMax H3"}


def directory_record(root, service, key):
    prefix = "heygen" if service == "heygen" else "h3"
    return root / "cache" / (prefix + "-" + key + ".json")


def estimate_seconds(script, languages):
    lengths = []
    for lang in languages:
        lines = [line[lang] for line in script["lines"]]
        if lang == "zh":
            spoken = sum(len(re.sub(r"[^\w]", "", line)) for line in lines) / 3.5
        else:
            spoken = sum(len(line.split()) for line in lines) / 2.2
        lengths.append(spoken + .22 * len(lines))
    return math.ceil(max(lengths))


def pin_duration(scene, languages):
    if scene.get("planned_video_seconds"):
        return False
    if all(lang in scene.get("audio",{}) for lang in languages):
        seconds = math.ceil(max(track["duration"] for track in scene["audio"].values()))
    else:
        seconds = estimate_seconds(scene.get("script") or scene["cue_plan"],languages)
    scene["planned_video_seconds"] = seconds
    return True


def frame_recipe(scene, service):
    low, high = (5, 30) if service == "heygen" else (4, 15)
    measured = scene.get("planned_video_seconds") or max(track["duration"] for track in scene["audio"].values())
    seconds = min(high, max(low, math.ceil(measured)))
    prompt = scene["script"]["video_prompt"] + f"\nSingle continuous shot, {seconds} seconds. Hold the final composition. Silent; no new lettering, captions or speech balloons."
    return seconds, prompt


def render_recipe(scene, service):
    seconds, prompt = frame_recipe(scene, service)
    revision = [scene["animation_revision"]] if scene.get("animation_revision") else []
    key = api.digest(MODELS[service], scene["frame"], prompt, seconds, *revision)
    return key, seconds, prompt


def normalized_film(root, source, key):
    target = root / "assets" / ("film-" + key + ".mp4")
    if not target.is_file():
        with source.open("rb") as src:
            if src.read(12)[4:8] != b"ftyp":
                raise ValueError("请选择 HeyGen 导出的 MP4 视频。")
        probe = api.run_binary(["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-f", "mov", "-select_streams", "v:0",
                                "-show_entries", "stream=duration", "-of", "csv=p=0", str(source)], capture_output=True, timeout=30)
        try:
            seconds = float(probe.stdout.strip()) if not probe.returncode else 0
        except ValueError:
            seconds = 0
        if not math.isfinite(seconds) or not 0 < seconds <= 32:
            raise ValueError("请导入一个可播放的单幕 MP4。最长生成时长为 30 秒，允许编码时的尾帧余量。")
        temporary = target.with_name(target.stem + "." + uuid.uuid4().hex + ".mp4")
        try:
            run = api.run_binary(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-protocol_whitelist", "file",
                   "-f", "mov", "-i", str(source), "-map", "0:v:0", "-an", "-t", "32", "-vf",
                   "scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary)], capture_output=True, timeout=180)
            if run.returncode or not temporary.is_file() or temporary.stat().st_size < 1000:
                raise ValueError("视频转码失败。请检查下载的 MP4 能否播放，再导入这一幕。")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return target


def attach_heygen(root, scene, source, task_id="", sent_prompt="", requested_seconds=None):
    if not scene.get("frame") or not scene.get("script"):
        raise ValueError("请先确认本幕首帧和动作提示词。")
    if task_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
        raise ValueError("HeyGen 视频编号格式无效。")
    with source.open("rb") as stream:
        content = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            content.update(chunk)
    key = api.digest(api.HEYGEN, scene["frame"], content.hexdigest())
    target = normalized_film(root, source, key)
    if scene.get("video") and scene["video"] != target.name:
        raise ValueError("本幕已有动画，请选择对应的待补场景。")
    meta = api.run_binary(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration", "-of", "csv=p=0", str(target)], capture_output=True, timeout=30)
    if meta.returncode:
        raise ValueError("未能读取片段时长。")
    actual = float(meta.stdout.strip())
    if not math.isfinite(actual) or actual<=0:
        raise ValueError("片段没有可播放时长。")
    details = {"provider":"heygen", "model":api.HEYGEN, "id":task_id, "input_frame":scene["frame"], "delivery":"import"}
    if sent_prompt:
        details["prompt"] = sent_prompt
    if requested_seconds:
        details["requested_seconds"] = requested_seconds
    scene.update(video=target.name, video_seconds=actual, video_source=details,
                 task={"provider":"heygen", "key":key, "id":task_id, "state":"succeeded"})


def animate(root, scene, service, notify, save):
    if scene.get("video") and (root / "assets" / scene["video"]).is_file():
        return
    old = scene.get("task", {})
    if old.get("state") in {"submitting", "pending"} and old.get("provider", "minimax") != service:
        raise ValueError("本幕在另一家平台有待处理任务。请先核对原任务或导入已完成的 MP4。")
    model, label = MODELS[service], LABELS[service]
    key, seconds, prompt = render_recipe(scene, service)
    if old.get("state") in {"pending","submitting"} and old.get("key") and old["key"] != key:
        raise ValueError(f"{label} 已有本幕的待处理任务。请核对原任务和首帧后再提交。")
    path = directory_record(root, service, key)
    journal = json.loads(path.read_bytes()) if path.exists() else {}
    auth = None
    if service=="minimax":
        if journal.get("state") in {"submitting","pending","succeeded"}:
            auth = journal.get("auth","subscription")
        else:
            auth = "h3"
    def checkpoint(state, **fields):
        record = {"provider":service, "key":key, "state":state, **fields}
        if auth:
            record["auth"] = auth
        api.save_json(path, record)
        scene["task"] = record
        save()
    def finish(target, task):
        scene.update(video=target.name, video_seconds=seconds, video_source={"provider":service, "model":model,
                     "id":task, "prompt":prompt, "requested_seconds":seconds, "input_frame":scene["frame"], "delivery":"api"})
        checkpoint("succeeded", id=task)
    cached = root/"assets"/f"film-{key}.mp4"
    if journal.get("state")=="succeeded" and journal.get("id") and cached.is_file():
        finish(cached,journal["id"])
        return
    if journal.get("state") == "submitting":
        raise ValueError(f"{label} 的提交回执中断。请在本幕关联控制台任务号，或核对未受理后重提。")
    task = journal.get("id") if journal.get("state") != "failed" else None
    if not task:
        if service == "heygen" and not api.setting(*api.NAMES["HeyGen"]):
            raise ValueError("本机直连 HeyGen 需要用户变量 HEYGEN_API_KEY。已有 HeyGen 成片时可在本页选择场景并导入 MP4。")
        checkpoint("submitting", id="")
        try:
            with Image.open(root / "assets" / scene["frame"]) as picture:
                task = api.start_heygen(picture,prompt,seconds) if service=="heygen" else api.start_video(picture,prompt,seconds,auth)
        except Exception as error:
            if isinstance(error, api.ProviderRejected) or re.search(r"curl (6|7|35)\b|用户环境变量", str(error)):
                checkpoint("failed", id="", message=str(error)[:280])
            raise
    checkpoint("pending", id=task)
    deadline = time.monotonic() + 45*60
    while time.monotonic() < deadline:
        result = api.query_heygen(task) if service=="heygen" else api.query_video(task,auth)
        status = result.get("status")
        notify(f"{scene['title']} · {label} {status} · {task}")
        if status in {"failed", "cancelled", "canceled"}:
            error = result.get("error") or {}
            reason = str(error.get("message", ""))[:180]
            checkpoint("failed", id=task, code=str(error.get("code", "")), message=reason)
            raise RuntimeError(f"{label} 任务未完成。{error.get('code','')} {reason}")
        if status == "succeeded":
            raw = root / "cache" / (key + ".mp4")
            if not raw.exists():
                url = (result.get("content") or {}).get("url") or result.get("video_url")
                if not url:
                    raise ValueError(f"{label} 已完成但暂未返回下载地址。再次运行将沿用同一任务号。")
                api.atomic(raw, api.fetch_media(url))
            target = normalized_film(root, raw, key)
            finish(target, task)
            try:
                raw.unlink(missing_ok=True)
            except PermissionError:
                pass
            return
        time.sleep(10)
    raise RuntimeError(f"{label} 任务 {task} 仍在排队，点击同一生成按钮可继续查询。")
