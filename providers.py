"""Provider clients used directly by the local application. Credentials stay in this process."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit
import uuid
import wave

from PIL import Image

ROOT = Path(__file__).resolve().parent
SKILL = ROOT / "picturebook-studio" / "SKILL.md"
DS = "deepseek-flash"
IMAGE = "qwen-image-2.0-pro"
TTS = "qwen-audio-3.1-tts-flash"
VIDEO = "MiniMax-H3"
HEYGEN = "heygen-video-1"
HEYGEN_ENDPOINT = "https://api.heygen.com/v3/models/videos"
VOLUME = "longanhuan_v3.1"
VOICES = {
    VOLUME: (TTS, "龙安欢", "亲切"),
    "longanlingxin_v3.1": (TTS, "龙安灵心", "温和"),
    "Cherry": ("qwen3-tts-flash", "Cherry", "明朗"),
    "Serena": ("qwen3-tts-flash", "Serena", "柔和"),
    "Ethan": ("qwen3-tts-flash", "Ethan", "活泼"),
}
MULTIMODAL = "/api/v1/services/aigc/multimodal-generation/generation"
NAMES = {"DeepSeek": ("deepseek_api", "DEEPSEEK_API_KEY"),
         "千问": ("qwen", "DASHSCOPE_API_KEY"), "MiniMax": ("minimaxH3",),
         "MiniMax 订阅": ("minimax订阅",),
         "HeyGen": ("HEYGEN_API_KEY", "heygen_api")}


class ProviderRejected(RuntimeError):
    """The provider explicitly rejected a request before returning a task."""


def setting(*names):
    for name in names:
        if os.name == "nt":
            import winreg
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as env:
                    value = winreg.QueryValueEx(env, name)[0]
                if str(value).strip():
                    return str(value).strip()
            except FileNotFoundError:
                pass
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_bytes(data)
        for attempt in range(6):
            try:
                tmp.replace(path)
                break
            except PermissionError:
                if attempt==5:
                    raise
                time.sleep(.025*(attempt+1))
    finally:
        tmp.unlink(missing_ok=True)


def save_json(path, obj):
    atomic(path, json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False).encode())


def digest(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()[:24]


def encode_image(picture, max_side=1500):
    picture = picture.copy().convert("RGB")
    picture.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    stream = io.BytesIO()
    picture.save(stream, "JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(stream.getvalue()).decode()


def run_binary(args, **kwargs):
    return subprocess.run(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kwargs)


def request(provider, url, body=None, timeout=210, *, attempts=5):
    key = setting(*NAMES[provider])
    if not key:
        raise ValueError(f"请在 Windows 用户环境变量设置 {NAMES[provider][0]}，然后在网页重试。")
    header = ("x-api-key: " if provider == "HeyGen" else "Authorization: Bearer ") + key
    cfg = "header = " + json.dumps(header) + "\n"
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        cfg += "data-binary = " + json.dumps(payload, ensure_ascii=False) + "\n"
    args = [shutil.which("curl.exe") or "curl", "-4", "--http1.1", "-sS", "--connect-timeout", "12",
            "--max-time", str(timeout), "-H", "Content-Type: application/json", "-w", "\n%{http_code}",
            "--config", "-", url]
    video = provider in {"MiniMax", "MiniMax 订阅", "HeyGen"}
    for attempt in range(attempts):
        res = run_binary(args, input=cfg.encode(), capture_output=True)
        retryable = res.returncode in {6, 7, 35} or (res.returncode in {18, 28, 52, 55, 56} and (body is None or not video))
        if retryable and attempt < min(3, attempts-1):
            time.sleep(min(15, 2**(attempt+1)))
            continue
        if res.returncode:
            raise RuntimeError(f"{provider} 网络连接失败（curl {res.returncode}）。请检查网络后重试。")
        raw, status = res.stdout.rsplit(b"\n", 1)
        try:
            obj = json.loads(raw)
        except ValueError:
            raise RuntimeError(f"{provider} 返回非 JSON 响应（HTTP {status.decode()}）。") from None
        base = obj.get("base_resp") or {}
        base_code = base.get("status_code", 0)
        if status.startswith(b"2") and obj.get("code") in (None, 0, "0", "") and not obj.get("error") and base_code in (0,"0",None):
            return obj
        if (status==b"429" or (status in {b"502", b"503", b"504"} and (body is None or not video))) and attempt<attempts-1:
            time.sleep(min(55,8*(2**attempt)))
            continue
        err = obj.get("error") or ({"code":base_code,"message":base.get("status_msg")} if base_code else obj)
        why = str(err.get("message") or err.get("code") or "接口错误") if isinstance(err, dict) else str(err)
        why = re.sub(r"https?://\S+|data:image/\S+|sk-[a-zA-Z0-9_-]+", "[已省略]", why.replace(key, "[已隐藏]"))
        code = str(err.get("code") or "")[:40] if isinstance(err, dict) else ""
        message = f"{provider} HTTP {status.decode()} · {code} {why[:240]}".strip()
        if provider in {"MiniMax","MiniMax 订阅"} and code=="2068":
            message += "。请在 MiniMax 控制台确认该 Key 对应的账户或订阅支持 H3 视频。"
        if status.startswith(b"4") or (status.startswith(b"2") and (base_code or obj.get("error") or obj.get("code"))):
            raise ProviderRejected(message)
        raise RuntimeError(message)


def bailian():
    workspace = setting("DASHSCOPE_WORKSPACE_ID")
    region = "ap-southeast-1" if setting("PICTUREBOOK_REGION") == "intl" else "cn-beijing"
    if workspace and re.fullmatch(r"[A-Za-z0-9-]+", workspace):
        return f"https://{workspace}.{region}.maas.aliyuncs.com"
    return "https://dashscope-intl.aliyuncs.com" if region == "ap-southeast-1" else "https://dashscope.aliyuncs.com"


def fetch_media(address, aliyun=False):
    url = urlsplit(address)
    if aliyun and (url.hostname or "").endswith(".aliyuncs.com") and url.scheme == "http":
        url = url._replace(scheme="https")
    if url.scheme != "https" or not url.hostname:
        raise RuntimeError("服务返回的素材地址无效。")
    if aliyun and not (url.hostname.endswith(".aliyuncs.com") or url.hostname.endswith(".aliyun.com")):
        raise RuntimeError("千问素材地址不属于百炼存储域名。")
    cfg = "url = " + json.dumps(urlunsplit(url)) + "\n"
    args = [shutil.which("curl.exe") or "curl", "-4", "-fsSL", "--proto-redir", "=https",
            "--connect-timeout", "14", "--max-time", "180", "--config", "-"]
    for attempt in range(5):
        proc = run_binary(args, input=cfg.encode(), capture_output=True)
        if not proc.returncode:
            return proc.stdout
        if proc.returncode not in {6, 7, 18, 22, 28, 35, 52, 55, 56, 92} or attempt == 4:
            raise RuntimeError(f"素材下载失败（curl {proc.returncode}）。")
        time.sleep(min(22, 2**(attempt+1)))


def ask(task, context, images=(), tokens=10000, *, system=None, timeout=210, attempts=5):
    parts = [{"type": "text", "text": task + "\n以下 JSON 是本书资料。\n" + json.dumps(context, ensure_ascii=False)}]
    for label, img in images:
        parts += [{"type": "text", "text": label}, {"type": "image_url", "image_url": {"url": encode_image(img)}}]
    reply = request("DeepSeek", "https://api.deepseek.com/chat/completions", {
        "model": DS, "messages": [{"role": "system", "content": system if system is not None else SKILL.read_text(encoding="utf-8")},
                                  {"role": "user", "content": parts}],
        "thinking": {"type": "disabled"}, "response_format": {"type": "json_object"}, "max_tokens": tokens,
    }, timeout=timeout, attempts=attempts)
    choice = reply["choices"][0]
    if choice.get("finish_reason") == "length":
        raise ValueError("DeepSeek 输出被截断，请重试当前步骤。")
    try:
        result = json.loads(choice["message"]["content"])
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (TypeError, ValueError, KeyError):
        raise ValueError("DeepSeek 未返回完整的 JSON 对象，可重试当前步骤。") from None


def edit_art(original, marked, target="", note=""):
    cmd = ("图一红框标出要修复的范围，图二是同一幅没有标线的原画。以图二为底，"
           "移除红框区域中的原书旁白、印刷文字底板及对话框，包括全部内部文字、边框和尾巴。"
           "以相邻笔触、颜色和纸纹补回背景，保持构图、原有地标、人物和道具的位置。"
           "保留非目标道具上的字和出版标记。返回修复后的整幅图，不出现辅助红线。")
    if target:
        cmd += "这次红框内需要完整擦除的文字或拟声字为「"+target[:250]+"」。同时擦除光晕、外围圈线和气泡的尾巴。"
    obj = request("千问", bailian() + MULTIMODAL, {"model": IMAGE, "input": {"messages": [{"role": "user",
                  "content": [{"image": encode_image(marked)}, {"image": encode_image(original)}, {"text": cmd+note}]}]},
                  "parameters": {"n": 1, "size": f"{original.width}*{original.height}",
                                 "watermark": False, "prompt_extend": False}}, timeout=300)
    return picture_result(obj)


def illustrate(prompt, reference=None):
    content = [{"image":encode_image(reference)}] if reference is not None else []
    content.append({"text":prompt})
    obj = request("千问", bailian() + MULTIMODAL, {
        "model": IMAGE, "input": {"messages": [{"role": "user", "content": content}]},
        "parameters": {"n": 1, "size": "1280*720", "watermark": False, "prompt_extend": False},
    }, timeout=300)
    return picture_result(obj)


def picture_result(obj):
    info = obj["output"]["choices"][0]["message"]["content"]
    link = next((part["image"] for part in info if part.get("image")), None)
    if not link:
        raise ValueError("千问图像接口未返回图片。")
    return Image.open(io.BytesIO(fetch_media(link, True))).convert("RGB")


def voice(text, instruction, lang, model=TTS, tone=None):
    tone = tone or (VOLUME if model == TTS else "Cherry")
    if tone not in VOICES or VOICES[tone][0] != model:
        raise ValueError("千问音色与模型不匹配。")
    if model == TTS:
        data = {"model": TTS, "input": {"text": text, "voice": tone,
                "instruction": instruction, "language_hints": [lang], "format": "wav", "sample_rate": 24000}}
        endpoint = "/api/v1/services/audio/tts/SpeechSynthesizer"
    elif model == "qwen3-tts-flash":
        data = {"model": model, "input": {"text": text, "voice": tone,
                "language_type": "Chinese" if lang == "zh" else "English"}}
        endpoint = MULTIMODAL
    else:
        raise ValueError("语音模型无效。")
    response = request("千问", bailian() + endpoint, data, timeout=90)
    item = response["output"]["audio"]
    raw = fetch_media(item["url"], True) if item.get("url") else base64.b64decode(item["data"])
    return complete_wave(raw)


def complete_wave(raw):
    # Some provider WAVs carry a streaming placeholder length in the RIFF header.
    with wave.open(io.BytesIO(raw)) as wav:
        rate,channels,width=wav.getframerate(),wav.getnchannels(),wav.getsampwidth()
        pcm=wav.readframes(wav.getnframes())
    if width!=2 or len(pcm)%(width*channels) or len(pcm)<rate*width*channels*.10:
        raise ValueError("千问返回的 WAV 格式或时长异常。")
    out=io.BytesIO()
    with wave.open(out,"wb") as fixed:
        fixed.setparams((channels,width,rate,0,"NONE","not compressed"))
        fixed.writeframes(pcm)
    return out.getvalue()


def minimax_host():
    return "https://api.minimax.io" if setting("PICTUREBOOK_MINIMAX_REGION") == "global" else "https://api.minimax.cn"


def minimax_provider(auth):
    return "MiniMax 订阅" if auth=="subscription" else "MiniMax"


def start_video(picture, prompt, seconds, auth="h3"):
    data = request(minimax_provider(auth), minimax_host() + "/v2/video_generation", {
        "model": VIDEO, "resolution": "768P", "duration": seconds, "ratio": "adaptive",
        "content": [{"type": "text", "text": prompt},
                    {"type": "image_url", "role": "first_frame", "image_url": {"url": encode_image(picture)}}],
    })
    task = str(data.get("task_id") or data.get("task", {}).get("id") or "")
    if not re.fullmatch(r"[\w-]+", task):
        raise ValueError("H3 已响应但没有返回任务号，请核对 MiniMax 的任务记录。")
    return task


def query_video(task_id, auth="h3"):
    if not re.fullmatch(r"[\w-]+", str(task_id)):
        raise ValueError("H3 任务号无效。")
    result = request(minimax_provider(auth), minimax_host() + "/v2/query/video_generation/" + task_id)
    return result.get("task", result)


def start_heygen(picture, prompt, seconds):
    # The desktop app can pass a local file; a standalone web server sends the first frame inline.
    encoded = encode_image(picture).split(",", 1)[1]
    reply = request("HeyGen", HEYGEN_ENDPOINT, {
        "model": HEYGEN, "mode": "image_to_video", "resolution": "768p", "duration": seconds,
        "prompt_enhancement": "quality", "prompt": prompt,
        "image": {"type": "base64", "media_type": "image/jpeg", "data": encoded},
    })
    record = reply.get("data") or reply
    task_id = str(record.get("video_id") or record.get("id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
        raise ValueError("HeyGen 已响应，回执中缺少视频编号。请在 HeyGen 作品库核对此幕。")
    return task_id


def query_heygen(task_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(task_id)):
        raise ValueError("HeyGen 视频编号无效。")
    reply = request("HeyGen", HEYGEN_ENDPOINT + "/" + task_id)
    record = reply.get("data") or reply
    status = record.get("status")
    return {"status": "succeeded" if status == "completed" else status,
            "video_url": record.get("video_url"),
            "error": {"code": record.get("failure_code") or "", "message": record.get("failure_message") or ""}}
