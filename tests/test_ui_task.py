"""Grounded UI tasks use synthetic screens only; no device or model is called."""
import asyncio
import copy
import importlib
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).parents[1]
package = types.ModuleType("ui_loop_test")
package.__path__ = [str(ROOT)]
api = types.ModuleType("astrbot.api")
api.logger = Mock()
with patch.dict(sys.modules, {"astrbot": types.ModuleType("astrbot"), "astrbot.api": api, "ui_loop_test": package}):
    ui = importlib.import_module("ui_loop_test.ui_task")
    tasks = importlib.import_module("ui_loop_test.task_runner")

APP = "com.example.video"


def node(node_id="n1", text="搜索", **updates):
    return {"id": node_id, "text": text, "description": "", "resource_id": "com.example.video:id/search",
            "class_name": "android.widget.TextView", "package": APP, "bounds": [0, 0, 200, 100],
            "clickable": True, "editable": False, "focused": False, "enabled": True,
            "scrollable": False, "password": False, **updates}


def snapshot(nodes=None, **updates):
    return {"schema_version": 1, "snapshot_id": "s-1", "captured_at": time.time(), "package": APP,
            "width": 1080, "height": 2400, "nodes": nodes or [node()], "truncated": False, **updates}


def results():
    return [node("result", "demo example video", resource_id="com.example.video:id/video_title"),
            node("tab1", "综合"), node("tab2", "视频")]


