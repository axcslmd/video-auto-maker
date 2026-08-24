import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import license_compliance  # noqa: E402
import media_tools  # noqa: E402
import package_delivery  # noqa: E402


class LicenseComplianceTests(unittest.TestCase):
    def test_repository_passes_license_audit(self):
        result = license_compliance.audit(ROOT)
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["commercial_contact"], "359971043@qq.com")

    def test_external_ffmpeg_directory_is_supported(self):
        with tempfile.TemporaryDirectory() as temp:
            suffix = ".exe" if os.name == "nt" else ""
            ffmpeg = Path(temp) / ("ffmpeg" + suffix)
            ffprobe = Path(temp) / ("ffprobe" + suffix)
            ffmpeg.touch()
            ffprobe.touch()
            with mock.patch.dict(os.environ, {
                    "BASICROUTER_FFMPEG_DIR": temp,
                    "PATH": "",
            }, clear=False):
                self.assertEqual(
                    media_tools.resolve_ffmpeg_pair(allow_fetch=True),
                    (str(ffmpeg), str(ffprobe)),
                )

    def test_bundled_ffmpeg_is_never_resolved(self):
        self.assertEqual(media_tools.bundled_ffmpeg_paths(), (None, None))

    def test_community_builder_rejects_local_runtime(self):
        with self.assertRaisesRegex(RuntimeError, "LOCAL_RUNTIME_REDISTRIBUTION_DISABLED"):
            package_delivery.build("unused", include_local_runtime=True)


if __name__ == "__main__":
    unittest.main()
