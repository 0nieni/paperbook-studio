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
import quality
from rsi import instructions

LANGS = ("zh","en")
ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
VOICE_LOCKS = [Lock() for _ in range(64)]


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
        layouts.append(ed.read_page(root,book["pages"],n,layouts[-1] if layouts else {},book))
    ed.check_spreads(root,layouts,notice)
    scenes,regions = ed.join_layout(layouts)
    for scene in scenes:
        instructions.stamp(scene,book,"story")
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
    quality.capture_story(book)
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
    for role in ("tts","image2video","interaction"):
        scene.get("rsi_used",{}).pop(role,None)
    for key in ("script","cue_plan","redraw_lines","narration","planned_video_seconds","audio","video","video_seconds","video_source","task"):
        scene.pop(key,None)
    clear_film_review(scene)


def clear_film_review(scene):
    for key in ("film_review","film_retry","film_retry_used"):
        scene.pop(key,None)


def redraw_scene(root,book,sid,notice,note="",edited_prompt=None):
    scene = next((s for s in book["scenes"] if s["id"]==sid),None)
    if not scene:
        raise ValueError("请选择要重画的分镜。")
    if scene.get("task",{}).get("state") in {"pending","submitting"}:
        raise ValueError("本幕已有视频任务，请先核对任务结果，再重画首帧。")
    if scene.get("needs_illustration") or not all(art.pagefile(root,p["page"]).is_file() for p in scene["parts"]):
        raise ValueError("本幕原页尚未生成，请先完成原页插画。")

    original,_,_ = art.compose(root,scene,[])
    reference = original
    if scene.get("frame") and (root/"assets"/scene["frame"]).is_file():
        with Image.open(root/"assets"/scene["frame"]) as im:
            reference = im.convert("RGB")
    profile = book["profile"]
    previous = scene.get("art_prompt") or scene["summary"] or scene["title"]
    design = edited_prompt if edited_prompt is not None else previous
    if note:
        design += "\n本次修改：" + note
    prompt = (f"参考随附的本幕画面，重新绘制一张 16:9 儿童绘本分镜。保持角色身份、画风与故事连续。"
              f"\n全书画风：{profile.get('style','')}\n当前场景名：{scene['title']}\n当前情节：{scene['summary']}。"
              "\n画面干净，保留道具细节，去除旁白、对话气泡和字幕。"
              f"\n请按家长确认的本幕提示词作画，其中本次修改优先：\n{design}")
    prompt = instructions.apply(prompt,book,"text2image")
    notice(f"千问正在重画「{scene['title']}」")
    picture = api.illustrate(prompt,reference)
    frame = artifact(root,"frame",art.image_bytes(picture),"png")
    source = artifact(root,"original",art.image_bytes(original),"png")
    plan = scene.get("script") or scene.get("cue_plan") or {}
    lines = plan.get("lines") or scene.get("redraw_lines")
    narration = dict(scene.get("narration",{}))
    narration.update({lang:{k:v for k,v in track.items() if k!="spots"} for lang,track in scene.get("audio",{}).items()})
    if frame != scene.get("frame"):
        clear_script(scene)
        if lines:
            scene["redraw_lines"] = lines
            scene["narration"] = narration
        book.update(activities=None,faq_audio={})
    scene.update(original=source,frame=frame,redrawn_frame=frame,clean_review=[],art_prompt=design,
                 redraw_edit={"id":uuid.uuid4().hex,"scene":sid,"title":scene["title"],"before":previous,
                              "after":design,"note":note,"sent":prompt,"frame":frame})
    scene.pop("clean_approved",None)
    instructions.stamp(scene,book,"text2image")
    write(root,book)


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
    art_note = instructions.apply(scene.get("clean_instruction",""),book,"text2image")
    def fix(ids,pass_number):
        nonlocal frame
        for rid in ids:
            r,mask=visible[rid]
            partial = all(art.area(art.intersection(part["box"],r["box"])) < .8*art.area(r["box"])
                          for part in scene["parts"] if part["page"]==r["page"])
            fingerprint=api.digest(("whole-line-1" if partial else "region-2") if pass_number==0 else "ink-recovery-1",scene["parts"],cloud_regions,list(ids),rid,pass_number,*([art_note] if art_note else []))
            cached=root/"cache"/("repair-"+fingerprint+".png")
            if cached.exists() and not force:
                with Image.open(cached) as image:
                    frame=Image.composite(image.convert("RGB"),frame,mask)
            else:
                if pass_number==1:
                    recovered=art.restore_print_ink(before_raster_edit,mask)
                    frame=Image.composite(recovered,frame,mask)
                elif partial:
                    restored = art.repair_before_crop(root,scene,r,regions,art_note)
                    frame = Image.composite(restored,frame,mask)
                else:
                    frame=art.repair_crop(frame,mask,r["text"],art_note)
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
    if visible:
        instructions.stamp(scene,book,"text2image")