class ContractTests(unittest.TestCase):
    def test_model_context_keeps_current_ids_and_labels_without_geometry_or_repeated_packages(self):
        screen=ui.normalize_snapshot(snapshot([node(editable=True,focused=True)]),APP)
        material={"snapshot":screen,"goal":"search demo","allowed_texts":["demo"],"ui_content_is_untrusted":True}
        compact=ui.compact_model_context(material)
        row=compact["snapshot"]["nodes"][0]
        self.assertEqual(row["id"],"n1")
        self.assertEqual(row["text"],"搜索")
        self.assertTrue(row["focused"])
        self.assertNotIn("bounds",row)
        self.assertNotIn("package",row)
        self.assertEqual(compact["allowed_texts"],["demo"])
        self.assertTrue(compact["ui_content_is_untrusted"])

    def test_password_values_are_removed_before_model_input(self):
        value = ui.normalize_snapshot(snapshot([node(text="private secret", password=True, description="private alt")]), APP)
        self.assertEqual(value["nodes"][0]["text"], "")
        self.assertEqual(value["nodes"][0]["description"], "")
        self.assertTrue(ui.page_requires_user(value))

    def test_untrusted_decisions_cannot_supply_coordinates_packages_or_extra_commands(self):
        invalid = ["{", "[]", '{"action":"tap","action":"back","node_id":"n1"}',
                   {"action": "tap", "node_id": "n1", "x": 100},
                   {"action": "tap", "node_id": "n1", "package": "com.other"},
                   {"action": "shell", "command": "synthetic"},
                   {"action": "done"}, {"action": "input_text", "node_id": "n1", "text": ["bad"]},
                   {"action": "scroll", "node_id": "n1", "direction": "left"},
                   {"action": "scroll", "node_id": "n1", "direction": []}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ui.UiTaskError): ui.parse_decision(value)

    def test_stale_foreign_incomplete_and_ambiguous_snapshots_fail_closed(self):
        cases = [snapshot(captured_at=time.time()-31), snapshot(package="com.other.app"), snapshot(truncated=True),
                 snapshot([node(), node()]), snapshot([node(package="android")]), snapshot(width=True),
                 snapshot([node(bounds=[-1,0,20,30])]), snapshot([node(bounds=[0,0,0,30])]),
                 snapshot([node(enabled="true")]), snapshot(captured_at=10**400)]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ui.UiTaskError): ui.normalize_snapshot(value, APP)

    def test_action_is_node_grounded_and_input_requires_explicit_literal_and_focus(self):
        screen = ui.normalize_snapshot(snapshot([node(editable=True, focused=True)]), APP)
        args = ui.ground_action({"action":"input_text", "node_id":"n1", "text":"demo"}, screen, ("demo",))
        self.assertEqual(args, {"kind":"input_text", "node_id":"n1", "text":"demo", "expected_package":APP, "snapshot_id":"s-1"})
        self.assertNotIn("x", args)
        for decision in [{"action":"tap", "node_id":"unknown"},
                         {"action":"input_text", "node_id":"n1", "text":"model invented private text"}]:
            with self.assertRaises(ui.UiTaskError): ui.ground_action(decision, screen, ("demo",))
        screen["nodes"][0]["focused"] = False
        with self.assertRaisesRegex(ui.UiTaskError, "ui_input_not_focused"):
            ui.ground_action({"action":"input_text", "node_id":"n1", "text":"demo"}, screen, ("demo",))

    def test_sensitive_and_unlabelled_targets_need_user(self):
        for target in [node(text="发送"), node(text="确认支付"), node(text="继续"), node(text=""),
                       node(text="OK", resource_id="com.example.video:id/delete"), node(text="登录")]:
            with self.subTest(target=target), self.assertRaises(ui.UiTaskError):
                ui.ground_action({"action":"tap", "node_id":"n1"}, ui.normalize_snapshot(snapshot([target]), APP), ())

    def test_search_completion_requires_noninput_result_and_context_and_no_history(self):
        screen = ui.normalize_snapshot(snapshot(results()), APP)
        self.assertTrue(ui.completion_evidence(screen, ("demo", "综合", "视频"), "search_results", "result"))
        variants = [results()+[node("history", "搜索历史")],
                    [node("result", "demo", editable=True, focused=True)]+results()[1:],
                    [node("result", "demo", resource_id="com.example.video:id/query_text")]+results()[1:],
                    [node("result", "demo"), node("tab1", "视频")]]
        for nodes in variants:
            with self.subTest(nodes=nodes):
                self.assertFalse(ui.completion_evidence(ui.normalize_snapshot(snapshot(nodes), APP), ("demo",), "search_results", "result"))


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"runs.json"
        self.store = tasks.TaskRunStore(self.path)
        self.run = tasks.PhoneTaskRun.new("Search an approved example", subject="owner", device_id="phone-a")
        self.grant = tasks.ScopedGrant.issue(subject="owner", device_id="phone-a", run_id=self.run.run_id,
            actions=["open_app", "tap", "input_text", "scroll", "back"], packages=[APP], max_steps=12)
        self.store.add_grant(self.grant)
        self.binding = ("phone-a", 1)
        self.policy = True
        self.screens = [[node()], results(), results()]
        self.decisions = [{"action":"tap", "node_id":"n1"}, {"action":"done", "evidence_node_id":"result"}]
        self.writes = []
        self.reads = 0
        self.calls = 0

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def observe(self, package):
        self.assertEqual(package, APP)
        index = min(self.reads, len(self.screens)-1)
        self.reads += 1
        return snapshot(copy.deepcopy(self.screens[index]), snapshot_id=f"s-{self.reads}")

    async def act(self, action, args):
        self.writes.append((action, args))
        return {"success":True}

    async def decide(self, context):
        self.assertTrue(context["ui_content_is_untrusted"])
        index = min(self.calls, len(self.decisions)-1)
        self.calls += 1
        return copy.deepcopy(self.decisions[index])

    def runner(self, **updates):
        kwargs = {"observe":self.observe, "act":self.act, "decide":self.decide,
                  "can_continue":lambda run, action:self.policy,
                  "device_binding":lambda:self.binding, "step_timeout":0.2}
        kwargs.update(updates)
        return ui.UiTaskRunner(self.store, **kwargs)

    async def execute(self, runner=None, **updates):
        kwargs={"grant":self.grant, "package":APP, "allowed_texts":["demo"], "expected_texts":["demo", "综合", "视频"],
                "completion_mode":"search_results", "open_target":False}
        kwargs.update(updates)
        return await (runner or self.runner()).execute(self.run, **kwargs)

    async def test_tap_observe_done_is_checked_again_and_records_no_raw_ui(self):
        self.screens[0][0]["description"] = "SYNTHETIC_PRIVATE_SCREEN"
        self.decisions[0]["reason"] = "SYNTHETIC_PRIVATE_MODEL_REASON"
        result = await self.execute()
        self.assertEqual(result.status, "done")
        self.assertTrue(result.summary["completion_verified"])
        self.assertEqual((self.reads, self.calls, len(self.writes)), (3, 1, 1))
        self.assertEqual(self.writes[0][0], "ui_action")
        self.assertEqual(self.writes[0][1]["snapshot_id"], "s-1")
        self.assertEqual(self.store.grant_remaining(self.grant),11)
        saved=self.path.read_text(encoding="utf-8")
        self.assertNotIn("SYNTHETIC_PRIVATE_SCREEN",saved)
        self.assertNotIn("SYNTHETIC_PRIVATE_MODEL_REASON",saved)
        self.assertNotIn("demo example video",saved)

    async def test_open_app_counts_toward_budget_and_requires_matching_foreground(self):
        self.screens = [results(),results()]
        self.decisions = [{"action":"done", "evidence_node_id":"result"}]
        result = await self.execute(open_target=True)
        self.assertEqual(result.status,"done")
        self.assertEqual(self.writes,[("open_app",{"package":APP})])
        self.assertEqual(self.store.grant_remaining(self.grant),11)
        self.assertEqual(result.steps[0].verify["reason"],"foreground_matches")

    async def test_search_result_completion_needs_no_extra_model_call(self):
        self.screens=[results(),results()]
        async def unavailable(context): raise AssertionError("completion must be local")
        result=await self.execute(self.runner(decide=unavailable))
        self.assertEqual(result.status,"done")
        self.assertTrue(result.summary["completion_verified"])
        self.assertEqual(result.summary["model_calls"],0)
        self.assertEqual(self.reads,2)

    async def test_model_timeout_pauses_without_dispatching_an_action(self):
        async def slow(context): await asyncio.sleep(1)
        result=await self.execute(self.runner(decide=slow,step_timeout=0.05))
        self.assertEqual(result.status,"waiting_approval")
        self.assertEqual(result.last_error,"ui_model_timeout")
        self.assertTrue(result.summary["needs_user"])
        self.assertFalse(self.writes)

    async def test_done_cannot_be_proved_by_input_field_or_a_model_assertion(self):
        self.screens = [[node("query","demo",editable=True,focused=True),node("tab1","综合"),node("tab2","视频")]]
        self.decisions = [{"action":"done", "evidence_node_id":"query", "reason":"trust me done"}]
        result = await self.execute()
        self.assertEqual(result.status,"waiting_approval")
        self.assertTrue(result.summary["needs_user"])
        self.assertEqual(result.last_error,"ui_completion_unverified")
        self.assertFalse(self.writes)

    async def test_completion_must_remain_visible_on_fresh_confirmation_read(self):
        self.screens=[results(),[node(text="首页")]]
        self.decisions=[{"action":"done", "evidence_node_id":"result"}]
        result=await self.execute()
        self.assertEqual(result.last_error,"ui_completion_changed")
        self.assertNotEqual(result.status,"done")

    async def test_sensitive_page_stops_before_model_and_sensitive_target_before_write(self):
        self.screens=[[node(password=True,text="secret")]]
        result=await self.execute()
        self.assertEqual(result.last_error,"ui_sensitive_page")
        self.assertTrue(result.summary["needs_user"])
        self.assertEqual(self.calls,0)
        self.assertFalse(self.writes)

    async def test_sensitive_model_target_is_paused_without_consuming_budget(self):
        self.screens=[[node(text="确认支付")]]
        result=await self.execute()
        self.assertTrue(result.summary["needs_user"])
        self.assertFalse(self.writes)
        self.assertEqual(self.store.grant_remaining(self.grant),12)

    async def test_binding_change_during_model_call_prevents_write(self):
        async def decide(context):
            self.binding=("phone-b",2)
            return {"action":"tap", "node_id":"n1"}
        result=await self.execute(self.runner(decide=decide))
        self.assertEqual(result.last_error,"device_changed")
        self.assertFalse(self.writes)

    async def test_policy_change_during_model_call_prevents_write(self):
        async def decide(context):
            self.policy=False
            return {"action":"tap", "node_id":"n1"}
        result=await self.execute(self.runner(decide=decide))
        self.assertEqual(result.last_error,"ui_policy_changed")
        self.assertFalse(self.writes)

    async def test_cancel_during_model_call_is_propagated_and_never_dispatches(self):
        async def decide(context): raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError): await self.execute(self.runner(decide=decide))
        self.assertEqual(self.run.status,"cancelled")
        self.assertFalse(self.writes)

    async def test_lost_action_reply_is_unknown_not_retried(self):
        async def lost(action,args):
            self.writes.append((action,args))
            await asyncio.sleep(1)
        result=await self.execute(self.runner(act=lost,step_timeout=0.05))
        self.assertEqual(result.status,"failed")
        self.assertEqual(result.last_error,"ui_timeout")
        self.assertEqual(result.steps[-1].status,"unknown")
        self.assertEqual(len(self.writes),1)
        self.assertEqual(self.store.grant_remaining(self.grant),11)
        await self.execute()
        self.assertEqual(len(self.writes),1)

    async def test_phone_rejects_stale_snapshot_and_no_retry_occurs(self):
        async def changed(action,args):
            self.writes.append((action,args));return {"success":False,"error_code":"ui_snapshot_changed"}
        result=await self.execute(self.runner(act=changed))
        self.assertTrue(result.summary["needs_user"])
        self.assertEqual(result.last_error,"ui_snapshot_changed")
        self.assertEqual(len(self.writes),1)

    async def test_repeated_no_change_action_never_runs_twice(self):
        self.screens=[[node()]]
        self.decisions=[{"action":"tap","node_id":"n1"}]
        result=await self.execute()
        self.assertEqual(result.last_error,"ui_repeated_action")
        self.assertEqual(len(self.writes),1)

    async def test_bounded_steps_and_model_calls_stop_without_extra_writes(self):
        self.screens=[[node()], [node(text="下一页")], [node(text="最终页")]]
        self.decisions=[{"action":"tap","node_id":"n1"}]
        result=await self.execute(self.runner(max_steps=1))
        self.assertEqual(result.last_error,"ui_step_limit")
        self.assertEqual(len(self.writes),1)

    async def test_missing_or_foreign_grant_never_reads_screen(self):
        self.store.revoke_grant(self.grant.grant_id)
        result=await self.execute()
        self.assertEqual(result.last_error,"grant_missing")
        self.assertEqual(self.reads,0)
        self.assertEqual(self.calls,0)

    async def test_changed_generation_since_approval_never_opens_or_reads_phone(self):
        self.run.summary["device_generation"] = 0
        result = await self.execute(open_target=True)
        self.assertEqual(result.last_error,"device_changed")
        self.assertEqual(self.reads,0)
        self.assertFalse(self.writes)

    async def test_model_call_budget_is_bounded_separately_from_write_budget(self):
        self.screens=[[node()],[node(text="下一页")]]
        result = await self.execute(self.runner(max_model_calls=1))
        self.assertEqual(result.last_error,"ui_model_limit")
        self.assertEqual(self.calls,1)
        self.assertEqual(len(self.writes),1)

    async def test_removed_grant_while_model_runs_prevents_write(self):
        async def decide(context):
            self.store.revoke_grant(self.grant.grant_id)
            return {"action":"tap","node_id":"n1"}
        result = await self.execute(self.runner(decide=decide))
        self.assertEqual(result.last_error,"grant_missing")
        self.assertFalse(self.writes)

    async def test_last_budgeted_action_can_still_receive_completion_verification(self):
        self.grant = tasks.ScopedGrant.issue(subject="owner", device_id="phone-a", run_id=self.run.run_id,
            actions=["tap"], packages=[APP], max_steps=1)
        self.store.add_grant(self.grant)
        result = await self.execute()
        self.assertEqual(result.status,"done")
        self.assertEqual(self.store.grant_remaining(self.grant),0)

    async def test_revocation_during_completion_confirmation_discards_result(self):
        self.screens=[results()]
        self.decisions=[{"action":"done","evidence_node_id":"result"}]
        async def observe(package):
            value = await self.observe(package)
            if self.reads == 2:self.store.revoke_grant(self.grant.grant_id)
            return value
        result=await self.execute(self.runner(observe=observe))
        self.assertEqual(result.last_error,"grant_missing")
        self.assertNotEqual(result.status,"done")

    async def test_input_method_requirement_pauses_without_retry(self):
        async def act(action,args):
            self.writes.append((action,args));return {"success":False,"error_code":"needs_input_method"}
        result=await self.execute(self.runner(act=act))
        self.assertTrue(result.summary["needs_user"])
        self.assertEqual(result.last_error,"needs_input_method")
        self.assertEqual(len(self.writes),1)

    async def test_persistence_failure_never_dispatches(self):
        with patch.object(self.store,"save",return_value=False): result=await self.execute()
        self.assertEqual(result.last_error,"ui_persistence_failed")
        self.assertFalse(self.writes)

    async def test_approved_literal_input_is_visible_and_not_saved_as_step_text(self):
        self.screens=[[node("field","",resource_id="com.example.video:id/search_input",editable=True,focused=True)],
                      [node("field","demo",editable=True,focused=True)],results(),results()]
        self.decisions=[{"action":"input_text","node_id":"field","text":"demo"},
                        {"action":"back"},{"action":"done","evidence_node_id":"result"}]
        result=await self.execute()
        self.assertEqual(result.status,"done")
        self.assertEqual(result.steps[0].args["text_length"],4)
        self.assertNotIn("text",result.steps[0].args)
        self.assertEqual(result.steps[0].verify["reason"],"input_visible")

    async def test_visible_draft_label_cannot_complete_before_any_verified_input(self):
        self.screens = [[node("title", "草稿编辑", clickable=False)]]
        self.decisions = [{"action": "done", "evidence_node_id": "title"}]
        # An old or externally supplied summary flag cannot substitute for
        # this run actually entering and observing an approved literal.
        self.run.summary["ui_input_verified"] = True
        result = await self.execute(completion_mode="visible_text", expected_texts=["草稿编辑"])
        self.assertEqual(result.status, "waiting_approval")
        self.assertEqual(result.last_error, "ui_completion_unverified")
        self.assertTrue(result.summary["needs_user"])
        self.assertIs(result.summary["ui_input_verified"], False)
        self.assertFalse(self.writes)

    async def test_visible_draft_can_complete_after_one_approved_alternative_is_verified(self):
        literal = "synthetic approved draft"
        title = node("title", "草稿编辑", clickable=False)
        editor = node("field", "", resource_id=APP + ":id/draft_input", editable=True, focused=True)
        entered = {**editor, "text": literal}
        self.screens = [[editor, title], [entered, title], [entered, title]]
        self.decisions = [{"action": "input_text", "node_id": "field", "text": literal},
                          {"action": "done", "evidence_node_id": "title"}]
        result = await self.execute(completion_mode="visible_text", expected_texts=["草稿编辑"],
                                    allowed_texts=[literal, "unused approved alternative"])
        self.assertEqual(result.status, "done")
        self.assertIs(result.summary["ui_input_verified"], True)
        self.assertTrue(result.summary["completion_verified"])
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(result.steps[0].verify["reason"], "input_visible")
        saved = self.path.read_text(encoding="utf-8")
        self.assertNotIn(literal, saved)
        self.assertNotIn("unused approved alternative", saved)

    async def test_input_acknowledged_without_matching_fresh_text_cannot_complete_draft(self):
        editor = node("field", "", resource_id=APP + ":id/draft_input", editable=True, focused=True)
        title = node("title", "草稿编辑", clickable=False)
        self.screens = [[editor, title], [{**editor, "text": "wrong content"}, title]]
        self.decisions = [{"action": "input_text", "node_id": "field", "text": "demo"},
                          {"action": "done", "evidence_node_id": "title"}]
        result = await self.execute(completion_mode="visible_text", expected_texts=["草稿编辑"])
        self.assertEqual(result.status, "waiting_approval")
        self.assertEqual(result.last_error, "ui_completion_unverified")
        self.assertIs(result.summary["ui_input_verified"], False)
        self.assertEqual(len(self.writes), 1)

    async def test_visible_text_without_typing_scope_still_uses_fresh_label_evidence(self):
        self.screens = [[node("title", "显示设置", clickable=False)]]
        self.decisions = [{"action": "done", "evidence_node_id": "title"}]
        result = await self.execute(completion_mode="visible_text", expected_texts=["显示设置"], allowed_texts=[])
        self.assertEqual(result.status, "done")
        self.assertIs(result.summary["ui_input_verified"], False)
        self.assertEqual(self.reads, 2)
        self.assertFalse(self.writes)

    async def test_reused_snapshot_cannot_verify_action(self):
        async def repeated(package): return snapshot()
        result=await self.execute(self.runner(observe=repeated))
        self.assertEqual(result.last_error,"ui_snapshot_not_new")
        self.assertNotEqual(result.status,"done")

    async def test_second_concurrent_execute_does_not_start_duplicate_loop(self):
        started,released=asyncio.Event(),asyncio.Event()
        async def decide(context):
            started.set();await released.wait();return {"action":"need_user"}
        runner=self.runner(decide=decide)
        pending=asyncio.create_task(self.execute(runner))
        await started.wait()
        await self.execute(runner)
        released.set();await pending
        self.assertEqual(self.reads,1)
        self.assertFalse(self.writes)

    async def test_requested_results_are_collected_and_revalidated_before_completion(self):
        self.screens=[results()]
        received=[]
        async def collect(context):
            received.append(context)
            return {"items":[{"title":{"node_id":"result","attribute":"text"}}]}
        result=await self.execute(self.runner(collect_results=collect),result_spec={"fields":["title","url"],"max_items":2})
        self.assertEqual(result.status,"done")
        self.assertEqual(result.summary["ui_results"]["items"],[{"title":"demo example video","url":None}])
        self.assertEqual(self.reads,3)
        self.assertEqual(result.summary["model_calls"],1)
        self.assertTrue(received[0]["ui_content_is_untrusted"])

    async def test_requested_results_cannot_be_silently_skipped_without_a_collector(self):
        self.screens=[results()]
        result=await self.execute(result_spec={"fields":["title"]})
        self.assertEqual(result.status,"waiting_approval")
        self.assertEqual(result.last_error,"ui_result_collector_unavailable")
        self.assertNotIn("ui_results",result.summary)
        self.assertNotIn("completion_verified",result.summary)

    async def test_search_tab_cannot_be_returned_as_a_result_title(self):
        self.screens=[results()]
        async def collect(context):return {"items":[{"title":{"node_id":"tab1"}}]}
        result=await self.execute(self.runner(collect_results=collect),result_spec={"fields":["title"]})
        self.assertEqual(result.last_error,"ui_result_evidence_invalid")
        self.assertNotIn("ui_results",result.summary)

    async def test_changed_result_after_selection_is_not_persisted(self):
        changed=results();changed[0]["text"]="demo changed title"
        self.screens=[results(),results(),changed]
        async def collect(context):return {"items":[{"title":{"node_id":"result"}}]}
        result=await self.execute(self.runner(collect_results=collect),result_spec={"fields":["title"]})
        self.assertEqual(result.last_error,"ui_result_evidence_changed")
        self.assertNotIn("ui_results",result.summary)
        self.assertNotIn("demo example video",self.path.read_text(encoding="utf-8"))

    async def test_revoked_policy_during_collection_cannot_save_visible_results(self):
        self.screens=[results()]
        async def collect(context):
            self.policy=False
            return {"items":[{"title":{"node_id":"result"}}]}
        result=await self.execute(self.runner(collect_results=collect),result_spec={"fields":["title"]})
        self.assertEqual(result.last_error,"ui_policy_changed")
        self.assertNotIn("ui_results",result.summary)

    async def test_more_required_items_keep_the_model_in_control_until_result_scope_is_met(self):
        self.screens=[results()]
        self.decisions=[{"action":"need_user"}]
        result=await self.execute(result_spec={"fields":["title"],"max_items":3,"required_items":3})
        self.assertEqual(result.last_error,"ui_user_handoff")
        self.assertEqual(self.calls,1)
        self.assertNotIn("completion_verified",result.summary)

    async def test_verified_recipe_is_returned_only_after_success_and_contains_no_input_literal(self):
        field=node("field","",resource_id=APP+":id/query",editable=True,focused=True)
        entered={**field,"text":"demo"}
        self.screens=[[node()],[field],[entered],results(),results()]
        self.decisions=[{"action":"tap","node_id":"n1"},{"action":"input_text","node_id":"field","text":"demo"},
                        {"action":"back"}]
        # Back cannot be learned with just one page anchor; use a labelled
        # search button and a verified tap to the results instead.
        button=node("submit","搜索",resource_id=APP+":id/submit")
        self.screens[1].append(button);self.screens[2].append(button)
        self.decisions[2]={"action":"tap","node_id":"submit"}
        result=await self.execute(remember_recipe=True)
        self.assertEqual(result.status,"done")
        recipe=result.summary["ui_recipe"]
        self.assertEqual(recipe["version"],1)
        self.assertEqual(recipe["steps"][1]["parameter_index"],0)
        self.assertNotIn("demo",json.dumps(recipe))
        self.assertNotIn("bounds",json.dumps(recipe))

    async def test_recipe_unique_match_runs_without_model_and_postcondition_is_checked(self):
        before=ui.normalize_snapshot(snapshot([node()]),APP)
        after=ui.normalize_snapshot(snapshot(results()),APP)
        learned=ui.learn_step({"action":"tap","node_id":"n1"},before,after,("demo",))
        recipe={"version":1,"package":APP,"steps":[learned]}
        async def no_model(context):raise AssertionError("matching recipe needs no model")
        result=await self.execute(self.runner(decide=no_model),recipe=recipe)
        self.assertEqual(result.status,"done")
        self.assertEqual(result.summary["ui_recipe_reused"],1)
        self.assertEqual(result.summary["model_calls"],0)

    async def test_recipe_target_or_postcondition_mismatch_replans_without_replay(self):
        before=ui.normalize_snapshot(snapshot([node()]),APP)
        after=ui.normalize_snapshot(snapshot(results()),APP)
        learned=ui.learn_step({"action":"tap","node_id":"n1"},before,after,("demo",))
        recipe={"version":1,"package":APP,"steps":[learned]}
        self.screens=[[node(text="不同入口")],results(),results()]
        result=await self.execute(recipe=recipe)
        self.assertEqual(result.status,"done")
        self.assertEqual(result.summary["ui_recipe_status"],"mismatch_replanned")
        self.assertEqual(self.calls,1)
        self.assertEqual(len(self.writes),1)

    async def test_recipe_postcondition_mismatch_drops_rest_and_asks_model(self):
        before=ui.normalize_snapshot(snapshot([node()]),APP)
        after=ui.normalize_snapshot(snapshot(results()),APP)
        learned=ui.learn_step({"action":"tap","node_id":"n1"},before,after,("demo",))
        recipe={"version":1,"package":APP,"steps":[learned,learned]}
        self.screens=[[node()],[node(text="中间页")]]
        self.decisions=[{"action":"need_user"}]
        result=await self.execute(recipe=recipe,remember_recipe=True)
        self.assertEqual(result.last_error,"ui_user_handoff")
        self.assertEqual(result.summary["ui_recipe_status"],"postcondition_replanned")
        self.assertEqual(len(self.writes),1)
        self.assertEqual(self.calls,1)
        self.assertNotIn("ui_recipe",result.summary)

    async def test_unknown_recipe_action_is_never_replayed_or_saved(self):
        before=ui.normalize_snapshot(snapshot([node()]),APP)
        after=ui.normalize_snapshot(snapshot(results()),APP)
        recipe={"version":1,"package":APP,"steps":[ui.learn_step({"action":"tap","node_id":"n1"},before,after,("demo",))]}
        async def unknown(action,args):
            self.writes.append((action,args))
            return {"success":False,"error_code":"connection_failed","outcome":"unknown"}
        result=await self.execute(self.runner(act=unknown),recipe=recipe,remember_recipe=True)
        self.assertEqual(result.steps[-1].status,"unknown")
        self.assertEqual(len(self.writes),1)
        self.assertNotIn("ui_recipe",result.summary)

    async def test_visual_snapshots_require_explicit_scope_and_never_persist_image(self):
        async def observe(package):
            value=await self.observe(package)
            value.update(source="ocr",screenshot={"data_base64":"PRIVATE_IMAGE_BYTES"})
            return value
        result=await self.execute(self.runner(observe=observe))
        self.assertEqual(result.last_error,"ui_vision_not_authorized")
        self.assertFalse(self.writes)
        self.assertNotIn("PRIVATE_IMAGE_BYTES",self.path.read_text(encoding="utf-8"))

    async def test_authorized_visual_tap_is_grounded_and_never_learned(self):
        async def observe(package):
            value=await self.observe(package);value["source"]="ocr"
            return value
        result=await self.execute(self.runner(observe=observe),allow_vision=True,remember_recipe=True)
        self.assertEqual(result.status,"done")
        self.assertNotIn("ui_recipe",result.summary)
        self.assertEqual(self.writes[0][1]["node_id"],"n1")
        self.assertNotIn("x",self.writes[0][1])

    async def test_visual_input_is_not_silently_upgraded_to_a_coordinate_action(self):
        async def observe(package):
            value=await self.observe(package);value["source"]="ocr"
            return value
        self.screens=[[node(editable=True,focused=True)]]
        self.decisions=[{"action":"input_text","node_id":"n1","text":"demo"}]
        result=await self.execute(self.runner(observe=observe),allow_vision=True)
        self.assertEqual(result.last_error,"ui_visual_action_requires_user")
        self.assertFalse(self.writes)

    async def test_resume_new_run_observes_current_page_without_opening_app_or_old_grant(self):
        self.run.summary["parent_run_id"]="previous-run"
        self.screens=[results()]
        result=await self.execute(open_target=False)
        self.assertEqual(result.status,"done")
        self.assertEqual(result.summary["parent_run_id"],"previous-run")
        self.assertFalse(self.writes)

    async def test_failed_final_persistence_cannot_publish_results_or_recipe(self):
        self.screens=[results()]
        original=self.store.save
        def fail_done():return False if self.run.status=="done" else original()
        with patch.object(self.store,"save",side_effect=fail_done):result=await self.execute()
        self.assertEqual(result.status,"failed")
        self.assertEqual(result.last_error,"ui_persistence_failed")
        self.assertNotIn("completion_verified",result.summary)


if __name__ == "__main__": unittest.main()
