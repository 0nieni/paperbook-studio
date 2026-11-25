import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import reader_questions


class EditionQuestions(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.version = "a" * 24
        folder = self.root / "editions" / self.version
        folder.mkdir(parents=True)
        script = {"title": {"zh": "井沿", "en": "At the well"},
                  "lines": [{"role": "小鸟", "zh": "河水流向远方。", "en": "The river flows far away."}],
                  "spots": [{"label":{"en":"Sun"},"explain":{"en":"The sun is warm."}}]}
        book = {"title": "小青蛙", "scenes": [{"id": "bird", "script": script, "interaction_spots":[
            {"label":{"en":"River"},"explain":{"en":"The river goes far away."}}]}]}
        (folder / "book.js").write_text("window.EDITION = " + json.dumps(book) + ";", encoding="utf-8")
        self.body = {"question": "What did the bird say?", "language": "en", "scene": "bird", "edition": self.version}
        env = patch("reader_questions.providers.setting", return_value="configured")
        env.start()
        self.addCleanup(env.stop)

    @patch("reader_questions.providers.ask")
    def test_uses_the_playing_edition_and_its_language(self, ask):
        ask.return_value = {"supported": True, "answer": "The river flows far away.", "evidence": ["bird", "bird"]}
        answer = reader_questions.answer(self.root, self.body)
        self.assertEqual(answer, {"reply": ask.return_value["answer"], "evidence": ["bird"]})
        context = ask.call_args.args[1]
        self.assertEqual(context["scenes"][0]["lines"], [["小鸟", "The river flows far away."]])
        self.assertEqual(context["scenes"][0]["objects"], [["River","The river goes far away."]])
        self.assertEqual(context["language"], "en")

    @patch("reader_questions.providers.ask")
    def test_unsupported_and_invalid_citations(self, ask):
        ask.return_value = {"supported": False, "answer": "An invented fact", "evidence": []}
        answer = reader_questions.answer(self.root, self.body)
        self.assertEqual(answer["evidence"], [])
        self.assertNotIn("invented", answer["reply"])
        for refs in ([], ["another-edition"], ["bird", "invented"]):
            with self.subTest(refs=refs), self.assertRaises(RuntimeError):
                ask.return_value = {"supported": True, "answer": "A reply", "evidence": refs}
                reader_questions.answer(self.root, self.body)

    @patch("reader_questions.providers.ask")
    def test_rejects_wrong_scene_and_edition_before_calling_provider(self, ask):
        for changes in ({"scene": "elsewhere"}, {"language": "fr"}, {"edition": "../invalid"}, {"question": "  "}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                reader_questions.answer(self.root, {**self.body, **changes})
        ask.assert_not_called()
