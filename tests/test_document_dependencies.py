import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import doc_extract


class DocumentDependencyTests(unittest.TestCase):
    def _fake_pypdf(self):
        pages = [
            types.SimpleNamespace(
                extract_text=lambda: "第一页\n第二行",
                images=[object(), object()],
            ),
            types.SimpleNamespace(
                extract_text=lambda: None,
                images=[object()],
            ),
        ]
        return types.SimpleNamespace(PdfReader=lambda _path: types.SimpleNamespace(pages=pages))

    def test_pdf_extraction_uses_pypdf_and_preserves_page_numbers(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle, \
                mock.patch.dict(sys.modules, {"pypdf": self._fake_pypdf()}):
            self.assertEqual(doc_extract.extract(handle.name), [
                {"page": 1, "text": "第一页"},
                {"page": 1, "text": "第二行"},
            ])

    def test_pdf_image_count_uses_pypdf_page_images(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle, \
                mock.patch.dict(sys.modules, {"pypdf": self._fake_pypdf()}):
            self.assertEqual(doc_extract.count_images(handle.name), 3)

    def test_runtime_sources_no_longer_import_pymupdf(self):
        sources = [
            ROOT / "scripts" / "doc_extract.py",
            ROOT / "scripts" / "setup_env.py",
            ROOT / "requirements.txt",
            ROOT / "requirements.lock.txt",
        ]
        text = "\n".join(path.read_text(encoding="utf-8") for path in sources)
        self.assertNotIn("import fitz", text)
        self.assertNotIn("PyMuPDF==", text)
        self.assertNotIn("pymupdf>=", text.lower())


if __name__ == "__main__":
    unittest.main()
