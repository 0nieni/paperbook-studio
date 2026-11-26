"""Trace educational goals from their source to the actual interactive book."""
from copy import deepcopy
import hashlib
import re

import artwork as art
import editorial as ed
import providers as api


PREPARE_TASK = """确定学习目标在这本书里的依据。家长给了目标原文时，逐个保留给定 ID，不能删改或换成更好实现的目标。
没有家长输入时，只从教学资料卡提炼有原稿依据的具体学习目标，按资料次序列出，文本写入 text。
每个目标将真正体现目标的原稿情节节点列入 relations，每项填 plot_id 和可核对的 reason。
请找出与该目标有关且故事已经表现的现实科学或生活事实，给出准确、具体、在画面或朗读中可判断的约束。
物理或自然现象要讲清对象、条件、先后、方向或变化，避免抽象要求。facts 中每条填 text 和相关 plot_ids，
只可引用该目标 relations 中的节点。保留寓言、拟人、幻想和笑话的故事身份，不把它们解释成现实事实。
找不到相关情节时 relations 留空，找不到现实事实时 facts 留空。不可捏造节点或依据。
返回 {"goals":[{"id":"g01","text":"仅从资料卡推导时填写","relations":[{"plot_id":"情节ID","reason":"依据"}],
"facts":[{"text":"有具体对象和条件的事实约束","plot_ids":["情节ID"]}]}]}。"""


FILM_TASK = """核对已生成动画和实际朗读中是否出现与给定现实事实相矛盾的表达。
成片语言观察来自对实际视频和朗读的独立复述，图片是实际成片的帧。只采纳这些证据。
画面或朗读明确呈现相反动作、状态或因果时标 contradiction。画外、模糊、动作未展示、资料不足时标 unobserved。
寓言、幻想和笑话中明确属于想象的表达可以保留。学科术语只出现在核对清单中时不能算作成片证据。
每个 fact_id 返回一项。contradiction 必须给出具体 reason 和至少一个可定位的 evidence。
使用语言观察时给 observation、field（visible、spoken 或 retelling）和逐字 quote；
使用成片帧时给从 1 开始的图片序号 frame 和肉眼可见的 detail。其他状态 evidence 可留空。
返回 {"checks":[{"fact_id":"事实ID","status":"supported|unobserved|contradiction","reason":"",
"evidence":[{"observation":"观察ID","field":"visible","quote":"所见原词"}]}]}。"""


COVER_TASK = """独立复核当前互动绘本的教学覆盖。逐项核对每个学习目标与给定的情节、热点、题目内容。
只有由同一目标的已归幕原稿情节支持，且热点自身的名称和讲解确实引导孩子理解目标，才列入 hotspots。
只有考查该目标且题目的作答或讲解能从它自己引用的场景找到依据，才列入 quizzes。
小测须填写它自己引用的场景 ID、该场景内同一目标的情节 ID、该情节或该幕实际台词的逐字 quote。
无关道具、仅关键词重合、问法无关的题目、题目无法从所列场景回答，不能算覆盖。
理由说明目标、情节与热点或小测之间的具体联系。允许一道题独立支持多个目标，每个目标单独判断。
每个传入的目标 ID 返回一项，没有有效证据的数组留空，不得造出任何引用。
返回 {"matches":[{"id":"目标ID","hotspots":[{"scene":"场景ID","spot":"热点ID","plot_id":"情节ID","reason":""}],
"quizzes":[{"index":1,"scene":"场景ID","plot_id":"情节ID","quote":"逐字依据","reason":""}]}]}。
index 是小测表中从 1 开始的编号。"""

GOAL_ITEM_MARKER = re.compile(r"^\s*(?:[0-9]+\.\s+|[0-9]+[、）)]\s*|[（(][0-9]+[）)]\s*|[-*•]\s+)")
GOAL_SENTENCE_BREAK = re.compile(r"(?<=[。！？])(?![。！？])\s*|(?<=[.!?])\s+")


def _authority(book):
    creation = book.get("creation") or {}
    if creation.get("kind") == "text":
        parent = (creation.get("brief") or {}).get("education") or ""
        return "parent", parent.strip()
    parent = (creation.get("education") or "").strip()
    if parent:
        return "parent", parent
    return "pdf_profile", ((book.get("profile") or {}).get("education") or "").strip()


