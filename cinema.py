"""Plan, assemble and inspect silent shots, with a local narration mix for playback."""
import io
import math
import uuid
import wave

from PIL import Image

import providers as api


FPS = 24
TRANSITION_FRAMES = 4
CONTACT_FRAMES = 9
FRAMING = (
    ("establishing wide", "gentle forward dolly"),
    ("medium character group", "slow lateral track, parallel to the subjects"),
    ("medium detail of the active character and prop", "subtle arc, without crossing the action axis"),
    ("medium two-plane composition", "soft focus shift from the near plane to the action"),
)


def plan(root, book, scene, service, languages):
    """Return a reproducible shot plan to persist before the first paid submission."""
    from motion import MODEL_LIMITS, estimate_seconds

    frame = scene.get("frame")
    if not frame or not (root / "assets" / frame).is_file():
        raise ValueError("请先完成本幕首帧，再编排动画镜头。")
    script = scene.get("script") or scene["cue_plan"]
    tracks = {**scene.get("narration", {}), **scene.get("audio", {})}
    measured = [tracks[lang]["duration"] for lang in languages if tracks.get(lang)]
    missing = [lang for lang in languages if not tracks.get(lang)]
    if missing:
        measured.append(estimate_seconds(script, missing))
    seconds = max(measured, default=scene.get("planned_video_seconds", 0))
    low, high = MODEL_LIMITS[service]
    plot_ids = scene.get("plot_ids", [])
    nodes = {node["id"]: node for node in book.get("plot_nodes", [])}
    count = 3 if seconds > 8 or len(plot_ids) > 2 else 2
    while count * high < seconds + (count - 1) * TRANSITION_FRAMES / FPS:
        count += 1
    required = math.ceil(seconds + (count - 1) * TRANSITION_FRAMES / FPS)
    average, extra = divmod(required, count)
    shot_plan = []
    for index in range(count):
        final = index == count - 1
        start = index * len(plot_ids) // count
        stop = max(start + 1, (index + 1) * len(plot_ids) // count)
        ids = plot_ids[start:stop]
        events = [nodes[item] for item in ids if item in nodes]
        events_text = "; ".join(
            " → ".join(str(event.get(key) or "").strip() for key in ("cause", "action", "result")
                       if event.get(key)) for event in events)
        framing, camera = FRAMING[index % len(FRAMING)]
        if final:
            framing, camera = "full interaction composition", "gentle pull back, then lock off"
        previous = shot_plan[-1]["plot_ids"] if shot_plan else []
        if index == 0:
            preparation = "Begin in the attached illustration. Let the character register the goal before moving."
        elif ids == previous:
            preparation = "Begin during the preceding action at the same screen position. Do not replay the setup."
        else:
            preparation = "Start with the preceding outcome and carry the character's screen direction into this beat."
        action = (f"Show the visible part of this source event during this segment: {events_text}." if events_text
                  else "Show only the assigned part of the base action, as one readable, continuous movement.")
        if ids == previous and index:
            action += " Continue that action from its current stage; do not restart or add a new event."
        follow_through = ("Complete the visible outcome, return to the full composition, and keep the established "
                          "interactive objects wholly visible, stationary and clear for the final pause." if final else
                          "Let the weight settle and preserve screen direction and prop position for the next cut.")
        secondary = ("Use occasional natural blinks when eyes are visible. Give clothing, ears, hair or nearby "
                     "illustrated foliage a small delayed movement if already present. Let secondary motion settle before the cut.")
        first_image = "accepted illustration" if index == 0 else "last used frame of the previous shot"
        source = ("Use the attached " + first_image + " as the exact first image; retain the original illustration's line, palette, "
                  "characters, props and background." if book.get("creation", {}).get("kind") != "text" else
                  "Use the attached " + first_image + " for the opening. Keep all character features, outfit, scale and props consistent.")
        duration = min(high, max(low, average + (index < extra)))
        prompt = (f"{script['video_prompt']}\n"
                  f"Shot {index + 1} of {count}. {source} Animate only this shot's portion of the base story direction. "
                  "Use the shot-specific framing and camera below for this take.\n"
                  f"Framing: {framing}. Camera: {camera}. Keep sightlines and positions continuous with the sequence.\n"
                  f"Preparation: {preparation}\nAction: {action}\nFollow-through: {follow_through}\n"
                  f"Secondary motion: {secondary}")
        shot_plan.append({"id": f"{scene['id']}-shot-{index + 1:02d}", "plot_ids": ids, "duration": duration,
                          "provider": service, "frame": frame, "prompt": prompt, "framing": framing, "camera": camera,
                          "preparation": preparation, "action": action, "follow_through": follow_through,
                          "secondary_motion": secondary})
    return shot_plan


def video_duration(path):
    result = api.run_binary(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration",
                             "-of", "csv=p=0", str(path)], capture_output=True, timeout=30)
    try:
        seconds = float(result.stdout.strip()) if not result.returncode else 0
    except ValueError:
        seconds = 0
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("无法读取动画镜头的实际时长。")
    return seconds


