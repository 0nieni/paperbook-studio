"""Structured editorial tasks. Scene and source IDs are assigned by the host."""
import hashlib
import json
import re

from PIL import Image

import artwork as art
import providers as api
from rsi import instructions

PAGE_TASK = """阅读当前页，前后页仅作判断接缝和阅读顺序的参考。输出 JSON 对象：
{"direction":"ltr或rtl","skip_reason":"故事页留空，封面/扉页/版权等说明原因","panels":[{"title":"简短中文标题","summary":"可见事件","box":[左,上,右,下],"join_previous":0}],"regions":[{"text":"准确转录，可包含多行","speaker":"旁白或角色名","kind":"narration/dialogue/bubble/prop/mark/note","box":[左,上,右,下],"panel":1}]}。
所有坐标为相对当前整页的 0~1 小数，原点左上。panels 按故事阅读顺序，每个独立插画、连续动作都要列，连成一幅图的跨页部分保留至书脊。画框可排除指导栏和页眉；台词即使在画框外也要关联。跨页确属同一幅连续画时 join_previous 填前一页对应 panel 的 1 基编号，其余为 0。不要因同样的人物背景就合并。
regions 录入每句旁白和对白；同一气泡有多行时可整体记录，box 包含气泡尾巴及印刷底板，纯文字留适量字边。道具上的字用 prop，水印版权用 mark，教学边栏用 note。panel 为本页分镜的 1 基编号，不能确定填 0。关键无字画面也占一幕。"""
PROFILE_TASK = """依据全书逐页摘要和原文输出资料卡 JSON：
{"title":"","original_language":"","direction":"ltr或rtl","age":"","education":"主题及具体原页依据","style":"","glossary":"角色和专名的中英写法","summary":"前因后果","characters":[{"name":"","features":"固定可见特征","refs":[{"page":1,"box":[0,0,1,1],"view":"已可见角度"}]}]}。角色参考取原书真实所在区域，可提供多种角度，未见角度勿捏造。title 用原书正式书名。"""
SCRIPT_TASK = """读取原文、资料卡及清图，按照技能编写仅当前场景的分镜和配音稿。输出 JSON：
{"title":{"zh":"","en":""},"lines":[{"role":"旁白或角色名","sources":["原文ID"],"zh":"只读的中文正文","en":"English spoken text","direction":{"zh":"普通话表演指令","en":"English performance direction"}}],"video_prompt":"可直接发给 HeyGen Video 1 或 MiniMax H3 的完整英文或中文首帧动作提示，含起始构图、可见动作、结束姿势与静音","spots":[{"label":{"zh":"","en":""},"box":[左,上,右,下],"explain":{"zh":"","en":""}}],"clean_warnings":[]}。
每个原文旁白和对白都要有对应的 sources；已有双语合用同一条。来源为当前语言时逐字保留正文，数字、次序和对话尤其要精确，不要复述相邻幕或再配额外描述。无字图可依据画面补一句简短旁白。每次说话人变化就换行，长句自然分句。至少一条双语朗读和一个清晰可点物品。每条配音使用儿童能听懂的自然语句；不在正文里写表演说明。spots 的 0~1 坐标相对清理后的 16:9 整张首帧。按本幕情节节点的顺序写入可见动作，要求成片末帧停稳且候选互动道具清晰可见。对照原图与清理图查残字、气泡轮廓和被改坏的脸或道具；确有问题写入 clean_warnings，出版标记和道具文字均可保留。"""
ACTIVITY_TASK = """为这一本书生成可离线使用的双语互动 JSON。所有 zh/en 字段写同义的自然中英文，evidence 只能列给定场景 ID。
{"faq":[{"question":{"zh":"","en":""},"reply":{"zh":"","en":""},"keywords":{"zh":[""],"en":[""]},"evidence":["场景ID"]}],
"observation":{"scene":"场景ID","spot":"该幕实际热点ID","question":{"zh":"","en":""},"hint":{"zh":"","en":""}},
"quiz":[{"type":"关键情节","mode":"choice","question":{"zh":"","en":""},"options":[{"zh":"","en":""}],"answer":0,"hint":{"zh":"","en":""},"explain":{"zh":"","en":""},"keywords":{"zh":[""],"en":[""]},"evidence":["场景ID"]}]}。
FAQ 有三至八条。六道小测按技能的六种题型排列，mode 依次 choice,order,choice,choice,choice,open。每道选择或排序均给 3 个不同选项，索引从 0 开始。排序的 answer 是三个索引的正确排列。开放题也给启发式双语 hint 和 explain，options 可空且不计分。第 3、4 题要有至少两处不同原页的证据（全书只有一页时用该页真实证据）。观察题选一个真实热点。每句话以原句和 role 为准，核对角色、物品和事件顺序。"""


