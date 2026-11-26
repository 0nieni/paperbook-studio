"""Freeze the specific edition (or illustrated draft) before sending it to the review queue."""
import hashlib
import io
import json
import re

from PIL import Image, ImageChops, ImageStat
import providers as api


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def snapshot(root, book, edition=""):
    if edition:
        if not re.fullmatch(r"[a-f0-9]{24}",edition):
            raise ValueError("绘本版本号无效。")
        folder = root/"editions"/edition
        script = read_json(folder/"制作稿.json")
        model = json.loads((folder/"book.js").read_text(encoding="utf-8").partition("=")[2].strip().removesuffix(";"))
        details = {s["id"]:s for s in script["scenes"]}
        scenes = [{**details[s["id"]], **s} for s in model["scenes"]]
        profile, settings = script["profile"], script.get("creation", {})
        kind = model.get("source_kind","pdf")
        media = folder/"assets"
        activities = model.get("activities") or {}
        flavor = "动画样片" if model.get("partial") else ("校对版" if model.get("proof") else "完整动画")
    else:
        profile, settings, scenes = book["profile"], book.get("creation",{}), book["scenes"]
        kind, media = settings.get("kind","pdf"), root/"assets"
        activities = book.get("activities") or {}
        flavor = "分镜草稿"
    if not scenes:
        raise ValueError("这本书还没有可评估的分镜。")

    data = {"title":profile.get("title",""), "kind":kind, "flavor":flavor,
            "assigned_at":(script if edition else book).get("rsi_assigned_at",0),
            "intent":settings.get("brief") or settings.get("education") or profile.get("education",""),
            "style":profile.get("style",""), "scenes":[], "near_duplicates":[],
            "activities":{k:[{f:q[f] for f in ("question","evidence","type") if f in q} for q in activities.get(k,[])]
                          for k in ("quiz","faq")}}
    samples = []
    for s in scenes:
        sc = s.get("script") or {}
        item = {k:s[k] for k in ("id","title","summary","visual","art_prompt","clean_review","rsi_used") if k in s}
        item.update(pages=[p["page"] for p in s.get("parts",[])], lines=sc.get("lines") or s.get("story_lines",[]),
                    spots=sc.get("spots",[]), video_prompt=sc.get("video_prompt",""),
                    video=bool(s.get("video")),
                    audio={l:t.get("narration_duration",t.get("duration")) for l,t in s.get("audio",{}).items()})
        if "interaction_spots" in s:
            item.update(spots=s["interaction_spots"],spot_frame="成片末帧")
        name = s.get("frame","")
        source = media/name if re.fullmatch(r"[\w-]+\.png",name) else None
        if not edition and not source and kind=="text" and item["pages"]:
            source = root/"pages"/f"p{item['pages'][0]:04d}.jpg"
        if source and source.is_file():
            with Image.open(source) as im:
                thumb = im.convert("RGB")
                thumb.thumbnail((640,480),Image.Resampling.LANCZOS)
            fp = thumb.resize((32,18),Image.Resampling.LANCZOS)
            for sid, old in samples:
                delta = max(ImageStat.Stat(ImageChops.difference(old,fp)).mean)
                if delta < 5:
                    data["near_duplicates"].append([sid,s["id"],round(delta,2)])
            samples.append((s["id"], fp))
            buf = io.BytesIO()
            thumb.save(buf,"JPEG",quality=85)
            frame = hashlib.sha256(buf.getvalue()).hexdigest()[:24]
            path = root/"cache"/("rsi-frame-"+frame+".jpg")
            if not path.exists():
                api.atomic(path,buf.getvalue())
            item["thumbnail"] = frame
        data["scenes"].append(item)
    return data
