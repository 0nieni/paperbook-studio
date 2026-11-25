"""Check the story and the interaction image against the delivered media."""
from copy import deepcopy
import hashlib
import io
import math
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

import artwork as art
import editorial as ed
import providers as api


def capture_story(book):
    regions = {r["id"]:r for r in book["regions"]}
    book["plot_source"] = [
        {**deepcopy(ed.scene_brief(s)), "quotes":[deepcopy(regions[k]) for k in s["sources"] if k in regions]}
        for s in book["scenes"]
    ]
    book.pop("plot_nodes",None)


def crop(picture, bounds):
    x,y,r,b = bounds
    return picture.crop((math.floor(x*picture.width), math.floor(y*picture.height),
                         math.ceil(r*picture.width), math.ceil(b*picture.height)))


def page_parts(root, scene):
    pictures = []
    for part in scene["parts"]:
        with Image.open(art.pagefile(root,part["page"])) as page:
            pictures.append(crop(page,part["box"]).convert("RGB"))
    return pictures


def page_location(part, bounds):
    x,y,r,b = part["box"]
    a,c,d,e = bounds
    return {"page":part["page"],"box":[x+a*(r-x),y+c*(b-y),x+d*(r-x),y+e*(b-y)]}


def prepare_plot(root, book, notice):
    """Build the source checklist once, then map it to the current scene order."""
    if "plot_source" not in book:
        capture_story(book)
    if "plot_nodes" not in book:
        nodes = []
        pdf = book.get("creation",{}).get("kind") != "text"
        for source_index, source in enumerate(book["plot_source"],1):
            notice(f"整理原稿情节 {source_index}/{len(book['plot_source'])} · {source['title']}")
            pictures = page_parts(root,source) if pdf else []
            task = """只从给定原稿抽取必须在本幕展现的事件，依原稿顺序排列。
每个事件分别写可见动作或状态、起因、结果，原稿未交代时留空。用 part 指定证据所在图块序号（从 1 起），
用 box 标记事件视觉重点在该图块内的归一化矩形。原创大纲没有图时按大纲取证，part=1，box 可取整图。
sources 填原稿中直接支持该事件的原句 ID，没有时用空数组。无字场景也需记录其可见情节，不要虚构。
返回 {"events":[{"action":"","cause":"","result":"","part":1,"box":[0,0,1,1],"sources":[]}]}。"""
            context = {"情节原稿":source}
            images = [(f"原稿图块 {i}",image) for i,image in enumerate(pictures,1)]

            def validate(raw):
                events = raw["events"]
                if not isinstance(events,list) or not events:
                    raise ValueError("原稿缺少情节节点。")
                result = []
                for i,event in enumerate(events,1):
                    part = event["part"]
                    quotes = event["sources"]
                    if type(part) is not int or not 1 <= part <= len(source["parts"]):
                        raise ValueError("情节节点引用了无效的原稿图块。")
                    if not isinstance(quotes,list) or any(q not in source["sources"] for q in quotes):
                        raise ValueError("情节节点引用了本幕以外的原句。")
                    result.append({"id":f"e{source_index:03d}-{i:02d}","action":ed.label(event["action"]),
                                   "cause":str(event.get("cause") or ""),"result":str(event.get("result") or ""),
                                   "origin":page_location(source["parts"][part-1],ed.model_box(event["box"])),
                                   "quotes":[q for q in source["quotes"] if q["id"] in quotes],
                                   "source_scene":source["id"]})
                return result

            signature = [hashlib.sha256(art.image_bytes(image)).hexdigest() for image in pictures]
            nodes.extend(ed.cached_ask(root,task,context,images,signature,validate,4000))
        book["plot_nodes"] = nodes

    placement = [[] for _ in book["scenes"]]
    previous = 0
    for event in book["plot_nodes"]:
        origin = event["origin"]
        bounds = origin["box"]
        references = {q["id"] for q in event["quotes"]}
        possible = []
        for i,scene in enumerate(book["scenes"]):
            overlap = max((art.area(art.intersection(bounds,p["box"])) for p in scene["parts"]
                           if p["page"] == origin["page"]),default=0)
            if overlap >= .50 * art.area(bounds):
                possible.append((len(references.intersection(scene["sources"])),overlap,
                                 scene["id"] == event["source_scene"],i))
        if not possible:
            raise ValueError(f"原稿情节 {event['id']}「{event['action']}」没有归入分镜。")
        index = max(possible)[-1]
        if index < previous:
            raise ValueError(f"原稿情节 {event['id']}「{event['action']}」的分镜顺序有误。")
        previous = index
        placement[index].append(event["id"])
    for scene, ids in zip(book["scenes"],placement):
        scene["plot_ids"] = ids


def scene_nodes(book, scene):
    ids = set(scene.get("plot_ids",()))
    return [n for n in book.get("plot_nodes",()) if n["id"] in ids]


def inputs(book, scene):
    enabled = book.get("languages") or ["zh","en"]
    return [scene.get("audio",{}).get(lang) or scene.get("narration",{}).get(lang) for lang in enabled]


