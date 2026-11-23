"""Keep PDF coordinates, paper order and the locations of repaired pixels traceable."""
import ctypes
import io
import math
import re
from pathlib import Path
from threading import RLock

import pypdfium2 as pdf
import pypdfium2.raw as raw
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat

from providers import atomic, digest

PDF_LOCK = RLock()
SIZE = (1280, 720)
REMOVE = {"narration", "dialogue", "bubble"}
KINDS = REMOVE | {"prop", "mark", "note"}
PROTECTED = ("请勿外传", "纸书智伴", "人工智能+", "仅供第")


def box(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("区域必须包含左、上、右、下四个坐标。")
    coords = [round(float(n), 5) for n in value]
    if any(not math.isfinite(n) for n in coords) or not (0 <= coords[0] < coords[2] <= 1 and 0 <= coords[1] < coords[3] <= 1):
        raise ValueError("区域坐标应在 0 与 1 之间，且右下角应在左上角之后。")
    return coords


def intersection(a, b):
    x, y, r, d = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    return (x, y, r, d) if x < r and y < d else None


def area(b):
    return (b[2] - b[0]) * (b[3] - b[1]) if b else 0


def vector_text(region, page):
    if region["kind"] not in {"narration","dialogue"}:
        return False
    def plain(s):
        return re.sub(r"[^\w\u3400-\u9fff]", "", s).casefold()
    remaining = plain(region["text"])
    if not remaining:
        return False
    fragments = [o for o in page["objects"] if area(intersection(o["box"],region["box"])) >= .72*area(o["box"])]
    for obj in fragments:
        word = plain(obj["text"])
        if word:
            remaining = remaining.replace(word,"",1)
    return not remaining


def blend_repair(base, candidate, mask):
    # A local colour correction prevents a generated fill from leaving a rectangular seam.
    outer = ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(9)),mask)
    if outer.getbbox():
        src = ImageStat.Stat(base,outer).mean
        dst = ImageStat.Stat(candidate,outer).mean
        channels = [c.point(lambda v, d=src[i]-dst[i]:min(255,max(0,round(v+d))))
                    for i,c in enumerate(candidate.split())]
        candidate = Image.merge("RGB",channels)
    feather = ImageChops.multiply(mask,mask.filter(ImageFilter.GaussianBlur(2)))
    return Image.composite(candidate,base,feather)


def repair_crop(frame, mask, text, note=""):
    """Give the edit model one visible target at a useful reading size and blend only its ink area."""
    x,y,r,b=mask.getbbox()
    margin=max(28,round(max(r-x,b-y)*.27))
    bounds=(max(0,x-margin),max(0,y-margin),min(frame.width,r+margin),min(frame.height,b+margin))
    crop=frame.crop(bounds)
    local_mask=mask.crop(bounds)
    ratio=(768**2/(crop.width*crop.height))**.5
    size=tuple(max(16,round(n*ratio/16)*16) for n in crop.size)
    source=crop.resize(size,Image.Resampling.LANCZOS)
    marked=source.copy()
    overlay=local_mask.resize(size,Image.Resampling.NEAREST)
    ImageDraw.Draw(marked).rectangle(overlay.getbbox(),outline="#ff2338",width=max(3,round(size[0]/220)))
    from providers import edit_art
    edited=edit_art(source,marked,text,note).resize(crop.size,Image.Resampling.LANCZOS)
    frame=frame.copy()
    frame.paste(blend_repair(crop,edited,local_mask),bounds[:2])
    return frame


def repair_before_crop(root, scene, region, regions, note=""):
    """Recover a whole printed line on its source page when only a fragment falls inside a panel."""
    n = region["page"]
    marks = [r for r in regions if r["page"] == n and r["kind"] in REMOVE]
    paper = clean_vector_page(root, n, marks)
    mask = Image.new("L", paper.size)
    draw = ImageDraw.Draw(mask)
    def rectangle(r, fill):
        points = [round(v * size) for v, size in zip(r["box"], (paper.width,paper.height)*2)]
        draw.rectangle(points, fill=fill)
    rectangle(region, 255)
    for r in regions:
        if r["page"] == n and r["kind"] in {"prop", "mark"}:
            rectangle(r, 0)
    if mask.getbbox():
        fixed = repair_crop(paper, mask, region["text"], note)
        if colored_ink_survives(paper, fixed, mask):
            fixed = restore_print_ink(paper, mask)
    else:
        fixed = paper
    return compose(root, scene, [], overrides={n:fixed})[0]


