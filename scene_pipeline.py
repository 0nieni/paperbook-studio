"""Stream scene manuscripts into independent frame, voice and animation workers."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from queue import Queue
from threading import Lock

from PIL import Image

import artwork as art
import editorial as ed
import motion
import original_story as story
import production as prod
import providers as api
from rsi import instructions


def prepare_frame(root, context, scene, compositions):
    if scene.get("needs_illustration"):
        picture, data, _ = story.illustrate_scene(root,scene["art_prompt"],compositions)
        n = scene["parts"][0]["page"]
        api.atomic(art.pagefile(root,n),data)
        frame,_,_ = art.compose(root,scene,[])
        encoded = art.image_bytes(frame)
        return {"frame":prod.artifact(root,"frame",encoded,"png"),"original":prod.artifact(root,"original",encoded,"png"),
                "clean_review":[]}, picture.size
    prod.clean(root,dict(context),scene)
    fields = ("frame","original","clean_review","clean_approved","rsi_used")
    return {k:scene[k] for k in fields if k in scene}, None


def prepare_script(root, context, beats, index, scene, designs):
    # Build a private working view only when a worker takes the task, even for long PDFs.
    scenes = list(beats)
    if designs:
        scenes = [{**beat,"visual":designs[beat["id"]]} if beat["id"] in designs else beat for beat in beats]
    scenes[index] = scene
    book = {**context,"scenes":scenes}
    if scene.get("needs_illustration"):
        return story.scene_prompt(root,book,index)
    ready = bool(scene.get("frame") and (root/"assets"/scene["frame"]).is_file())
    if ready:
        return {"script":ed.draft(root,book,index)}
    return ed.draft(root,book,index,before_frame=True)


def run(root, book, scenes, languages, notice, *, frames_only=False):
    """The coordinator alone updates project.json. Workers handle detached scene snapshots."""
    context = {k:book[k] for k in ("profile","pages","regions","creation","languages","voices","tts_model","rsi_prompts") if k in book}
    service = book.get("video_provider","heygen")
    video_limit = 1 if service == "heygen" else 3
    events = Queue()
    running = set()
    video_queue = deque()
    video_locks = {}
    errors = []
    video_failed = False
    compositions = story.Compositions()
    if book.get("pending_source_pdf"):
        for s in book["scenes"]:
            if not s.get("needs_illustration"):
                page = art.pagefile(root,s["parts"][0]["page"])
                if page.is_file():
                    with Image.open(page) as picture:
                        compositions.accept(story.composition(picture))

    with ExitStack() as stack:
        limits = {"plan":6,"frame":2,"voice":2,"spots":2,"video":video_limit}
        pools = {k:stack.enter_context(ThreadPoolExecutor(max_workers=n,thread_name_prefix=k)) for k,n in limits.items()}

        def submit(pool, stage, scene, fn, *args, lang=""):
            future = pools[pool].submit(fn,*args)
            running.add(future)
            future.add_done_callback(lambda f:events.put((stage,scene,lang,f)))

        def progress(text):
            events.put(("notice",None,None,text))

        def has_frame(scene):
            return scene.get("frame") and (root/"assets"/scene["frame"]).is_file()

        def explain(scene,lang):
            submit("voice","explain",scene,prod.add_spot_audio,root,context,deepcopy(scene),lang,
                   deepcopy(scene["narration"][lang]),progress,lang=lang)

        def render(scene,draft,lock):
            def checkpoint():
                fields = ("task","video","video_seconds","video_source")
                events.put(("checkpoint",scene,None,{k:deepcopy(draft[k]) for k in fields if k in draft}))
            with lock:
                motion.animate(root,draft,service,progress,checkpoint)

        video_active = 0
        def next_video():
            nonlocal video_active
            while video_queue and video_active < video_limit and not video_failed:
                scene = video_queue.popleft()
                draft = deepcopy(scene)
                draft["script"] = draft.get("script") or draft["cue_plan"]
                key,_,_ = motion.render_recipe(draft,service)
                submit("video","video",scene,render,scene,draft,video_locks.setdefault(key,Lock()))
                video_active += 1

        def frame_ready(scene):
            if frames_only:
                return
            if scene.get("clean_review") and not scene.get("clean_approved"):
                raise ValueError("首帧仍有清图复核项，请核对后继续制作。")
            if not (scene.get("video") and (root/"assets"/scene["video"]).is_file()):
                # Pin the recipe before submission, even if a voice track arrives during polling.
                if motion.pin_duration(scene,prod.languages(book)):
                    prod.write(root,book)
                video_queue.append(scene)
                next_video()
            if scene.get("cue_plan"):
                submit("spots","spots",scene,ed.locate_spots,root,context,deepcopy(scene))

        def dispatch(scene):
            if not frames_only:
                for lang in languages:
                    if lang in scene.get("audio",{}):
                        continue
                    if lang not in scene.get("narration",{}):
                        submit("voice","narrate",scene,prod.narrate,root,context,deepcopy(scene),lang,progress,lang=lang)
                    elif scene.get("script"):
                        explain(scene,lang)
            if has_frame(scene):
                frame_ready(scene)
            else:
                notice(f"制作首帧 · {scene['title']}")
                submit("frame","frame",scene,prepare_frame,root,context,deepcopy(scene),compositions)

        def dispatch_ready(scene):
            try:
                dispatch(scene)
            except (ValueError,RuntimeError,OSError) as error:
                errors.append(f"「{scene['title']}」{error}")

        order = {s["id"]:i for i,s in enumerate(book["scenes"])}
        beats = tuple(ed.scene_brief(s) for s in book["scenes"])
        designs = {s["id"]:s["visual"] for s in book["scenes"] if s.get("visual")}
        replanned = set()
        def start_plan(scene):
            index = order[scene["id"]]
            submit("plan","plan",scene,prepare_script,root,context,beats,index,deepcopy(scene),
                   designs.copy() if scene.get("needs_illustration") else {})

        notice(f"已建立 {len(scenes)} 个分镜子任务，开始并行制作")
        for scene in scenes:
            if scene.get("script") or scene.get("cue_plan"):
                dispatch_ready(scene)
            else:
                start_plan(scene)
        while running:
            stage, scene, lang, item = events.get()
            if stage == "notice":
                notice(item)
                continue
            if stage == "checkpoint":
                scene.update(item)
                prod.write(root,book)
                continue
            running.remove(item)
            if stage == "video":
                video_active -= 1
            try:
                result = item.result()
                if stage == "plan":
                    # Simultaneous writers may propose the same composition before either is saved.
                    if "visual" in result:
                        visual = result["visual"]
                        if any((other["environment"],other["shot"]) == (visual["environment"],visual["shot"])
                               for sid,other in designs.items() if sid != scene["id"]):
                            if scene["id"] in replanned:
                                raise ValueError("分镜设计仍与另一幕雷同，请调整本幕环境或机位后继续。")
                            replanned.add(scene["id"])
                            start_plan(scene)
                            continue
                        designs[scene["id"]] = visual
                    scene.update(result)
                    instructions.stamp(scene,book,"tts","image2video","interaction")
                    if scene.get("needs_illustration"):
                        instructions.stamp(scene,book,"story","text2image")
                    prod.write(root,book)
                    dispatch_ready(scene)
                elif stage == "frame":
                    fields, size = result
                    scene.update(fields)
                    if size:
                        scene.pop("needs_illustration",None)
                        page = book["pages"][scene["parts"][0]["page"]-1]
                        page.update(width=size[0],height=size[1])
                    prod.write(root,book)
                    frame_ready(scene)
                elif stage == "narrate":
                    scene.setdefault("narration",{})[lang] = result
                    prod.write(root,book)
                    if scene.get("script"):
                        explain(scene,lang)
                elif stage == "spots":
                    scene["script"] = result
                    scene.pop("cue_plan")
                    prod.write(root,book)
                    for language in languages:
                        if language in scene.get("narration",{}) and language not in scene.get("audio",{}):
                            explain(scene,language)
                elif stage == "explain":
                    scene.setdefault("audio",{})[lang] = result
                    scene["narration"].pop(lang,None)
                    prod.write(root,book)
                elif stage == "video":
                    next_video()
            except Exception as error:
                labels = {"plan":"编写提示词","frame":"制作首帧","narrate":"朗读","explain":"物品配音","spots":"热点定位","video":"动画"}
                errors.append(f"「{scene['title']}」{labels[stage]} · {error}")
                if stage == "video":
                    video_failed = True
    if book.get("pending_source_pdf") and all(not s.get("needs_illustration") for s in book["scenes"]):
        story.finish_source(root,book)
        prod.write(root,book)
    return errors