def continuation_frame(root,shot):
    """Capture the same final used frame that will enter the scene edit."""
    assets = root/"assets"
    video = assets/shot["video"]
    seconds = min(shot["duration"],video_duration(video))
    start = max(0,math.floor(seconds*FPS-1)/FPS-.5)
    key = api.digest("shot-continuation",shot["video"],shot["duration"])
    target = assets/f"shot-frame-{key}.png"
    if not target.is_file():
        result = api.run_binary(["ffmpeg","-nostdin","-v","error","-ss",str(start),"-i",str(video),"-map","0:v:0",
                                 "-vf",f"trim=duration={seconds-start:.6f},reverse","-frames:v","1","-threads:v","1",
                                 "-c:v","png","-f","image2pipe","pipe:1"],capture_output=True,timeout=60)
        if result.returncode or not result.stdout:
            raise ValueError("无法读取前一镜头的衔接末帧。")
        api.atomic(target,result.stdout)
    return target.name


def compose(root, scene):
    """Trim and crossfade the actual clips at their native speed, then hold the ending."""
    shots = scene["shot_plan"]
    assets = root / "assets"
    actual = [video_duration(assets / shot["video"]) for shot in shots]
    frames = [max(1, math.floor(min(shot["duration"], seconds) * FPS + .001))
              for shot, seconds in zip(shots, actual)]
    overlaps = [min(TRANSITION_FRAMES, left // 5, right // 5) for left, right in zip(frames, frames[1:])]
    key = api.digest("cinematic-v1", [shot["video"] for shot in shots], frames, overlaps, CONTACT_FRAMES)
    target = assets / f"cinema-{key}.mp4"
    if not target.is_file():
        inputs = []
        filters = []
        for index, (shot, length) in enumerate(zip(shots, frames)):
            inputs.extend(["-i", str(assets / shot["video"])])
            filters.append(f"[{index}:v:0]fps={FPS},scale=1280:720:force_original_aspect_ratio=decrease:"
                           "force_divisible_by=2,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,"
                           f"trim=end_frame={length},settb=1/{FPS},setpts=PTS-STARTPTS[clip{index}]")
        previous = "clip0"
        end = frames[0]
        for index, overlap in enumerate(overlaps, 1):
            if overlap:
                filters.append(f"[{previous}][clip{index}]xfade=transition=fade:"
                               f"duration={overlap / FPS:.8f}:offset={(end - overlap) / FPS:.8f}[cut{index}]")
            else:
                filters.append(f"[{previous}][clip{index}]concat=n=2:v=1:a=0,settb=1/{FPS}[cut{index}]")
            previous = f"cut{index}"
            end += frames[index] - overlap
        filters.append(f"[{previous}]tpad=stop_mode=clone:stop={CONTACT_FRAMES}[video]")
        temporary = target.with_name(f"{target.stem}.{uuid.uuid4().hex}.mp4")
        try:
            result = api.run_binary(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *inputs,
                                     "-filter_complex_threads", "1", "-filter_complex", ";".join(filters),
                                     "-map", "[video]", "-an", "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
                                     "-threads:v", "2", "-movflags", "+faststart", str(temporary)], capture_output=True,
                                    timeout=max(180, 60 * len(shots)))
            if result.returncode or not temporary.is_file():
                raise ValueError("动画镜头拼接失败，请核对各镜头视频。")
            video_duration(temporary)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    start = 0
    for shot, actual_seconds, length, overlap in zip(shots, actual, frames, [*overlaps, 0]):
        shot.update(video_seconds=actual_seconds, used_seconds=length / FPS,
                    timeline_start=start / FPS, timeline_end=(start + length) / FPS)
        start += length - overlap
    return target, video_duration(target)


def _actual_shots(root, scene):
    slots = scene.get("shot_plan") or [{"id": scene.get("id", "scene"), "video": scene.get("video")}]
    for slot in slots:
        name = slot.get("video")
        path = root / "assets" / name if name else None
        yield slot["id"], path, slot.get("used_seconds")


def motion_report(root, scene, *, minimum=.20, maximum=16.0):
    """Measure optical flow in each clip before compositing. Values are pixels per 1/8 s."""
    import cv2
    import numpy as np

    shots, issues = [], []
    for shot_id, path, used_seconds in _actual_shots(root, scene):
        if not path or not path.is_file():
            issues.append(f"{shot_id} 缺少可检查的实际镜头视频。")
            continue
        seconds = used_seconds or video_duration(path)
        result = api.run_binary(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-t", str(seconds),
                                 "-map", "0:v:0", "-vf", "fps=8,scale=192:108,format=gray", "-threads:v", "1",
                                 "-an", "-f", "rawvideo", "pipe:1"], capture_output=True, timeout=60)
        if result.returncode or len(result.stdout) < 2 * 192 * 108:
            issues.append(f"{shot_id} 的实际镜头太短或无法解码，未能检查动作。")
            continue
        images = np.frombuffer(result.stdout, dtype=np.uint8).reshape((-1, 108, 192))
        movement, jumps = [], []
        for before, after in zip(images, images[1:]):
            flow = cv2.calcOpticalFlowFarneback(before, after, None, .5, 4, 17, 3, 5, 1.2, 0)
            displacement = np.hypot(flow[:, :, 0], flow[:, :, 1])
            movement.append(float(np.percentile(displacement, 97)))
            jumps.append(float(np.mean(cv2.absdiff(before, after) > 42)))
        median = float(np.median(movement))
        peak = float(np.percentile(movement, 90))
        active = sum(value >= minimum for value in movement) / len(movement)
        unstable = sum(value >= .42 for value in jumps) / len(jumps)
        shots.append({"id": shot_id, "seconds": round(seconds, 3), "motion": round(median, 3),
                      "peak_motion": round(peak, 3), "active_fraction": round(active, 3),
                      "abrupt_fraction": round(unstable, 3), "sample_pairs": len(movement)})
        if peak < minimum or active < .17:
            issues.append(f"{shot_id} 的实际镜头动作过少，接近静帧。")
        if peak > maximum or unstable > .28:
            issues.append(f"{shot_id} 的实际镜头位移或画面跳变过大。")
    return {"shots": shots, "issues": issues}