def recording(root,text,instruction,lang,model,tone=None):
    key = api.digest(model,tone or api.VOLUME,lang,text,instruction if model==api.TTS else "")
    name = "voice-"+key+".wav"
    path = root/"assets"/name
    with VOICE_LOCKS[int(key[:2],16) % len(VOICE_LOCKS)]:
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


def narrate(root,book,scene,lang,notice):
    sc = scene.get("script") or scene["cue_plan"]
    clips = []
    for i,line in enumerate(sc["lines"],1):
        notice(f"{scene['title']} · {lang.upper()} 配音 {i}/{len(sc['lines'])}")
        name = recording(root,line[lang],line["direction"][lang],lang,*recording_style(book,lang))
        clips.append((line["id"],name))
    return assemble(root,clips)


def add_spot_audio(root,book,scene,lang,result,notice):
    result["spots"] = {}
    for h in scene["script"]["spots"]:
        notice(f"{scene['title']} · {lang.upper()} 物品讲解")
        result["spots"][h["id"]] = recording(root,h["explain"][lang],explanation_direction(lang),lang,*recording_style(book,lang))
    return result


def synthesize(root,book,scene,lang,notice):
    track = scene.get("narration",{}).get(lang) or narrate(root,book,scene,lang,notice)
    scene.setdefault("audio",{})[lang] = add_spot_audio(root,book,scene,lang,track,notice)


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
        if not scene.get("frame") or not scene.get("script"):
            raise ValueError(f"请先完成「{scene['title']}」的首帧和动作提示。")
        if scene.get("clean_review") and not scene.get("clean_approved"):
            raise ValueError(f"请先核对「{scene['title']}」的清图对照。")
        motion.pin_duration(scene,languages(book))
    if not unfinished:
        return
    write(root,book)
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
    receipt = (s.get("film_retry",{}).get("task") or s.get("task")) if s else None
    if not receipt or receipt["state"]!="submitting":
        raise ValueError("这幕没有待恢复的提交。")
    if task and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}",task):
        raise ValueError("任务号格式无效。")
    journal={"id":task,"state":"pending" if task else "failed"}
    service = receipt.get("provider", "minimax")
    journal["provider"] = service
    if receipt.get("auth"):
        journal["auth"] = receipt["auth"]
    api.save_json(motion.directory_record(root,service,receipt["key"]),journal)
    receipt.update(journal)
    write(root,book)


def prepare_plot(root,book,notice):
    before = (book.get("plot_nodes"),[s.get("plot_ids") for s in book["scenes"]])
    quality.prepare_plot(root,book,notice)
    if before != (book["plot_nodes"],[s["plot_ids"] for s in book["scenes"]]):
        write(root,book)


def review_spot_audio(root,book,scene,review):
    original = {spot["id"] for spot in scene["script"]["spots"]}
    extra = [spot for spot in review["spots"] if spot["id"] not in original]
    if extra:
        review["spot_audio"] = {
            lang:{spot["id"]:recording(root,spot["explain"][lang],explanation_direction(lang),
                                       lang,*recording_style(book,lang)) for spot in extra}
            for lang in languages(book)
        }