def label(value, key="文字"):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key}为空，请在制作台修正或重新生成。")
    return value.strip()


def bilingual(value):
    if not isinstance(value, dict):
        raise ValueError("双语字段缺少中英文内容。")
    return {l: label(value.get(l), l) for l in ("zh", "en")}


def model_box(value):
    if isinstance(value, list) and len(value) == 4 and max(float(x) for x in value) > 1:
        value = [float(x)/1000 for x in value]
    return art.box(value)


def normalize_layout(n, raw):
    panels, regions = [], []
    for j, p in enumerate(raw.get("panels", []), 1):
        panels.append({"id": f"p{n:03d}-s{j}", "title": label(p.get("title")), "summary": str(p.get("summary","")),
                       "box": model_box(p["box"]), "join": int(p.get("join_previous") or 0)})
    for j, r in enumerate(raw.get("regions", []), 1):
        kind = r.get("kind")
        if kind not in art.KINDS:
            raise ValueError(f"第 {n} 页文字种类无效。")
        bounds=model_box(r["box"])
        if kind in art.REMOVE:
            bounds=art.box([max(0,bounds[0]-.009),max(0,bounds[1]-.008),min(1,bounds[2]+.014),min(1,bounds[3]+.008)])
        regions.append({"id": f"p{n:03d}-t{j}", "page": n, "text": str(r.get("text") or ""),
                        "speaker": str(r.get("speaker") or ""), "kind": kind, "box": bounds,
                        "panel": int(r.get("panel") or 0)})
    reason = str(raw.get("skip_reason") or "")
    if not panels and not reason:
        raise ValueError(f"第 {n} 页没有分镜或省略原因，请重试分镜识别。")
    return {"page": n, "panels": panels, "regions": regions, "reason": reason,
            "direction": "rtl" if raw.get("direction") == "rtl" else "ltr"}


def cached_ask(root, task, context, images, source_key, validate, tokens):
    signature = api.digest(api.DS, api.SKILL.read_text(encoding="utf-8"), task, context, source_key)
    path = root/"cache"/("ds-"+signature+".json")
    if path.exists():
        return validate(json.loads(path.read_text(encoding="utf-8")))
    result = api.ask(task, context, images, tokens)
    try:
        normalized = validate(result)
    except (ValueError, TypeError, KeyError, IndexError) as error:
        repair = task + "\n前次 JSON 校验失败，请按原图和资料修正后完整重发。具体问题：" + str(error)[:300]
        result = api.ask(repair, {**context,"待修正输出":result}, images, tokens)
        normalized = validate(result)
    api.save_json(path,result)
    return normalized


