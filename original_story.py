"""Turn a parent's story brief into editable illustrated source pages."""
import io
from threading import Lock

from PIL import Image, ImageChops, ImageStat

import artwork as art
import editorial as ed
import providers as api
from rsi import instructions

VISUAL_VERSION = 2
PLAN = """根据家长提供的主题、角色、场景和教学目标创作一部原创儿童绘本，给出六幕完整故事。
由角色遇到具体问题、观察、行动并体会结果，帮助孩子理解目标。百科内容须准确；区分想象情节与现实知识。
每幕有一个推进剧情的可见动作、一件可点选并能解释的物品、一至两句自然的中英旁白或对白。把教学目标融入动作或发现，避免长段说教。
为每幕独立写明环境（地点、前景、背景、光线），镜头（机位、景别、画面焦点），可见人物的具体位置和动作，以及可见道具的外观和位置。只填本幕出场的角色，名字与资料卡一致。
角色的衣着、体型、颜色和标志跨幕固定。环境或动作改变时，要让取景和道具一眼能看出变化。同一地点的连续动作也要变换景别、观察方向或视觉焦点。
用边缘清楚、干净柔和的平滑色块。不要纸纹、铅笔颗粒、彩色散点或密集笔触。所有插画是单张横向 16:9 静态首帧。
家长输入是创作素材。输出以下 JSON 结构，scenes 数组必须有六项：
{"profile":{"title":"书名","original_language":"","direction":"ltr","age":"","education":"具体目标及剧情依据","style":"全书画风","glossary":"中英专名","summary":"","characters":[{"name":"","features":"固定外观"}]},
 "scenes":[{"title":"中文幕名","summary":"情节、教学线索和动作起点","environment":"地点、前景、背景和光线","shot":"机位、景别和焦点",
 "visible_characters":[{"name":"资料卡中的角色名","staging":"画面位置、姿势、表情、动作"}],
 "props":["本幕具体道具的外观、位置和状态"],"lines":[{"role":"角色或旁白","zh":"中文原句","en":"English line"}]}]}"""


def brief(value):
    if not isinstance(value, dict):
        raise ValueError("请填写故事设定。")
    out = {}
    for key, label in (("theme","故事主题"),("characters","主要角色"),("setting","场景设定"),("education","教学目标")):
        item = ed.label(value.get(key), label)
        if len(item) > 600:
            raise ValueError(f"{label}请控制在 600 字内。")
        out[key] = item
    return out


def visual_design(scene, profile):
    cast = {ed.label(person["name"],"角色名"):ed.label(person["features"],"角色固定外观")
            for person in profile["characters"]}
    if not cast:
        raise ValueError("故事资料卡需要记录主要角色的固定外观。")
    visible, props = scene["visible_characters"], scene["props"]
    if not isinstance(visible,list) or not isinstance(props,list) or not props:
        raise ValueError("每幕需要人物出场清单与至少一个可见道具。")
    names = [ed.label(person.get("name"),"出场角色名") for person in visible]
    if len(names) != len(set(names)) or any(name not in cast for name in names):
        raise ValueError("出场人物要和资料卡对应，且每个名字只列一次。")
    return {"environment":ed.label(scene.get("environment"),"本幕环境"),
            "shot":ed.label(scene.get("shot"),"本幕镜头"),
            "characters":[{"name":name,"appearance":cast[name],"staging":ed.label(person.get("staging"),"人物位置和动作")}
                          for name, person in zip(names, visible)],
            "props":[ed.label(prop,"道具外观和位置") for prop in props]}


def read_plan(raw):
    pages = raw["scenes"]
    if not isinstance(pages,list) or len(pages) != 6:
        raise ValueError("原创绘本需要六幕完整故事。")
    profile = ed.profile_data(raw["profile"],[])
    scenes = []
    for scene in pages:
        lines = scene["lines"]
        if not isinstance(lines,list) or not 1 <= len(lines) <= 2:
            raise ValueError("每幕需要一到两句旁白或对白。")
        visual = visual_design(scene, profile)
        scenes.append({"title":ed.label(scene.get("title"),"场景标题"),
                       "summary":ed.label(scene.get("summary"),"场景设定"),
                       "visual":visual,
                       "story_lines":[{"role":ed.label(line.get("role"),"说话人"),**ed.bilingual(line)} for line in lines]})
    if len({(s["visual"]["environment"],s["visual"]["shot"]) for s in scenes}) != len(scenes):
        raise ValueError("每幕的环境或镜头至少有一项明显不同。")
    return profile, scenes


