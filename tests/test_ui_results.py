"""Result extraction uses synthetic node references and independent evidence."""
import copy
import importlib.util
import unittest

from test_ui_task import ui, node, snapshot, APP, ROOT

spec=importlib.util.spec_from_file_location("ui_results_under_test",ROOT/"ui_results.py")
results=importlib.util.module_from_spec(spec)
spec.loader.exec_module(results)


class ResultEvidenceTests(unittest.TestCase):
    def screen(self):
        return ui.normalize_snapshot(snapshot([
            node("title", "Kotlin intro", resource_id=APP+":id/title",bounds=[0,0,400,60]),
            node("author", "Example Author", resource_id=APP+":id/author",bounds=[0,70,300,100]),
            node("duration", "12:34", resource_id=APP+":id/duration",bounds=[0,110,200,140]),
            node("url", "https://example.com/video/1", resource_id=APP+":id/link",bounds=[0,150,400,190]),
        ]), APP)

    def test_spec_is_bounded_and_requires_title(self):
        for invalid in [[], {}, {"fields":["summary"]}, {"fields":["author"]},
                        {"fields":["title"],"max_items":11}, {"fields":["title"],"required_items":0},
                        {"fields":["title"],"max_items":True}, {"fields":["title"],"extra":"all data"}]:
            if invalid == {}:
                self.assertEqual(results.validate_result_spec(invalid)["max_items"],3)
                continue
            with self.subTest(invalid=invalid), self.assertRaises(results.ResultEvidenceError):
                results.validate_result_spec(invalid)

    def test_values_are_copied_from_fresh_nodes_and_missing_fields_are_honest(self):
        spec=results.validate_result_spec({"fields":["title","author","duration","url"],"max_items":3})
        screen=self.screen()
        selected=results.select_result_evidence({"items":[{"title":{"node_id":"title"},"author":{"node_id":"author"}}]},screen,spec)
        fresh=copy.deepcopy(screen)
        for n in fresh["nodes"]: n["id"]="fresh-"+n["id"]
        value=results.verify_result_evidence(selected,fresh,spec)
        self.assertEqual(value["items"],[{"title":"Kotlin intro","author":"Example Author","duration":None,"url":None}])
        self.assertTrue(value["verified"])
        self.assertEqual(value["missing_fields"],["duration","url"])

    def test_invented_values_foreign_nodes_password_input_and_duplicate_titles_fail(self):
        spec=results.validate_result_spec({"fields":["title","url"]})
        screen=self.screen()
        bad=[{"items":[{"title":{"node_id":"title","value":"invented"}}]},
             {"items":[{"title":"invented"}]}, {"items":[{"title":{"node_id":"missing"}}]},
             {"items":[{"title":{"node_id":"title"}},{"title":{"node_id":"title"}}]},
             {"items":[{"title":{"node_id":"title"},"author":{"node_id":"author"}}]},
             '{"items":[],"items":[]}', '{"items":NaN}']
        for value in bad:
            with self.subTest(value=value),self.assertRaises(results.ResultEvidenceError):
                results.select_result_evidence(value,screen,spec)
        for flag in ["password","editable"]:
            changed=copy.deepcopy(screen);changed["nodes"][0][flag]=True
            with self.assertRaises(results.ResultEvidenceError):
                results.select_result_evidence({"items":[{"title":{"node_id":"title"}}]},changed,spec)

    def test_changed_or_ambiguous_fresh_content_is_not_returned(self):
        spec=results.validate_result_spec({"fields":["title"]})
        screen=self.screen()
        selected=results.select_result_evidence({"items":[{"title":{"node_id":"title"}}]},screen,spec)
        fresh=copy.deepcopy(screen);fresh["nodes"][0]["text"]="Different title"
        with self.assertRaisesRegex(results.ResultEvidenceError,"evidence_changed"):
            results.verify_result_evidence(selected,fresh,spec)
        fresh=copy.deepcopy(screen);fresh["nodes"].append({**fresh["nodes"][0],"id":"duplicate"})
        with self.assertRaisesRegex(results.ResultEvidenceError,"evidence_changed"):
            results.verify_result_evidence(selected,fresh,spec)

    def test_url_must_itself_be_an_observed_http_url(self):
        spec=results.validate_result_spec({"fields":["title","url"]})
        screen=self.screen()
        for value in ["javascript:doThing()","https://user:secret@example.com/a","https://example.com/a b","video.example"]:
            screen["nodes"][-1]["text"]=value
            with self.subTest(value=value),self.assertRaises(results.ResultEvidenceError):
                results.select_result_evidence({"items":[{"title":{"node_id":"title"},"url":{"node_id":"url"}}]},screen,spec)
        screen["nodes"][-1]["text"]="https://example.com/video/1"
        selected=results.select_result_evidence({"items":[{"title":{"node_id":"title"},"url":{"node_id":"url"}}]},screen,spec)
        self.assertEqual(results.verify_result_evidence(selected,screen,spec)["items"][0]["url"],"https://example.com/video/1")

    def test_required_count_cannot_silently_shrink(self):
        spec=results.validate_result_spec({"fields":["title"],"max_items":3,"required_items":3})
        with self.assertRaisesRegex(results.ResultEvidenceError,"incomplete"):
            results.select_result_evidence({"items":[{"title":{"node_id":"title"}}]},self.screen(),spec)

    def test_author_from_another_result_card_is_not_attached(self):
        screen=self.screen()
        screen["nodes"].append(node("other-title","Another title",resource_id=APP+":id/other",bounds=[0,300,400,360]))
        screen["nodes"][1]["bounds"]=[0,370,300,400]
        spec=results.validate_result_spec({"fields":["title","author"],"max_items":2})
        with self.assertRaisesRegex(results.ResultEvidenceError,"association_ambiguous"):
            results.select_result_evidence({"items":[{"title":{"node_id":"title"},"author":{"node_id":"author"}},
                                                     {"title":{"node_id":"other-title"}}]},screen,spec)


if __name__=="__main__":unittest.main()