def read_page(root, pages, n, previous, book=None):
    context = {"page": n, "total": len(pages), "previous_panels": previous.get("panels", []),
               "文字层": pages[n-1]["text"][:18000]}
    images = []
    for k in range(max(1,n-1), min(len(pages),n+1)+1):
        with Image.open(art.pagefile(root,k)) as im:
            p = im.convert("RGB")
            if k != n:
                p.thumbnail((760,760))
            images.append((f"PDF 第 {k} 页" + ("，当前要处理的页" if k==n else "，相邻参考页"),p))
    layout = cached_ask(root,instructions.apply(PAGE_TASK,book or {},"story"),context,images,n,lambda r:normalize_layout(n,r),11000)
    reconcile_text(layout, pages[n-1]["objects"])
    return layout


def reconcile_text(layout, objects):
    """PDF glyph runs give more accurate bounds than vision. Surface uncovered source runs."""
    def plain(text):
        return re.sub(r"[^\w\u3400-\u9fff]", "", text).casefold()
    claimed = set()
    for region in layout["regions"]:
        text = plain(region["text"])
        b = region["box"]
        candidates = []
        for i,obj in enumerate(objects):
            word = plain(obj["text"])
            ob = obj["box"]
            dx = max(ob[0]-b[2], b[0]-ob[2], 0)
            dy = max(ob[1]-b[3], b[1]-ob[3], 0)
            if word and word in text and dx < .065 and dy < .075:
                candidates.append((i, ob))
                claimed.add(i)
        if candidates and region["kind"] in art.REMOVE:
            # Include model-detected bubbles and any embedded text not in the PDF layer.
            bounds = [b] + [ob for _,ob in candidates]
            region["box"] = art.box([max(0,min(x[0] for x in bounds)-.002), max(0,min(x[1] for x in bounds)-.003),
                             min(1,max(x[2] for x in bounds)+.002), min(1,max(x[3] for x in bounds)+.003)])
    for i,obj in enumerate(objects):
        if i in claimed or len(plain(obj["text"]))<2 or not layout["panels"]:
            continue
        if any(r["kind"] in {"note","mark","prop"} and art.area(art.intersection(r["box"],obj["box"])) >
               .45*art.area(obj["box"]) for r in layout["regions"]):
            continue
        layout["regions"].append({"id":f"p{layout['page']:03d}-v{i+1}", "page":layout["page"], "text":obj["text"],
                  "kind":"narration","speaker":"待核对","box":obj["box"],"panel":0})


def review_assignments(root, scenes, regions, profile):
    prompt = """核对全书每行文字的语义归属，尤其是印刷位置在右页、讲述左图的台词。输出 JSON
{"changes":[{"region":"文字ID","scene":"应归入的真实场景ID","kind":"narration/dialogue/bubble/mark/prop/note"}]}。
只列必须修正的项。为待核对的原始文字片段分类。汉字字源、阅读指导、页码与教学提问标为 note，出版标记标为 mark，已知台词的重复文字片段可标为 note。其他原句按动作和说话人分配给同页或相邻页场景，保留有效原分配。非朗读区域的 scene 可为空。"""
    by = {s["id"]:s for s in scenes}
    rows = {r["id"]:r for r in regions}
    def check(v):
        changes = v["changes"]
        for c in changes:
            if c["region"] not in rows or c["kind"] not in art.KINDS:
                raise ValueError("原文复核编号或分类无效。")
            if c["kind"] in art.REMOVE and c.get("scene") not in by:
                raise ValueError("原文复核缺少目标场景。")
        return changes
    context = {"全书":profile["summary"], "场景":[scene_brief(s) for s in scenes],
               "文字":[{k:r[k] for k in ("id","page","text","kind","speaker")} for r in regions]}
    changes = cached_ask(root,prompt,context,[],0,check,6500)
    for c in changes:
        rid=c["region"]
        for s in scenes:
            if rid in s["sources"]:
                s["sources"].remove(rid)
        rows[rid]["kind"]=c["kind"]
        if c["kind"] in art.REMOVE:
            by[c["scene"]]["sources"].append(rid)
    order={r["id"]:i for i,r in enumerate(regions)}
    for s in scenes:
        s["sources"].sort(key=order.get)


