import ast
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


import architecture_audit
import board_prompts
import storyboard


class OrchestrationBoundaryTests(unittest.TestCase):
    def _function_metric(self, module_name, function_name):
        path = SCRIPTS / (module_name + ".py")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        function = next(
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        )
        branches = sum(
            isinstance(node, architecture_audit.BRANCH_NODES)
            for node in ast.walk(function)
        )
        return function.end_lineno - function.lineno + 1, branches

    def test_former_monoliths_stay_below_complexity_limits(self):
        boundaries = (
            ("storyboard", "render_storyboard"),
            ("storyboard_orchestrator", "_render_final_storyboard_stage"),
            ("script_splitter", "split"),
            ("script_splitter", "_compile_split_segments"),
            ("video_engine", "_submit_batch_tasks"),
            ("video_engine", "_poll_batch_tasks"),
            ("workflow_canvas", "discover_canvas_inputs"),
            ("workflow_canvas", "render_html"),
            ("explainer_layout_preview", "render_execution_preview"),
        )
        for module_name, function_name in boundaries:
            with self.subTest(module=module_name, function=function_name):
                lines, branches = self._function_metric(module_name, function_name)
                self.assertLess(lines, architecture_audit.LARGE_FUNCTION_LINE_THRESHOLD)
                self.assertLess(branches, architecture_audit.LARGE_FUNCTION_BRANCH_THRESHOLD)

    def test_quality_debt_budgets_are_zero(self):
        report = architecture_audit.audit()
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["metrics"]["large_function_count"], 0)
        self.assertEqual(report["metrics"]["broad_exception_count"], 0)
        self.assertEqual(report["metrics"]["large_function_budget"], 0)
        self.assertEqual(report["metrics"]["broad_exception_budget"], 0)

    def test_cli_and_long_chain_adapters_are_physically_separate(self):
        expected = (
            "run_manifest_cli.py",
            "script_splitter_cli.py",
            "video_engine_cli.py",
            "video_chain_orchestrator.py",
            "storyboard_orchestrator.py",
            "board_prompts.py",
        )
        for name in expected:
            with self.subTest(module=name):
                self.assertTrue((SCRIPTS / name).is_file())

    def test_storyboard_prompt_policy_has_one_authoritative_implementation(self):
        plan = {
            "project_title": "unit",
            "characters": [],
            "shots": [{"id": "s1", "visual": "product close-up"}],
        }
        shot = plan["shots"][0]
        self.assertEqual(
            storyboard.shot_prompt(plan, shot, 1),
            board_prompts.shot_prompt(plan, shot, 1),
        )


if __name__ == "__main__":
    unittest.main()
