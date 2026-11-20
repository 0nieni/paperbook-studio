"""Resumable book production from PDF to independent language timelines."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from threading import Lock
import io
import json
import math
from pathlib import Path
import re
import subprocess
import time
import uuid
import wave

from PIL import Image

import artwork as art
import editorial as ed
import providers as api
import motion
import original_story

LANGS = ("zh","en")
ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def initialize(root, name, creation=None):
    for sub in ("pages","assets","cache","editions"):
        (root/sub).mkdir(parents=True,exist_ok=True)
    book = {"id":root.name,"name":name,"revision":0,"pages":[],"profile":{"title":Path(name).stem},
            "scenes":[],"regions":[],"layouts":[],"activities":None,"faq_audio":{},"video_provider":"minimax","tts_model":api.TTS}
    if creation:
        book.update(creation=creation, languages=creation["languages"], voices=creation["voices"])
    write(root,book)
    return book


def read(root):
    return json.loads((root/"project.json").read_text(encoding="utf-8"))


def write(root,book):
    book["revision"] += 1
    api.save_json(root/"project.json",book)


def languages(book):
    return tuple(book.get("languages") or LANGS)


def recording_style(book,lang):
    tone = book.get("voices",{}).get(lang)
    return (api.VOICES[tone][0], tone) if tone else (book["tts_model"], None)


def creation_settings(kind, language, voices, brief=None):
    langs = {"zh":["zh"],"en":["en"],"both":list(LANGS)}.get(language)
    if not langs or not isinstance(voices,dict):
        raise ValueError("请选择中文、英文或中英双语。")
    selected = {lang:voices.get(lang,api.VOLUME) for lang in langs}
    if any(not isinstance(tone,str) or tone not in api.VOICES for tone in selected.values()):
        raise ValueError("请选择列表中的千问音色。")
    settings = {"kind":kind,"languages":langs,"voices":selected}
    if kind=="text":
        settings["brief"] = original_story.brief(brief)
    elif brief:
        if not isinstance(brief,str) or len(brief)>600:
            raise ValueError("补充教学目标请控制在 600 字内。")
        settings["education"] = brief.strip()
    return settings


def coverage(book):
    warnings = []
    assigned = {ref for scene in book["scenes"] for ref in scene["sources"]}
    for region in book["regions"]:
        if region["kind"] in art.REMOVE and region["id"] not in assigned:
            warnings.append(f"原句未归入场景 · 第 {region['page']} 页 {region['text'][:38]}")
    for layout in book["layouts"]:
        for p in layout["panels"]:
            candidates = [part["box"] for s in book["scenes"] for part in s["parts"] if part["page"]==layout["page"]]
            covered = sum(art.area(art.intersection(p["box"],b)) for b in candidates)
            if covered < .30*art.area(p["box"]):
                warnings.append(f"插画未覆盖 · 第 {layout['page']} 页 {p['title']}")
    return warnings


def analyze(root,book,notice):
    layouts = []
    for page in book["pages"]:
        n = page["n"]
        notice(f"DeepSeek 阅读第 {n}/{len(book['pages'])} 页与相邻跨页")
        layouts.append(ed.read_page(root,book["pages"],n,layouts[-1] if layouts else {}))
    ed.check_spreads(root,layouts,notice)
    scenes,regions = ed.join_layout(layouts)
    if not scenes:
        raise ValueError("没有识别出故事画面，可从网页手工添加场景。")
    notice("整理全书资料、教育主题与角色参考图")
    profile = ed.make_profile(root,layouts,book["pages"])
    education = book.get("creation",{}).get("education")
    if education:
        profile["education"] += "\n家长补充的教学目标（仅结合原书可验证的情节）："+education
    notice("按情节核对印刷文字与对应画面")
    ed.review_assignments(root,scenes,regions,profile)
    book.update(scenes=scenes,regions=regions,layouts=layouts,profile=profile,activities=None,faq_audio={})
    book["reference"] = art.character_sheet(root,profile)
    write(root,book)


def region_set(scene,regions):
    return [r for r in regions if any(p["page"]==r["page"] and art.intersection(p["box"],r["box"])
                                        for p in scene["parts"])]


def artifact(root, prefix, data, ext):
    import hashlib
    name = f"{prefix}-{hashlib.sha256(data).hexdigest()[:24]}.{ext}"
    path = root/"assets"/name
    if not path.exists():
        api.atomic(path,data)
    return name


def clear_script(scene):
    for key in ("script","audio","video","video_seconds","video_source","task"):
        scene.pop(key,None)


def clean(root,book,scene,force=False):
    if scene.get("frame") and (root/"assets"/scene["frame"]).exists() and not force:
        return
    regions = region_set(scene,book["regions"])
    original,_,_ = art.compose(root,scene,regions)
    base,_,_ = art.compose(root,scene,regions,vector=True)
    target_regions=[r for r in regions if r["kind"] in art.REMOVE]
    _,_,vector_mask=art.compose(root,scene,target_regions)
    frame=Image.composite(base,original,vector_mask)
    before_raster_edit=frame.copy()
    repair_regions=[r for r in regions if not art.vector_text(r,book["pages"][r["page"]-1])]
    protect=[r for r in regions if r["kind"] in {"prop","mark"}]
    visible={}
    for r in repair_regions:
        if r["kind"] in art.REMOVE:
            _,_,mask=art.compose(root,scene,[r,*protect])
            if mask.getbbox():
                visible[r["id"]]=(r,mask)
    cloud_regions = [{k:v for k,v in region.items() if not k.startswith("ink_")} for region in regions]
    def fix(ids,pass_number):
        nonlocal frame
        for rid in ids:
            r,mask=visible[rid]
            partial = all(art.area(art.intersection(part["box"],r["box"])) < .8*art.area(r["box"])
                          for part in scene["parts"] if part["page"]==r["page"])
            fingerprint=api.digest(("whole-line-1" if partial else "region-2") if pass_number==0 else "ink-recovery-1",scene["parts"],cloud_regions,list(ids),rid,pass_number)
            cached=root/"cache"/("repair-"+fingerprint+".png")
            if cached.exists() and not force:
                with Image.open(cached) as image:
                    frame=Image.composite(image.convert("RGB"),frame,mask)
            else:
                if pass_number==1:
                    recovered=art.restore_print_ink(before_raster_edit,mask)
                    frame=Image.composite(recovered,frame,mask)
                elif partial:
                    restored = art.repair_before_crop(root,scene,r,regions)
                    frame = Image.composite(restored,frame,mask)
                else:
                    frame=art.repair_crop(frame,mask,r["text"])
                api.atomic(cached,art.image_bytes(frame))
    def apply_outlines():
        nonlocal frame
        for region, mask in visible.values():
            if region.get("ink_outlines"):
                repaired = art.precise_ink(root,scene,region,regions)
                frame = Image.composite(repaired,frame,mask)

    pending=list(visible)
    scene["clean_review"]=[]
    scene.pop("clean_approved",None)
    for attempt in range(2):
        if not pending:
            break
        fix(pending,attempt)
        apply_outlines()
        audit_task=('比较原图和清图，只检查给定目标文字、气泡边框和尾巴。清图仍看得清的残字或空泡才算残留。'
                    '出版标记、道具文字、独立的动作速度线不计。按下列 JSON 返回 {"remaining":[{"id":"目标ID","reason":"具体残留"}]}。全部干净时返回空数组。')
        result=api.ask(audit_task,{"目标":[{"id":rid,"text":r["text"],"frame_box":list(m.getbbox())}
                           for rid,(r,m) in visible.items()],"size":list(frame.size)},[("原图",original),("清图",frame)],1000)
        remain=[e for e in result.get("remaining",[]) if isinstance(e,dict) and e.get("id") in visible]
        flagged = {e["id"] for e in remain}
        for rid, (region, mask) in visible.items():
            if rid not in flagged and art.colored_ink_survives(before_raster_edit, frame, mask):
                remain.append({"id": rid, "reason": f"请核对彩色印字『{region['text'][:22]}』的残留笔画。"})
        pending=list(dict.fromkeys(e["id"] for e in remain))
        scene["clean_review"]=[str(e.get("reason") or e["id"]) for e in remain]
    name = artifact(root,"frame",art.image_bytes(frame),"png")
    if scene.get("frame") != name:
        clear_script(scene)
        book["activities"] = None
        book["faq_audio"] = {}
    scene["original"] = artifact(root,"original",art.image_bytes(original),"png")
    scene["frame"] = name


def recording(root,text,instruction,lang,model,tone=None):
    key = api.digest(model,tone or api.VOLUME,lang,text,instruction if model==api.TTS else "")
    name = "voice-"+key+".wav"
    path = root/"assets"/name
    if not path.exists():
        api.atomic(path,api.voice(text,instruction,lang,model,tone))
    else:
        raw=path.read_bytes()
        normalized=api.complete_wave(raw)
        if normalized!=raw:
            api.atomic(path,normalized)
    return name


def assemble(root,clips):
    name = "track-"+api.digest("pcm-v3",clips)+".wav"
    output, cues = io.BytesIO(), []
    rate, channels, samples = 0, 0, 0
    with wave.open(output,"wb") as track:
        for cid,filename in clips:
            with wave.open(str(root/"assets"/filename)) as part:
                if not rate:
                    rate,channels = part.getframerate(),part.getnchannels()
                    track.setparams((channels,2,rate,0,"NONE","not compressed"))
                if (channels,2,rate) != (part.getnchannels(),part.getsampwidth(),part.getframerate()):
                    raise ValueError("同一幕内的配音采样率不同，请重新生成对应句。")
                start = samples / rate
                pcm=part.readframes(part.getnframes())
                track.writeframes(pcm)
                samples += len(pcm)//(channels*2)
                cues.append({"id":cid,"start":start,"end":samples/rate})
                gap = round(rate*.22)
                track.writeframes(b"\0"*(gap*channels*2))
                samples += gap
    path = root/"assets"/name
    if not path.exists():
        api.atomic(path,output.getvalue())
    return {"file":name,"duration":samples/rate,"cues":cues}


def synthesize(root,book,scene,lang,notice):
    sc = scene["script"]
    clips = []
    for i,line in enumerate(sc["lines"],1):
        notice(f"{scene['title']} · {lang.upper()} 配音 {i}/{len(sc['lines'])}")
        name = recording(root,line[lang],line["direction"][lang],lang,*recording_style(book,lang))
        clips.append((line["id"],name))
    result = assemble(root,clips)
    result["spots"] = {}
    for h in sc["spots"]:
        notice(f"{scene['title']} · {lang.upper()} 物品讲解")
        result["spots"][h["id"]] = recording(root,h["explain"][lang],explanation_direction(lang),lang,*recording_style(book,lang))
    scene.setdefault("audio",{})[lang] = result


def explanation_direction(lang):
    return "用普通话，像陪孩子一起看图一样，用亲切清楚的声音讲解。" if lang=="zh" else "In English, speak warmly and clearly to a child looking at the picture."


def faq_speech(root,book,langs,notice,save):
    for f in (book.get("activities") or {}).get("faq",[]):
        for lang in langs:
            notice(f"故事问答 {f['id']} · {lang.upper()} 配音")
            track = book["faq_audio"].setdefault(f["id"],{})
            if lang not in track:
                track[lang] = recording(root,f["reply"][lang],explanation_direction(lang),lang,*recording_style(book,lang))
                save()


def animate_book(root, book, scenes, notice):
    unfinished = [scene for scene in scenes if not scene.get("video") or not (root/"assets"/scene["video"]).is_file()]
    for scene in unfinished:
        if any(lang not in scene.get("audio", {}) for lang in languages(book)):
            raise ValueError(f"请先完成「{scene['title']}」的配音。")
        if scene.get("clean_review") and not scene.get("clean_approved"):
            raise ValueError(f"请先核对「{scene['title']}」的清图对照。")
    if not unfinished:
        return
    service = book.get("video_provider", "heygen")
    lock = Lock()
    recipe_locks = {motion.render_recipe(s,service)[0]:Lock() for s in unfinished}
    def render(scene):
        draft = deepcopy(scene)
        def checkpoint():
            with lock:
                for field in ("task", "video", "video_seconds", "video_source"):
                    if field in draft:
                        scene[field] = deepcopy(draft[field])
                write(root, book)
        def progress(message):
            with lock:
                count = sum(bool(s.get("video")) for s in scenes)
                notice(f"已完成 {count}/{len(scenes)} 幕 · {message}")
        key, _, _ = motion.render_recipe(draft,service)
        with recipe_locks[key]:
            motion.animate(root, draft, service, progress, checkpoint)
            checkpoint()
    remaining = iter(unfinished)
    workers = 1 if service == "heygen" else 3
    with ThreadPoolExecutor(max_workers=workers) as pool:
        active = {pool.submit(render, next(remaining)) for _ in range(min(workers, len(unfinished)))}
        while active:
            done, active = wait(active, return_when=FIRST_COMPLETED)
            for completed in done:
                completed.result()
            for _ in done:
                next_scene = next(remaining, None)
                if next_scene is not None:
                    active.add(pool.submit(render, next_scene))


def recover(root,book,sid,task):
    s = next((s for s in book["scenes"] if s["id"]==sid),None)
    if not s or not s.get("task") or s["task"]["state"]!="submitting":
        raise ValueError("这幕没有待恢复的提交。")
    if task and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}",task):
        raise ValueError("任务号格式无效。")
    journal={"id":task,"state":"pending" if task else "failed"}
    service = s["task"].get("provider", "minimax")
    journal["provider"] = service
    api.save_json(motion.directory_record(root,service,s["task"]["key"]),journal)
    s["task"].update(journal)
    write(root,book)


def make(root,book,action,sid,lang,notice):
    save = lambda:write(root,book)
    if action=="build":
        if book.get("creation",{}).get("kind")=="text":
            original_story.generate(root,book,notice)
            save()
        elif not book["scenes"]:
            make(root,book,"analyze","","",notice)
        try:
            make(root,book,"all","","",notice)
        except (ValueError, RuntimeError):
            if all(s.get("script") and set(languages(book)) <= s.get("audio",{}).keys() for s in book["scenes"]):
                try:
                    make(root,book,"proof","","",notice)
                except (ValueError, RuntimeError, OSError):
                    pass
            raise
        return
    if action=="refresh_scene":
        scene = next((s for s in book["scenes"] if s["id"]==sid),None)
        if not scene:
            raise ValueError("请先选择要重新制作的分镜。")
        for step in ("clean","script","voice"):
            make(root,book,step,sid,"",notice)
        if scene.get("video"):
            for key in ("video","video_seconds","video_source","task"):
                scene.pop(key,None)
            scene["animation_revision"] = uuid.uuid4().hex
            save()
        make(root,book,"video",sid,"",notice)
        if any(not s.get("script") or not set(languages(book)) <= s.get("audio",{}).keys() for s in book["scenes"]):
            make(root,book,"export",sid,"",notice)
        else:
            make(root,book,"activities","","",notice)
            faq_speech(root,book,languages(book),notice,save)
            make(root,book,"export" if all(s.get("video") for s in book["scenes"]) else "proof","","",notice)
        return
    if action=="import":
        book["pages"]=art.render_pages(root,notice)
        save()
        return
    if action=="analyze":
        if not book["pages"]:
            book["pages"]=art.render_pages(root,notice)
            save()
        analyze(root,book,notice)
        return
    scenes = [s for s in book["scenes"] if s["id"]==sid] if sid else book["scenes"]
    if not scenes:
        raise ValueError("请先建立场景清单。")
    enabled = languages(book)
    if lang and lang not in enabled:
        raise ValueError("此作品未选择该配音语言。")
    langs = (lang,) if lang else enabled
    if action in {"all","sample"}:
        steps = ["clean","script"] + ([] if action=="sample" else ["activities"]) + ["voice","video","export"]
    else:
        steps = [action]
    for step in steps:
        if step=="activities":
            if any(not s.get("script") for s in book["scenes"]):
                raise ValueError("请先生成全书的逐幕制作稿。")
            notice("DeepSeek 编写观察题、双语问答和六道小测")
            if not book.get("activities"):
                book["activities"]=ed.make_activities(root,book)
                save()
            continue
        if step in {"export","proof","reel"}:
            from publishing import publish
            if step=="reel":
                animated = [s for s in scenes if s.get("video") and (root/"assets"/s["video"]).is_file()]
                if not animated:
                    raise ValueError("还没有已完成的动画镜头。")
                book["sample_edition"] = publish(root,book,animated,partial=True)
                save()
                continue
            if not sid and coverage(book):
                raise ValueError("全书仍有遗漏的原句或画面。请在场景页核对并归入场景。")
            notice("组装离线播放器、双语字幕和制作稿")
            if step=="proof" and sid:
                raise ValueError("整书校对版请选择全书范围。")
            edition = publish(root,book,scenes,partial=bool(sid),proof=step=="proof")
            book["proof_edition" if step=="proof" else ("sample_edition" if sid else "edition")]=edition
            if not sid:
                book["play_edition"] = edition
            save()
            continue
        if step=="video" and len(scenes)>1:
            animate_book(root,book,scenes,notice)
            continue
        for i,s in enumerate(scenes,1):
            notice(f"{i}/{len(scenes)} · {s['title']}")
            if step in {"clean","reclean"}:
                clean(root,book,s,step=="reclean")
                if action in {"all","sample"} and s.get("clean_review") and not s.get("clean_approved"):
                    save()
                    raise ValueError(f"「{s['title']}」仍有清图复核项，请到“清图与台词”查看对应位置。")
            elif step=="script":
                if not s.get("frame"):
                    raise ValueError(f"请先清理「{s['title']}」的首帧。")
                if not s.get("script"):
                    s["script"]=ed.draft(root,book,book["scenes"].index(s))
            elif step=="voice":
                if not s.get("script"):
                    raise ValueError(f"请先生成「{s['title']}」的制作稿。")
                for l in langs:
                    if l not in s.get("audio",{}):
                        synthesize(root,book,s,l,notice)
                        save()
            elif step=="video":
                if any(l not in s.get("audio",{}) for l in enabled):
                    raise ValueError("请先合成配音，以确定镜头时长。")
                if s.get("clean_review") and not s.get("clean_approved"):
                    raise ValueError(f"请先在清图对照中核对「{s['title']}」的残留标记。")
                motion.animate(root,s,book.get("video_provider","heygen"),notice,save)
            else:
                raise ValueError("未知的制作步骤。")
            save()
        if step=="voice" and not sid:
            faq_speech(root,book,langs,notice,save)
