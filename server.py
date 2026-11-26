"""A loopback-only picture-book workbench. Start with python server.py --open."""
import argparse
from datetime import datetime, timezone
import io
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import shutil
import socket
import tempfile
from threading import Lock, Thread
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import build_opener, ProxyHandler
import uuid
import webbrowser
import zipfile

import artwork
import editing
import production
import motion
import providers
import quality
import reader_questions
from rsi import evidence
from rsi.service import StudioRSI

HOME = Path(__file__).resolve().parent
ACTIONS = {"import","analyze","clean","reclean","script","activities","voice","video","sample","all","export","proof",
           "reel","build","redraw","redraw_scene","refresh_scene","review_film","approve_film"}


class Studio:
    def __init__(self,data):
        self.data=data.resolve()
        data.mkdir(exist_ok=True,parents=True)
        self.guard=Lock()
        self.job_files=Lock()
        self.working=set()
        self.rsi=StudioRSI(self.data)
        for project in data.iterdir():
            if project.is_dir() and (project/"project.json").is_file() and self.job(project)["state"]=="running":
                self.status(project,"interrupted",self.job(project).get("stage",""),"服务已重启。重新执行该步骤可沿用缓存和已有视频任务。")

    def project(self,pid):
        if not re.fullmatch(r"[0-9a-f]{12}",pid):
            raise FileNotFoundError
        root=self.data/pid
        if not (root/"project.json").is_file():
            raise FileNotFoundError
        return root

    def job(self,root):
        path=root/"job.json"
        with self.job_files:
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"state":"idle","stage":"","message":"就绪"}

    def status(self,root,state,stage,message):
        with self.job_files:
            providers.save_json(root/"job.json",{"state":state,"stage":stage,"message":message})

    def start(self,root,body):
        action=body.get("action")
        if action not in ACTIONS:
            raise ValueError("未知的制作步骤。")
        with self.guard:
            if root in self.working:
                raise ValueError("这本书正在制作，可在完成后执行下一步。")
            book=production.read(root)
            if "revision" in body and book["revision"]!=body["revision"]:
                raise ValueError("项目进度已变化，请刷新后再试。")
            sid=body.get("scene","")
            if sid and sid not in {s["id"] for s in book["scenes"]}:
                raise ValueError("场景不存在。")
            if action in {"sample","refresh_scene","redraw_scene","review_film","approve_film"} and not sid:
                raise ValueError("请先选择要制作的分镜。")
            options = {}
            if action=="redraw_scene":
                note=body.get("redraw_note","")
                if not isinstance(note,str) or len(note)>1000:
                    raise ValueError("重画要求请控制在 1000 字内。")
                options["redraw_note"]=note.strip()
                prompt=body.get("redraw_prompt")
                if prompt is not None and (not isinstance(prompt,str) or not prompt.strip() or len(prompt)>20000):
                    raise ValueError("请填写本幕插画提示词，最多 20000 字。")
                options["redraw_prompt"]=prompt.strip() if prompt is not None else None
            if sid and action in {"all","build","redraw"}:
                raise ValueError("整书生成请使用全书范围。")
            if action=="refresh_scene" and body.get("use_minimax") is True:
                book["video_provider"] = "minimax"
                production.write(root,book)
            if action not in {"import","export","proof","reel","review_film","approve_film"} and not book.get("rsi_prompts"):
                self.rsi.select(book)
                production.write(root,book)
            edition_fields = ("edition","play_edition","proof_edition","sample_edition")
            previous = {(book.get(k) or {}).get("id") for k in edition_fields}
            self.working.add(root)
            self.status(root,"running",action,"正在开始")
        def worker():
            try:
                production.make(root,book,action,sid,body.get("language",""),
                                lambda text:self.status(root,"running",action,text),**options)
                if action=="redraw_scene":
                    self.rsi.enqueue_redraw(root,book,next(s for s in book["scenes"] if s["id"]==sid))
                self.status(root,"done",action,"本步骤已完成")
            except Exception as e:
                self.status(root,"error",action,str(e) or type(e).__name__)
            finally:
                try:
                    editions = {book[k]["id"] for k in edition_fields if book.get(k)}
                    for edition in editions-previous:
                        self.rsi.enqueue(root,edition,self.reviews(root))
                    completed = self.job(root)["state"]=="done"
                    if book["scenes"] and not editions-previous and (
                            completed and action in {"analyze","redraw","clean","reclean","script","activities","voice","video"}
                            or action in {"build","refresh_scene","all","sample"} and not completed):
                        self.rsi.enqueue(root,draft=evidence.snapshot(root,book))
                finally:
                    with self.guard:
                        self.working.discard(root)
        Thread(target=worker,daemon=True).start()

    def view(self,root):
        book=production.read(root)
        for scene in book["scenes"]:
            scene["film_review_current"] = bool(quality.current_review(root,book,scene))
        if book.get("education_review"):
            book["education_review_current"] = book["education_review"].get("key") == quality.education_key(book)
        book["coverage"]=production.coverage(book)
        book["pages"]=[{k:v for k,v in p.items() if k!="objects"} for p in book["pages"]]
        book["layouts"]=[{k:v for k,v in l.items() if k!="regions"} for l in book["layouts"]]
        book["job"]=self.job(root)
        book["reviews"]=self.reviews(root)
        book["published"]=self.playable(root,book)
        return book

    def reviews(self,root):
        path = root/"reviews.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []

    def add_review(self,root,body):
        rating, notes, version = body.get("rating"),body.get("notes",""),body.get("edition")
        if type(rating) is not int or not 1 <= rating <= 5:
            raise ValueError("请选择 1 至 5 分。")
        if not isinstance(notes,str) or len(notes) > 2000:
            raise ValueError("详细不足请控制在 2000 字内。")
        if not isinstance(version,str) or not re.fullmatch(r"[0-9a-f]{24}",version) or not (root/"editions"/version/"index.html").is_file():
            raise ValueError("这份绘本版本暂时无法评价，请刷新首页。")
        entry = {"rating":rating,"notes":notes.strip(),"edition":version,"created":datetime.now(timezone.utc).isoformat()}
        with self.guard:
            reviews = [entry,*self.reviews(root)]
            providers.save_json(root/"reviews.json", reviews)
            self.rsi.enqueue(root,version,reviews)
        return entry

    def playable(self,root,book):
        for key in ("play_edition","edition","proof_edition"):
            edition = book.get(key)
            if edition and not edition.get("partial") and (root/"editions"/edition["id"]/"index.html").is_file():
                return edition
        # Early workbench versions cleared the pointer after an edit but kept every exported edition.
        archives = sorted((root/"editions").glob("*/book.js"),key=lambda file:file.stat().st_mtime,reverse=True)
        for file in archives:
            version = file.parent.name
            if not re.fullmatch(r"[0-9a-f]{24}",version) or not (file.parent/"index.html").is_file():
                continue
            try:
                model = json.loads(file.read_text(encoding="utf-8").partition("=")[2].strip().removesuffix(";"))
                if model["partial"] or not model["scenes"]:
                    continue
                return {"id":version,"partial":False,"proof":bool(model.get("proof")),
                        "scenes":[scene["id"] for scene in model["scenes"]],
                        "animated":sum(bool(scene.get("video")) for scene in model["scenes"])}
            except (ValueError, KeyError, OSError):
                continue
        for key in ("sample_edition","edition"):
            edition = book.get(key)
            if edition and (root/"editions"/edition["id"]/"index.html").is_file():
                return edition
        return None