def _parent_goals(text):
    goals = []
    for paragraph in re.split(r"[；;\r\n]+",text):
        paragraph = GOAL_ITEM_MARKER.sub("",paragraph)
        goals.extend(goal.strip() for goal in GOAL_SENTENCE_BREAK.split(paragraph) if goal.strip())
    return {f"g{i:02d}":goal for i,goal in enumerate(goals or [text],1)}


def _objects(value, label):
    if not isinstance(value,list) or any(not isinstance(item,dict) for item in value):
        raise ValueError(f"{label}应为对象数组。")
    return value


def _by_id(rows, ids, label):
    result = {}
    for row in _objects(rows,label):
        key = row.get("id")
        if not isinstance(key,str) or key not in ids or key in result:
            raise ValueError(f"{label}包含未知或重复的目标 ID。")
        result[key] = row
    if result.keys() != set(ids):
        raise ValueError(f"{label}缺少原始教学目标。")
    return result


def _prepare_goals(raw, parent, source, order):
    if parent:
        rows = _by_id(raw["goals"],parent,"教学目标")
        entries = [(key,text,rows[key]) for key,text in parent.items()]
    else:
        rows = _objects(raw["goals"],"资料卡教学目标")
        entries = [(f"g{i:02d}",ed.label(row.get("text"),"资料卡教学目标"),row)
                   for i,row in enumerate(rows,1)]
        if len({text for _,text,_ in entries}) != len(entries):
            raise ValueError("资料卡教学目标重复。")
    goals = []
    for goal_id,text,row in entries:
        relations = {}
        for relation in _objects(row.get("relations"),"目标情节依据"):
            plot_id = relation.get("plot_id")
            if not isinstance(plot_id,str) or plot_id not in order or plot_id in relations:
                raise ValueError(f"教学目标 {goal_id} 的原稿情节引用无效或重复。")
            relations[plot_id] = ed.label(relation.get("reason"),"目标与情节的关系")
        plot_ids = [plot_id for plot_id in order if plot_id in relations]
        facts = []
        for i,fact in enumerate(_objects(row.get("facts"),"教学事实"),1):
            refs = fact.get("plot_ids")
            if (not isinstance(refs,list) or not refs or
                    any(not isinstance(ref,str) or ref not in relations for ref in refs)):
                raise ValueError(f"教学目标 {goal_id} 的事实引用了与本目标无关的情节。")
            facts.append({"id":f"{goal_id}-f{i:02d}","text":ed.label(fact.get("text"),"教学事实"),
                          "plot_ids":[plot_id for plot_id in plot_ids if plot_id in refs]})
        goals.append({"id":goal_id,"text":text,"source":source,"plot_ids":plot_ids,
                      "relations":[{"plot_id":ref,"reason":relations[ref]} for ref in plot_ids],"facts":facts})
    return goals


def prepare(root, book, notice):
    """Resolve authority and source events before scene prompts or publication."""
    source, text = _authority(book)
    nodes = book.get("plot_nodes",[])
    parent = _parent_goals(text) if source == "parent" and text else {}
    key = api.digest(PREPARE_TASK,source,text,parent,nodes)
    if book.get("learning_goal_key") == key and "learning_goals" in book:
        return
    if not text:
        book.update(learning_goals=[],learning_goal_key=key)
        return
    if "plot_nodes" not in book:
        raise ValueError("请先整理原稿情节，再确定教学目标。")
    context = {"原稿情节节点":nodes}
    if parent:
        context["教学目标原文"] = [{"id":goal_id,"text":goal} for goal_id,goal in parent.items()]
    else:
        context["教学资料卡"] = text
    notice("整理教学目标与原稿情节、现实事实的对应关系")
    order = dict.fromkeys(node["id"] for node in nodes)
    goals = ed.cached_ask(root,PREPARE_TASK,context,[],0,
                          lambda raw:_prepare_goals(raw,parent,source,order),6500)
    book.update(learning_goals=goals,learning_goal_key=key)


def activity_context(book):
    return deepcopy(book.get("learning_goals",[]))


def direction(book, scene):
    """Return only the teaching guidance for events assigned to this scene."""
    ids = set(scene.get("plot_ids",[]))
    guidance = []
    for goal in activity_context(book):
        goal["plot_ids"] = [ref for ref in goal["plot_ids"] if ref in ids]
        if goal["plot_ids"]:
            goal["relations"] = [relation for relation in goal["relations"] if relation["plot_id"] in ids]
            goal["facts"] = [fact for fact in goal["facts"] if ids.intersection(fact["plot_ids"])]
            guidance.append(goal)
    return guidance


