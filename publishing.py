"""Publish immutable, independently playable editions with measured subtitle timecodes."""
import hashlib
import json
import math
from pathlib import Path
import shutil
import tempfile
import wave
import zipfile

import providers as api
import cinema
import motion
import quality
from production import languages, recording_style


def stamp(seconds, decimal):
    total=round(seconds*1000)
    h,total=divmod(total,3600000)
    m,total=divmod(total,60000)
    s,ms=divmod(total,1000)
    return f"{h:02}:{m:02}:{s:02}{decimal}{ms:03}"


def subtitles(scenes,lang,vtt):
    rows=["WEBVTT",""] if vtt else []
    offset=0
    count=0
    for s in scenes:
        lines={line["id"]:line[lang] for line in s["script"]["lines"]}
        track=s["audio"][lang]
        for cue in track["cues"]:
            count+=1
            sep="." if vtt else ","
            text=lines[cue["id"]]
            if vtt:
                text=text.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
            rows.extend([str(count),f"{stamp(offset+cue['start'],sep)} --> {stamp(offset+cue['end'],sep)}",text,""])
        offset+=track["duration"]
    return "\n".join(rows)+"\n"


def publish(root,book,scenes,partial,proof=False):
    enabled = languages(book)
    for s in scenes:
        if not s.get("frame") or not s.get("script") or (not proof and not s.get("video")) or not all(l in s.get("audio",{}) for l in enabled):
            raise ValueError(f"「{s['title']}」还缺首帧、动画或配音。")
        if not proof and not quality.accepted(root,book,s):
            raise ValueError(f"「{s['title']}」的成片需要通过校验。")
    if not (partial or proof) and book.get("quality_version",1) >= 2:
        review = book.get("education_review",{})
        if review.get("state") != "passed" or review.get("key") != quality.education_key(book):
            raise ValueError("全书需通过学习目标覆盖校验。")
    acts=book.get("activities") if not partial else None
    if not partial and not acts:
        raise ValueError("请先生成全书的观察题、问答与小测。")
    if acts and any(not all(l in book["faq_audio"].get(f["id"],{}) for l in enabled) for f in acts["faq"]):
        raise ValueError("请在全书范围执行配音，补齐问答讲解。")
    order=[s["id"] for s in scenes]
    model={"title":book["profile"]["title"],"partial":partial,"scenes":[],"languages":enabled,"book_id":book["id"],
           "source_kind":book.get("creation",{}).get("kind","pdf"),
           "activities":acts or {"observation":None,"faq":[],"quiz":[]},"faq_audio":book["faq_audio"] if acts else {}}
    model["proof"] = proof
    files=set()
    for s in scenes:
        sc={k:s[k] for k in ("id","frame","script","audio")}
        approved = quality.accepted(root,book,s)
        sc["video"]=s["video"] if approved else None
        sc["animation_model"]=(s.get("video_source") or {}).get("model",api.VIDEO) if sc["video"] else None
        sc["pages"]=list(dict.fromkeys(p["page"] for p in s["parts"]))
        model["scenes"].append(sc)
        files.add(s["frame"])
        if sc["video"]:
            files.add(sc["video"])
            review = s["film_review"]
            sc.update(interaction_frame=review["frame"],interaction_spots=review["spots"])
            files.add(review["frame"])
            if review.get("spot_audio"):
                sc["audio"] = {lang:{**track,"spots":{**track["spots"],**review["spot_audio"].get(lang,{})}}
                               for lang,track in s["audio"].items()}
            if book.get("quality_version",1) >= 2:
                sc["cinematic"] = True
                playback = {**s,"video_seconds":s.get("video_seconds") or cinema.video_duration(root/"assets"/s["video"]),
                            "video_source":{**(s.get("video_source") or {}),"cinematic":True}}
                sc["audio"] = {}
                for lang, track in s["audio"].items():
                    filename = cinema.soundtrack(root,track["file"],playback)
                    with wave.open(str(root/"assets"/filename),"rb") as handle:
                        seconds = handle.getnframes()/handle.getframerate()
                    sc["audio"][lang] = {**track,"file":filename,"duration":seconds,"narration_duration":track["duration"],
                                         "spots":{**track["spots"],**review.get("spot_audio",{}).get(lang,{})}}
        sc["plot_ids"] = s.get("plot_ids",[])
        for track in sc["audio"].values():
            files.add(track["file"])
            files.update(track["spots"].values())
    for d in model["faq_audio"].values():
        files.update(d.values())
    model["subtitles"] = {lang:subtitles(model["scenes"],lang,True) for lang in enabled}
    ob = model["activities"]["observation"]
    if ob and not any(sc["id"] == ob["scene"] and any(h["id"] == ob["spot"] for h in
                      sc.get("interaction_spots",sc["script"]["spots"])) for sc in model["scenes"]):
        model["activities"] = {**model["activities"],"observation":None}
    if not partial and not proof and book.get("education_review"):
        goals = book["education_review"]["goals"]
        model["activities"] = {**model["activities"],"quiz":[
            {**quiz,"id":f"quiz-{i}","goal_ids":[g["id"] for g in goals if i in g["quiz_indices"]]}
            for i,quiz in enumerate(model["activities"]["quiz"],1)
        ]}
    context={"profile":book["profile"],"tts":book["tts_model"],"voices":{lang:recording_style(book,lang) for lang in enabled},
             "models":{"planning":api.DS,"image_edit":api.IMAGE,"voice":book["tts_model"],"animation":list(dict.fromkeys(s["animation_model"] for s in model["scenes"] if s["animation_model"])),
                       "next_animation":motion.MODELS[book.get("video_provider","heygen")]},
             "edition_kind":"校对版" if proof else ("样片" if partial else "完整动画"),
             "motion":{s["id"]:(s["animation_model"] or "校对静帧") for s in model["scenes"]},
             "video_receipts":{s["id"]:s.get("video_source") or {"model":api.VIDEO,"id":s.get("task",{}).get("id","")} for s in scenes if s.get("video")},"scenes":[{k:s[k] for k in
                  ("id","title","summary","parts","sources","script")} for s in scenes],
             "regions":[r for r in book["regions"] if any(r["page"] in s["pages"] for s in model["scenes"])]}
    # Freeze generation evidence alongside the edition that the family can rate.
    context["creation"] = book.get("creation", {})
    node_ids = {nid for s in model["scenes"] for nid in s["plot_ids"]}
    context["plot_nodes"] = [n for n in book.get("plot_nodes",[]) if n["id"] in node_ids]
    for field in ("cast_reference","learning_goals","education_review"):
        if field in book:
            context[field] = book[field]
    for person in context.get("cast_reference",{}).get("characters",[]):
        files.update(ref["asset"] for ref in person["refs"] if (root/"assets"/ref["asset"]).is_file())
    context["rsi_assigned_at"] = book.get("rsi_assigned_at",0)
    for record, scene in zip(context["scenes"],scenes):
        record.update({k:scene[k] for k in ("visual","art_prompt","clean_instruction","rsi_used","clean_review",
                      "video_seconds","plot_ids","film_review","shot_plan","frame_cast_review") if k in scene})
        for shot in scene.get("shot_plan",[]):
            for field in ("input_frame","reference_image"):
                if shot.get(field) and (root/"assets"/shot[field]).is_file():
                    files.add(shot[field])
        if scene.get("shot_plan"):
            record["raw_shot_directory"] = "制作台项目 assets"
    template=api.ROOT/"reader"
    template_hash=hashlib.sha256(b"".join(p.read_bytes() for p in sorted(template.iterdir()) if p.is_file())).hexdigest()
    key=api.digest(model,context,template_hash)
    editions=root/"editions"
    dest=editions/key
    archive=editions/(key+".zip")
    if not dest.is_dir():
        temp=Path(tempfile.mkdtemp(prefix="build-",dir=editions))
        try:
            for path in template.iterdir():
                if path.is_file():
                    shutil.copy2(path,temp/path.name)
            (temp/"assets").mkdir()
            for filename in files:
                shutil.copyfile(root/"assets"/filename,temp/"assets"/filename)
            for l in enabled:
                for ext in ("vtt","srt"):
                    payload = model["subtitles"][l] if ext=="vtt" else subtitles(model["scenes"],l,False)
                    (temp/f"{l}.{ext}").write_bytes(payload.encode("utf-8"))
            (temp/"book.js").write_text("window.EDITION = "+json.dumps(model,ensure_ascii=False)
                    .replace("\u2028","\\u2028").replace("\u2029","\\u2029")+";\n",encoding="utf-8")
            api.save_json(temp/"制作稿.json",context)
            temp.rename(dest)
        finally:
            if temp.exists():
                shutil.rmtree(temp)
    if not archive.is_file():
        tmp=archive.with_suffix(".partial")
        with zipfile.ZipFile(tmp,"w",zipfile.ZIP_DEFLATED,compresslevel=2) as out:
            for path in sorted(dest.rglob("*")):
                if path.is_file():
                    out.write(path,path.relative_to(dest).as_posix())
        tmp.replace(archive)
    return {"id":key,"partial":partial,"proof":proof,"scenes":order,"animated":sum(bool(s["video"]) for s in model["scenes"])}