class LocalServer(ThreadingHTTPServer):
    def server_bind(self):
        if os.name=="nt":
            self.allow_reuse_address=False
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    server_version="PictureBook/1"
    def log_message(self,*args):
        pass

    def data(self,content,ctype="application/json; charset=utf-8",status=200,download="",head=False):
        if not isinstance(content,bytes):
            content=json.dumps(content,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type",ctype)
        self.send_header("Content-Length",str(len(content)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        if download:
            self.send_header("Content-Disposition","attachment; filename*=UTF-8''"+quote(download))
        self.end_headers()
        if not head:
            self.wfile.write(content)

    def file(self,path,download="",head=False):
        if not path.is_file():
            raise FileNotFoundError
        size=path.stat().st_size
        start,end=0,size-1
        byte_range=self.headers.get("Range")
        if byte_range:
            match=re.fullmatch(r"bytes=(\d*)-(\d*)",byte_range)
            if not match or not any(match.groups()):
                return self.data({"error":"无效的文件区间"} ,status=416)
            a,b=match.groups()
            start=int(a) if a else max(0,size-int(b))
            end=min(int(b),size-1) if a and b else size-1
            if not 0<=start<=end<size:
                return self.data({"error":"文件区间超出范围"},status=416)
        self.send_response(206 if byte_range else 200)
        mime=mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_header("Content-Type",{"text/html":"text/html; charset=utf-8",
                         "text/vtt":"text/vtt; charset=utf-8"}.get(mime,mime))
        self.send_header("Content-Length",str(end-start+1))
        self.send_header("Accept-Ranges","bytes")
        self.send_header("X-Content-Type-Options","nosniff")
        if byte_range:
            self.send_header("Content-Range",f"bytes {start}-{end}/{size}")
        if download:
            self.send_header("Content-Disposition","attachment; filename*=UTF-8''"+quote(download))
        self.end_headers()
        if not head:
            with path.open("rb") as f:
                f.seek(start)
                remain=end-start+1
                while remain:
                    block=f.read(min(256*1024,remain))
                    if not block:
                        break
                    self.wfile.write(block)
                    remain-=len(block)

    def upload(self,app,query):
        if self.headers.get_content_type()!="application/pdf":
            raise ValueError("请选择一个 PDF 绘本。")
        size=int(self.headers.get("Content-Length",0))
        if not 5<=size<=512*1024*1024:
            raise ValueError("PDF 文件应在 512 MB 以内。")
        first=self.rfile.read(min(1024,size))
        if b"%PDF-" not in first:
            raise ValueError("文件内容不是 PDF。")
        creation = None
        if query.get("build")==["1"]:
            tone = {lang:query.get(lang,[providers.VOLUME])[0] for lang in production.LANGS}
            creation = production.creation_settings("pdf",query.get("language",["both"])[0],tone,
                                                     query.get("education",[""])[0])
        root=app.data/uuid.uuid4().hex[:12]
        root.mkdir()
        try:
            with (root/"source.pdf").open("wb") as stream:
                stream.write(first)
                remain=size-len(first)
                while remain:
                    block=self.rfile.read(min(1024*1024,remain))
                    if not block:
                        raise ValueError("上传中断，请重新选择 PDF。")
                    stream.write(block)
                    remain-=len(block)
            name=Path(query.get("name",["绘本.pdf"])[0]).name
            production.initialize(root,name,creation)
            app.start(root,{"action":"build" if creation else "import"})
        except Exception:
            # Only this request's freshly generated directory can be removed.
            if root.resolve().parent==app.data and re.fullmatch("[0-9a-f]{12}",root.name):
                shutil.rmtree(root,ignore_errors=True)
            raise
        self.data({"id":root.name},status=201)

    def create_story(self,app,body):
        settings = production.creation_settings("text",body.get("language"),body.get("voices",{}),body.get("brief"))
        root = app.data/uuid.uuid4().hex[:12]
        root.mkdir()
        production.initialize(root,settings["brief"]["theme"],settings)
        app.start(root,{"action":"build"})
        return self.data({"id":root.name},status=201)

    def attach_clip(self, app, root, scene_id, query):
        size = int(self.headers.get("Content-Length", 0))
        if self.headers.get_content_type() != "video/mp4" or not 12 <= size <= 150*1024*1024:
            raise ValueError("请选择 150 MB 以内的 HeyGen MP4。")
        revision = int(query.get("revision", ["-1"])[0])
        remote_id = query.get("task", [""])[0]
        with tempfile.NamedTemporaryFile(dir=root/"cache", suffix=".mp4", delete=False) as out:
            path = Path(out.name)
        try:
            with path.open("wb") as dest:
                remaining = size
                while remaining:
                    block = self.rfile.read(min(remaining, 1024*1024))
                    if not block:
                        raise ValueError("视频上传中断，可以重新选择原 MP4。")
                    dest.write(block)
                    remaining -= len(block)
            with app.guard:
                if root in app.working:
                    raise ValueError("本书正在生成，请完成后再导入片段。")
                book = production.read(root)
                if book["revision"] != revision:
                    raise ValueError("项目进度已变化，请刷新后再选对应场景。")
                scene = next((s for s in book["scenes"] if s["id"] == scene_id), None)
                if not scene:
                    raise ValueError("场景不存在。")
                motion.attach_heygen(root, scene, path, remote_id)
                production.write(root, book)
            if all(quality.inputs(book,scene)):
                try:
                    app.start(root,{"action":"review_film","scene":scene_id,"revision":book["revision"]})
                except ValueError:
                    # A concurrent edit/job can still pick up the saved clip at export.
                    pass
            self.data(app.view(root))
        finally:
            path.unlink(missing_ok=True)

    def dispatch(self,method):
        app=self.server.studio
        host=self.headers.get("Host","").lower()
        allowed={f"127.0.0.1:{self.server.server_port}",f"localhost:{self.server.server_port}"}
        if host not in allowed:
            return self.data({"error":"仅限本机访问"},status=403)
        if method in {"POST","PUT"}:
            origin=self.headers.get("Origin")
            if (origin and origin!="http://"+host) or self.headers.get("Sec-Fetch-Site")=="cross-site":
                return self.data({"error":"请从本机制作台发起操作"},status=403)
        uri=urlsplit(self.path)
        parts=[unquote(p) for p in uri.path.split("/") if p]
        if any(p in {".",".."} or any(c in p for c in "/\\:\0") for p in parts):
            raise FileNotFoundError
        head=method=="HEAD"
        if method in {"GET","HEAD"}:
            if not parts:
                return self.file(HOME/"web"/"index.html",head=head)
            if len(parts)==1 and parts[0] in {"studio.js","studio.css","refine.js","library.js","library.css"}:
                return self.file(HOME/"web"/parts[0],head=head)
            if parts==["skills.md"]:
                return self.file(providers.SKILL,"skills.md",head)
            if parts==["skill.zip"]:
                mem=io.BytesIO()
                with zipfile.ZipFile(mem,"w",zipfile.ZIP_DEFLATED) as z:
                    z.write(providers.SKILL,"picturebook-studio/SKILL.md")
                return self.data(mem.getvalue(),"application/zip",download="picturebook-studio.zip",head=head)
            if parts==["api","health"]:
                return self.data({"app":"picturebook-studio","models":{brand:bool(providers.setting(*names))
                    for brand,names in providers.NAMES.items()},"ffmpeg":bool(shutil.which("ffmpeg")),
                    "voices":[{"id":tone,"model":info[0],"name":info[1],"style":info[2]} for tone,info in providers.VOICES.items()]},head=head)
            if parts==["api","projects"]:
                items=[]
                for path in sorted(app.data.glob("*/project.json"),key=lambda p:p.stat().st_mtime,reverse=True):
                    book=production.read(path.parent)
                    root = path.parent
                    cover = next((s["frame"] for s in book["scenes"] if s.get("frame")), "")
                    if book["pages"] and not cover:
                        cover = "p0001.jpg"
                    items.append({"id":book["id"],"title":book["profile"]["title"],"name":book["name"],
                                  "edition":app.playable(root,book), "cover":cover, "languages":production.languages(book),
                                  "summary":book["profile"].get("summary",""), "job":app.job(root),
                                  "kind":book.get("creation",{}).get("kind","pdf")})
                return self.data(items,head=head)
            if len(parts)>=3 and parts[:2]==["api","projects"]:
                root=app.project(parts[2])
                if len(parts)==3:
                    return self.data(app.view(root),head=head)
                if parts[3:]==["job"]:
                    return self.data(app.job(root),head=head)
                if parts[3:]==["rsi"]:
                    return self.data(app.rsi.view(root),head=head)
                if len(parts)==5 and parts[3]=="original":
                    book=production.read(root)
                    s=next((s for s in book["scenes"] if s["id"]==parts[4]),None)
                    if not s:
                        raise FileNotFoundError
                    pic,_,_=artwork.compose(root,s,[])
                    return self.data(artwork.image_bytes(pic,"JPEG"),"image/jpeg",head=head)
            if len(parts)>=4 and parts[0]=="media":
                root=app.project(parts[1])
                if len(parts)==4 and parts[2] in {"pages","assets"} and re.fullmatch(r"[\w-]+\.(?:jpg|png|wav|mp4)",parts[3]):
                    return self.file(root/parts[2]/parts[3],head=head)
                if parts[2]=="editions":
                    if len(parts)==4 and re.fullmatch(r"[0-9a-f]{24}\.zip",parts[3]):
                        book=production.read(root)
                        edition=app.playable(root,book) or {}
                        preview=(edition.get("proof") and edition.get("id")==parts[3][:-4]) or book.get("proof_edition",{}).get("id")==parts[3][:-4]
                        suffix="-双语校对版.zip" if preview else "-双语交互绘本.zip"
                        return self.file(root/"editions"/parts[3],book["profile"]["title"]+suffix,head)
                    if len(parts) in {5,6} and re.fullmatch(r"[0-9a-f]{24}",parts[3]):
                        sub=parts[4:]
                        # Hosted editions share the current player; downloaded editions keep their bundled one.
                        if sub == ["player.js"] and (root/"editions"/parts[3]/"book.js").is_file():
                            return self.file(HOME/"reader"/"player.js",head=head)
                        if (len(sub)==1 and sub[0] in {"index.html","book.js","player.js","player.css","zh.srt","en.srt","zh.vtt","en.vtt","制作稿.json"}) or (len(sub)==2 and sub[0]=="assets" and re.fullmatch(r"[\w-]+\.(?:png|wav|mp4)",sub[1])):
                            return self.file(root/"editions"/parts[3]/Path(*sub),head=head)
            raise FileNotFoundError
        if method=="POST" and parts==["api","projects"]:
            return self.upload(app,parse_qs(uri.query))
        if method=="POST" and parts==["api","stories"]:
            return self.create_story(app,self.json_body())
        if method == "POST" and len(parts)==5 and parts[:2]==["api","projects"] and parts[3]=="clips":
            return self.attach_clip(app, app.project(parts[2]), parts[4], parse_qs(uri.query))
        if len(parts) not in {3,4} or parts[:2]!=["api","projects"]:
            raise FileNotFoundError
        root=app.project(parts[2])
        body=self.json_body()
        if method=="POST" and parts[3:]==["ask"]:
            try:
                return self.data(reader_questions.answer(root,body))
            except RuntimeError as exc:
                return self.data({"error":str(exc)},status=502)
        if method=="POST" and parts[3:]==["reviews"]:
            return self.data(app.add_review(root,body),status=201)
        if method=="POST" and parts[3:]==["rsi"]:
            if body.get("retry"):
                app.rsi.retry(root,body["retry"])
            elif body.get("edition"):
                version=body["edition"]
                if not isinstance(version,str) or not re.fullmatch(r"[0-9a-f]{24}",version) or not (root/"editions"/version/"book.js").is_file():
                    raise ValueError("请选择这本书的播放版本。")
                app.rsi.enqueue(root,version,app.reviews(root))
            else:
                with app.guard:
                    if root in app.working:
                        raise ValueError("本书正在制作，请等分镜保存后再评估。")
                    app.rsi.enqueue(root,draft=evidence.snapshot(root,production.read(root)))
            return self.data(app.rsi.view(root),status=202)
        if method=="POST" and parts[3:]==["jobs"]:
            app.start(root,body)
            return self.data(app.job(root),status=202)
        with app.guard:
            if root in app.working:
                raise ValueError("这本书正在生成，请稍后编辑。")
            book=production.read(root)
            if method=="PUT" and len(parts)==3:
                editing.edit(root,book,body)
            elif method=="POST" and parts[3:]==["recover"]:
                production.recover(root,book,body.get("scene"),str(body.get("task","")).strip())
            else:
                raise FileNotFoundError
        self.data(app.view(root))

    def json_body(self):
        size=int(self.headers.get("Content-Length","0"))
        if self.headers.get_content_type()!="application/json" or not 1<=size<=16*1024*1024:
            raise ValueError("请求内容应为 JSON。")
        body=json.loads(self.rfile.read(size))
        if not isinstance(body,dict):
            raise ValueError("操作参数格式无效。")
        return body

    def route(self):
        try:
            self.dispatch(self.command)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
            pass
        except FileNotFoundError:
            self.data({"error":"找不到这个项目或文件"},status=404)
        except (ValueError,TypeError,KeyError,IndexError) as e:
            self.data({"error":str(e)},status=400)
        except Exception as e:
            self.data({"error":"本机制作服务处理失败 · "+str(e)[:240]},status=500)
    do_GET=do_POST=do_PUT=do_HEAD=route


def launch():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port",type=int,default=8765)
    p.add_argument("--data",type=Path,default=HOME/"作品")
    p.add_argument("--open",action="store_true")
    a=p.parse_args()
    url=f"http://127.0.0.1:{a.port}/"
    try:
        server=LocalServer(("127.0.0.1",a.port),Handler)
    except OSError:
        try:
            with build_opener(ProxyHandler({})).open(url+"api/health",timeout=2) as r:
                ours=json.load(r).get("app")=="picturebook-studio"
        except OSError:
            ours=False
        if not ours:
            raise SystemExit("端口被其他程序占用，可用 --port 指定其他本机端口。")
        if a.open:
            webbrowser.open(url)
        return
    server.studio=Studio(a.data)
    if a.open:
        webbrowser.open(url)
    print(url,flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.studio.rsi.closed.set()
        server.studio.rsi.signal.set()
        server.server_close()

if __name__=="__main__":
    launch()