def _film_evidence(observations):
    evidence = {}
    for lang,observation in observations.items():
        for index,event in enumerate(observation.get("events",[]),1):
            evidence[f"{lang}-{index}"] = {"time":event.get("time"),"visible":event.get("visible",""),
                                           "spoken":event.get("spoken","")}
        if observation.get("retelling"):
            evidence[f"{lang}-retelling"] = {"retelling":observation["retelling"]}
    return evidence


def check_film(root, book, scene, observations, *, frames=()):
    """Report factual contradictions grounded in the actual film or its retelling."""
    facts = {fact["id"]:fact for goal in direction(book,scene) for fact in goal["facts"]}
    if not facts:
        return []
    evidence = _film_evidence(observations)
    images = [(f"{i} · {caption}",picture) for i,(caption,picture) in enumerate(frames,1)]
    context = {"成片教学事实":list(facts.values()),"成片语言观察":evidence}
    signature = [(caption,hashlib.sha256(art.image_bytes(picture)).hexdigest()) for caption,picture in images]

    def validate(raw):
        checks = _by_id([{**check,"id":check.get("fact_id")} for check in _objects(raw["checks"],"成片事实核对")],
                        facts,"成片事实核对")
        issues = []
        for fact_id,check in checks.items():
            status = check.get("status")
            if status not in ("supported","unobserved","contradiction"):
                raise ValueError("成片事实核对状态无效。")
            if status != "contradiction":
                continue
            reason = ed.label(check.get("reason"),"事实矛盾的说明")
            references = []
            for ref in _objects(check.get("evidence"),"实际成片证据"):
                if "frame" in ref:
                    index = ref["frame"]
                    if type(index) is not int or not 1 <= index <= len(images):
                        raise ValueError("事实矛盾引用了无效的实际成片帧。")
                    detail = ed.label(ref.get("detail"),"实际成片帧所见")
                    references.append(f"{images[index-1][0]}「{detail}」")
                    continue
                key,field = ref.get("observation"),ref.get("field")
                quote = ed.label(ref.get("quote"),"实际成片复述引文")
                event = evidence.get(key,{}) if isinstance(key,str) else {}
                observed = event.get(field) if isinstance(field,str) else None
                if (field not in ("visible","spoken","retelling") or
                        not isinstance(observed,str) or quote not in observed):
                    raise ValueError("事实矛盾引用的内容未出现在实际成片观察中。")
                second = f" {event['time']} 秒" if event.get("time") is not None else ""
                references.append(f"{key}{second}「{quote}」")
            if not references:
                raise ValueError("事实矛盾缺少实际成片证据。")
            issues.append(f"成片教学事实有误 {fact_id}「{facts[fact_id]['text']}」"
                          f" · {reason} · {' / '.join(references)}")
        return issues

    return ed.cached_ask(root,FILM_TASK,context,images,signature,validate,4500)


def _scene_evidence(scenes, nodes):
    evidence = {}
    for scene in scenes:
        script = scene.get("script") or {}
        evidence[scene["id"]] = {
            "id":scene["id"],"plot_ids":[ref for ref in scene.get("plot_ids",[]) if ref in nodes],
            "lines":[{lang:line[lang] for lang in ("zh","en") if lang in line}
                     for line in script.get("lines",[])],
            "spots":[{"id":spot["id"],"label":spot.get("label"),"explain":spot.get("explain")}
                     for spot in script.get("spots",[])],
        }
    return evidence


def _quiz_quote(quote, scene, node):
    texts = [node.get(key) for key in ("action","cause","result")]
    texts.extend(q.get("text") for q in node.get("quotes",[]))
    texts.extend(text for line in scene["lines"] for text in line.values())
    return any(isinstance(text,str) and quote in text for text in texts)


