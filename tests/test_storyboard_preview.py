import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


import storyboard_preview


class StoryboardPreviewTests(unittest.TestCase):
    def _render_partial_cast_stage(self, storyboard_role, shots_marker):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            cast_board = out_dir / "cast_board.jpg"
            cast_board.write_bytes(b"\xff\xd8\xff\xd9")
            results = {
                "storyboard_role": storyboard_role,
                "cast_board": {"abspath": str(cast_board)},
            }
            if shots_marker is not ...:
                results["shots"] = shots_marker

            paths = storyboard_preview.write_index(
                {"project_title": "Partial Cast Stage"},
                results,
                str(out_dir),
                normalize_panel_plan_fn=lambda _shot: [],
            )
            markdown = Path(paths[0]).read_text(encoding="utf-8")
            self.assertTrue(all(Path(path).is_file() for path in paths))
            return markdown

    def test_partial_cast_stage_without_shots_writes_design_board_previews(self):
        markdown = self._render_partial_cast_stage("design_board", ...)
        self.assertIn("客户确认执行版式板：0 张", markdown)

    def test_partial_cast_stage_with_null_shots_writes_storyboard_previews(self):
        markdown = self._render_partial_cast_stage("video_storyboard", None)
        self.assertIn("分段故事板：0 张", markdown)


if __name__ == "__main__":
    unittest.main()
