from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import wave

from PIL import Image

import motion
import providers as api


class CinemaTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        (self.root / "assets").mkdir()
        (self.root / "cache").mkdir()
        Image.new("RGB", (160, 90), "green").save(self.root / "assets" / "accepted.png")
        self.scene = {"id": "p001-s1", "title": "星星", "frame": "accepted.png",
                      "script": {"video_prompt": "A bear lifts the star.",
                                 "lines": [{"zh": "小熊拿起星星。", "en": "The bear lifts the star."}]},
                      "plot_ids": ["e001-01", "e001-02"]}
        self.book = {"creation": {"kind": "pdf"}, "plot_nodes": [
            {"id": "e001-01", "action": "小熊靠近星星"},
            {"id": "e001-02", "action": "小熊拿起星星", "result": "星星在手里"}]}

    def clip(self, name, source):
        path = self.root / "assets" / name
        api.run_binary(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", source,
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], capture_output=True,
                       check=True, timeout=30)
        return path

    def test_plan_uses_longest_real_narration_and_same_approved_composition(self):
        import cinema

        self.scene["audio"] = {"zh": {"duration": 11.2}}
        self.scene["narration"] = {"en": {"duration": 48.5}}
        shots = cinema.plan(self.root, self.book, self.scene, "minimax", ("zh", "en"))
        self.assertEqual(shots, cinema.plan(self.root, self.book, self.scene, "minimax", ("en", "zh")))
        self.assertGreaterEqual(len(shots), 4)
        self.assertGreater(sum(s["duration"] for s in shots), 48.5)
        self.assertTrue(all(4 <= s["duration"] <= 15 for s in shots))
        self.assertTrue(all(s["frame"] == "accepted.png" for s in shots))
        self.assertGreaterEqual(len({s["framing"] for s in shots}), 3)
        self.assertGreaterEqual(len({s["camera"] for s in shots}), 3)
        self.assertEqual(shots[0]["plot_ids"], ["e001-01"])
        self.assertEqual(shots[-1]["plot_ids"], ["e001-02"])
        self.assertIn("full composition", shots[-1]["prompt"])
        self.assertTrue(all(s[key] for s in shots for key in
                            ("preparation", "action", "follow_through", "secondary_motion")))

    def test_composition_resumes_receipt_and_keeps_source_speed(self):
        import cinema

        first = self.clip("blue.mp4", "color=blue:s=160x90:r=24:d=1,drawbox=0:0:iw:ih:red:t=fill:enable=gte(t\\,0.5)")
        last = self.clip("green.mp4", "color=green:s=160x90:r=24:d=1")
        scene = self.scene
        scene["shot_plan"] = cinema.plan(self.root, self.book, scene, "minimax", ("zh",))
        saved = []
        submissions = []
        input_colors = []
        def start(picture, prompt, seconds, auth):
            submissions.append(prompt)
            input_colors.append(picture.getpixel((picture.width//2,picture.height//2)))
            return str(len(submissions))
        def query(identifier, auth):
            return {"status": "succeeded", "video_url": identifier}
        def fetch(identifier):
            return {"1": first, "2": last}[identifier].read_bytes()
        with patch.object(api, "start_video", side_effect=start), \
                patch.object(api, "query_video", side_effect=RuntimeError("interrupted")), \
                patch.object(api, "fetch_media", side_effect=fetch):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                motion.animate_scene(self.root, scene, "minimax", lambda _: None, lambda: saved.append(deepcopy(scene)))
        persisted = saved[-1]
        task = persisted["shot_plan"][0]["task"]
        self.assertEqual((task["state"], task["id"]), ("pending", "1"))
        self.assertEqual(len(submissions), 1)
        with patch.object(api, "start_video", side_effect=start), \
                patch.object(api, "query_video", side_effect=query), patch.object(api, "fetch_media", side_effect=fetch):
            motion.animate_scene(self.root, persisted, "minimax", lambda _: None, lambda: None)
            motion.animate_scene(self.root, persisted, "minimax", lambda _: None, lambda: None)
        self.assertEqual(len(submissions), 2)
        self.assertNotEqual(*submissions)
        self.assertEqual(input_colors[0], (0,128,0))
        self.assertGreater(input_colors[1][0], 240)
        self.assertLess(input_colors[1][1], 10)
        self.assertEqual(persisted["shot_plan"][1]["input_frame"],persisted["shot_plan"][1]["video_source"]["input_frame"])
        self.assertEqual([s["task"]["id"] for s in persisted["shot_plan"]], ["1", "2"])
        self.assertEqual([s["video_seconds"] for s in persisted["shot_plan"]], [1, 1])
        self.assertTrue(persisted["video_source"]["cinematic"])
        self.assertEqual(persisted["video_source"]["delivery"], "api")
        self.assertAlmostEqual(persisted["video_seconds"], 2.2, delta=.09)
        video = self.root / "assets" / persisted["video"]
        for timestamp, rgb in ((".25", (0, 0, 255)), (".65", (255, 0, 0)), ("2.05", (0, 127, 0))):
            result = api.run_binary(["ffmpeg", "-loglevel", "error", "-ss", "0" + timestamp if timestamp[0] == "." else timestamp,
                                     "-i", str(video), "-frames:v", "1", "-vf", "scale=1:1,format=rgb24",
                                     "-f", "rawvideo", "pipe:1"], capture_output=True, check=True)
            self.assertEqual(len(result.stdout), 3)
            self.assertTrue(all(abs(value - expected) < 6 for value, expected in zip(result.stdout, rgb)))
        probe = api.run_binary(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                                str(video)], capture_output=True, check=True)
        self.assertEqual(probe.stdout.strip(), b"video")

    def test_inspects_actual_shots_and_samples_all_three_positions(self):
        import cinema

        static = self.clip("static.mp4", "color=blue:s=160x90:r=24:d=1")
        active = self.clip("active.mp4", "testsrc2=s=160x90:r=24:d=1")
        scene = {"shot_plan": [{"id": "s1", "video": static.name}, {"id": "s2", "video": active.name}]}
        report = cinema.motion_report(self.root, scene)
        self.assertEqual(len(report["shots"]), 2)
        self.assertLess(report["shots"][0]["motion"], report["shots"][1]["motion"])
        self.assertTrue(any("s1" in issue for issue in report["issues"]))
        samples = list(cinema.sample_frames(self.root, scene))
        try:
            self.assertEqual(len(samples), 6)
            self.assertTrue(all(isinstance(picture, Image.Image) for _, picture in samples))
            self.assertEqual(len({label for label, _ in samples}), 6)
        finally:
            for _, picture in samples:
                picture.close()

    def test_flags_repeated_hard_flashes_and_samples_the_actual_last_frame(self):
        import cinema

        fast = self.clip("flashes.mp4", "color=white:s=160x90:r=24:d=1,"
                         "drawbox=0:0:iw:ih:black:t=fill:enable=lt(mod(n\\,6)\\,3)")
        ending = self.clip("ending.mp4", "color=blue:s=160x90:r=24:d=1,"
                           "drawbox=0:0:iw:ih:red:t=fill:enable=gte(n\\,22)")
        scene = {"shot_plan": [{"id": "fast", "video": fast.name}, {"id": "ending", "video": ending.name}]}
        report = cinema.motion_report(self.root, scene)
        self.assertTrue(any("fast" in issue and "过大" in issue for issue in report["issues"]))
        for label, picture in cinema.sample_frames(self.root, scene):
            with picture:
                if label.startswith("ending"):
                    pixel = picture.getpixel((0, 0))
                    self.assertGreater(pixel[0 if label.endswith("末尾") else 2], 220)

    def test_submitting_shot_uses_the_linked_console_task_after_restart(self):
        import cinema

        video = self.clip("provider.mp4", "testsrc2=s=160x90:r=24:d=1")
        self.scene["shot_plan"] = cinema.plan(self.root, self.book, self.scene, "minimax", ("zh",))
        with patch.object(api, "start_video", side_effect=RuntimeError("lost response")):
            with self.assertRaisesRegex(RuntimeError, "lost response"):
                motion.animate_scene(self.root, self.scene, "minimax", lambda _: None, lambda: None)
        recovered = deepcopy(self.scene)
        receipt = motion.receipt(recovered)
        self.assertEqual(receipt["state"], "submitting")
        journal = motion.directory_record(self.root, "minimax", receipt["key"])
        stalled = deepcopy(recovered)
        stalled.pop("task")
        stalled["shot_plan"][0].pop("task")
        with patch.object(api, "start_video", side_effect=AssertionError("repeat submission")):
            with self.assertRaisesRegex(ValueError, "提交回执中断"):
                motion.animate_scene(self.root, recovered, "minimax", lambda _: None, lambda: None)
            with self.assertRaisesRegex(ValueError, "提交回执中断"):
                motion.animate_scene(self.root, stalled, "minimax", lambda _: None, lambda: None)
        self.assertEqual(motion.receipt(stalled)["key"],receipt["key"])
        receipt.update(state="pending", id="from-console")
        self.assertEqual(recovered["shot_plan"][0]["task"]["id"], "from-console")
        journal.unlink()  # The persisted scene receipt alone is sufficient to resume.
        queried = []
        def query(identifier, auth):
            queried.append(identifier)
            return {"status": "succeeded", "video_url": "mock://video"}
        with patch.object(api, "start_video", return_value="next-shot") as start, \
                patch.object(api, "query_video", side_effect=query), patch.object(api, "fetch_media", return_value=video.read_bytes()):
            motion.animate_scene(self.root, recovered, "minimax", lambda _: None, lambda: None)
            self.assertEqual(start.call_count, 1)
        self.assertEqual(queried, ["from-console", "next-shot"])

    def test_reference_asset_is_part_of_the_shot_recipe_and_receipt(self):
        import cinema

        clip = self.clip("fixture.mp4", "testsrc2=s=160x90:r=24:d=1")
        Image.new("RGB", (80, 120), "purple").save(self.root / "assets" / "cast.png")
        self.scene["shot_plan"] = cinema.plan(self.root, self.book, self.scene, "minimax", ("zh",))
        self.scene["shot_plan"][0]["reference_image"] = "cast.png"
        references = []
        def start(picture, prompt, seconds, auth, *, reference=None):
            references.append((picture.size, reference.size if reference else None))
            return str(len(references))
        with patch.object(api, "start_video", side_effect=start), patch.object(api, "query_video", return_value={
                "status": "succeeded", "video_url": "mock://video"}), \
                patch.object(api, "fetch_media", return_value=clip.read_bytes()):
            motion.animate_scene(self.root, self.scene, "minimax", lambda _: None, lambda: None)
        self.assertEqual(references, [((160, 90), (80, 120)), ((1280, 720), None)])
        self.assertEqual(self.scene["video_source"]["shots"][0]["reference_image"], "cast.png")
        draft = {"frame": "accepted.png", "script": self.scene["script"], "planned_video_seconds": 4}
        plain_key, seconds, prompt = motion.render_recipe(draft, "minimax")
        self.assertEqual(plain_key, api.digest(api.VIDEO, "accepted.png", prompt, seconds))
        self.assertNotEqual(motion.render_recipe({**draft, "reference_image": "cast.png"}, "minimax")[0], plain_key)

    def test_soundtrack_preserves_narration_clock_and_is_cached(self):
        import cinema

        path = self.root / "assets" / "narration.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            wav.writeframes(bytes(16000))
        scene = {"id": "s1", "video_source": {"cinematic": True}, "video_seconds": 1.25,
                 "shot_plan": [{"id": "s1-01", "timeline_start": 0}, {"id": "s1-02", "timeline_start": .55}]}
        soundtrack = cinema.soundtrack(self.root, path.name, scene)
        self.assertEqual(soundtrack, cinema.soundtrack(self.root, path.name, scene))
        self.assertNotEqual(soundtrack, path.name)
        with wave.open(str(self.root / "assets" / soundtrack), "rb") as wav:
            self.assertEqual((wav.getnframes(), wav.getframerate(), wav.getnchannels()), (10000, 8000, 1))
            self.assertNotEqual(wav.readframes(8000), bytes(16000))