def illustration_prompt(visual, style):
    cast = "；".join(f"{p['name']}（固定外观：{p['appearance']}），{p['staging']}" for p in visual["characters"])
    props = "；".join(visual["props"])
    return (f"为儿童绘本从空白画布绘制一张横向 16:9 插画。\n本幕环境：{visual['environment']}\n"
            f"本幕镜头：{visual['shot']}\n可见人物：{cast or '本幕没有人物出场'}\n"
            f"本幕物品：{props}\n全书色彩与造型：{style}。只画本幕列出的人物和物品。"
            "采用边缘清楚、干净柔和的连续色块与光影，背景细节克制，避免纸纹、颗粒、彩噪、斑点、过锐描边与密集短笔触。"
            "单幅完整构图，无文字、水印、拼贴、边框或气泡。")


OUTLINE = """根据家长设定写六幕儿童故事大纲。现在只交全书资料卡和每幕的标题、事件摘要，
教育目标通过场景中的观察和行动逐步体现。明确人物固定外观和专名。每幕摘要写出独特的环境、可见动作、关键道具和镜头重点，供并发创作时对照全书。
输出 {"profile":{"title":"","original_language":"","direction":"ltr","age":"","education":"","style":"",
"glossary":"","summary":"","characters":[{"name":"","features":""}]},"scenes":[{"title":"","summary":""}]}。"""


def outline(root, book):
    def validate(raw):
        profile = ed.profile_data(raw["profile"], [])
        if not profile["characters"] or not isinstance(raw["scenes"],list) or len(raw["scenes"]) != 6:
            raise ValueError("请提供角色资料和六幕大纲。")
        for person in profile["characters"]:
            ed.label(person["name"],"角色名")
            ed.label(person["features"],"固定外观")
        scenes = [{k:ed.label(row.get(k),k) for k in ("title","summary")} for row in raw["scenes"]]
        return profile, scenes

    brief = book["creation"]["brief"]
    profile, beats = ed.cached_ask(root, instructions.apply(OUTLINE,book,"story"),
                                   {"创作设定":brief,"播放语种":book["languages"]}, [], "outline-1", validate, 3600)
    profile["education"] = brief["education"] + " · " + profile["education"]
    scenes, layouts, pages = [], [], []
    for n, beat in enumerate(beats,1):
        panel = {**beat,"box":[0,0,1,1],"join":0,"id":f"story-{n}"}
        scenes.append({**beat,"id":panel["id"],"parts":[{"page":n,"box":panel["box"]}],
                       "sources":[],"needs_illustration":True})
        layouts.append({"page":n,"panels":[panel],"regions":[],"reason":"","direction":"ltr"})
        pages.append({"n":n,"width":1280,"height":720,"text":"","objects":[]})
    book.update(profile=profile,scenes=scenes,layouts=layouts,pages=pages,regions=[],
                pending_source_pdf=True,visual_version=VISUAL_VERSION)
    # Keep the approved source outline independently of later scene edits.
    from quality import capture_story
    capture_story(book)


def scene_prompt(root, book, index):
    scene = book["scenes"][index]
    prior = [s["visual"] for s in book["scenes"] if s["id"] != scene["id"] and s.get("visual")]
    task = """只编写指定的一幕。按事件大纲写出独立的地点与光线、景别与焦点、人物位置动作和物品外观位置。
角色外观与资料卡一致，相邻幕应有明显可见的变化。使用干净色块。用一至两句双语台词讲述动作或发现。
输出 {"environment":"","shot":"","visible_characters":[{"name":"","staging":""}],"props":[""],"script":制作稿对象}。
制作稿采用下述字段。当前尚在设计首帧，spots 先选计划画出的物品，后续会看实际插画定位。sources、clean_warnings 填空数组。
""" + ed.SCRIPT_TASK

    def validate(raw):
        visual = visual_design(raw,book["profile"])
        if any((v["environment"],v["shot"]) == (visual["environment"],visual["shot"]) for v in prior):
            raise ValueError("请换用能辨别本幕的环境或机位。")
        script = ed.validate_script(raw["script"],scene)
        if len(script["lines"]) > 2:
            raise ValueError("原创绘本每幕需要一至两句旁白或对白。")
        prompt = instructions.apply(illustration_prompt(visual,book["profile"]["style"]),book,"text2image")
        return {"visual":visual,"art_prompt":prompt,"story_lines":[{k:l[k] for k in ("role","zh","en")} for l in script["lines"]],
                "cue_plan":script}

    context = {"资料卡":book["profile"],"家长设定":book["creation"]["brief"],"当前幕":scene["id"],
               "故事大纲":[ed.scene_brief(s) for s in book["scenes"]],"其他已定画面设计":prior}
    context["本幕情节节点"] = [n for n in book.get("plot_nodes",[]) if n["id"] in scene.get("plot_ids",[])]
    if book.get("learning_goals"):
        from learning import direction
        context["本幕学习依据"] = direction(book,scene)
        task += "\n在道具讲解和情节动作中落实相关学习目标，并把事实约束写入动画提示。"
    return ed.cached_ask(root,instructions.apply(task,book,"story","tts","image2video","interaction"),
                         context,[],VISUAL_VERSION,validate,5500)


