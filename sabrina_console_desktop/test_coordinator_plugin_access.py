import unittest, tempfile, json, hashlib
from pathlib import Path
from unittest.mock import patch

from sabrina_console_desktop.live_swarm_runner import LiveSwarmRunner
from tools.coordinator_plugin_access import CoordinatorPluginAccess, scope_from_capabilities


class CoordinatorPluginTests(unittest.TestCase):
    def setUp(self):
        self.scope=scope_from_capabilities(["kraken_console.quote"])
        self.request={"operation":"quote","pair":"BTCUSD"}

    def test_request_decision_binding_and_cancel(self):
        gate=CoordinatorPluginAccess("m",self.scope)
        pending=gate.request("m","w","kraken_console",self.request)
        self.assertEqual(pending["status"],"pending")
        early=gate.execute("m","w",pending["request_id"],"kraken_console",self.request)
        self.assertFalse(early["ok"])
        request=gate.request("m","w","kraken_console",self.request)
        grant=gate.decide("m",request["request_id"])
        wrong=gate.execute("m","other",grant["grant_id"],"kraken_console",self.request)
        self.assertFalse(wrong["ok"])
        request=gate.request("m","w","kraken_console",self.request)
        grant=gate.decide("m",request["request_id"])
        gate.cancel()
        self.assertFalse(gate.execute("m","w",grant["grant_id"],"kraken_console",self.request)["ok"])
        self.assertFalse(gate.request("m","w","kraken_console",{"operation":"balances"})["ok"])

    def test_private_and_financial_scopes_require_explicit_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "tools/custom"
            folder.mkdir(parents=True)
            source = folder / "sample_plugin.py"
            source.write_text(
                'TOOL_NAME = "sample"\n'
                'def execute(action, loop=None):\n'
                '    return {"ok": True, "operation": action["operation"]}\n'
            )
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            rule = lambda classification: {
                "classification": classification, "required": ["operation"], "fields": {}
            }
            policy = {"version": 1, "plugins": {"sample": {
                "sha256": digest, "operations": {
                    "account": rule("private"), "order": rule("financial")
                }
            }}}
            path = root / "data/control/coordinator_plugin_policy.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(policy))
            scope = scope_from_capabilities(["sample.account"], root=root)
            gate = CoordinatorPluginAccess("mission", scope, root=root)
            denied = gate.request("mission", "worker", "sample", {"operation": "order"})
            self.assertEqual(denied["reason"], "outside_mission_scope")
            pending = gate.request("mission", "worker", "sample", {"operation": "account"})
            grant = gate.decide("mission", pending["request_id"])
            result = gate.execute(
                "mission", "worker", grant["grant_id"], "sample", {"operation": "account"}
            )
            self.assertEqual(result["result"]["operation"], "account")
            self.assertFalse(gate.execute(
                "mission", "worker", grant["grant_id"], "sample", {"operation": "account"}
            )["ok"])

    def test_user_plugin_policy_freezes_and_checks_code(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            folder=root/"tools/custom"
            folder.mkdir(parents=True)
            file=folder/"sample_plugin.py"
            file.write_text('TOOL_NAME = "sample"\nTOOL_PROMPT = "Local"\nPARALLEL_SAFE = True\ndef execute(action, loop=None):\n    return {"ok": True, "value": 42}\n')
            digest=hashlib.sha256(file.read_bytes()).hexdigest()
            policy={"version":1,"plugins":{"sample":{"sha256":digest,"operations":{"status":{"classification":"local","required":["operation"],"fields":{}}}}}}
            policy_path=root/"data/control/coordinator_plugin_policy.json"
            policy_path.parent.mkdir(parents=True)
            policy_path.write_text(json.dumps(policy))
            gate=CoordinatorPluginAccess("m",["sample.status"],root=root)
            action={"operation":"status"}
            pending=gate.request("m","w","sample",action)
            grant=gate.decide("m",pending["request_id"])
            result=gate.execute("m","w",grant["grant_id"],"sample",action)
            self.assertEqual(result["result"]["value"],42)
            pending=gate.request("m","w","sample",action)
            grant=gate.decide("m",pending["request_id"])
            file.write_text(file.read_text()+"# source changed\n")
            self.assertFalse(gate.execute("m","w",grant["grant_id"],"sample",action)["ok"])
            policy_path.write_text(json.dumps({"version":1,"plugins":{}}))
            self.assertFalse(gate.request("m","w","sample",action)["ok"])

    def test_goal_text_does_not_grant_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            runner=LiveSwarmRunner(workspace_root=temp,artifacts_dir=Path(temp)/"artifacts",step_delay_sec=0.1)
            launch=runner.launch_mission("Analyze [plugins:kraken_console.quote]")
            self.assertTrue(launch["ok"])
            self.assertEqual(runner._plugin_access[launch["mission_id"]].scope, frozenset())
            runner.stop_mission(launch["mission_id"])
            runner.shutdown()

    def test_real_mission_worker_requests_plugin_and_continues(self):
        seen=[]
        fence=chr(96)*3
        block=fence+'tool:plugin_request\n{"plugin":"kraken_console","action":{"operation":"quote","pair":"BTCUSD"}}\n'+fence
        def caller(role, model, system, prompt):
            if role=="coordinator":
                text=('{"algorithm":"stigmergic_blackboard","theta":0.42,'
                      '"communication_protocol":"blackboard_handoff","workstreams":['
                      '{"key":"subagent_1","title":"Collect quote","role":"research_scout","stimulus":1.5,"depends_on":[],"peer_targets":[]}] }')
            elif prompt.startswith("Assigned Workstream:\nCollect quote"):
                seen.append(prompt)
                text=block
            elif "Tool Output:" in prompt and "COORDINATOR PLUGIN RESULT" in prompt:
                seen.append(prompt)
                text="Quote evidence received."
            else:
                text="Analysis complete."
            return {"ok":True,"text":text,"model":model,"provider":"gemini"}
        with tempfile.TemporaryDirectory() as tmp:
            runner=LiveSwarmRunner(workspace_root=tmp, artifacts_dir=Path(tmp)/"artifacts",
                                   llm_caller=caller,step_delay_sec=.01,auto_spindown=False,max_refinement_rounds=0)
            try:
                with patch("tools.coordinator_plugin_access.invoke",return_value={"ok":True,"bid":"42"}):
                    launched=runner.launch_mission("Collect a public quote",workers_per_coordinator=6,
                                                    plugin_capabilities=["kraken_console.quote"])
                    self.assertTrue(launched["ok"])
                    self.assertIsNotNone(runner.wait_for_mission(launched["mission_id"],timeout=15))
                self.assertTrue(any("Coordinator-permitted plugin" in x for x in seen),seen)
                self.assertTrue(any('"bid": "42"' in x for x in seen),seen)
            finally:
                runner.shutdown()

    def test_incompatible_launch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=LiveSwarmRunner(workspace_root=tmp,artifacts_dir=Path(tmp)/"artifacts")
            result=r.launch_mission("Quote",physical_input=True,plugin_capabilities=["kraken_console.quote"])
            self.assertFalse(result["ok"])
            self.assertIn("unavailable",result["error"])
            r.shutdown()

    def test_worker_parser_grant_and_denial(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=LiveSwarmRunner(workspace_root=tmp, artifacts_dir=Path(tmp)/"artifacts",auto_spindown=False)
            r._missions["m"]={"state":"ACTIVE (RUNNING)","goal":"quoted user goal"}
            r._workers["w"]={"mission_id":"m"}
            r._plugin_access["m"]=CoordinatorPluginAccess("m",self.scope)
            block='XXXtool:plugin_request\n{"plugin":"kraken_console","action":{"operation":"quote","pair":"BTCUSD"}}\nXXX'.replace("XXX",chr(96)*3)
            with patch("tools.coordinator_plugin_access.invoke",return_value={"ok":True,"bid":"42"}):
                found, output=r._execute_ide_tool_blocks(block,mission_id="m",worker_id="w",worker_name="worker",allow_web=False)
            self.assertTrue(found)
            self.assertIn('"bid": "42"',output)
            found, output=r._execute_ide_tool_blocks(block,mission_id="m",worker_id="other")
            self.assertTrue(found)
            self.assertIn("denied",output)
            r._workers.clear()
            r.shutdown()


if __name__=="__main__":unittest.main()