def review_key(book, scene):
    return api.digest("film-review-2",api.OMNI,scene.get("video"),scene.get("frame"),scene["parts"],
                      scene.get("script"),scene_nodes(book,scene),
                      [(track["file"],track["duration"]) if track else None for track in inputs(book,scene)])


def accepted(root, book, scene):
    review = current_review(root,book,scene)
    if not review or review["state"] not in {"passed","approved"}:
        return False
    extra = {spot["id"] for spot in review["spots"]} - {spot["id"] for spot in scene["script"]["spots"]}
    for language in book.get("languages") or ["zh","en"]:
        files = review.get("spot_audio",{}).get(language,{})
        if any(sid not in files or not (root/"assets"/files[sid]).is_file() for sid in extra):
            return False
    return True


def current_review(root, book, scene):
    review = scene.get("film_review",{})
    if (scene.get("video") and (root/"assets"/scene["video"]).is_file() and
            review.get("frame") and (root/"assets"/review["frame"]).is_file() and
            review.get("key") == review_key(book,scene)):
        return review
    return None


def duration(video):
    probe = api.run_binary(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=duration",
                            "-of","csv=p=0",str(video)],capture_output=True,timeout=30)
    try:
        seconds = float(probe.stdout.strip()) if not probe.returncode else 0
    except ValueError:
        seconds = 0
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("无法读取成片的实际时长。")
    return seconds


def final_frame(video, seconds):
    seek = ["-sseof","-0.75"] if seconds > .75 else []
    result = api.run_binary(["ffmpeg","-nostdin","-loglevel","error","-xerror",*seek,"-i",str(video),
                             "-map","0:v:0","-vf","reverse","-frames:v","1","-c:v","png","-threads:v","1",
                             "-f","image2pipe","pipe:1"],capture_output=True,timeout=60)
    if result.returncode or not result.stdout:
        raise ValueError("无法从成片截取末帧。")
    with Image.open(io.BytesIO(result.stdout)) as image:
        return image.convert("RGB"), result.stdout


def with_narration(video, audio, video_seconds, audio_seconds):
    # The player ends the scene with the narration, even if a clip is longer.
    rate = max(.25,min(2,video_seconds/audio_seconds))
    with TemporaryDirectory() as directory:
        output = Path(directory)/"review.mp4"
        result = api.run_binary(["ffmpeg","-nostdin","-loglevel","error","-xerror","-i",str(video),"-i",str(audio),
                                 "-map","0:v:0","-map","1:a:0","-t",str(audio_seconds),
                                 "-vf",f"setpts=PTS/{rate},fps=12,scale=640:-2,tpad=stop_mode=clone:stop_duration={audio_seconds}",
                                 "-c:v","libx264","-pix_fmt","yuv420p","-maxrate","480k","-bufsize","960k",
                                 "-c:a","aac","-b:a","40k","-movflags","+faststart",str(output)],
                                capture_output=True,timeout=120)
        if result.returncode or not output.is_file():
            raise ValueError("无法合成本幕校验用的动画和实际朗读。")
        return output.read_bytes()