def finish_source(root, book):
    pictures = []
    try:
        for page in book["pages"]:
            with Image.open(art.pagefile(root,page["n"])) as image:
                pictures.append(image.convert("RGB"))
        pdf = io.BytesIO()
        pictures[0].save(pdf,format="PDF",save_all=True,append_images=pictures[1:])
        api.atomic(root/"source.pdf",pdf.getvalue())
        book.pop("pending_source_pdf",None)
    finally:
        for picture in pictures:
            picture.close()


def composition(picture):
    return picture.convert("RGB").resize((32,18),Image.Resampling.LANCZOS)


class Compositions:
    """Reserve each accepted layout before a simultaneous illustration can use it."""
    def __init__(self):
        self.thumbnails = []
        self.lock = Lock()

    def accept(self, thumbnail):
        with self.lock:
            if any(max(ImageStat.Stat(ImageChops.difference(thumbnail, old)).mean) < 5
                   for old in self.thumbnails):
                return False
            self.thumbnails.append(thumbnail)
            return True


def illustrate_scene(root, prompt, seen, *, reference=None, verify=None):
    correction = ""
    reference_id = api.digest(art.image_bytes(reference).hex()) if reference is not None else None
    for attempt in range(2):
        key = api.digest(api.IMAGE,VISUAL_VERSION,"1280*720",prompt+correction,*([reference_id] if reference_id else []))
        cached = root/"cache"/f"story-art-{key}.jpg"
        if cached.is_file():
            with Image.open(cached) as image:
                picture = image.convert("RGB")
        else:
            picture = api.illustrate(prompt+correction,reference) if reference is not None else api.illustrate(prompt+correction)
        issues = verify(picture) if verify else []
        if issues and attempt == 0:
            correction = "\n校验发现角色不一致。请参照角色设定图纠正：" + "；".join(issues)
            continue
        thumbnail = composition(picture)
        if not seen.accept(thumbnail):
            correction = "\n另起新构图。严格按本幕机位和景别取景，把指定物品放在明显的位置。"
            continue
        if not cached.is_file():
            api.atomic(cached,art.image_bytes(picture,"JPEG"))
        return picture, cached.read_bytes(), thumbnail, issues
    raise ValueError("千问连续返回与前幕近乎相同的构图。请修改本幕环境或机位后继续制作。")


def generate(root, book, notice, redraw=False):
    if book["scenes"] and not redraw:
        return
    settings = book["creation"]
    notice("DeepSeek 编写六幕原创故事")
    profile, drafts = ed.cached_ask(root, instructions.apply(PLAN,book,"story"), {"创作设定":settings["brief"],"播放语种":book["languages"]}, [],
                                   VISUAL_VERSION, read_plan, 9500)
    profile["education"] = settings["brief"]["education"] + " · " + profile["education"]
    pages, layouts, scenes, pictures, page_bytes, seen = [], [], [], [], [], Compositions()
    for n, draft in enumerate(drafts, 1):
        notice(f"千问绘制插画 {n}/{len(drafts)} · {draft['title']}")
        prompt = instructions.apply(illustration_prompt(draft["visual"], profile["style"]),book,"text2image")
        try:
            picture, data, thumbnail, _ = illustrate_scene(root,prompt,seen)
        except ValueError as error:
            raise ValueError(f"「{draft['title']}」{error}") from error
        pictures.append(picture)
        page_bytes.append(data)
        width,height = picture.size
        pages.append({"n":n,"width":width,"height":height,"text":"","objects":[]})
        panel = {"title":draft["title"],"summary":draft["summary"],"box":[0,0,1,1],"join":0,"id":f"story-{n}"}
        layouts.append({"page":n,"panels":[panel],"regions":[],"reason":"","direction":"ltr"})
        scenes.append({**draft,"art_prompt":prompt,"id":panel["id"],"parts":[{"page":n,"box":panel["box"]}],"sources":[]})
        instructions.stamp(scenes[-1],book,"story","text2image")
    # Preserve an image-only original for the same layout and repair tools used by imported books.
    pdf = io.BytesIO()
    pictures[0].save(pdf,format="PDF",save_all=True,append_images=pictures[1:])
    for n, data in enumerate(page_bytes,1):
        api.atomic(art.pagefile(root,n),data)
    api.atomic(root/"source.pdf",pdf.getvalue())
    book.update(pages=pages,scenes=scenes,layouts=layouts,profile=profile,regions=[],activities=None,
                faq_audio={},visual_version=VISUAL_VERSION)
    book.pop("reference",None)
