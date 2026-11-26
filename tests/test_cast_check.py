from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

import artwork as art
import cast_check


def colors(image):
    return {pixel for _, pixel in image.getcolors(image.width * image.height)}


class CastCheckTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        (self.root / "pages").mkdir()
        self.book = {
            "profile": {
                "style": "温柔色块",
                "characters": [
                    {"name": "小熊", "features": "圆耳朵，蓝帽子"},
                ],
            },
        }
        self.scene = {"id": "s1", "summary": "小熊看星星",
                      "visual": {"characters": [{"name": "小熊", "staging": "小熊在左边"}]}}

    def write_source(self, hue):
        source = Image.new("RGB", (100, 100), "white")
        source.paste(hue, (20, 30, 60, 80))
        source.save(art.pagefile(self.root, 1), "PNG")

    def test_pdf_records_original_crops_and_leaves_missing_views_explicit(self):
        self.write_source("#C04040")
        box = [.2, .3, .6, .8]
        self.book["profile"]["characters"][0]["refs"] = [
            {"page": 1, "box": box, "view": "正面"},
            {"page": 2, "box": box, "view": "背面"},
        ]
        with patch.object(cast_check.api, "illustrate", side_effect=AssertionError("PDF 不生成新角度")):
            self.assertTrue(cast_check.prepare(self.root, self.book, lambda _: None))
            self.assertFalse(cast_check.prepare(self.root, self.book, lambda _: None))
            bear = self.book["cast_reference"]["characters"][0]
            self.assertIn("#C04040",bear["palette"].values())
            self.assertEqual(bear["traits"],["圆耳朵","蓝帽子"])
            self.assertEqual(bear["missing_views"], ["side", "back"])
            self.assertEqual(len(bear["refs"]), 1)
            ref = bear["refs"][0]
            self.assertEqual((ref["view"], ref["page"], ref["box"]), ("front", 1, box))
            with Image.open(self.root / "assets" / ref["asset"]) as cropped:
                self.assertEqual(cropped.size, (40, 50))
                self.assertEqual(cropped.getpixel((20, 25)), (192, 64, 64))
            self.write_source("#406080")
            self.assertTrue(cast_check.prepare(self.root, self.book, lambda _: None))
            updated = self.book["cast_reference"]["characters"][0]["refs"][0]
            self.assertNotEqual(updated["asset"], ref["asset"])

    def test_text_saves_a_verified_turnaround_and_reuses_it_for_illustrations(self):
        self.book["creation"] = {"kind": "text"}
        self.book["profile"]["characters"].append({"name": "小鹿", "features": "方形斑点"})
        rendered = []

        def ask(task, context, images, tokens):
            if "角色定型" in context:
                return {"characters": [
                    {"name": "小熊", "palette": {"帽子": "#3355aa"}, "traits": ["圆耳朵", "星形徽章"]},
                    {"name": "小鹿", "palette": {"斑点": "#c04040"}, "traits": ["尖耳朵", "方形斑点"]},
                ]}
            self.assertIn("角色三视图核对", context)
            self.assertEqual(len(images), 1)
            self.assertIn(images[0][1].getpixel((20, 20)), [(255, 0, 0), (0, 0, 255)])
            return {"views": {"front": "正脸与鼻子", "side": "轮廓与鼻尖", "back": "后脑与后背"},
                    "matches": True, "issues": []}

        def illustrate(prompt):
            rendered.append(prompt)
            color = "red" if len(rendered) == 1 else "blue"
            return Image.new("RGB", (1280, 720), color)

        with patch.object(cast_check.api, "ask", ask), patch.object(cast_check.api, "illustrate", illustrate):
            self.assertTrue(cast_check.prepare(self.root, self.book, lambda _: None))
            self.assertFalse(cast_check.prepare(self.root, self.book, lambda _: None))
        bear = self.book["cast_reference"]["characters"][0]
        self.assertEqual(bear["palette"], {"帽子": "#3355AA"})
        self.assertEqual(bear["refs"][0]["views"], ["front", "side", "back"])
        self.assertEqual(bear["missing_views"], [])
        self.assertEqual(len(rendered), 2)
        self.assertIn("#3355AA", rendered[0])
        direction = cast_check.direction(self.book, self.scene)
        for item in ("小熊", "#3355AA", "圆耳朵", "星形徽章"):
            self.assertIn(item, direction)
        self.assertNotIn("小鹿", direction)
        collage = cast_check.image(self.root, self.book, self.scene)
        self.assertIn((255, 0, 0), colors(collage))
        self.assertNotIn((0, 0, 255), colors(collage))
        self.assertIsNone(cast_check.image(self.root, self.book, {"visual": {"characters": []}}))

        prior = bear["refs"][0]["asset"]
        (self.root / "assets" / prior).unlink()
        with patch.object(cast_check.api, "ask", ask), patch.object(cast_check.api, "illustrate", illustrate):
            self.assertTrue(cast_check.prepare(self.root, self.book, lambda _: None))
        self.assertEqual(len(rendered), 3)
        self.assertNotEqual(self.book["cast_reference"]["characters"][0]["refs"][0]["asset"], prior)

    def test_rejected_turnaround_does_not_claim_missing_angles(self):
        self.book["creation"] = {"kind": "text"}
        reviews = []

        def ask(task, context, images, tokens):
            if "角色定型" in context:
                return {"characters": [{"name": "小熊", "palette": {"帽子": "#3355AA"},
                                        "traits": ["圆耳朵", "星形徽章"]}]}
            reviews.append(images)
            return {"views": {"front": "看见脸", "side": "看见鼻尖", "back": ""},
                    "matches": False, "issues": ["三张都是正面或侧面"]}

        with patch.object(cast_check.api, "ask", ask), \
                patch.object(cast_check.api, "illustrate", return_value=Image.new("RGB", (1280, 720), "red")):
            with self.assertRaisesRegex(ValueError, "三视图"):
                cast_check.prepare(self.root, self.book, lambda _: None)
        self.assertNotIn("cast_reference", self.book)
        self.assertEqual(len(reviews), 1)

    def test_checks_actual_frames_and_distinguishes_drift_from_an_absent_character(self):
        self.write_source("#C04040")
        self.book["profile"]["characters"][0]["refs"] = [
            {"page": 1, "box": [.2, .3, .6, .8], "view": "正面"},
        ]
        scene = {"id": "s1", "summary": "小熊走出画面",
                 "parts": [{"page": 1, "box": [0, 0, 1, 1]}]}
        cast_check.prepare(self.root, self.book, lambda _: None)
        frames = [(label, Image.new("RGB", (320, 180), color))
                  for label, color in (("首帧", "red"), ("中帧", "blue"), ("末帧", "white"))]
        calls = []

        def ask(task, context, images, tokens):
            pixels = {label: frame.getpixel((20, 20)) for label, frame in images}
            self.assertEqual(context["实际帧标签"], ["首帧", "中帧", "末帧"])
            self.assertEqual(pixels["首帧"], (255, 0, 0))
            self.assertEqual(pixels["末帧"], (255, 255, 255))
            self.assertTrue(any(label != "首帧" and (192, 64, 64) in colors(frame)
                                for label, frame in images))
            calls.append(pixels["中帧"])
            return {"frames": [
                {"label": "首帧", "characters": [{"name": "小熊", "status": "match", "evidence": "圆耳朵和蓝帽子与原裁图一致"}]},
                {"label": "中帧", "characters": [{"name": "小熊", "status": "drift", "evidence": "帽子换成了绿色"}]},
                {"label": "末帧", "characters": [{"name": "小熊", "status": "absent", "evidence": "角色走出画框，画面仅保留路面"}]},
            ]}

        with patch.object(cast_check.api, "ask", ask):
            errors = cast_check.check(self.root, self.book, scene, frames)
            self.assertEqual(cast_check.check(self.root, self.book, scene, frames), errors)
            frames[1][1].paste("cyan", (0, 0, 60, 60))
            self.assertEqual(cast_check.check(self.root, self.book, scene, frames), errors)
        self.assertEqual(len(errors), 1)
        self.assertIn("中帧", errors[0])
        self.assertIn("帽子换成了绿色", errors[0])
        self.assertEqual(calls, [(0, 0, 255), (0, 255, 255)])

    def test_missing_reference_is_distinct_from_drift_even_if_model_claims_match(self):
        self.write_source("#C04040")
        scene = {"id": "s1", "summary": "小熊看星星",
                 "parts": [{"page": 1, "box": [0, 0, 1, 1]}]}
        cast_check.prepare(self.root, self.book, lambda _: None)
        result = {"frames": [{"label": "首帧", "characters": [
            {"name": "小熊", "status": "match", "evidence": "在插画中看见圆耳朵和蓝帽子"},
        ]}]}
        with patch.object(cast_check.api, "ask", return_value=result):
            errors = cast_check.check(self.root, self.book, scene, [("首帧", Image.new("RGB", (32, 32)))])
        self.assertEqual(len(errors), 1)
        self.assertIn("缺少角色参考", errors[0])

    def test_omitted_actual_frame_cannot_pass(self):
        cast_check.prepare(self.root, self.book, lambda _: None)
        result = {"frames": [{"label": "首帧", "characters": [
            {"name": "小熊", "status": "absent", "evidence": "画面只有星空"},
        ]}]}
        with patch.object(cast_check.api, "ask", return_value=result):
            with self.assertRaisesRegex(ValueError, "帧"):
                cast_check.check(self.root, self.book, self.scene, [
                    ("首帧", Image.new("RGB", (32, 32))), ("末帧", Image.new("RGB", (32, 32))),
                ])

    def test_prop_only_shot_requires_visual_evidence_of_empty_cast(self):
        self.book["creation"] = {"kind": "text"}
        self.book["profile"]["characters"] = []
        cast_check.prepare(self.root, self.book, lambda _: None)
        scene = {"id": "s1", "visual": {"characters": []}}
        frames = [("首帧", Image.new("RGB", (32, 32), "yellow"))]
        row = {"label": "首帧", "characters": [], "unexpected": []}
        with patch.object(cast_check.api, "ask", return_value={"frames": [row]}):
            with self.assertRaisesRegex(ValueError, "无人镜头"):
                cast_check.check(self.root, self.book, scene, frames)
            row["no_character_evidence"] = "单独的黄色灯罩特写"
            self.assertEqual(cast_check.check(self.root, self.book, scene, frames), [])


if __name__ == "__main__":
    unittest.main()