def colored_ink_survives(original, candidate, mask):
    """Look for the original coloured letter strokes at the same position in the edited image."""
    import cv2
    import numpy as np
    bounds = mask.getbbox()
    source = np.array(original.crop(bounds))
    result = np.array(candidate.crop(bounds))
    if min(source.shape[:2]) < 9:
        return False
    source_delta = source.astype(np.float32) - cv2.medianBlur(source, 19)
    result_delta = result.astype(np.float32) - cv2.medianBlur(result, 19)
    hsv = cv2.cvtColor(source, cv2.COLOR_RGB2HSV)
    zone = np.array(mask.crop(bounds)) > 0
    ink = zone & (np.max(np.abs(source_delta), axis=2) > 44) & (hsv[:,:,1] > 130) & (hsv[:,:,2] > 100)
    if np.count_nonzero(ink) < 25:
        return False
    hues = hsv[:,:,0].astype(np.int16)
    histogram = np.bincount(hues[ink], minlength=180)
    peak = int(np.argmax(sum(np.roll(histogram, jump) for jump in range(-6, 7))))
    distance = np.abs(hues - peak)
    core = ink & (np.minimum(distance, 180 - distance) < 12)
    if np.count_nonzero(core) < max(18, .45 * np.count_nonzero(ink)):
        return False
    direction = source_delta[core]
    same_stroke = np.sum(result_delta[core] * direction, axis=1) > .46 * np.sum(direction**2, axis=1)
    return float(same_stroke.mean()) > .67


def precise_ink(root, scene, region, regions):
    """Restore the original paper and remove only the marked ink colour within the reader's outlines."""
    import cv2
    import numpy as np
    from PIL import ImageColor
    n = region["page"]
    marks = [r for r in regions if r["page"] == n and r["kind"] in REMOVE]
    paper = clean_vector_page(root, n, marks)
    selection = Image.new("L", paper.size)
    draw = ImageDraw.Draw(selection)
    for outline in region["ink_outlines"]:
        draw.polygon([(round(x*paper.width), round(y*paper.height)) for x,y in outline], fill=255)
    protected = [r for r in regions if r["page"] == n and r["kind"] in {"prop", "mark"}]
    for mark in protected:
        draw.rectangle([round(v*size) for v,size in zip(mark["box"], paper.size*2)], fill=0)
    zone = np.array(selection) > 0
    rgb = np.array(paper)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV).astype(np.int16)
    sample = np.uint8([[ImageColor.getrgb(region["ink_color"])]])
    hue, saturation, value = cv2.cvtColor(sample, cv2.COLOR_RGB2HSV)[0,0].astype(int)
    if saturation < 60:
        same_colour = (hsv[:,:,1] < 95) & (np.abs(hsv[:,:,2]-value) < 80)
    else:
        distance = np.abs(hsv[:,:,0]-hue)
        same_colour = (np.minimum(distance,180-distance) <= 13) & (hsv[:,:,1] > 64) & (hsv[:,:,2] > 72)
    ink = (same_colour & zone).astype(np.uint8)*255
    ink = cv2.dilate(ink,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(5,5)))
    for mark in protected:
        x,y,right,bottom = [round(v*size) for v,size in zip(mark["box"], paper.size*2)]
        ink[y:bottom+1,x:right+1] = 0
    repaired = Image.fromarray(cv2.inpaint(rgb,ink,3,cv2.INPAINT_TELEA))
    return compose(root,scene,[],overrides={n:repaired})[0]


