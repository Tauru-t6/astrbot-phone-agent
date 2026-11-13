"""Recipe hints never replay coordinates or expand a granted text scope."""
import copy
import importlib.util
import unittest
from test_ui_task import ui, node, snapshot, APP, ROOT

spec=importlib.util.spec_from_file_location("ui_recipes_under_test",ROOT/"ui_recipes.py")
recipes=importlib.util.module_from_spec(spec)
spec.loader.exec_module(recipes)


class RecipeTests(unittest.TestCase):
    def screen(self,nodes):return ui.normalize_snapshot(snapshot(nodes),APP)

    def test_input_recipe_uses_parameter_index_and_reobserved_editor_identity(self):
        before=self.screen([node("field","old value",editable=True,focused=True,resource_id=APP+":id/search")])
        after=self.screen([node("field","Kotlin",editable=True,focused=True,resource_id=APP+":id/search")])
        step=recipes.learn_step({"action":"input_text","node_id":"field","text":"Kotlin"},before,after,("Kotlin",))
        self.assertEqual(step["parameter_index"],0)
        self.assertNotIn("Kotlin",str(step))
        self.assertNotIn("old value",str(step))
        renamed=copy.deepcopy(before);renamed["nodes"][0]["id"]="new"
        decision=recipes.recipe_decision(step,renamed,("Android",))
        self.assertEqual(decision,{"action":"input_text","node_id":"new","text":"Android"})
        self.assertFalse(recipes.postcondition_matches(step,after,("Android",)))
        after["nodes"][0]["text"]="Android"
        self.assertTrue(recipes.postcondition_matches(step,after,("Android",)))

    def test_ambiguous_or_missing_node_disables_recipe(self):
        before=self.screen([node()]);after=self.screen([node("field","",editable=True,focused=True,resource_id=APP+":id/query")])
        step=recipes.learn_step({"action":"tap","node_id":"n1"},before,after,())
        self.assertIsNotNone(step)
        ambiguous=self.screen([node(),node("other")])
        self.assertIsNone(recipes.recipe_decision(step,ambiguous,()))
        missing=self.screen([node(text="Elsewhere")])
        self.assertIsNone(recipes.recipe_decision(step,missing,()))

    def test_coordinate_and_literal_injection_is_rejected(self):
        before=self.screen([node()]);after=self.screen([node("field","",editable=True,focused=True,resource_id=APP+":id/query")])
        step=recipes.learn_step({"action":"tap","node_id":"n1"},before,after,())
        value={"version":1,"package":APP,"steps":[step]}
        self.assertEqual(recipes.validate_recipe(value,APP),value)
        for bad in [{**value,"package":"com.other.app"},{**value,"steps":[{**step,"x":1}]},
                    {**value,"steps":[{**step,"selector":{**step["selector"],"bounds":[1,2,3,4]}}]}]:
            with self.assertRaises(recipes.RecipeError):recipes.validate_recipe(bad,APP)

    def test_ocr_nodes_are_never_learned_or_matched_as_recipes(self):
        before=self.screen([node()]);after=self.screen([node("field","",editable=True,focused=True,resource_id=APP+":id/query")])
        before["source"]="ocr"
        self.assertIsNone(recipes.learn_step({"action":"tap","node_id":"n1"},before,after,()))

    def test_literal_replacement_does_not_rewrite_generated_placeholders(self):
        n=node(text="input and Kotlin")
        selector=recipes.selector_for(n,("Kotlin","input"))
        self.assertEqual(selector["text"],"{{input:1}} and {{input:0}}")
        screen=self.screen([node(text="new and replacement")])
        self.assertIsNotNone(recipes.match_selector(selector,screen,("replacement","new")))


if __name__=="__main__":unittest.main()
