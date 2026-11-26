import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import time

from PIL import Image
import production
import providers
import server
from rsi import evidence, review
from rsi.credit import Blame
from rsi.instructions import apply, stamp
from rsi.service import StudioRSI


class ModelDouble:
    def __init__(self):
        self.tasks = []
        self.safe = True

    def see(self,root,scenes):
        return {s["id"]:"画中可见井沿和青蛙" for s in scenes}

    def complete(self,system,user,*,task,temperature):
        self.tasks.append(task)
        if task=="judge":
            scenes = json.loads(user)["scenes"]
            result = {"safety_ok":self.safe,"dimensions":{"story":{"score":.4,"evidence":"两幕动作相同",
                "scenes":[s["id"] for s in scenes]}}}
        elif task=="optimize_prompt":
            result = {"candidates":[{"text":"- 检查相邻幕动作的起点和终点。","rationale":"动作重复",
                                     "target_dimensions":["story"]}]}
        elif task=="repair_candidate":
            result = {"lines":[]}
        elif task=="replay":
            result = {"draft":"候选稿" if "- 检查" in system else "基线稿"}
        elif task=="compare":
            args = json.loads(user)
            result = {label:{"target":.9 if args[label]=="候选稿" else .4,"fidelity":.8,"quality":.8}
                      for label in ("A","B")}
            result.update(safe=True,reason="动作更清楚")
        else:
            raise AssertionError(task)
        return json.dumps(result)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        self.root = self.data/"abcd1234abcd"
        self.model = ModelDouble()
        self.rsi = StudioRSI(self.data,self.model,background=False)
        self.book = production.initialize(self.root,"青蛙",{"kind":"text","languages":["zh"],"voices":{}})
        self.book.pop("quality_version",None)
        image = Image.new("RGB",(96,54),"green")
        for i in (1,2):
            image.save(self.root/"pages"/f"p{i:04d}.jpg")
            self.book["scenes"].append({"id":f"s{i}","title":f"幕{i}","summary":"青蛙看井口",
                "parts":[{"page":i}],"sources":[]})
        self.rsi.select(self.book)
        production.write(self.root,self.book)

    def test_generation_and_retry_and_persistence(self):
        draft = evidence.snapshot(self.root,self.book)
        self.assertEqual(len(draft["near_duplicates"]),1)
        key = self.rsi.enqueue(self.root,draft=draft)
        self.assertEqual(key,self.rsi.enqueue(self.root,draft=draft))
        with patch.object(self.model,"complete",side_effect=RuntimeError("test outage")):
            self.assertTrue(self.rsi.step())
        row = self.rsi.view(self.root)["events"][0]
        self.assertEqual(row["state"],"error")
        self.rsi.retry(self.root,key)
        self.assertTrue(self.rsi.step())
        view = self.rsi.view(self.root)
        self.assertEqual(view["events"][0]["state"],"done")
        self.assertEqual(view["events"][0]["report"]["vision"],2)
        self.assertEqual(view["policies"][0]["status"],"challenger")
        self.assertEqual(self.model.tasks.count("compare"),2)
        self.assertFalse(self.rsi.step())
        restarted = StudioRSI(self.data,self.model,background=False)
        self.assertEqual(restarted.view(self.root),view)
        self.assertIn("分镜",apply("任务",self.book,"story"))
        stamp(self.book["scenes"][0],self.book,"story")
        self.assertEqual(self.book["rsi_prompts"]["story"]["id"],self.book["scenes"][0]["rsi_used"]["story"])

    def test_archived_review_is_never_bound_to_new_draft(self):
        edition = "a"*24
        path = self.root/"editions"/edition
        (path/"assets").mkdir(parents=True)
        providers.save_json(path/"制作稿.json",{"profile":{"title":"旧版"},"scenes":[{"id":"old","title":"当年的幕","parts":[]}]})
        (path/"book.js").write_text('window.EDITION = '+json.dumps({"source_kind":"text","proof":True,"scenes":[{"id":"old","script":{}}]})+';',encoding="utf8")
        different = [{"edition":"b"*24,"rating":1,"notes":"别本"}]
        reviews = [{"edition":edition,"rating":4,"notes":"配音太快"},*different]
        self.rsi.enqueue(self.root,edition,reviews)
        self.rsi.step()
        report = self.rsi.view(self.root)["events"][0]["report"]
        self.assertEqual(report["scene_ids"],["old"])
        self.assertEqual(report["reviews"],1)
        self.assertEqual(report["vision"],0)
        self.assertEqual(report["overall"],.575)

    def test_rejected_gate_and_unsafe_report(self):
        data = evidence.snapshot(self.root,self.book)
        self.model.safe = False
        key = self.rsi.enqueue(self.root,draft=data)
        self.rsi.step()
        view=self.rsi.view(self.root)
        self.assertFalse(view["events"][0]["report"]["safety"])
        self.assertFalse(view["policies"])
        self.assertNotIn("optimize_prompt",self.model.tasks)
        self.model.safe = True
        with patch("rsi.review.paired_trial",return_value=(False,-.1,"质量下降")):
            self.rsi.enqueue(self.root,draft={**data,"title":"新快照"})
            self.rsi.step()
        self.assertFalse(self.rsi.view(self.root)["policies"])

    def test_single_book_cannot_promote_itself(self):
        store = review.unpack({})
        data = evidence.snapshot(self.root,self.book)
        review.cycle(self.root,store,data,[],self.model)
        scope="user:text"
        trial=store.challenger("story",scope)
        self.assertIsNotNone(trial)
        data["assigned_at"] = trial.created_at+1
        for scene in data["scenes"]:
            scene["rsi_used"]={"story":trial.id}
        feedback=[{"rating":5,"notes":"很好"}]
        for _ in range(3):
            review.cycle(self.root,store,data,feedback,self.model)
        self.assertEqual(trial.n,1)
        self.assertEqual(trial.status,"challenger")

    def test_target_the_reported_duplicate_and_fix_candidate_format(self):
        data = evidence.snapshot(self.root,self.book)
        blame=Blame("text2image",1,["picture"],["相邻画面雷同"],{1,2})
        self.assertEqual(review.trial_scene(data,blame,[])["id"],"s2")
        self.assertEqual(review.trial_scene(data,blame,[{"notes":"请看第1幕"}])["id"],"s1")
        texts=[json.dumps({"candidates":[{"text":"1. 错误的编号","target_dimensions":["picture"]}]}),
               json.dumps({"lines":["按动作变化设计构图。"],"rationale":"重复画面"})]
        with patch.object(self.model,"complete",side_effect=texts):
            candidates=review.PromptOptimizer(self.model).propose(role="text2image",scope="user:text",
                current_text="",blame=blame,history=[],optimizer_prompt="test",k=1)
        self.assertEqual(candidates[0].text,"- 按动作变化设计构图。")

    def test_new_work_uses_winner_after_distinct_rated_books(self):
        store=review.unpack({})
        data=evidence.snapshot(self.root,self.book)
        review.cycle(self.root,store,data,[],self.model)
        trial=store.challenger("story","user:text")
        base=store.active("story","user:text")
        data["assigned_at"]=time.time()+1
        for i in range(9):
            root=self.data/f"test{i}"
            root.mkdir()
            # Capture real scene IDs and assignment while avoiding media reads in this unit test.
            for scene in data["scenes"]:
                scene.pop("thumbnail",None)
                scene["rsi_used"]={"story":base.id if i<3 else trial.id}
            review.cycle(root,store,data,[{"notes":"","rating":1 if i<3 else 5}],self.model)
        self.assertEqual(store.active("story","user:text").id,trial.id)
        self.assertTrue(store.active("story","user:pdf").text=="")
        self.assertIn("动作的起点",apply("生成六幕",{"rsi_prompts":review.selection(store,"text")},"story"))

    def test_server_generation_and_review_hooks(self):
        edition="f"*24
        def publish(root,book,action,sid,lang,notice):
            self.assertIn("story",book["rsi_prompts"])
            folder=root/"editions"/edition
            folder.mkdir()
            (folder/"index.html").write_text("<title>test</title>")
            book["play_edition"]={"id":edition}
            production.write(root,book)
        with patch.object(server,"StudioRSI",return_value=self.rsi):
            app=server.Studio(self.data)
        with patch.object(production,"make",side_effect=publish):
            app.start(self.root,{"action":"build"})
            for _ in range(100):
                if self.root not in app.working:
                    break
                time.sleep(.01)
        self.assertEqual(app.job(self.root)["state"],"done")
        self.assertEqual(self.rsi.view(self.root)["events"][0]["source"],"播放版本")
        app.add_review(self.root,{"edition":edition,"rating":3,"notes":"请看第二幕"})
        self.assertEqual(app.reviews(self.root)[0]["rating"],3)
        self.assertEqual(self.rsi.view(self.root)["events"][0]["source"],"家长评价")

    def test_redraw_reviews_exact_submitted_prompt_for_only_the_selected_scene(self):
        scene=self.book["scenes"][1]
        scene["parts"][0]["box"]=[0,0,1,1]
        scene["art_prompt"]="小青蛙在井底"
        production.write(self.root,self.book)
        with patch.object(server,"StudioRSI",return_value=self.rsi):
            app=server.Studio(self.data)
        with patch.object(providers,"illustrate",return_value=Image.new("RGB",(32,18),"yellow")) as draw:
            app.start(self.root,{"action":"redraw_scene","scene":scene["id"],
                                 "redraw_prompt":"井口开阔","redraw_note":"青蛙在左边"})
            for _ in range(500):
                if self.root not in app.working:
                    break
                time.sleep(.01)
        self.assertEqual(app.job(self.root)["state"],"done")
        book=production.read(self.root)
        record=book["scenes"][1]["redraw_edit"]
        self.assertEqual(record["before"],"小青蛙在井底")
        self.assertEqual(record["sent"],draw.call_args.args[0])
        self.assertIn("青蛙在左边",record["after"])
        self.assertNotIn("redraw_edit",book["scenes"][0])
        complete=self.model.complete
        def evaluate(system,user,*,task,temperature):
            if task=="revise_scene_prompt":
                self.assertEqual(json.loads(user)["本幕修改记录"]["sent"],record["sent"])
                return json.dumps({"prompt":"青蛙位于画面左侧，仰望宽阔的井口。","reason":"明确角色位置"})
            return complete(system,user,task=task,temperature=temperature)
        with patch.object(self.model,"complete",side_effect=evaluate):
            self.assertTrue(self.rsi.step())
        restarted=StudioRSI(self.data,self.model,background=False)
        events=restarted.view(self.root)["events"]
        self.assertEqual(len(events),1)
        report=events[0]["report"]
        self.assertEqual(report["scene_ids"],[scene["id"]])
        self.assertEqual(report["redraw"]["position"],2)
        self.assertIn("左侧",report["redraw"]["suggestion"])
        self.assertFalse(restarted.step())


if __name__=="__main__":
    unittest.main()