def restore_print_ink(base, mask):
    """Keep the original paper and locally inpaint high-contrast printed ink after a failed AI edit."""
    import cv2
    import numpy as np
    bounds=mask.getbbox()
    x,y,r,b=bounds
    context=(max(0,x-20),max(0,y-20),min(base.width,r+20),min(base.height,b+20))
    raw=np.array(base.crop(context).convert("RGB"))
    zone=np.array(mask.crop(context))>0
    patch=cv2.medianBlur(raw,25)
    contrast=np.max(cv2.absdiff(raw,patch),axis=2)
    edges=(contrast>22)&zone
    hsv=cv2.cvtColor(raw,cv2.COLOR_RGB2HSV)
    saturated=edges&(hsv[:,:,1]>105)&(hsv[:,:,2]>78)
    if np.count_nonzero(saturated)>max(12,np.count_nonzero(edges)*.20):
        hues=hsv[:,:,0].astype(np.int16)
        hist=np.bincount(hues[saturated],minlength=180)
        scores=sum(np.roll(hist,delta) for delta in range(-7,8))
        hue=int(np.argmax(scores))
        delta=np.abs(hues-hue)
        ink=zone&(np.minimum(delta,180-delta)<15)&(hsv[:,:,1]>82)&(hsv[:,:,2]>105)&(contrast>9)
        if np.count_nonzero(ink)<10:
            ink=edges
    else:
        ink=edges
    ink=cv2.dilate(ink.astype(np.uint8)*255,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(5,5)))
    ink[~zone]=0
    retouched=cv2.inpaint(raw,ink,4,cv2.INPAINT_TELEA)
    result=base.copy()
    result.paste(Image.fromarray(retouched),context[:2])
    return result


def image_bytes(image, fmt="PNG"):
    buf = io.BytesIO()
    image.save(buf, fmt, **({"quality": 91, "subsampling": 0} if fmt == "JPEG" else {}))
    return buf.getvalue()


def pagefile(root, n):
    return root / "pages" / f"p{n:04d}.jpg"