def review_films(root,book,scenes,notice,*,retry=False,force=False,required=False):
    candidates = [s for s in scenes if s.get("video") and (root/"assets"/s["video"]).is_file()]
    if not candidates and not required:
        return []
    prepare_plot(root,book,notice)
    errors = []
    for scene in scenes if required else candidates:
        title = scene["title"]
        try:
            inputs = quality.inputs(book,scene)
            if scene not in candidates or not scene.get("script") or any(
                    not track or not (root/"assets"/track["file"]).is_file() for track in inputs):
                if required:
                    raise ValueError("请先完成本幕动画、制作稿和所选语言的朗读。")
                continue
            if quality.accepted(root,book,scene) and not force:
                continue
            notice(f"校验成片末帧与情节 · {title}")
            review = quality.current_review(root,book,scene) or {}
            if not scene.get("film_retry") and (
                    force or review.get("key") != quality.review_key(book,scene) or review.get("state") != "needs_review"):
                review = quality.evaluate(root,book,scene)
                if review["state"] == "passed":
                    review_spot_audio(root,book,scene,review)
                scene["film_review"] = review
                write(root,book)
            if review.get("state") == "needs_review" and retry and not scene.get("film_retry_used") and (
                    (scene.get("video_source") or {}).get("delivery") == "api"):
                prompt = scene["script"]["video_prompt"] + "\n复核发现：" + "；".join(review["issues"])
                prompt += "\n完整呈现本幕关键动作，末帧停稳并清楚保留关键道具。"
                scene["film_retry"] = {"revision":uuid.uuid4().hex,"task":{},"prompt":prompt}
                scene["film_retry_used"] = True
                write(root,book)
            if scene.get("film_retry") and retry:
                pending = scene["film_retry"]
                draft = {**deepcopy(scene),"animation_revision":pending["revision"],"task":pending["task"]}
                draft.pop("video",None)
                draft["script"]["video_prompt"] = pending["prompt"]
                service = scene["video_source"]["provider"]

                def checkpoint():
                    pending["task"] = draft["task"]
                    write(root,book)

                notice(f"按成片校验结果重生成一次 · {title}")
                try:
                    motion.animate(root,draft,service,notice,checkpoint)
                except (RuntimeError,ValueError,OSError):
                    if pending["task"].get("state") not in {"pending","submitting"}:
                        scene.pop("film_retry",None)
                        write(root,book)
                    raise
                for field in ("video","video_seconds","video_source","task","animation_revision"):
                    scene[field] = draft[field]
                scene.pop("film_retry",None)
                scene.pop("film_review",None)
                write(root,book)
                review = quality.evaluate(root,book,scene)
                if review["state"] == "passed":
                    review_spot_audio(root,book,scene,review)
                scene["film_review"] = review
                write(root,book)
            if review["state"] == "needs_review":
                errors.append(f"「{title}」待人工复核 · "+" / ".join(review["issues"]))
        except (ValueError,RuntimeError,OSError,KeyError,TypeError,AttributeError) as error:
            errors.append(f"「{title}」成片校验待处理 · {error}")
    return errors


def observation_available(root,book):
    acts = book.get("activities")
    if not acts:
        return False
    ob = acts.get("observation")
    if not ob:
        return True
    scene = next((s for s in book["scenes"] if s["id"] == ob["scene"]),None)
    if not scene or not scene.get("script"):
        return False
    spots = scene["film_review"]["spots"] if quality.accepted(root,book,scene) else scene["script"]["spots"]
    return any(h["id"] == ob["spot"] for h in spots)