def cover(root, book, scenes, activities):
    """Check coverage using only the caller-approved hotspots for this edition."""
    goals = activity_context(book)
    authority, source_text = _authority(book)
    if not goals and source_text and (authority == "parent" or "learning_goals" not in book):
        return {"state":"needs_review","goals":[],"issues":["教学目标尚未与原稿情节建立对应。"]}
    nodes = {node["id"]:node for node in book.get("plot_nodes",[])}
    scene_evidence = _scene_evidence(scenes,nodes)
    attached = {ref for scene in scene_evidence.values() for ref in scene["plot_ids"]}
    quizzes = (activities or {}).get("quiz",[])
    report = [{"id":goal["id"],"text":goal["text"],"source":goal["source"],
               "plot_ids":[ref for ref in goal["plot_ids"] if ref in attached],
               "hotspots":[],"quiz_indices":[],"hotspot_evidence":[],"quiz_evidence":[],"issues":[]}
              for goal in goals]
    ready = [entry for entry in report if entry["plot_ids"]]

    def validate(raw):
        matches = _by_id(raw["matches"],{entry["id"] for entry in ready},"教学覆盖核对")
        verified = {}
        for entry in ready:
            goal_id = entry["id"]
            row = matches[goal_id]
            hotspots, tested = {}, {}
            for ref in _objects(row.get("hotspots"),"教学热点"):
                scene = scene_evidence.get(ref["scene"],{})
                spot = ref["spot"]
                if (ref["plot_id"] not in entry["plot_ids"] or ref["plot_id"] not in scene.get("plot_ids",[]) or
                        spot not in {h["id"] for h in scene.get("spots",[])}):
                    raise ValueError(f"教学目标 {goal_id} 引用了无效的批准热点或无关情节。")
                hotspots[(ref["scene"],spot)] = {"scene":ref["scene"],"spot":spot,"plot_id":ref["plot_id"],
                                                "reason":ed.label(ref.get("reason"),"热点教学依据")}
            for ref in _objects(row.get("quizzes"),"教学小测"):
                index,scene_id,plot_id = ref["index"],ref["scene"],ref["plot_id"]
                scene = scene_evidence.get(scene_id,{})
                if type(index) is not int or not 1 <= index <= len(quizzes):
                    raise ValueError(f"教学目标 {goal_id} 引用了无效的小测编号。")
                evidence = quizzes[index-1].get("evidence",[])
                if (not isinstance(evidence,list) or not evidence or
                        any(not isinstance(key,str) or key not in scene_evidence for key in evidence) or
                        scene_id not in evidence or plot_id not in scene.get("plot_ids",[]) or
                        plot_id not in entry["plot_ids"]):
                    raise ValueError(f"教学目标 {goal_id} 的小测缺少该题引用的有效目标场景。")
                quote = ed.label(ref.get("quote"),"小测证据引文")
                if not _quiz_quote(quote,scene,nodes[plot_id]):
                    raise ValueError(f"教学目标 {goal_id} 的小测证据不在选定原稿情节或场景中。")
                tested[index] = {"index":index,"scene":scene_id,"plot_id":plot_id,"quote":quote,
                                 "reason":ed.label(ref.get("reason"),"小测教学依据")}
            verified[goal_id] = list(hotspots.values()),[tested[index] for index in sorted(tested)]
        return verified

    matches = {}
    if ready:
        context = {"教学目标覆盖核对":[goal for goal in goals if goal["id"] in {entry["id"] for entry in ready}],
                   "场景证据":list(scene_evidence.values()),"原稿情节节点":list(nodes.values()),
                   "小测":[{"index":i,**quiz} for i,quiz in enumerate(quizzes,1)]}
        matches = ed.cached_ask(root,COVER_TASK,context,[],0,validate,6500)
    for entry in report:
        hotspots, tested = matches.get(entry["id"],([],[]))
        entry["hotspot_evidence"] = hotspots
        entry["quiz_evidence"] = tested
        entry["hotspots"] = [{"scene":ref["scene"],"spot":ref["spot"]} for ref in hotspots]
        entry["quiz_indices"] = [ref["index"] for ref in tested]
        for field,message in (("plot_ids","缺少已归入分镜的原稿情节"),
                              ("hotspots","缺少真实且讲解本目标的已批准热点"),
                              ("quiz_indices","缺少引用本目标情节证据的小测")):
            if not entry[field]:
                entry["issues"].append(f"教学目标 {entry['id']}「{entry['text']}」{message}。")
    issues = [issue for entry in report for issue in entry["issues"]]
    return {"state":"needs_review" if issues else "passed","goals":report,"issues":issues}
