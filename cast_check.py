"""Build traceable character references and compare actual artwork and video frames."""
import hashlib
from io import BytesIO
import json
import math
import re

from PIL import Image, ImageDraw, ImageOps

import artwork as art
import editorial as ed
import providers as api


VIEWS = ("front", "side", "back")
VIEW_NAMES = {"front": "正面", "side": "侧面", "back": "背面",
              "three-quarter": "斜侧面", "unknown": "角度未标明", "turnaround": "正侧背参考卡"}
VIEW_MARKERS = (
    ("three-quarter", ("斜侧", "斜前", "斜后", "四分之三", "three-quarter", "three quarter", "3/4", "¾")),
    ("front", ("正面", "正脸", "正视", "正前", "front", "frontal")),
    ("side", ("侧面", "侧脸", "侧身", "侧视", "左侧", "右侧", "side", "profile")),
    ("back", ("背面", "背影", "背身", "后视", "正后", "背对", "back", "rear")),
)
HEX = re.compile(r"#[0-9A-Fa-f]{6}\Z")

PALETTE_TASK = """为原创绘本的每位已命名角色建立全书固定的造型设定。沿用已有外观和家长设定，
为毛发、肤色、服装或标志物的主要纯色色块选定 #RRGGBB 色号。把角色在画面中能直接观察、反复辨认的特征写成 2 或 3 条，
例如耳朵轮廓、固定衣着、标志物的位置和形状。用已给出的角色名，不能增删角色或重设其已有形象。
返回 {"characters":[{"name":"原角色名","palette":{"身体或服装部位":"#AABBCC"},"traits":["可见特征一","可见特征二"]}]}。"""

TURNAROUND_TASK = """验证实际参考卡的三个人物视角。左边是正面，中间是完整侧面，右边是背面。
逐栏写出能证明对应方向的实际可见细节。空栏、重复角度、被裁断的主要部位或图中看不到指定角度时，相关细节留空。
核对三栏属于同一造型，主要形状、服装、固定标志的位置及主要色相与角色设定相符。允许正常光影造成明暗差异。
齐备且相符才给 matches=true。否则 matches=false 并写出问题。
返回 {"views":{"front":"","side":"","back":""},"matches":true,"issues":[]}。"""

CHECK_TASK = """以收到的实际帧和角色参考图逐帧核对，按帧标签列出判断。故事摘要、分镜和台词只说明预期，
不得用来代替图像事实。PDF 模式同时对照本幕原画以辨认谁出场。逐帧列出核对清单中的每个角色。
match 表示实际可见的稳定特征（例如轮廓和服装）与对应参考一致，证据必须写出可见的对应关系。
drift 表示出现可见变化，如身份互换、脸或体型改变、毛发服装的主色改变、标志物消失或移位、肢体变形。
允许透视、姿势、表情、合理遮挡、光影和压缩造成的外观差异。只有不可见的特征时使用 uncertain，说明被遮挡或模糊的位置。
absent 用于画面合理未呈现该角色，例如没有到场、取景只含道具或从相邻实帧可见角色离开画框，指出实际可见内容。
若漏画原画中应保留的人物，或以别的人物替代，使用 drift。角色可见但无可用参考，使用 missing_reference 并说明可见内容。
原书没有某个角度时，仅凭已知可见特征判断，无足够对应证据则 uncertain。
意外出现的角色或多画的肢体写进该帧的 unexpected，给出位置、外观等可见细节。群像中的原有背景人物可保留。
没有待核对角色时，逐帧在 no_character_evidence 说明实际画面中可见的人物或物体。意外出现的角色仍写入 unexpected。
每帧每个角色只能判断一次，逐帧逐角色给出实际可见证据。
返回 {"frames":[{"label":"收到的帧标签","characters":[{"name":"角色名","status":"match|drift|uncertain|absent|missing_reference","evidence":"具体可见证据"}],"unexpected":[],"no_character_evidence":""}]}。"""


def _view(text):
    value = str(text or "").strip().lower()
    if re.search(r"不[明清确定]|未见|未知|没有|缺少|疑似|推测|unknown|unclear", value):
        return "unknown"
    if "半侧" in value or "斜" in value or any(word in value for word in VIEW_MARKERS[0][1]):
        return "three-quarter"
    matches = [name for name, markers in VIEW_MARKERS[1:] if any(word in value for word in markers)]
    return matches[0] if len(matches) == 1 else "unknown"


