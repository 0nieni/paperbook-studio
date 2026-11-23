"""Validate editor input and invalidate only the products derived from changed material."""
from copy import deepcopy
import math
import re

import artwork as art
import editorial as ed
import production as prod
import providers as api


def identifier(value):
    if not isinstance(value,str) or not prod.ID.fullmatch(value):
        raise ValueError("编号仅使用字母、数字、短横线或下划线。")
    return value


def page_number(value,count):
    if type(value) is not int or not 1 <= value <= count:
        raise ValueError("原页编号超出本书范围。")
    return value


def unique(items):
    ids = [row["id"] for row in items]
    if len(ids)!=len(set(ids)):
        raise ValueError("同一清单中出现重复编号。")


def ink_settings(region):
    outlines = region.get("ink_outlines", [])
    if not outlines:
        return {}
    colour = region.get("ink_color", "")
    if not isinstance(outlines,list) or len(outlines)>24 or not re.fullmatch(r"#[0-9a-fA-F]{6}",colour):
        raise ValueError("请选取印字的颜色，并在原图上描出轮廓。")
    validated = []
    for outline in outlines:
        if not isinstance(outline,list) or not 3 <= len(outline) <= 128:
            raise ValueError("一个轮廓需要 3 至 128 个顶点。")
        points = []
        for point in outline:
            if not isinstance(point,list) or len(point)!=2:
                raise ValueError("轮廓点需要包含横纵坐标。")
            coords = [round(float(n),6) for n in point]
            if any(not math.isfinite(n) or not 0<=n<=1 for n in coords):
                raise ValueError("轮廓点超出了原页范围。")
            points.append(coords)
        validated.append(points)
    return {"ink_color":colour.lower(), "ink_outlines":validated}


def edit(root,book,body):
    if body.get("revision") != book["revision"]:
        raise ValueError("项目已有新进度，请刷新页面后重新编辑。")
    count=len(book["pages"])
    if not count:
        raise ValueError("请先载入原页。")
    profile=ed.profile_data(body["profile"],book["pages"])
    model=body.get("tts_model",book["tts_model"])
    if model not in (api.TTS,"qwen3-tts-flash"):
        raise ValueError("不支持的千问语音模型。")
    video_provider = body.get("video_provider",book.get("video_provider","heygen"))
    if video_provider not in prod.motion.MODELS:
        raise ValueError("不支持的动画模型。")
    regions=[]
    for r in body["regions"]:
        if r.get("kind") not in art.KINDS:
            raise ValueError("文字种类无效。")
        regions.append({"id":identifier(r["id"]),"page":page_number(r["page"],count),"box":art.box(r["box"]),
                        "kind":r["kind"],"speaker":str(r.get("speaker","")),"text":str(r.get("text","")), **ink_settings(r)})
    unique(regions)
    lookup={r["id"]:r for r in regions}
    old_regions={r["id"]:r for r in book["regions"]}
    old={s["id"]:s for s in book["scenes"]}
    old_ids=list(old)
    input_ids=[identifier(s.get("id")) for s in body["scenes"]]
    profile_changed=profile!=book["profile"]
    activities_changed=profile_changed or input_ids!=old_ids
    scenes=[]
    for idx,inp in enumerate(body["scenes"]):
        sid=input_ids[idx]
        was=old.get(sid,{})
        sources=list(dict.fromkeys(inp.get("sources",[])))
        if any(r not in lookup or lookup[r]["kind"] not in art.REMOVE for r in sources):
            raise ValueError("场景引用了无效的旁白或对白。")
        parts=[{"page":page_number(p["page"],count),"box":art.box(p["box"])} for p in inp["parts"]]
        if not parts:
            raise ValueError("每个场景至少需要一块原画。")
        s={**deepcopy(was),"id":sid,"title":ed.label(inp.get("title"),"场景标题"),
           "summary":str(inp.get("summary","")),"sources":sources,"parts":parts}
        current_regions=prod.region_set(s,regions)
        prior_regions=prod.region_set(was,book["regions"]) if was else []
        geometry=parts!=was.get("parts") or current_regions!=prior_regions
        if geometry:
            s.get("rsi_used",{}).pop("text2image",None)
            for key in ("frame","original","clean_review","clean_approved"):
                s.pop(key,None)
        elif was.get("clean_review") and inp.get("clean_approved") is True:
            s["clean_approved"]=True
        metadata=(profile_changed or any(s.get(k)!=was.get(k) for k in ("summary","title","sources")) or
                  any(lookup.get(k)!=old_regions.get(k) for k in sources))
        # The two neighboring plot summaries form part of the production prompts.
        if was:
            oi=old_ids.index(sid)
            def neighbors(seq,i):
                return (seq[i-1] if i else "",seq[i+1] if i+1<len(seq) else "")
            before=neighbors(old_ids,oi)
            after=neighbors(input_ids,idx)
            if not was.get("video"):
                metadata |= before!=after
        if geometry or metadata:
            s.pop("clean_instruction",None)
            s.get("rsi_used",{}).pop("story",None)
            prod.clear_script(s)
            activities_changed=True
        elif was.get("script") and inp.get("script") is not None:
            script=ed.validate_script(inp["script"],s)
            if script!=was["script"]:
                for role, key in (("tts","lines"),("image2video","video_prompt"),("interaction","spots")):
                    if script[key]!=was["script"][key]:
                        s.get("rsi_used",{}).pop(role,None)
                activities_changed |= (script["spots"]!=was["script"]["spots"] or
                                       [(t["role"],t["zh"],t["en"]) for t in script["lines"]] !=
                                       [(t["role"],t["zh"],t["en"]) for t in was["script"]["lines"]])
                for l in prod.LANGS:
                    def voice_copy(d):
                        return ([(x["id"],x[l],x["direction"][l]) for x in d["lines"]],
                                [(x["id"],x["explain"][l]) for x in d["spots"]])
                    if voice_copy(script)!=voice_copy(was["script"]):
                        s.get("audio",{}).pop(l,None)
                        s.get("narration",{}).pop(l,None)
                if script["video_prompt"]!=was["script"]["video_prompt"]:
                    for key in ("video","video_seconds","video_source","task","planned_video_seconds"):
                        s.pop(key,None)
            s["script"]=script
        if model!=book["tts_model"]:
            s.pop("audio",None)
            s.pop("narration",None)
        scenes.append(s)
    unique(scenes)
    if activities_changed:
        book["activities"]=None
        book["faq_audio"]={}
    if model!=book["tts_model"]:
        book["faq_audio"]={}
    if profile_changed:
        book["reference"]=art.character_sheet(root,profile)
    if model!=book["tts_model"]:
        book.pop("voices",None)
    book.update(profile=profile,scenes=scenes,regions=regions,tts_model=model,video_provider=video_provider)
    prod.write(root,book)
