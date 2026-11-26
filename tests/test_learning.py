from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

import learning
import providers


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.dry = "手电靠近墙壁时，光斑缩小"
        self.rain = "水滴落进土里"
        self.book = {
            "creation":{"kind":"text","brief":{"education":f"观察光斑变化；{self.rain}。"}},
            "profile":{"education":"模型复述取代家长输入"},
            "plot_nodes":[{"id":"e1","action":self.dry,"cause":"","result":"","quotes":[]},
                          {"id":"e2","action":self.rain,"cause":"","result":"","quotes":[]}],
        }
        self.scenes = [
            {"id":"s1","plot_ids":["e1"],"script":{"lines":[],"spots":[
                {"id":"h1","label":{"zh":"墙上的光斑"},"explain":{"zh":self.dry}}]}},
            {"id":"s2","plot_ids":["e2"],"script":{"lines":[],"spots":[
                {"id":"h1","label":{"zh":"土里的水滴"},"explain":{"zh":self.rain}}]}},
        ]
        self.preparation = {"goals":[
            {"id":"g01","relations":[{"plot_id":"e1","reason":"墙上的光斑大小发生了变化"}],
             "facts":[{"text":"在同一束发散光下，离灯近的墙上光斑更小。","plot_ids":["e1"]}]},
            {"id":"g02","relations":[{"plot_id":"e2","reason":"水滴到达土里"}],"facts":[]},
        ]}
        self.quiz = {"quiz":[
            {"question":{"zh":"灯靠近墙的时候光斑变成什么样？"},"evidence":["s1"]},
            {"question":{"zh":"水滴最后落到哪里？"},"evidence":["s2"]},
        ]}
        self.associations = {"matches":[
            {"id":"g01",
             "hotspots":[{"scene":"s1","spot":"h1","plot_id":"e1","reason":"介绍光斑大小变化"}],
             "quizzes":[{"index":1,"scene":"s1","plot_id":"e1","quote":self.dry,"reason":"题目考查光斑缩小"}]},
            {"id":"g02",
             "hotspots":[{"scene":"s2","spot":"h1","plot_id":"e2","reason":"介绍水滴最后的位置"}],
             "quizzes":[{"index":2,"scene":"s2","plot_id":"e2","quote":self.rain,"reason":"题目考查水滴去向"}]},
        ]}

    def prepare(self):
        with patch.object(providers,"ask",return_value=self.preparation):
            learning.prepare(self.root,self.book,lambda _:None)

    def test_parent_source_wins_and_constraints_only_direct_relevant_scene(self):
        self.prepare()
        goals = learning.activity_context(self.book)
        self.assertEqual([(goal["id"],goal["text"],goal["source"]) for goal in goals],
                         [("g01","观察光斑变化","parent"),("g02",self.rain+"。","parent")])
        self.assertEqual(learning.direction(self.book,self.scenes[0]),[goals[0]])
        self.assertEqual(learning.direction(self.book,self.scenes[1]),[goals[1]])
        self.assertEqual(learning.direction(self.book,{"plot_ids":["different"]}),[])

    def test_pdf_profile_supplies_goals_when_parent_field_is_absent(self):
        self.book.update(creation={"kind":"pdf"},profile={"education":"结合第 2 页了解水滴渗进土里。"})
        response = {"goals":[{"text":"观察水滴渗入土壤","relations":[
            {"plot_id":"e2","reason":"水滴接触土壤"}],"facts":[]}]}
        with patch.object(providers,"ask",return_value=response):
            learning.prepare(self.root,self.book,lambda _:None)
        self.assertEqual([(g["text"],g["source"],g["plot_ids"]) for g in self.book["learning_goals"]],
                         [("观察水滴渗入土壤","pdf_profile",["e2"])])

    def test_numbered_parent_list_does_not_create_a_goal_from_its_list_marker(self):
        self.book["creation"]["brief"]["education"] = f"1. 观察光斑变化？！\n2、{self.rain}"
        self.prepare()
        self.assertEqual([goal["text"] for goal in self.book["learning_goals"]],
                         ["观察光斑变化？！",self.rain])

    def test_pdf_parent_input_is_retained_when_story_cannot_teach_it(self):
        self.book["creation"] = {"kind":"pdf","education":"识别七种鸟的叫声"}
        response = {"goals":[{"id":"g01","relations":[],"facts":[]}]}
        with patch.object(providers,"ask",return_value=response):
            learning.prepare(self.root,self.book,lambda _:None)
        report = learning.cover(self.root,self.book,self.scenes,self.quiz)
        self.assertEqual(report["state"],"needs_review")
        self.assertEqual(report["goals"][0]["text"],"识别七种鸟的叫声")
        self.assertEqual(report["goals"][0]["plot_ids"],[])

    def test_model_cannot_bind_fact_to_unrelated_event(self):
        self.preparation["goals"][0]["facts"][0]["plot_ids"] = ["e2"]
        with self.assertRaisesRegex(ValueError,"事实.*情节"):
            self.prepare()
        self.assertNotIn("learning_goals",self.book)

    def test_actual_retelling_contradiction_cites_observed_words(self):
        self.prepare()
        observed = "手电靠近墙面，光斑由小变大。"
        observations = {"zh":{"events":[{"time":1.5,"visible":observed,"spoken":""}],"retelling":observed}}
        check = {"fact_id":"g01-f01","status":"contradiction",
                 "evidence":[{"observation":"zh-1","field":"visible","quote":"光斑由小变大"}],
                 "reason":"成片显示相反的变化"}
        with patch.object(providers,"ask",return_value={"checks":[check]}):
            issues = learning.check_film(self.root,self.book,self.scenes[0],observations)
        self.assertEqual(len(issues),1)
        self.assertIn("光斑由小变大",issues[0])
        self.assertIn("1.5",issues[0])

    def test_expected_plot_cannot_be_used_as_video_evidence(self):
        self.prepare()
        observed = "墙上有一个光斑。"
        observations = {"zh":{"events":[{"time":1,"visible":observed,"spoken":""}],"retelling":observed}}
        with patch.object(providers,"ask",return_value={"checks":[
            {"fact_id":"g01-f01","status":"unobserved","evidence":[],"reason":""}]}):
            self.assertEqual(learning.check_film(self.root,self.book,self.scenes[0],observations),[])
        observations["zh"]["retelling"] += "镜头没有移动。"
        with patch.object(providers,"ask",return_value={"checks":[
            {"fact_id":"g01-f01","status":"contradiction","reason":"错误引用原稿",
             "evidence":[{"observation":"zh-1","field":"visible","quote":self.dry}]}]}):
            with self.assertRaisesRegex(ValueError,"实际成片"):
                learning.check_film(self.root,self.book,self.scenes[0],observations)

    def test_frame_only_contradiction_names_actual_supplied_frame(self):
        self.prepare()
        picture = Image.new("RGB",(2,2),"red")
        with patch.object(providers,"ask",return_value={"checks":[
            {"fact_id":"g01-f01","status":"contradiction","reason":"距离和大小的关系颠倒",
             "evidence":[{"frame":1,"detail":"近墙的灯投出覆盖整面墙的光斑"}]}]}):
            issues = learning.check_film(self.root,self.book,self.scenes[0],{},frames=[("成片 2.0 秒",picture)])
        self.assertIn("成片 2.0 秒",issues[0])

    def test_semantic_coverage_records_one_based_quiz_and_scene_local_hotspot(self):
        self.prepare()
        with patch.object(providers,"ask",return_value=self.associations):
            report = learning.cover(self.root,self.book,self.scenes,self.quiz)
        self.assertEqual(report["state"],"passed")
        self.assertEqual([goal["quiz_indices"] for goal in report["goals"]],[[1],[2]])
        self.assertEqual([goal["hotspots"] for goal in report["goals"]],
                         [[{"scene":"s1","spot":"h1"}],[{"scene":"s2","spot":"h1"}]])

    def test_coverage_rejects_fabricated_hotspot_even_when_same_id_exists_elsewhere(self):
        self.prepare()
        self.scenes[0]["script"]["spots"] = []
        with patch.object(providers,"ask",return_value=self.associations):
            with self.assertRaisesRegex(ValueError,"热点"):
                learning.cover(self.root,self.book,self.scenes,self.quiz)

    def test_coverage_requires_quiz_reference_in_goal_scene_and_citable_text(self):
        self.prepare()
        quiz = deepcopy(self.quiz)
        quiz["quiz"][0]["evidence"] = ["s2"]
        with patch.object(providers,"ask",return_value=self.associations):
            with self.assertRaisesRegex(ValueError,"小测.*场景"):
                learning.cover(self.root,self.book,self.scenes,quiz)
        self.associations["matches"][0]["quizzes"][0]["quote"] = "原书里没有这句话"
        with patch.object(providers,"ask",return_value=self.associations):
            with self.assertRaisesRegex(ValueError,"小测.*证据"):
                learning.cover(self.root,self.book,self.scenes,self.quiz)

    def test_structural_candidates_alone_do_not_pass_semantic_coverage(self):
        self.prepare()
        self.associations["matches"][0]["quizzes"] = []
        with patch.object(providers,"ask",return_value=self.associations):
            report = learning.cover(self.root,self.book,self.scenes,self.quiz)
        self.assertEqual(report["state"],"needs_review")
        self.assertEqual(report["goals"][0]["quiz_indices"],[])
        self.assertTrue(any("小测" in issue for issue in report["goals"][0]["issues"]))


if __name__ == "__main__":
    unittest.main()
