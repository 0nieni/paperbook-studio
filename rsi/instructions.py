"""The studio's versioned supplements, separate from provider contracts and book content."""

LABELS = {"story":"故事与分镜", "text2image":"插画与清图", "tts":"双语朗读",
          "image2video":"动画导演稿", "interaction":"热点与小测"}
SEEDS = {
    "story":"让相邻分镜的环境、可见动作与教学线索明确，按原书或原创设定保持因果顺序。",
    "text2image":"保留本幕角色的固定外观、空间关系和关键道具，清晰呈现画面焦点。",
    "tts":"根据说话人、语种和句意安排重音、短句及停顿，保留原文含义。",
    "image2video":"使可见动作与本幕朗读一致，交代起点、运动方向和结束姿势。",
    "interaction":"把问题和讲解落到孩子在画面上能指出的物品或故事中能回看的证据。",
}

def apply(task, book, *roles):
    selected = book.get("rsi_prompts", {})
    notes = [LABELS[r]+" · "+selected[r]["text"] for r in roles if selected.get(r, {}).get("text")]
    return task + ("\n本轮制作补充，沿用原有格式与约束：\n"+"\n".join(notes) if notes else "")

def stamp(item, book, *roles):
    for role in roles:
        version = book.get("rsi_prompts", {}).get(role)
        if version:
            item.setdefault("rsi_used", {})[role] = version["id"]