def join_layout(layouts):
    scenes, regions, mapping = [], [], {}
    for layout in layouts:
        n = layout["page"]
        for j,p in enumerate(layout["panels"],1):
            prev = mapping.get((n-1,p["join"])) if p["join"] else None
            part = {"page":n, "box":p["box"]}
            if prev is None:
                prev = {"id":p["id"],"title":p["title"],"summary":p["summary"],"parts":[part],"sources":[]}
                scenes.append(prev)
            else:
                if layout["direction"]=="rtl":
                    prev["parts"].insert(0,part)
                else:
                    prev["parts"].append(part)
                prev["summary"] += " " + p["summary"]
            mapping[(n,j)] = prev
        for entry in layout["regions"]:
            region = {k:v for k,v in entry.items() if k != "panel"}
            regions.append(region)
            target = mapping.get((n,entry["panel"]))
            if target and region["kind"] in art.REMOVE:
                target["sources"].append(region["id"])
    return scenes, regions


def seam_distance(left, right):
    """Measure whether a nonuniform vertical colour pattern continues across the binding."""
    a=left.convert("RGB").resize((256,256),Image.Resampling.BOX).crop((254,0,256,256)).resize((1,256))
    b=right.convert("RGB").resize((256,256),Image.Resampling.BOX).crop((0,0,2,256)).resize((1,256))
    ca,cb=list(a.getdata()),list(b.getdata())
    def distance(shift):
        return sum(abs(pixel[ch]-cb[(i+shift)%256][ch]) for i,pixel in enumerate(ca) for ch in range(3))/768
    direct=distance(0)
    shifted=min(distance(51),distance(83))
    return direct, shifted


def check_spreads(root, layouts, notice):
    pairs=[(a,b) for a,b in zip(layouts,layouts[1:]) if a["panels"] and b["panels"]]
    prompt="""逐张看已经按书脊对齐的双页拼图。不同拼图互相独立。判断两半是否原本为同一幅插画，尤其是人物、积木、笔触或地板纹理横跨接缝时，只保留一个场景。两页各有框、同人物但不同时刻的画面要分开。
输出 JSON {"pairs":[{"page":后页编号,"joins":[{"previous":前页分镜的1基序号,"current":后页分镜的1基序号}]}]}，核对全部给定拼图，未跨页时 joins 为空。"""
    for start in range(0,len(pairs),4):
        group=pairs[start:start+4]
        notice(f"核对双页拼图与书脊连续性 · {start+1}–{start+len(group)}")
        images, context, exact = [], [], {}
        for a,b in group:
            with Image.open(art.pagefile(root,a["page"])) as first, Image.open(art.pagefile(root,b["page"])) as second:
                rev=b["direction"]=="rtl"
                left,right=(second,first) if rev else (first,second)
                diff,shifted=seam_distance(left,right)
                exact[b["page"]]=diff<16 and shifted>24 and diff<.42*shifted
                scale=min(550/max(left.height,right.height),1500/(left.width+right.width))
                halves=[im.convert("RGB").resize((max(1,round(im.width*scale)),max(1,round(im.height*scale))),Image.Resampling.LANCZOS) for im in (left,right)]
                sheet=Image.new("RGB",(sum(im.width for im in halves),max(im.height for im in halves)),"white")
                sheet.paste(halves[0],(0,0));sheet.paste(halves[1],(halves[0].width,0))
                images.append((f"原稿第 {a['page']} 与 {b['page']} 页，对齐后的完整开本。",sheet))
            context.append({"previous_page":a["page"],"page":b["page"],"direction":b["direction"],
                            "previous_panels":a["panels"],"current_panels":b["panels"]})
        def validate(result):
            items=result.get("pairs",[])
            if {item["page"] for item in items}!={b["page"] for _,b in group}:
                raise ValueError("跨页复核没有覆盖全部相邻页。")
            return {item["page"]:item["joins"] for item in items}
        decisions=cached_ask(root,prompt,{"pairs":context},images,start,validate,3200)
        for a,b in group:
            joins=decisions[b["page"]]
            if exact[b["page"]] and len(a["panels"])==len(b["panels"])==1:
                joins=[{"previous":1,"current":1}]
            for panel in b["panels"]:
                panel["join"]=0
            for link in joins:
                pi,ci=int(link["previous"])-1,int(link["current"])-1
                if not (0<=pi<len(a["panels"]) and 0<=ci<len(b["panels"])):
                    raise ValueError("跨页复核给出无效分镜编号。")
                pa,pb=a["panels"][pi],b["panels"][ci]
                pb["join"]=pi+1
                if b["direction"]=="rtl":
                    pa["box"][0],pb["box"][2]=0,1
                else:
                    pa["box"][2],pb["box"][0]=1,0