def _pdf_refs(root, person):
    seen = set()
    refs = []
    for ref in person.get("refs", []):
        page = ref.get("page")
        if type(page) is not int or page < 1 or not art.pagefile(root, page).is_file():
            continue
        box = art.box(ref["box"])
        location = page, tuple(box)
        if location in seen:
            continue
        seen.add(location)
        with Image.open(art.pagefile(root, page)) as source:
            x, y, right, bottom = box
            bounds = (math.floor(x * source.width), math.floor(y * source.height),
                      math.ceil(right * source.width), math.ceil(bottom * source.height))
            crop = source.crop(bounds).convert("RGB")
        asset = _save_picture(root, "cast-source", crop)
        refs.append({"page": page, "box": box, "source_view": ref.get("view", ""),
                     "view": _view(ref.get("view")), "asset": asset})
    return refs


def _save_picture(root, prefix, picture):
    buffer = BytesIO()
    picture.save(buffer, "PNG")
    raw = buffer.getvalue()
    asset = f"{prefix}-{hashlib.sha256(raw).hexdigest()[:24]}.png"
    path = root / "assets" / asset
    if not path.is_file():
        api.atomic(path, raw)
    return asset


def _pdf_features(root,person,refs):
    traits = [part.strip() for part in re.split(r"[，、；。;\n]",person["appearance"]) if part.strip()]
    palette = {}
    if refs:
        primary = next((ref for ref in refs if ref["view"] == "front"),refs[0])
        with Image.open(root/"assets"/primary["asset"]) as source:
            picture = source.convert("RGB")
        picture.thumbnail((120,120),Image.Resampling.NEAREST)
        colors = sorted(picture.getcolors(picture.width*picture.height),reverse=True)
        bins = set()
        for _,rgb in colors:
            key = tuple(component//32 for component in rgb)
            if key in bins or min(rgb) > 242:
                continue
            bins.add(key)
            palette[f"原画取样 {len(palette)+1}"] = "#%02X%02X%02X" % rgb
            if len(palette) == 3:
                break
    return {"palette":palette,"traits":list(dict.fromkeys(traits))[:3]}


def _pixels(picture):
    return picture.size, hashlib.sha256(picture.tobytes()).hexdigest()


def _specs(raw, names):
    specs = {}
    rows = raw.get("characters")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("角色定型缺少角色外观清单。")
    for item in rows:
        name = ed.label(item.get("name"), "角色名")
        if name not in names or name in specs:
            raise ValueError("角色定型重复或改变了资料卡中的角色名。")
        colors = item["palette"]
        if not isinstance(colors, dict) or not colors:
            raise ValueError(f"{name}至少需要一个固定的角色色号。")
        palette = {}
        for k, value in colors.items():
            part = ed.label(k, "固定色号部位")
            if not isinstance(value, str) or not HEX.fullmatch(value) or part in palette:
                raise ValueError(f"{name}的色号必须是没有重复部位的 #RRGGBB。")
            palette[part] = value.upper()
        if not isinstance(item["traits"], list):
            raise ValueError(f"{name}需要两至三个不同的可见特征。")
        traits = [ed.label(trait, "角色标志特征") for trait in item["traits"]]
        if not 2 <= len(traits) <= 3 or len(set(traits)) != len(traits):
            raise ValueError(f"{name}需要两至三个不同的可见特征。")
        specs[name] = {"palette": palette, "traits": traits}
    if specs.keys() != set(names):
        raise ValueError("固定外观资料没有覆盖全部角色。")
    return specs


def _description(person):
    palette = "、".join(f"{part} {code}" for part, code in person["palette"].items())
    traits = "、".join(person["traits"])
    parts = [person["appearance"], f"固定主色 {palette}" if palette else "", f"标志特征 {traits}" if traits else ""]
    return f"{person['name']}（{'；'.join(part for part in parts if part)}）"


def _turnaround(root, person, style):
    prompt = ("为原创绘本绘制一张角色三视图参考卡，在 16:9 浅色背景上等距排列三个同高的单一全身姿势。"
              "左边正面，中间严格侧面，右边背面，三栏都从头到脚完整、比例一致，互不重叠。"
              "从三个方向画同一角色，保持固定的颜色和标志特征。不要背景布景、文字、标签、边框或其他角色。\n"
              f"角色设定：{_description(person)}\n画风：{style}")
    key = api.digest(api.IMAGE, api.DS, TURNAROUND_TASK, prompt)
    index = root / "cache" / f"cast-turnaround-{key}.json"
    asset = json.loads(index.read_text(encoding="utf-8"))["asset"] if index.is_file() else ""
    if not asset or not (root / "assets" / asset).is_file():
        picture = api.illustrate(prompt).convert("RGB")

        def validate(raw):
            views = raw.get("views")
            issues = raw.get("issues")
            matches = raw.get("matches")
            if not isinstance(views, dict) or set(views) != set(VIEWS) or not isinstance(matches, bool):
                raise ValueError("角色三视图复核缺少正面、侧面、背面或通过标记。")
            if any(not isinstance(value, str) for value in views.values()) or not isinstance(issues, list):
                raise ValueError("角色三视图复核未给出可见证据或问题清单。")
            issues = [ed.label(item, "角色三视图问题") for item in issues]
            if matches and (issues or not all(value.strip() for value in views.values())):
                raise ValueError("角色三视图通过时需要三种角度的证据，且没有未处理问题。")
            return matches, issues

        matches, issues = ed.cached_ask(root, TURNAROUND_TASK, {"角色三视图核对": person},
                                        [("待核对的角色三视图", picture)], _pixels(picture), validate, 1000)
        if not matches:
            raise ValueError(f"{person['name']}的角色三视图未通过核对。{'、'.join(issues)}")
        asset = _save_picture(root, "cast-turnaround", picture)
        api.save_json(index, {"asset": asset})
    return {"view": "turnaround", "views": list(VIEWS), "asset": asset}


def prepare(root, book, notice):
    """Persist a bible for the current profile and the actual source page bytes."""
    profile = book.get("profile", {})
    people = profile.get("characters", [])
    is_text = book.get("creation", {}).get("kind") == "text"
    mode = "text" if is_text else "pdf"
    style = profile.get("style", "")
    brief = book.get("creation", {}).get("brief", {}) if is_text else {}
    sources = {}
    if not is_text:
        for person in people:
            for ref in person.get("refs", []):
                n = ref.get("page")
                if type(n) is int and n > 0 and n not in sources:
                    path = art.pagefile(root, n)
                    sources[n] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
    signature = api.digest("cast-bible-2", mode, people, sources, style, brief)
    previous = book.get("cast_reference", {})
    if previous.get("signature") == signature and all(
        (root / "assets" / ref["asset"]).is_file()
        for person in previous["characters"] for ref in person["refs"]
    ):
        return False
    people = [{"name": person["name"].strip(), "appearance": person.get("features", ""),
               "source": person} for person in people if str(person.get("name") or "").strip()]
    specs = {}
    if is_text and people:
        notice("设定角色固定色号和标志特征")
        names = [person["name"] for person in people]
        if len(names) != len(set(names)):
            raise ValueError("资料卡重复使用了同一个角色名。")
        specs = ed.cached_ask(root, PALETTE_TASK, {"角色定型": people, "画风": style, "家长设定": brief},
                             [], signature, lambda raw: _specs(raw, names), 2400)
    elif people:
        notice("从原书保存实际可见的角色参考裁图")
    characters = []
    for person in people:
        source = person.pop("source")
        name = person["name"]
        person.update(specs.get(name, {"palette": {}, "traits": []}))
        if is_text:
            notice(f"绘制并核对{name}的角色三视图")
            refs = [_turnaround(root, person, style)]
            available = set(VIEWS)
        else:
            refs = _pdf_refs(root, source)
            available = {ref["view"] for ref in refs}
            person.update(_pdf_features(root,person,refs))
        characters.append({**person, "refs": refs, "missing_views": [view for view in VIEWS if view not in available]})
    book["cast_reference"] = {"mode": mode, "signature": signature, "characters": characters}
    return True


def _scene_characters(book, scene):
    characters = book.get("cast_reference", {}).get("characters", [])
    if "characters" not in scene.get("visual", {}):
        return characters
    named = {person["name"] for person in scene["visual"]["characters"]}
    return [person for person in characters if person["name"] in named]


def direction(book, scene):
    characters = _scene_characters(book, scene)
    if not characters:
        return ""
    return ("参考图只用于识别造型。只在分镜要求的位置绘制相应角色，保持其固定颜色、轮廓、服装及标志特征。"
            "画面使用单一场景构图，去掉参考卡的视图排版及标签。\n" +
            "\n".join(f"C{i} {_description(person)}" for i, person in enumerate(characters, 1)))


def _saved_images(root, person):
    for ref in person["refs"]:
        path = root / "assets" / ref["asset"]
        if path.is_file():
            with Image.open(path) as picture:
                yield ref, picture.convert("RGB")


def image(root, book, scene):
    """Give the image provider a visual reference for the characters in this scene."""
    cells = [(f"C{i} / {ref['view']}", reference)
             for i, person in enumerate(_scene_characters(book, scene), 1)
             for ref, reference in _saved_images(root, person)]
    if not cells:
        return None
    cols = min(2, len(cells))
    width, height, caption = 720 // cols, 420 // cols, 20
    sheet = Image.new("RGB", (width * cols, (height + caption) * math.ceil(len(cells) / cols)), "#FAF8F4")
    draw = ImageDraw.Draw(sheet)
    for i, (label, picture) in enumerate(cells):
        x, y = i % cols * width, i // cols * (height + caption)
        sheet.paste(ImageOps.pad(picture, (width, height), color="#FAF8F4"), (x, y))
        draw.text((x + 12, y + height + 3), label, fill="#161616")
    return sheet


def _findings(raw, labels, cast, still):
    rows = raw.get("frames")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("角色复核缺少逐帧结果。")
    frame_labels = [ed.label(row.get("label"), "帧标签") for row in rows]
    if len(frame_labels) != len(labels) or set(frame_labels) != set(labels):
        raise ValueError("角色复核没有覆盖所有实际帧，或帧标签重复。")
    failures = []
    for frame, row in zip(frame_labels, rows):
        observations = row.get("characters")
        if not isinstance(observations, list) or any(not isinstance(item, dict) for item in observations):
            raise ValueError(f"{frame}缺少逐角色复核结果。")
        names = [ed.label(item.get("name"), f"{frame}角色名") for item in observations]
        if len(names) != len(cast) or set(names) != cast.keys():
            raise ValueError(f"{frame}没有覆盖全部待核对角色，或角色名重复。")
        if not cast:
            ed.label(row.get("no_character_evidence"), f"{frame}无人镜头的可见证据")
        for person, item in zip(names, observations):
            status = ed.label(item.get("status"), f"{frame}的{person}复核结果")
            evidence = ed.label(item.get("evidence"), f"{frame}的{person}可见证据")
            if status not in {"match", "drift", "uncertain", "absent", "missing_reference"}:
                raise ValueError(f"{frame}的{person}复核结果无效。")
            role = cast[person]
            if status == "absent":
                if still and role["required"]:
                    failures.append(f"{frame} · {person} · 首帧缺少分镜所列角色 · {evidence}")
            elif not role["refs"] or status == "missing_reference":
                failures.append(f"{frame} · {person} · 缺少角色参考 · {evidence}")
            elif status == "drift":
                failures.append(f"{frame} · {person} · 角色外观变化 · {evidence}")
            elif status == "uncertain":
                failures.append(f"{frame} · {person} · 无法辨认固定特征 · {evidence}")
        unexpected = row.get("unexpected", [])
        if not isinstance(unexpected, list):
            raise ValueError(f"{frame}的额外角色复核结果无效。")
        failures.extend(f"{frame} · 额外角色或肢体 · {ed.label(item, '实际可见证据')}" for item in unexpected)
    return failures


def check(root, book, scene, frames):
    bible = book.get("cast_reference")
    if bible is None:
        raise ValueError("尚未建立角色参考，无法核对角色一致性。")
    pictures = [(ed.label(label, "实际帧标签"), picture.convert("RGB")) for label, picture in frames]
    labels = [label for label, _ in pictures]
    if not pictures or len(set(labels)) != len(labels):
        raise ValueError("角色一致性检查需要实际帧，且帧标签不能重复。")
    is_text = bible["mode"] == "text"
    people = _scene_characters(book, scene) if is_text else bible["characters"]
    required = {person["name"] for person in scene.get("visual", {}).get("characters", [])} if is_text else set()
    cast = {}
    references = []
    for i, person in enumerate(people, 1):
        available = []
        for j, (ref, picture) in enumerate(_saved_images(root, person), 1):
            label = f"参考 C{i}.{j} {person['name']} · {VIEW_NAMES[ref['view']]}"
            if ref.get("page"):
                label += f" · 原书第 {ref['page']} 页"
            references.append((label, picture))
            available.append({**ref, "label": label})
        cast[person["name"]] = {**person, "refs": available, "required": person["name"] in required}
    if not is_text and scene.get("parts"):
        original, _, _ = art.compose(root, scene, [])
        references.append(("本幕未清理的 PDF 原画", original))
    still = len(pictures) == 1
    context = {"角色一致性检查": list(cast.values()), "实际帧标签": labels, "模式": bible["mode"],
               "素材": "首帧插画" if still else "视频采样帧", "场景": scene.get("summary", ""),
               "分镜": scene.get("visual", {}), "动作": scene.get("script", {}).get("video_prompt", "")}
    inputs = references + pictures
    source_key = [(label, _pixels(picture)) for label, picture in inputs]
    return ed.cached_ask(root, CHECK_TASK, context, inputs, source_key,
                         lambda raw: _findings(raw, labels, cast, still), 7500)