def textobjects(page, bitmap):
    tp = page.get_textpage()
    conv = bitmap.get_posconv(page)
    rows = []
    try:
        for obj in page.get_objects(filter=(raw.FPDF_PAGEOBJ_TEXT,), max_depth=1):
            length = raw.FPDFTextObj_GetText(obj, tp, None, 0)
            if length < 2:
                continue
            buf = (ctypes.c_ushort * (length // 2))()
            raw.FPDFTextObj_GetText(obj, tp, buf, length)
            txt = bytes(buf).decode("utf-16-le", "replace").strip("\0 \r\n")
            if not txt:
                continue
            left, bottom, right, top = obj.get_bounds()
            points = [conv.to_bitmap(x, y) for x in (left, right) for y in (top, bottom)]
            xs, ys = zip(*points)
            b = intersection([min(xs) / bitmap.width, min(ys) / bitmap.height,
                              max(xs) / bitmap.width, max(ys) / bitmap.height], [0, 0, 1, 1])
            if b:
                rows.append((obj, txt, box(b)))
        return rows, tp.get_text_bounded()
    finally:
        tp.close()


def render_pages(root, notify):
    pages = []
    with PDF_LOCK, pdf.PdfDocument(root / "source.pdf") as doc:
        if not len(doc):
            raise ValueError("这个 PDF 没有可渲染的页面。")
        for i in range(len(doc)):
            notify(f"读取原画与文字 · {i+1}/{len(doc)}")
            page = doc[i]
            bitmap = page.render(scale=1600 / max(page.get_size()))
            try:
                objects, text = textobjects(page, bitmap)
                picture = bitmap.to_pil().convert("RGB")
                atomic(pagefile(root, i+1), image_bytes(picture, "JPEG"))
                pages.append({"n": i+1, "width": picture.width, "height": picture.height,
                              "text": text, "objects": [{"text": t, "box": b} for _, t, b in objects]})
                objects.clear()
            finally:
                bitmap.close()
                page.close()
    return pages


def clean_vector_page(root, number, regions):
    path = root / "cache" / ("paper-" + digest(number, regions) + ".png")
    if path.exists():
        with Image.open(path) as image:
            return image.convert("RGB")
    with PDF_LOCK, pdf.PdfDocument(root / "source.pdf") as doc:
        page = doc[number-1]
        scale = 1600 / max(page.get_size())
        bitmap = page.render(scale=scale)
        objs, _ = textobjects(page, bitmap)
        bitmap.close()
        for obj, text, bounds in objs:
            if any(mark in text for mark in PROTECTED):
                continue
            if any(area(intersection(bounds, r["box"])) / area(bounds) > .72 and
                   (not r["text"].strip() or text.strip() in r["text"] or r["text"] in text)
                   for r in regions):
                page.remove_obj(obj)
                obj.close()
        objs.clear()
        bitmap = page.render(scale=scale)
        pic = bitmap.to_pil().convert("RGB")
        bitmap.close()
        page.close()
    atomic(path, image_bytes(pic))
    return pic


def compose(root, scene, regions, vector=False, overrides=None):
    """Multiple parts are stored in physical left-to-right order, with vertical alignment in page space."""
    pieces = []
    common_y = min(part["box"][1] for part in scene["parts"])
    for part in scene["parts"]:
        n = part["page"]
        marks = [r for r in regions if r["page"] == n and r["kind"] in REMOVE]
        if overrides and n in overrides:
            page = overrides[n]
        elif vector and marks:
            page = clean_vector_page(root, n, marks)
        else:
            with Image.open(pagefile(root, n)) as im:
                page = im.convert("RGB")
        w, h = page.size
        b = box(part["box"])
        px = [round(x * s) for x, s in zip(b, (w, h, w, h))]
        px[2], px[3] = max(px[0]+1, px[2]), max(px[1]+1, px[3])
        piece = page.crop(tuple(px))
        # Normalize distinct scan resolutions to a common page height before joining.
        k = 1200 / h
        piece = piece.resize((max(1, round(piece.width*k)), max(1, round(piece.height*k))), Image.Resampling.LANCZOS)
        y = round((b[1] - common_y) * 1200)
        pieces.append((piece, y, b, n))
    width = sum(piece.width for piece, *_ in pieces)
    height = max(y + piece.height for piece, y, *_ in pieces)
    scale = min(SIZE[0] / width, SIZE[1] / height)
    shift_x, shift_y = (SIZE[0]-width*scale)/2, (SIZE[1]-height*scale)/2
    canvas = Image.new("RGB", SIZE, "#f4f2e8")
    boxes, preserve, cursor = [], [], 0
    for piece, y, src, n in pieces:
        left, top = round(shift_x + cursor*scale), round(shift_y + y*scale)
        end_x = round(shift_x+(cursor+piece.width)*scale)
        w, h = max(1, end_x-left), max(1, round(piece.height*scale))
        canvas.paste(piece.resize((w,h), Image.Resampling.LANCZOS), (left,top))
        for r in regions:
            if r["page"] != n:
                continue
            b = intersection(src, r["box"])
            if not b:
                continue
            mapped = (round(left + (b[0]-src[0])/(src[2]-src[0])*w),
                      round(top + (b[1]-src[1])/(src[3]-src[1])*h),
                      round(left + (b[2]-src[0])/(src[2]-src[0])*w),
                      round(top + (b[3]-src[1])/(src[3]-src[1])*h))
            if r["kind"] in REMOVE:
                boxes.append(mapped)
            elif r["kind"] in {"prop", "mark"}:
                preserve.append(mapped)
        cursor += piece.width
    mask = Image.new("L", SIZE)
    pen = ImageDraw.Draw(mask)
    marked = canvas.copy()
    outlines = ImageDraw.Draw(marked)
    for b in boxes:
        pen.rectangle(b, fill=255)
        outlines.rectangle(b, outline="#ff2338", width=3)
    for b in preserve:
        pen.rectangle(b, fill=0)
    return canvas, marked, mask


def character_sheet(root, profile):
    refs = [r for person in profile.get("characters", []) for r in person.get("refs", [])
            if isinstance(r, dict) and isinstance(r.get("page"), int)]
    if not refs:
        return ""
    cells = []
    for ref in refs:
        path = pagefile(root, ref["page"])
        if not path.exists():
            continue
        with Image.open(path) as im:
            x,y,r,d = box(ref["box"])
            crop = im.crop((int(x*im.width),int(y*im.height),math.ceil(r*im.width),math.ceil(d*im.height)))
            cells.append(ImageOps.pad(crop.convert("RGB"),(180,180),color="#f4f2e8"))
    if not cells:
        return ""
    sheet = Image.new("RGB",(min(len(cells),6)*180,math.ceil(len(cells)/6)*180),"#f4f2e8")
    for i,c in enumerate(cells):
        sheet.paste(c,((i%6)*180,(i//6)*180))
    name = "reference-" + digest(refs) + ".jpg"
    atomic(root/"assets"/name,image_bytes(sheet,"JPEG"))
    return name