def verify_spots(root, book, scene, picture):
    expected = {h["id"]:h for h in scene["script"]["spots"]}
    parts = page_parts(root,scene) if book.get("creation",{}).get("kind") != "text" else []
    images = [("成片末帧，热区用这张图的归一化坐标",picture)]
    images.extend((f"PDF 原稿图块 {i}",im) for i,im in enumerate(parts,1))
    task = """在成片末帧给候选物品重新定位；只保留能明确辨认且完整可点的物品。
有 PDF 图块时，同时给该物品在原稿图块的位置，找不到原稿对应物品就略过。part 从 1 起。
两个 box 均为所对应图片上的 [左,上,右,下] 归一化坐标。没有可确认的候选物品返回空数组。
可额外提供一个清楚可见且有原书图块或本幕故事依据的备用物品，说明其可见特点及本幕作用，依据不足时填 null。
返回 {"spots":[{"id":"h1","box":[0,0,1,1],"source":{"part":1,"box":[0,0,1,1]}}],
"replacement":{"label":{"zh":"","en":""},"explain":{"zh":"","en":""},"box":[0,0,1,1],
"source":{"part":1,"box":[0,0,1,1]}}}。非 PDF 省略 source。"""
    labels = [{"id":sid,"label":spot["label"]} for sid,spot in expected.items()]
    located = api.ask(task,{"成片候选物品":labels,"本幕原稿情节":scene_nodes(book,scene),
                           "计划画面道具":scene.get("visual",{}).get("props",[])},images,3000)
    if not isinstance(located["spots"],list) or any(not isinstance(h,dict) for h in located["spots"]):
        raise ValueError("成片热点定位结果格式有误。")
    candidates = [h for h in located["spots"] if h.get("id") in expected]
    replacement = located.get("replacement")
    if isinstance(replacement,dict):
        try:
            expected["h-end"] = {"id":"h-end","label":ed.bilingual(replacement.get("label")),
                                 "explain":ed.bilingual(replacement.get("explain"))}
        except ValueError:
            pass
        else:
            labels.append({"id":"h-end","label":expected["h-end"]["label"]})
            candidates = [*candidates,{**replacement,"id":"h-end"}]
    proposed, crops = {}, []
    for item in candidates:
        sid = item.get("id")
        if sid not in expected or sid in proposed:
            continue
        try:
            bounds = ed.model_box(item["box"])
            spot = {**expected[sid],"box":bounds}
            original_crop = None
            if parts:
                source = item["source"]
                index = source["part"]
                if type(index) is not int or not 1 <= index <= len(parts):
                    continue
                source_box = ed.model_box(source["box"])
                spot["source"] = page_location(scene["parts"][index-1],source_box)
                original_crop = crop(parts[index-1],source_box)
        except (KeyError,TypeError,ValueError):
            continue
        proposed[sid] = spot
        crops.append((f"{sid} 成片热区裁图",crop(picture,bounds)))
        if original_crop is not None:
            crops.append((f"{sid} PDF 原稿位置裁图",original_crop))
    if not proposed:
        return []
    task = """独立判断每张裁图是否清楚展示指定物品。原稿和末帧都有裁图时，两张都要展示同一道具。
空白、遮挡严重、无从判断、画错物品均不得通过。仅返回通过的物品 ID，格式 {"verified":["h1"]}。"""
    verified = api.ask(task,{"核对物品裁图":[h for h in labels if h["id"] in proposed]},crops,1800)["verified"]
    if not isinstance(verified,list) or any(type(sid) is not str or sid not in proposed for sid in verified):
        raise ValueError("成片热点裁图校验结果格式有误。")
    approved = [proposed[sid] for sid in expected if sid in verified and sid != "h-end"]
    return approved or ([proposed["h-end"]] if "h-end" in verified else [])


def verify_plot(nodes, observation, seconds):
    if not isinstance(observation.get("retelling"),str) or not isinstance(observation.get("events"),list):
        raise ValueError("成片复述格式有误。")
    events, timeline = [], {}
    for i,event in enumerate(observation["events"],1):
        time = event.get("time")
        if type(time) not in (int,float) or not 0 <= time <= seconds or not isinstance(event.get("visible"),str):
            raise ValueError("成片复述缺少有效的可见事件时间。")
        events.append({"index":i,"time":time,"visible":event["visible"],"spoken":str(event.get("spoken") or "")})
        timeline[i] = time
    if not nodes:
        return []
    task = """将原稿情节节点与独立的成片复述对照。只有复述的 visible 能直接支持预期动作时才匹配。
核对原稿的起因和结果，与成片所见或听到的内容有矛盾时也填 null。只有朗读提及、画面模糊、
角色或动作不符、推断的内容填 null。不同事件分别给出成片事件 index。
返回 {"matches":{"节点ID":1,"另一节点ID":null}}，每个节点必须出现。"""
    matches = api.ask(task,{"情节核对清单":nodes,"成片可见事件":events,
                           "成片复述":observation["retelling"]},[],3500)["matches"]
    if not isinstance(matches,dict) or matches.keys() != {node["id"] for node in nodes}:
        raise ValueError("情节校验未返回全部节点的结果。")
    issues, used, last = [], set(), (-1,0)
    for node in nodes:
        index = matches[node["id"]]
        title = f"{node['id']}「{node['action']}」"
        if index is None:
            issues.append("成片缺少可见情节 "+title)
        elif type(index) is not int or index not in timeline or not events[index-1]["visible"].strip():
            raise ValueError("情节校验引用了无效的可见事件。")
        elif index in used or (timeline[index],index) < last:
            issues.append("成片情节顺序或匹配有误 "+title)
        else:
            used.add(index)
            last = (timeline[index],index)
    return issues


def evaluate(root, book, scene):
    video = root/"assets"/scene["video"]
    length = duration(video)
    picture, pixels = final_frame(video,length)
    frame = f"interaction-{hashlib.sha256(pixels).hexdigest()[:24]}.png"
    api.atomic(root/"assets"/frame,pixels)
    spots = verify_spots(root,book,scene,picture)
    nodes = scene_nodes(book,scene)
    tracks = inputs(book,scene)
    observations = {}
    issues = []
    for lang, track in zip(book.get("languages") or ["zh","en"],tracks):
        clip = with_narration(video,root/"assets"/track["file"],length,track["duration"])
        observation = api.retell_video(clip)
        observations[lang] = observation
        prefix = "中文 · " if lang == "zh" else "英文 · "
        issues.extend(prefix+issue for issue in verify_plot(nodes,observation,track["duration"]+.10))
    if scene["script"]["spots"] and not spots:
        issues.append("成片末帧没有通过裁图校验的可点物品。")
    return {"state":"needs_review" if issues else "passed","key":review_key(book,scene),"video":scene["video"],
            "frame":frame,"spots":spots,"issues":issues,"observations":observations}