def make(root,book,action,sid,lang,notice,*,redraw_note="",redraw_prompt=None):
    save = lambda:write(root,book)
    if action=="redraw_scene":
        redraw_scene(root,book,sid,notice,redraw_note,redraw_prompt)
        return
    if action=="redraw":
        if book.get("creation",{}).get("kind")!="text":
            raise ValueError("只有文生绘本可以按故事设定重新绘制首帧。")
        if not book["scenes"] or book.get("pending_source_pdf"):
            from scene_pipeline import run
            if not book["scenes"]:
                original_story.outline(root,book)
                save()
            errors = run(root,book,book["scenes"],(),notice,frames_only=True)
            if errors:
                raise RuntimeError(" / ".join(errors))
        elif book.get("visual_version")!=original_story.VISUAL_VERSION:
            original_story.generate(root,book,notice,redraw=True)
            quality.capture_story(book)
        save()
        return
    if action=="build":
        if not book["scenes"] and book.get("creation",{}).get("kind")=="text":
            notice("DeepSeek 编写全书大纲与角色资料")
            original_story.outline(root,book)
            save()
        elif not book["scenes"]:
            make(root,book,"analyze","","",notice)
        try:
            make(root,book,"all","","",notice)
        except (ValueError, RuntimeError):
            if book.get("activities") and all(s.get("frame") and s.get("script") and
                    (not s.get("clean_review") or s.get("clean_approved")) and
                    set(languages(book)) <= s.get("audio",{}).keys() for s in book["scenes"]):
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
        if scene.get("film_retry"):
            errors = review_films(root,book,[scene],notice,retry=True,required=True)
        else:
            if scene.get("video"):
                for key in ("video","video_seconds","video_source","task","planned_video_seconds"):
                    scene.pop(key,None)
                scene["animation_revision"] = uuid.uuid4().hex
                clear_film_review(scene)
                save()
            from scene_pipeline import run
            errors = run(root,book,[scene],languages(book),notice)
            errors.extend(review_films(root,book,[scene],notice,retry=True))
        if errors:
            raise RuntimeError(" / ".join(errors))
        if any(not s.get("script") or not set(languages(book)) <= s.get("audio",{}).keys() for s in book["scenes"]):
            make(root,book,"export",sid,"",notice)
        else:
            make(root,book,"activities","","",notice)
            faq_speech(root,book,languages(book),notice,save)
            complete = all(quality.accepted(root,book,s) for s in book["scenes"])
            make(root,book,"export" if complete else "proof","","",notice)
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
    if action == "review_film":
        errors = review_films(root,book,scenes,notice,retry=True,force=True,required=True)
        if errors:
            raise RuntimeError(" / ".join(errors))
        return
    if action == "approve_film":
        scene = scenes[0]
        review = quality.current_review(root,book,scene)
        if (not sid or not review or review["state"] != "needs_review" or not review["spots"] or
                scene.get("film_retry")):
            raise ValueError("本幕需要先校验当前成片，并至少保留一个已验证的可点物品。")
        review_spot_audio(root,book,scene,review)
        review["state"] = "approved"
        save()
        return
    if action in {"all","sample"}:
        from scene_pipeline import run
        errors = run(root,book,scenes,langs,notice)
        errors.extend(review_films(root,book,scenes,notice,retry=True))
        if action=="all" and all(s.get("script") for s in book["scenes"]):
            make(root,book,"activities","","",notice)
            faq_speech(root,book,langs,notice,save)
        if errors:
            raise RuntimeError(" / ".join(dict.fromkeys(errors)))
        make(root,book,"export",sid,"",notice)
        return
    steps = [action]
    for step in steps:
        if step=="activities":
            if any(not s.get("script") for s in book["scenes"]):
                raise ValueError("请先生成全书的逐幕制作稿。")
            notice("DeepSeek 编写观察题、双语问答和六道小测")
            if book.get("activities") and not observation_available(root,book):
                book["activities"] = None
                book["faq_audio"] = {}
            if not book.get("activities"):
                activity_book = {**book,"scenes":[
                    {**s,"script":{**s["script"],"spots":s["film_review"]["spots"]}}
                    if quality.accepted(root,book,s) else s for s in book["scenes"]
                ]}
                book["activities"]=ed.make_activities(root,activity_book)
                save()
            continue
        if step in {"export","proof","reel"}:
            from publishing import publish
            if step=="reel":
                animated = [s for s in scenes if s.get("video") and (root/"assets"/s["video"]).is_file()]
                if not animated:
                    raise ValueError("还没有已完成的动画镜头。")
                errors = review_films(root,book,animated,notice,retry=True)
                animated = [s for s in animated if quality.accepted(root,book,s)]
                if not animated:
                    raise ValueError(" / ".join(errors) or "还没有通过校验的动画镜头。")
                book["sample_edition"] = publish(root,book,animated,partial=True)
                save()
                continue
            if not sid and coverage(book):
                raise ValueError("全书仍有遗漏的原句或画面。请在场景页核对并归入场景。")
            notice("组装离线播放器、双语字幕和制作稿")
            if step=="proof" and sid:
                raise ValueError("整书校对版请选择全书范围。")
            if step=="export":
                errors = review_films(root,book,scenes,notice,retry=True,required=True)
                if errors:
                    raise RuntimeError(" / ".join(errors))
                if not sid and not observation_available(root,book):
                    make(root,book,"activities","","",notice)
                    faq_speech(root,book,enabled,notice,save)
            edition = publish(root,book,scenes,partial=bool(sid),proof=step=="proof")
            book["proof_edition" if step=="proof" else ("sample_edition" if sid else "edition")]=edition
            if not sid:
                book["play_edition"] = edition
            save()
            continue
        if step=="video" and len(scenes)>1:
            prepare_plot(root,book,notice)
            animate_book(root,book,scenes,notice)
            errors = review_films(root,book,scenes,notice,retry=True)
            if errors:
                raise RuntimeError(" / ".join(errors))
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
                    prepare_plot(root,book,notice)
                    s["script"] = ed.locate_spots(root,book,s) if s.get("cue_plan") else ed.draft(root,book,book["scenes"].index(s))
                    s.pop("cue_plan",None)
                    instructions.stamp(s,book,"tts","image2video","interaction")
            elif step=="voice":
                if not s.get("script"):
                    raise ValueError(f"请先生成「{s['title']}」的制作稿。")
                for l in langs:
                    if l not in s.get("audio",{}):
                        synthesize(root,book,s,l,notice)
                        save()
            elif step=="video":
                if not s.get("frame") or not s.get("script"):
                    raise ValueError("请先完成本幕首帧和动作提示。")
                if s.get("clean_review") and not s.get("clean_approved"):
                    raise ValueError(f"请先在清图对照中核对「{s['title']}」的残留标记。")
                prepare_plot(root,book,notice)
                if motion.pin_duration(s,enabled):
                    save()
                motion.animate(root,s,book.get("video_provider","heygen"),notice,save)
            else:
                raise ValueError("未知的制作步骤。")
            save()
        if step in {"video","voice"}:
            errors = review_films(root,book,scenes,notice,retry=True)
            if errors:
                raise RuntimeError(" / ".join(errors))
        if step=="voice" and not sid:
            faq_speech(root,book,langs,notice,save)