def profile_data(value, pages):
    out = {k:str(value.get(k) or "") for k in ("title","original_language","direction","age","education","style","glossary","summary")}
    out["title"] = label(out["title"], "书名")
    out["characters"] = []
    for person in value.get("characters",[]):
        refs = []
        for r in person.get("refs",[]):
            n = int(r.get("page",0))
            if 1 <= n <= len(pages):
                refs.append({"page":n,"box":model_box(r["box"]),"view":str(r.get("view",""))})
        out["characters"].append({"name":str(person.get("name","")),"features":str(person.get("features","")),"refs":refs})
    return out


def make_profile(root, layouts, pages):
    overview = [{"page":l["page"],"panels":l["panels"],"text":[r["text"] for r in l["regions"] if r["kind"] in art.REMOVE],
                 "skip":l["reason"]} for l in layouts]
    # A labeled contact sheet provides original visual evidence for the reference crops.
    from PIL import ImageDraw
    sheets = []
    for start in range(0,len(pages),16):
        group = pages[start:start+16]
        sheet = Image.new("RGB",(4*250,((len(group)+3)//4)*252),"white")
        for i,p in enumerate(group):
            with Image.open(art.pagefile(root,p["n"])) as im:
                im.thumbnail((245,225))
                x,y = (i%4)*250,(i//4)*252
                sheet.paste(im,(x,y))
                ImageDraw.Draw(sheet).text((x,y+226),f"PDF {p['n']}",fill="black")
        sheets.append((f"逐页索引，第 {start+1} 页起。坐标相对于各原页。",sheet))
    return cached_ask(root,PROFILE_TASK,{"全书资料":overview},sheets,len(pages),lambda v:profile_data(v,pages),5000)


def validate_script(raw, scene):
    out = {"title":bilingual(raw["title"]), "video_prompt":label(raw.get("video_prompt"),"首帧动作提示"),
           "spots":[], "lines":[], "clean_warnings":[str(x) for x in raw.get("clean_warnings",[])]}
    covered = set()
    for i,line in enumerate(raw["lines"],1):
        sources = list(dict.fromkeys(str(s) for s in line.get("sources",[])))
        if set(sources)-set(scene["sources"]):
            raise ValueError("台词引用了本幕以外的原句。")
        covered.update(sources)
        out["lines"].append({"id": f"l{i}", "role":str(line.get("role") or "旁白"), "sources":sources,
                             **bilingual(line), "direction":bilingual(line["direction"])})
    if not out["lines"] or covered != set(scene["sources"]):
        raise ValueError("本幕缺少朗读或原文尚未全部对应。")
    for i,spot in enumerate(raw.get("spots",[]),1):
        out["spots"].append({"id":f"h{i}", "box":model_box(spot["box"]), "label":bilingual(spot["label"]),
                             "explain":bilingual(spot["explain"])})
    if not out["spots"]:
        raise ValueError("本幕至少标出一个可观察的物品。")
    return out


def scene_brief(scene):
    return {k:scene[k] for k in ("id","title","summary","parts","sources")}


def draft(root, book, i, before_frame=False):
    scenes, s = book["scenes"], book["scenes"][i]
    context = {"资料卡":book["profile"],"前一幕":scenes[i-1]["summary"] if i else "",
               "后一幕":scenes[i+1]["summary"] if i+1<len(scenes) else "",
               "当前场景":scene_brief(s),"逐句原文":[r for r in book["regions"] if r["id"] in s["sources"]]}
    context["本幕情节节点"] = [n for n in book.get("plot_nodes",[]) if n["id"] in s.get("plot_ids",[])]
    images = []
    task = SCRIPT_TASK
    if book.get("learning_goals"):
        from learning import direction
        context["本幕学习依据"] = direction(book,s)
        task += "\n按本幕学习依据安排可见动作与道具讲解，事实约束必须写入动画导演稿。原文照录。"
    if before_frame:
        original,_,_ = art.compose(root,s,[])
        images.append(("按本幕裁切、拼接的原画，尺寸与最终首帧一致",original))
        signature = ["pre-clean",hashlib.sha256(art.image_bytes(original)).hexdigest(),s["parts"]]
        task += ("\n此步骤先看已拼接的原画，根据原句和画面写朗读与动作。额外返回 clean_instruction 字符串，"
                 "用一两句话交代本幕待清除的印字及附近须保留的人物、道具、轮廓和背景。热点将在清图后复核。")
    else:
        for field, title in (("original","原图"),("frame","清理后的横屏首帧，标注热点请用此图")):
            with Image.open(root/"assets"/s[field]) as im:
                images.append((title,im.convert("RGB")))
        signature = [s["frame"],s["original"]]
    if book.get("creation",{}).get("kind") == "text":
        context["原创故事设定"] = book["creation"]["brief"]
        context["本幕原创台词草稿"] = s.get("story_lines",[])
        context["本幕画面设计"] = s.get("visual",{})
        task += "\n本书为原创插画，画面上未印台词。结合本幕情节与台词草稿编写双语朗读，sources 填空数组。用真实可见的物品讲解教学目标。首帧动画仅使用本幕已经出现的环境、角色与道具，写明哪个角色或物品如何移动及结束位置，不借用相邻幕画面。"
    if s.get("redraw_lines"):
        context["已确认的逐句台词与朗读指令"] = s["redraw_lines"]
        task += "\n沿用已确认的逐句台词，根据新首帧重新编排动作提示并定位互动道具。"
    task = instructions.apply(task,book,"tts","image2video","interaction")
    def validate(raw):
        if s.get("redraw_lines"):
            raw = {**raw,"lines":s["redraw_lines"]}
        script = validate_script(raw,s)
        if before_frame:
            return {"cue_plan":script,"clean_instruction":label(raw.get("clean_instruction"),"本幕清图指令")}
        return script
    return cached_ask(root,task,context,images,signature,validate,8500)


def locate_spots(root, book, scene):
    script = scene["cue_plan"]
    task = """查看最终横屏首帧，定位草稿里实际可见的可点物品，坐标相对于整张首帧。
草稿物品未出现时，选择实际画出的、有教学意义的物品，改写名称和中英讲解。至少一个。
返回 {"spots":[{"box":[左,上,右,下],"label":{"zh":"","en":""},"explain":{"zh":"","en":""}}]}。"""
    context = {"本幕":scene["summary"],"教学目标":book["profile"].get("education",""),"候选物品":script["spots"]}
    with Image.open(root/"assets"/scene["frame"]) as im:
        return cached_ask(root,instructions.apply(task,book,"interaction"),context,[("最终首帧",im.convert("RGB"))],
                          scene["frame"],lambda raw:validate_script({**script,"spots":raw["spots"]},scene),3000)


def keywords(value):
    return {lang:[str(x) for x in (value or {}).get(lang,[]) if str(x).strip()] for lang in ("zh","en")}


def activities(raw, scenes):
    by_id = {s["id"]:s for s in scenes}
    def evidence(item):
        ids = list(dict.fromkeys(item["evidence"]))
        if not ids or set(ids)-by_id.keys():
            raise ValueError("互动题引用了无效的场景。")
        return ids
    faq = [{"id":f"q{i}","question":bilingual(f["question"]),"reply":bilingual(f["reply"]),
            "keywords":keywords(f.get("keywords")),"evidence":evidence(f)} for i,f in enumerate(raw["faq"],1)]
    if not faq:
        raise ValueError("缺少故事问答。")
    ob = raw["observation"]
    target = by_id.get(ob["scene"])
    if not target or ob["spot"] not in {p["id"] for p in target["script"]["spots"]}:
        raise ValueError("观察题没有指向实际可点物品。")
    result = {"faq":faq,"observation":{"scene":ob["scene"],"spot":ob["spot"],"question":bilingual(ob["question"]),
              "hint":bilingual(ob["hint"])},"quiz":[]}
    modes = ["choice","order","choice","choice","choice","open"]
    if [q.get("mode") for q in raw["quiz"]] != modes:
        raise ValueError("六道小测的题型或数量不完整。")
    unique_pages = {p["page"] for s in scenes for p in s["parts"]}
    for i,(q,mode) in enumerate(zip(raw["quiz"],modes)):
        options = [bilingual(o) for o in q.get("options",[])]
        answer = q.get("answer")
        if mode != "open" and (len(options)!=3 or any(len({o[l] for o in options})!=3 for l in ("zh","en"))):
            raise ValueError("客观题需要三个内容不同的选项。")
        if mode=="choice" and (type(answer) is not int or answer not in range(3)):
            raise ValueError("选择题需有唯一的正确选项。")
        if mode=="order" and (not isinstance(answer,list) or sorted(answer)!=[0,1,2]):
            raise ValueError("排序题的答案不完整。")
        ids = evidence(q)
        if i in (2,3) and len(unique_pages)>1 and len({p["page"] for sid in ids for p in by_id[sid]["parts"]})<2:
            raise ValueError("跨页题缺少两处不同原页的证据。")
        result["quiz"].append({"type":str(q.get("type","")), "mode":mode, "options":options, "answer":answer,
                   "evidence":ids,"question":bilingual(q["question"]),"hint":bilingual(q["hint"]),
                   "explain":bilingual(q["explain"]),"keywords":keywords(q.get("keywords"))})
    return result


def make_activities(root, book):
    summaries = []
    for s in book["scenes"]:
        sc = s["script"]
        summaries.append({"id":s["id"],"pages":[p["page"] for p in s["parts"]],"title":sc["title"],
                          "summary":s["summary"], "lines":[{l:t[l] for l in ("id","role","sources","zh","en")} for t in sc["lines"]],
                          "plot_ids":s.get("plot_ids",[]),
                          "spots":[{key:h[key] for key in ("id","label","explain") if key in h} for h in sc["spots"]]})
    task = ACTIVITY_TASK
    if book.get("creation"):
        task += "\n将学习目标与故事动作关联。FAQ 同时覆盖预设知识点和孩子可能提出的开放好奇问题，每条都给出可回看的情节线索。"
    context = {"资料卡":book["profile"],"全书":summaries}
    if book.get("learning_goals"):
        from learning import activity_context
        context["学习目标"] = activity_context(book)
        task += "\n为每个学习目标安排一道能具体检验目标的小测，以该目标对应的原稿情节为证。保留六道题和原有题型，同一道题可据实覆盖多个相关目标。"
    if book.get("education_feedback"):
        context["上一版覆盖问题"] = book["education_feedback"]
        task += "\n按原稿和可点击道具修正这些问题。"
    return cached_ask(root,instructions.apply(task,book,"interaction"),context,[],0,
                      lambda x:activities(x,book["scenes"]),11000)