def sample_frames(root, scene):
    """Yield labeled RGB images, owned by the caller, from the shots used in the edit."""
    for shot_id, path, used_seconds in _actual_shots(root, scene):
        if not path or not path.is_file():
            raise ValueError(f"{shot_id} 缺少可供角色核对的镜头视频。")
        seconds = used_seconds or video_duration(path)
        for name, timestamp in (("开头", 0), ("中段", seconds / 2), ("末尾", max(0, seconds - .5))):
            resize = "scale=640:-2:force_original_aspect_ratio=decrease"
            filters = f"trim=duration={seconds - timestamp:.6f},reverse,{resize}" if name == "末尾" else resize
            result = api.run_binary(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{timestamp:.6f}", "-i", str(path),
                                     "-map", "0:v:0", "-frames:v", "1", "-vf", filters,
                                     "-threads:v", "1", "-c:v", "png", "-f", "image2pipe", "pipe:1"], capture_output=True, timeout=30)
            if result.returncode or not result.stdout:
                raise ValueError(f"无法截取 {shot_id} 的{name}画面。")
            with Image.open(io.BytesIO(result.stdout)) as picture:
                yield f"{shot_id} {name}", picture.convert("RGB")


def soundtrack(root, track_filename, scene):
    """Add quiet generated music and shot accents with the original narration clock."""
    if not (scene.get("video_source") or {}).get("cinematic"):
        return track_filename
    import numpy as np

    starts = [shot["timeline_start"] for shot in scene.get("shot_plan", []) if "timeline_start" in shot]
    length = scene.get("video_seconds", 0)
    flavor = int(api.digest(scene.get("id", ""))[:2], 16)
    key = api.digest("cinema-sound-v1", flavor, starts, length, track_filename)
    target = root / "assets" / f"cinema-track-{key}.wav"
    if target.is_file():
        return target.name
    with wave.open(str(root / "assets" / track_filename), "rb") as voice:
        params = voice.getparams()
        if params.sampwidth != 2:
            raise ValueError("电影配乐需要 16 位 PCM 朗读音轨。")
        speech = np.frombuffer(voice.readframes(params.nframes), dtype="<i2").astype(np.float64)
    rate, channels = params.framerate, params.nchannels
    count = max(params.nframes, math.ceil(length * rate))
    base = np.zeros((count, channels), dtype=np.float64)
    base[:params.nframes] = speech.reshape((-1, channels))
    t = np.arange(count) / rate
    music = np.zeros(count, dtype=np.float64)
    beats = (0, 7, 12, 4, 7, 0, 12, 4)
    for beat in range(math.ceil(count / rate / .5)):
        i = round(beat * .5 * rate)
        j = min(count, i + round(.65 * rate))
        phase = t[:j - i]
        freq = 164.81 * 2 ** ((flavor % 5 + beats[beat % len(beats)]) / 12)
        music[i:j] += (np.sin(math.tau * freq * phase) + .23 * np.sin(2 * math.tau * freq * phase)) * (
            260 * np.minimum(phase / .045, 1) * np.exp(-4.5 * phase))
    for start in starts[1:]:
        i = round(start * rate)
        if i >= count:
            continue
        j = min(count, i + round(.22 * rate))
        phase = t[:j - i]
        music[i:j] += np.sin(math.tau * (390 * phase + 420 * phase ** 2)) * (
            410 * np.sin(np.pi * phase / .22) ** 2 * np.exp(-7 * phase))
    duck = np.ones(count)
    stride = max(1, round(.04 * rate))
    for i in range(0, count, stride):
        sample = base[i:i+stride]
        level = np.sqrt(np.mean(sample ** 2))
        duck[i:i+stride] = 1 - .69 * min(level / 1600, 1)
    fade = np.clip(np.minimum(t / .10, (count / rate - t) / .20), 0, 1)
    combined = base + (music * duck * fade)[:, None]
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams(params)
        wav.writeframes(np.clip(combined, -32768, 32767).astype("<i2").tobytes())
    api.atomic(target, output.getvalue())
    return target.name
