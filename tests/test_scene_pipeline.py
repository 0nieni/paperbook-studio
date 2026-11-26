import io
import json
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Thread
import time
import unittest
from unittest.mock import patch
from urllib.request import build_opener, ProxyHandler, Request
import wave
import zipfile

from PIL import Image

import production
import providers
import server


def pair(s):
    return {"zh":s,"en":s}


def manuscript(i, sources=()):
    return {"title":pair(f"Scene {i}"),"video_prompt":f"Shot {i} moves","clean_warnings":[],
            "lines":[{"role":"旁白","sources":list(sources),"zh":f"看第{i}颗星。","en":f"Find star {i}.",
                      "direction":pair("gently")}],
            "spots":[{"box":[.1,.2,.3,.4],"label":pair("星星"),"explain":pair(f"Star {i}")}]}


class FilmProviderTests(unittest.TestCase):
    def test_minimax_separates_story_frame_and_cast_in_reference_mode(self):
        frame = Image.new("RGB",(1280,720),"green")
        cast = Image.new("RGB",(1280,720),"orange")
        with patch.object(providers,"request",return_value={"task_id":"clip-1"}) as request:
            self.assertEqual(providers.start_video(frame,"The bear waves.",4,reference=cast),"clip-1")
        payload = request.call_args.args[2]
        self.assertEqual([image["role"] for image in payload["content"][1:]],["reference_image","reference_image"])
        self.assertEqual(payload["ratio"],"16:9")
        self.assertNotEqual(payload["content"][1]["image_url"],payload["content"][2]["image_url"])

    def test_retelling_uses_existing_bailian_endpoint_and_embeds_video(self):
        response = {"choices":[{"message":{"content":'{"retelling":"test","events":[]}'}}]}
        with patch.object(providers,"bailian",return_value="https://dashscope.aliyuncs.com"), \
                patch.object(providers,"request",return_value=response) as request:
            result = providers.retell_video(b"test clip")
        provider, url, body = request.call_args.args
        self.assertEqual(provider,"千问")
        self.assertEqual(url,"https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
        self.assertEqual(body["messages"][0]["content"][0]["video_url"]["url"],"data:;base64,dGVzdCBjbGlw")
        self.assertEqual(result["retelling"],"test")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        self.root = Path(stack.enter_context(TemporaryDirectory()))/"abc123abc123"
        self.profile = {"title":"星星","characters":[{"name":"小熊","features":"蓝帽子"}],"style":"色块","education":"观察"}
        self.calls = Counter()
        self.started = {name:Event() for name in ("plan2","image1","voice1","video1","video2")}
        self.overlap = False
        self.enhanced = False
        self.interrupt = False
        self.fail_frame = 0
        self.writers = None
        self.submissions = []
        self.query_order = []
        self.missing_plot = False
        self.blank_hotspot = False
        self.replacement_hotspot = False
        self.retold_seconds = []
        stack.enter_context(patch.object(providers,"request",side_effect=AssertionError("网络必须由测试替身接管")))
        stack.enter_context(patch.object(providers,"ask",side_effect=self.ask))
        stack.enter_context(patch.object(providers,"retell_video",self.retell,create=True))
        stack.enter_context(patch.object(providers,"illustrate",side_effect=self.illustrate))
        stack.enter_context(patch.object(providers,"voice",side_effect=self.voice))
        stack.enter_context(patch.object(providers,"start_video",side_effect=self.start_video))
        stack.enter_context(patch.object(providers,"query_video",side_effect=self.query_video))
        self.clip = Path(self.root.parent/"fixture.mp4")
        providers.run_binary(["ffmpeg","-nostdin","-loglevel","error","-f","lavfi","-i",
                              "color=blue:s=64x36:r=12:d=0.5,drawbox=0:0:iw:ih:red:t=fill:enable=gte(t\\,0.25)",
                              "-c:v","libx264","-pix_fmt","yuv420p",str(self.clip)],check=True,timeout=30)
        stack.enter_context(patch.object(providers,"fetch_media",return_value=self.clip.read_bytes()))

    def ask(self,task,ctx,images,tokens):
        if "角色定型" in ctx:
            return {"characters":[{"name":p["name"],"palette":{"帽子":"#3355AA"},
                                    "traits":["圆耳朵","星形徽章"]} for p in ctx["角色定型"]]}
        if "角色三视图核对" in ctx:
            return {"views":{"front":"正面圆耳朵","side":"侧面蓝帽子","back":"背面星形徽章"},
                    "matches":True,"issues":[]}
        if "角色一致性检查" in ctx:
            return {"frames":[{"label":label,"characters":[{"name":p["name"],"status":"match",
                        "evidence":"蓝色帽子与圆耳朵一致"} for p in ctx["角色一致性检查"]]}
                       for label in ctx["实际帧标签"]]}
        if "教学目标原文" in ctx:
            node = ctx["原稿情节节点"][0]["id"]
            return {"goals":[{"id":g["id"],"relations":[{"plot_id":node,"reason":"观察第一颗星"}],
                              "facts":[]} for g in ctx["教学目标原文"]]}
        if "教学目标覆盖核对" in ctx:
            source = ctx["场景证据"][0]
            node = source["plot_ids"][0]
            return {"matches":[{"id":g["id"],"hotspots":[{"scene":source["id"],"spot":source["spots"][0]["id"],
                                  "plot_id":node,"reason":"通过道具观察星星"}],
                               "quizzes":[{"scene":source["id"],"index":1,"plot_id":node,
                                           "quote":"看第1颗星。","reason":"题目检验观察结果"}]}
                              for g in ctx["教学目标覆盖核对"]]}
        if "情节原稿" in ctx:
            return {"events":[{"action":ctx["情节原稿"]["summary"],"cause":"","result":"","part":1,
                               "box":[.2,.2,.4,.4],"sources":[]}]}
        if "成片候选物品" in ctx:
            if not self.enhanced:
                self.assertGreater(images[0][1].getpixel((640,360))[0],200)
            if self.replacement_hotspot:
                return {"spots":[],"replacement":{"box":[.65,.5,.80,.75],"label":pair("红灯"),"explain":pair("红色的灯")}}
            return {"spots":[{"id":h["id"],"box":[.65,.5,.80,.75],
                              **({"source":{"part":1,"box":[.15,.2,.3,.4]}} if len(images)>1 else {})}
                             for h in ctx["成片候选物品"]]}
        if "核对物品裁图" in ctx:
            return {"verified":[h["id"] for h in ctx["核对物品裁图"] if not self.blank_hotspot]}
        if "情节核对清单" in ctx:
            return {"matches":{n["id"]:(None if self.missing_plot else 1) for n in ctx["情节核对清单"]}}
        if "创作设定" in ctx:
            self.calls["outline"] += 1
            return {"profile":self.profile,"scenes":[{"title":f"第{i}幕","summary":f"发生事件{i}"} for i in range(1,7)]}
        if "当前幕" in ctx:
            i = int(ctx["当前幕"].split("-")[-1])
            self.calls["plan",i] += 1
            if self.writers:
                self.writers.wait(10)
            if self.overlap and i==2:
                self.started["plan2"].set()
                for stage in ("image1","voice1","video1"):
                    self.assertTrue(self.started[stage].wait(10),stage)
            return {"environment":f"地点{i}","shot":f"镜头{i}","visible_characters":[{"name":"小熊","staging":"向前看"}],
                    "props":["一颗星"],"script":manuscript(i)}
        if "候选物品" in ctx:
            self.assertEqual(len(images),1)
            return {"spots":[{**ctx["候选物品"][0],"box":[.5,.2,.7,.4]}]}
        if "page" in ctx:
            boxes = [[0,0,.5,1],[.5,0,1,1]] if ctx["page"]==1 else [[0,0,1,1]]
            return {"panels":[{"title":f"分镜{i}","summary":"发现星星","box":b} for i,b in enumerate(boxes,1)],"regions":[]}
        if "pairs" in ctx:
            return {"pairs":[{"page":2,"joins":[{"previous":2,"current":1}]}]}
        if "全书资料" in ctx:
            return self.profile
        if "文字" in ctx:
            return {"changes":[]}
        if "当前场景" in ctx:
            i = int(ctx["当前场景"]["id"][-1])
            self.calls["pdfplan",i] += 1
            if self.writers:
                self.writers.wait(10)
            self.assertEqual(len(images),1)
            return {**manuscript(i),"clean_instruction":"保留星星轮廓和底色"}
        if "全书" in ctx:
            ids=[s["id"] for s in ctx["全书"]]
            qa={"question":pair("看到什么？"),"hint":pair("看一看"),"evidence":ids}
            return {"observation":{"scene":ids[0],"spot":ctx["全书"][0]["spots"][0]["id"],**qa},
                    "faq":[{**qa,"reply":pair("看见星星")}],
                    "quiz":[{**qa,"mode":mode,"explain":pair("星星"),"options":[pair(s) for s in "ABC"],
                             "answer":[0,1,2] if mode=="order" else 0} for mode in
                            ["choice","order","choice","choice","choice","open"]]}
        raise AssertionError(list(ctx))

    def retell(self, clip):
        streams = providers.run_binary(["ffprobe","-v","error","-show_entries","stream=codec_type",
                                       "-of","csv=p=0","-i","pipe:0"],input=clip,capture_output=True,check=True)
        self.assertEqual(streams.stdout.split(),[b"video",b"audio"])
        probe = providers.run_binary(["ffprobe","-v","error","-show_entries","format=duration",
                                     "-of","csv=p=0","-i","pipe:0"],input=clip,capture_output=True,check=True)
        self.retold_seconds.append(round(float(probe.stdout.strip()),1))
        return {"retelling":"看见一颗星","events":[{"time":0.1,"visible":"一颗星","spoken":"看星星"}]}

    def illustrate(self,prompt,reference=None):
        if prompt.startswith("为原创绘本绘制一张角色三视图参考卡"):
            return Image.new("RGB",(960,540),"blue")
        if self.enhanced:
            self.assertIsNotNone(reference)
        i=int(re.search(r"地点(\d+)",prompt)[1])
        self.calls["image",i] += 1
        if i==self.fail_frame:
            raise RuntimeError("模拟生图暂时不可用")
        if self.overlap and i==1:
            self.started["image1"].set()
            for stage in ("plan2","voice1"):
                self.assertTrue(self.started[stage].wait(10),stage)
        return Image.new("RGB",(160,90),(i*32,45,125))

    def voice(self,text,direction,lang,model,tone):
        self.calls["voice",text,lang] += 1
        if self.overlap and text=="看第1颗星。":
            self.started["voice1"].set()
            self.assertTrue(self.started["video1"].wait(10),"视频必须在第一幕语音返回前启动")
        with io.BytesIO() as stream:
            with wave.open(stream,"wb") as wav:
                wav.setparams((1,2,8000,0,"NONE","not compressed"))
                wav.writeframes(bytes(6400 if lang=="zh" else 9600))
            return stream.getvalue()

    def start_video(self,picture,prompt,seconds,auth,*,reference=None):
        i=int(re.search(r"Shot (\d+)",prompt)[1])
        self.assertEqual(auth,"h3")
        self.assertTrue(4 <= seconds <= 15)
        if self.enhanced:
            self.assertIsNotNone(reference)
        if self.overlap:
            self.assertLess(abs(picture.getpixel((640,360))[0] - i*32),4)
        self.submissions.append(i)
        if i==1:
            self.started["video1"].set()
        return str(i)

    def query_video(self,task,auth):
        i=int(task)
        if self.interrupt and i==1:
            self.interrupt=False
            raise RuntimeError("模拟已提交后的查询中断")
        if self.overlap and i==1:
            self.assertTrue(self.started["video2"].wait(10),"须允许第二幕先完成")
        self.query_order.append(i)
        if i==2:
            self.started["video2"].set()
        return {"status":"succeeded","video_url":"mock://fixture"}

    def new_book(self,kind="text",enhanced=False):
        self.enhanced = enhanced
        brief = dict(theme="星星",characters="小熊",setting="森林",education="观察") if kind=="text" else None
        settings = production.creation_settings(kind,"both",{},brief)
        book = production.initialize(self.root,"离线测试",settings)
        if not enhanced:
            book.pop("quality_version",None)
            production.write(self.root,book)
        return book

    def build(self):
        book=production.read(self.root)
        production.make(self.root,book,"build","","",lambda msg:None)
        return production.read(self.root)

    def test_export_pairs_actual_last_frame_with_verified_hotspot(self):
        self.new_book()
        book = self.build()
        dest = self.root/"editions"/book["edition"]["id"]
        model = json.loads((dest/"book.js").read_text(encoding="utf-8").split("=",1)[1].strip().rstrip(";"))
        shot = model["scenes"][0]
        self.assertNotEqual(shot["interaction_frame"],shot["frame"])
        self.assertEqual(shot["interaction_spots"][0]["box"],[.65,.5,.80,.75])
        self.assertEqual(shot["script"]["spots"][0]["box"],[.5,.2,.7,.4])
        with Image.open(dest/"assets"/shot["interaction_frame"]) as frame:
            self.assertGreater(frame.getpixel((640,360))[0],200)
        self.assertTrue(shot["plot_ids"])
        self.assertEqual(Counter(self.retold_seconds),{.6:6,.8:6})

    def test_failed_visual_review_retries_once_and_exports_static_proof(self):
        self.new_book()
        self.missing_plot = self.blank_hotspot = True
        with self.assertRaisesRegex(RuntimeError,"人工复核"):
            self.build()
        book = production.read(self.root)
        self.assertEqual(self.submissions.count(1),2)
        self.assertEqual(book["scenes"][0]["film_review"]["state"],"needs_review")
        self.assertEqual(book["scenes"][0]["film_review"]["spots"],[])
        self.assertEqual(book["proof_edition"]["animated"],0)
        with self.assertRaisesRegex(RuntimeError,"人工复核"):
            self.build()
        self.assertEqual(self.submissions.count(1),2)

    def test_replacement_hotspot_has_offline_explanation_audio(self):
        self.new_book()
        self.replacement_hotspot = True
        book = self.build()
        dest = self.root/"editions"/book["edition"]["id"]
        model = json.loads((dest/"book.js").read_text(encoding="utf-8").split("=",1)[1].strip().rstrip(";"))
        shot = model["scenes"][0]
        self.assertEqual([h["label"]["zh"] for h in shot["interaction_spots"]],["红灯"])
        replacement = shot["interaction_spots"][0]["id"]
        self.assertEqual(model["activities"]["observation"]["spot"],replacement)
        for language in ("zh","en"):
            self.assertTrue((dest/"assets"/shot["audio"][language]["spots"][replacement]).is_file())

    def test_new_book_exports_checked_multishot_film_and_learning_evidence(self):
        book = self.new_book(enhanced=True)
        self.assertEqual(book.get("quality_version"),2)
        moving_clip = self.clip.with_name("moving.mp4")
        providers.run_binary(["ffmpeg","-nostdin","-loglevel","error","-f","lavfi","-i","color=red:s=160x90:r=24:d=1.1",
            "-f","lavfi","-i","testsrc2=s=80x64:r=24:d=1.1","-filter_complex","[0:v][1:v]overlay=x='5+24*t':y=3",
            "-c:v","libx264","-pix_fmt","yuv420p",str(moving_clip)],check=True,timeout=30)
        with patch.object(providers,"fetch_media",return_value=moving_clip.read_bytes()):
            book = self.build()
        first = book["scenes"][0]
        self.assertEqual(len(first["shot_plan"]),2)
        self.assertEqual({ref for shot in first["shot_plan"] for ref in shot["plot_ids"]},set(first["plot_ids"]))
        self.assertTrue(all(shot["video"] and shot["reference_image"] for shot in first["shot_plan"]))
        self.assertEqual(len(first["film_review"]["motion"]["shots"]),2)
        self.assertEqual(first["film_review"]["cast_issues"],[])
        dest = self.root/"editions"/book["edition"]["id"]
        plan = json.loads((dest/"制作稿.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["education_review"]["goals"][0]["quiz_indices"],[1])
        self.assertEqual(plan["education_review"]["goals"][0]["hotspots"],[{"scene":"story-1","spot":"h1"}])
        player = json.loads((dest/"book.js").read_text(encoding="utf-8").split("=",1)[1].strip().rstrip(";"))
        exported = player["scenes"][0]
        self.assertTrue(exported["cinematic"])
        self.assertGreaterEqual(exported["audio"]["zh"]["duration"],first["video_seconds"])
        self.assertEqual(exported["audio"]["zh"]["cues"],first["audio"]["zh"]["cues"])

    def test_resuming_automatic_retry_updates_full_edition_without_resubmitting(self):
        self.new_book()
        before = self.build()
        sid = before["scenes"][0]["id"]
        self.missing_plot = self.interrupt = True
        with self.assertRaisesRegex(RuntimeError,"查询中断"):
            production.make(self.root,before,"review_film",sid,"",lambda msg:None)
        saved = production.read(self.root)
        self.assertEqual(saved["scenes"][0]["film_retry"]["task"]["state"],"pending")
        self.assertEqual(saved["edition"],before["edition"])
        self.missing_plot = False
        production.make(self.root,saved,"refresh_scene",sid,"",lambda msg:None)
        after = production.read(self.root)
        self.assertNotEqual(after["edition"]["id"],before["edition"]["id"])
        self.assertEqual(after["edition"],after["play_edition"])
        self.assertEqual(self.submissions.count(1),2)

    def test_legacy_streamed_book_with_out_of_order_video_and_real_subtitle_timing(self):
        self.overlap=True
        self.writers=Barrier(6)
        initialize = production.initialize
        def legacy(root,name,creation=None):
            book = initialize(root,name,creation)
            book.pop("quality_version",None)
            production.write(root,book)
            return book
        with patch.object(production,"initialize",side_effect=legacy), patch.object(server,"StudioRSI"), \
             server.LocalServer(("127.0.0.1",0),server.Handler) as http:
            http.studio = server.Studio(self.root.parent)
            thread = Thread(target=http.serve_forever,daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{http.server_port}"
                client = build_opener(ProxyHandler({}))
                form = {"brief":dict(theme="星星",characters="小熊",setting="森林",education="观察"),
                        "language":"both","voices":{}}
                req = Request(url+"/api/stories",json.dumps(form).encode(),{"Content-Type":"application/json"})
                with client.open(req,timeout=5) as response:
                    self.root = self.root.parent/json.load(response)["id"]
                project = url+"/api/projects/"+self.root.name
                deadline=time.monotonic()+90
                while time.monotonic()<deadline:
                    with client.open(project+"/job",timeout=5) as response:
                        status=json.load(response)
                    if status["state"] != "running":
                        break
                    time.sleep(.08)
                self.assertEqual(status["state"],"done",status)
                with client.open(project,timeout=5) as response:
                    book=json.load(response)
                with client.open(url+"/media/"+self.root.name+"/editions/"+book["edition"]["id"]+"/index.html",timeout=5) as response:
                    self.assertIn(b'id="scene-title"',response.read())
            finally:
                http.shutdown()
                thread.join()
        self.assertLess(self.query_order.index(2),self.query_order.index(1))
        self.assertEqual(sorted(self.submissions),list(range(1,7)))
        dest=self.root/"editions"/book["edition"]["id"]
        edition=json.loads((dest/"book.js").read_text("utf8").removeprefix("window.EDITION = ").strip().removesuffix(";"))
        self.assertEqual([s["id"] for s in edition["scenes"]],[f"story-{i}" for i in range(1,7)])
        for scene in edition["scenes"]:
            self.assertTrue((dest/"assets"/scene["video"]).is_file())
            self.assertEqual(scene["script"]["spots"][0]["box"],[.5,.2,.7,.4])
            self.assertEqual(scene["audio"]["zh"]["cues"][0]["end"],.4)
            self.assertEqual(scene["audio"]["en"]["cues"][0]["end"],.6)
        self.assertIn("00:00:00.620 --> 00:00:01.020",(dest/"zh.vtt").read_text("utf8"))
        self.assertIn("00:00:00.820 --> 00:00:01.420",(dest/"en.vtt").read_text("utf8"))
        self.assertEqual(len(edition["activities"]["quiz"]),6)
        with zipfile.ZipFile(dest.with_suffix(".zip")) as bundle:
            files=set(bundle.namelist())
            self.assertEqual({p for p in files if p.endswith(".mp4")},{"assets/"+s["video"] for s in edition["scenes"]})
            for s in edition["scenes"]:
                for lang in ("zh","en"):
                    track=s["audio"][lang]
                    self.assertIn("assets/"+track["file"],files)
                    self.assertIn("assets/"+track["spots"]["h1"],files)
        for s in edition["scenes"]:
            clip=dest/"assets"/s["video"]
            probe=providers.run_binary(["ffprobe","-v","error","-show_entries","stream=codec_type","-of","csv=p=0",str(clip)],
                                       capture_output=True,check=True)
            self.assertEqual(probe.stdout.strip(),b"video")
        self.assertTrue((self.root/"source.pdf").is_file())

    def test_resume_after_accepted_video_uses_same_receipt(self):
        self.new_book()
        self.interrupt=True
        with self.assertRaisesRegex(RuntimeError,"查询中断"):
            self.build()
        partial=production.read(self.root)
        self.assertEqual(partial["scenes"][0]["task"]["state"],"pending")
        self.assertTrue(partial["proof_edition"]["proof"])
        calls=self.calls.copy()
        book=self.build()
        self.assertEqual(self.calls,calls)
        self.assertEqual(sorted(self.submissions),list(range(1,7)))
        self.assertEqual(book["edition"]["animated"],6)

    def test_frames_only_and_retry_a_missing_image(self):
        book=self.new_book()
        self.fail_frame=2
        with self.assertRaisesRegex(RuntimeError,"生图暂时不可用"):
            production.make(self.root,book,"redraw","","",lambda msg:None)
        self.assertFalse(self.submissions)
        self.assertFalse(any(k[0]=="voice" for k in self.calls if isinstance(k,tuple)))
        self.assertFalse((self.root/"source.pdf").exists())
        self.fail_frame=0
        book=production.read(self.root)
        production.make(self.root,book,"redraw","","",lambda msg:None)
        self.assertTrue((self.root/"source.pdf").is_file())
        self.assertEqual([self.calls["image",i] for i in range(1,7)],[1,2,1,1,1,1])
        self.assertFalse(self.submissions)
        self.assertEqual(self.build()["edition"]["animated"],6)

    def test_parallel_writers_resolve_a_shared_visual_before_rendering(self):
        book=self.new_book()
        together=Barrier(2)
        def colliding_plans(task,ctx,images,tokens):
            result=self.ask(task,ctx,images,tokens)
            sid=ctx.get("当前幕")
            if sid in {"story-1","story-2"}:
                i=int(sid[-1])
                if self.calls["plan",i]==1:
                    together.wait(10)
                    result.update(environment="地点1",shot="镜头1")
                else:
                    self.assertTrue(any(v["shot"]=="镜头1" for v in ctx["其他已定画面设计"]))
                    result.update(environment="地点7",shot="镜头7")
            return result
        with patch.object(providers,"ask",side_effect=colliding_plans):
            production.make(self.root,book,"redraw","","",lambda msg:None)
        self.assertEqual(self.calls["plan",1]+self.calls["plan",2],3)
        self.assertEqual(len({s["visual"]["shot"] for s in book["scenes"]}),6)
        self.assertFalse(self.submissions)

    def test_pdf_split_and_spread_keep_reading_order(self):
        self.writers=Barrier(2)
        self.new_book("pdf")
        Image.new("RGB",(160,90),"red").save(self.root/"source.pdf",save_all=True,
            append_images=[Image.new("RGB",(160,90),"blue")])
        book=self.build()
        scenes=book["scenes"]
        self.assertEqual([len(s["parts"]) for s in scenes],[1,2])
        self.assertEqual(book["edition"]["scenes"],["p001-s1","p001-s2"])
        self.assertEqual(self.calls["pdfplan",1],1)
        self.assertEqual(self.calls["pdfplan",2],1)
        self.assertEqual(len(self.submissions),2)
        source = scenes[1]["film_review"]["spots"][0]["source"]
        self.assertEqual(source["page"],scenes[1]["parts"][0]["page"])
        self.assertGreaterEqual(source["box"][0],scenes[1]["parts"][0]["box"][0])
        self.assertLessEqual(source["box"][2],scenes[1]["parts"][0]["box"][2])
        scenes.reverse()
        with self.assertRaisesRegex(ValueError,"分镜顺序有误"):
            production.make(self.root,book,"video","","",lambda msg:None)
        self.assertEqual(len(self.submissions),2)

    def test_unapproved_pdf_frame_only_pauses_its_own_animation(self):
        self.new_book("pdf")
        Image.new("RGB",(160,90),"green").save(self.root/"source.pdf")
        clean=production.clean
        def flag_one(root,book,scene,force=False):
            clean(root,book,scene,force)
            if scene["id"]=="p001-s1":
                scene["clean_review"]=["气泡边缘待核对"]
        with patch.object(production,"clean",side_effect=flag_one), self.assertRaisesRegex(RuntimeError,"清图复核"):
            self.build()
        self.assertEqual(self.submissions,[2])
        book=production.read(self.root)
        self.assertNotIn("edition",book)
        book["scenes"][0]["clean_approved"]=True
        production.write(self.root,book)
        self.assertEqual(self.build()["edition"]["animated"],2)
        self.assertEqual(self.submissions,[2,1])

    def test_single_scene_redraw_on_a_shared_page(self):
        self.new_book("pdf")
        Image.new("RGB",(160,90),"green").save(self.root/"source.pdf")
        book=self.build()
        before=production.read(self.root)
        shots=list(self.submissions)
        page=self.root/"pages"/"p0001.jpg"
        page_bytes=page.read_bytes()
        scene=book["scenes"][1]
        def redraw():
            production.make(self.root,book,"redraw_scene",scene["id"],"",lambda msg:None)
        with patch.object(providers,"illustrate",side_effect=RuntimeError("模拟重画失败")):
            with self.assertRaisesRegex(RuntimeError,"模拟重画失败"):
                redraw()
        self.assertEqual(before,production.read(self.root))
        with patch.object(providers,"illustrate",return_value=Image.new("RGB",(1280,720),"pink")) as draw:
            redraw()
        self.assertEqual(draw.call_count,1)
        self.assertIn(scene["summary"],draw.call_args.args[0])
        self.assertEqual(scene["frame"],scene["redrawn_frame"])
        self.assertNotEqual(scene["frame"],before["scenes"][1]["frame"])
        self.assertEqual(scene["redraw_lines"],before["scenes"][1]["script"]["lines"])
        self.assertNotIn("video",scene)
        self.assertEqual(before["scenes"][0],book["scenes"][0])
        self.assertEqual(page.read_bytes(),page_bytes)
        self.assertEqual(before["edition"],book["edition"])
        self.assertEqual(self.submissions,shots)
