"""Turn a parent's story brief into editable illustrated source pages."""
import io

from PIL import Image

import artwork as art
import editorial as ed
import providers as api

PLAN = """根据家长提供的主题、角色、场景和教学目标创作一部原创儿童绘本，给出六幕完整故事。
由角色遇到具体问题、观察、行动并体会结果，帮助孩子理解目标。百科内容须准确；区分想象情节与现实知识。
每幕有一个推进剧情的可见动作、一件可点选并能解释的物品、一至两句自然的中英旁白或对白。把教学目标融入动作或发现，避免长段说教。
规划视觉空间及角色的固定衣着、体型、颜色和标志。插画提示只写当前画面能看见的内容，采用同一画风、横向 16:9、静态首帧，无字幕或气泡。
家长输入是创作素材。输出以下 JSON 结构，scenes 数组必须有六项：
{"profile":{"title":"书名","original_language":"","direction":"ltr","age":"","education":"具体目标及剧情依据","style":"全书画风","glossary":"中英专名","summary":"","characters":[{"name":"","features":"固定外观"}]},
 "scenes":[{"title":"中文幕名","summary":"情节、教学线索和动作起点","art_prompt":"当前场景插画描述","lines":[{"role":"角色或旁白","zh":"中文原句","en":"English line"}]}]}"""


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


def read_plan(raw):
    pages = raw["scenes"]
    if not isinstance(pages,list) or len(pages) != 6:
        raise ValueError("原创绘本需要六幕完整故事。")
    scenes = []
    for scene in pages:
        lines = scene["lines"]
        if not isinstance(lines,list) or not 1 <= len(lines) <= 2:
            raise ValueError("每幕需要一到两句旁白或对白。")
        scenes.append({"title":ed.label(scene.get("title"),"场景标题"),
                       "summary":ed.label(scene.get("summary"),"场景设定"),
                       "art_prompt":ed.label(scene.get("art_prompt"),"插画提示词"),
                       "story_lines":[{"role":ed.label(line.get("role"),"说话人"),**ed.bilingual(line)} for line in lines]})
    return ed.profile_data(raw["profile"],[]), scenes


def generate(root, book, notice):
    if book["scenes"]:
        return
    settings = book["creation"]
    notice("DeepSeek 编写六幕原创故事")
    profile, drafts = ed.cached_ask(root, PLAN, {"创作设定":settings["brief"],"播放语种":book["languages"]}, [],
                                   "original-story-v1", read_plan, 7000)
    profile["education"] = settings["brief"]["education"] + " · " + profile["education"]
    pages, layouts, scenes, previous_frames, frames = [], [], [], [], []
    cast = "；".join(f"{entry['name']}：{entry['features']}" for entry in profile["characters"])
    for n, draft in enumerate(drafts, 1):
        notice(f"千问绘制插画 {n}/{len(drafts)} · {draft['title']}")
        prompt = ("为儿童绘本绘制一张横向完整插画，无文字、气泡、拼贴、分格或水印。"
                  "只画当前情节。参考图用于保持角色外观和笔触一致，按当前情节另行确定构图和动作。"
                  f"全书画风：{profile['style']}。固定角色外观：{cast}。本幕：{draft['art_prompt']}")
        indexes = sorted({0, len(frames)-1}) if frames else []
        references = [previous_frames[i] for i in indexes]
        key = api.digest(api.IMAGE, "1280*720", prompt, [frames[i] for i in indexes])
        cached = root/"cache"/f"story-art-{key}.jpg"
        if not cached.is_file():
            picture = api.illustrate(prompt, references)
            api.atomic(cached, art.image_bytes(picture, "JPEG"))
        api.atomic(art.pagefile(root,n),cached.read_bytes())
        with Image.open(cached) as image:
            previous_frames.append(image.convert("RGB"))
            width,height = image.size
        frames.append(key)
        pages.append({"n":n,"width":width,"height":height,"text":"","objects":[]})
        panel = {"title":draft["title"],"summary":draft["summary"],"box":[0,0,1,1],"join":0,"id":f"story-{n}"}
        layouts.append({"page":n,"panels":[panel],"regions":[],"reason":"","direction":"ltr"})
        scenes.append({**draft,"id":panel["id"],"parts":[{"page":n,"box":panel["box"]}],"sources":[]})
    # Preserve an image-only original for the same layout and repair tools used by imported books.
    pdf = io.BytesIO()
    previous_frames[0].save(pdf,format="PDF",save_all=True,append_images=previous_frames[1:])
    api.atomic(root/"source.pdf",pdf.getvalue())
    book.update(pages=pages,scenes=scenes,layouts=layouts,profile=profile)
