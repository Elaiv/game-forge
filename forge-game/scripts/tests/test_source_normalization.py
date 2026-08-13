from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from pypdf import PdfWriter

from forge_game_control.cli import main
from forge_game_control.errors import SourceNormalizationError
from forge_game_control.schemas import SchemaRegistry
from forge_game_control.source_diff import SourceDiffer
from forge_game_control.source_normalization import SourceBundleStore


def write_docx(path: Path) -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    document = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Roadmap</w:t></w:r></w:p>
    <w:p><w:r><w:t>Deliver one feature at a time.</w:t></w:r></w:p>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>ID</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Status</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
  </w:body>
</w:document>"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)


def source_inputs(gdd: Path, roadmap: Path) -> list[dict[str, str]]:
    return [
        {"source_id": "gdd-main", "role": "gdd", "path": str(gdd)},
        {"source_id": "roadmap-main", "role": "roadmap", "path": str(roadmap)},
    ]


class SourceNormalizationTests(unittest.TestCase):
    def test_normalizes_markdown_and_docx_reuses_cache_and_diffs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            gdd = root / "GDD.md"
            gdd.write_text(
                "# Game\r\n\r\nBuild a deterministic game.\r\n\r\n- Windows\r\n- Linux\r\n\r\n```text\r\nuntrusted command\r\n```\r\n",
                encoding="utf-8",
            )
            roadmap = root / "Roadmap.docx"
            write_docx(roadmap)
            schemas = SchemaRegistry()
            store = SourceBundleStore(schemas, root / "sources")
            manifest, first = store.normalize(
                "project-inputs",
                source_inputs(gdd, roadmap),
                normalized_at="2026-08-04T13:00:00Z",
                expected_previous_hash=None,
            )
            cached_manifest, cached = store.normalize(
                "project-inputs",
                source_inputs(gdd, roadmap),
                normalized_at="2026-08-04T13:00:01Z",
                expected_previous_hash=None,
            )
            _, documents, _ = store.read_normalized_sources(
                "project-inputs",
                revision=1,
            )
            gdd.write_text(
                "# Game\n\nBuild a deterministic cooperative game.\n\n- Windows\n- Linux\n",
                encoding="utf-8",
            )
            _, second = store.normalize(
                "project-inputs",
                source_inputs(gdd, roadmap),
                normalized_at="2026-08-04T13:00:02Z",
                expected_previous_hash=first.content_hash,
            )
            diff = SourceDiffer(schemas, store).compare(
                "project-inputs",
                1,
                "project-inputs",
                2,
                generated_at="2026-08-04T13:00:03Z",
            )
        self.assertEqual(manifest["revision"], 1)
        self.assertEqual(cached_manifest, manifest)
        self.assertTrue(cached.reused)
        self.assertEqual(first.content_hash, cached.content_hash)
        self.assertEqual(second.revision, 2)
        self.assertEqual(documents["gdd-main"]["status"], "valid")
        self.assertIn(
            "code",
            {fragment["kind"] for fragment in documents["gdd-main"]["fragments"]},
        )
        self.assertIn(
            "table",
            {
                fragment["kind"]
                for fragment in documents["roadmap-main"]["fragments"]
            },
        )
        self.assertGreater(diff["summary"]["changed"], 0)
        self.assertGreater(diff["summary"]["removed"], 0)

    def test_pdf_without_text_reports_needs_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            pdf = root / "Scanned.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            with pdf.open("wb") as stream:
                writer.write(stream)
            store = SourceBundleStore(SchemaRegistry(), root / "sources")
            manifest, _ = store.normalize(
                "pdf-input",
                [{"source_id": "gdd-pdf", "role": "gdd", "path": str(pdf)}],
                normalized_at="2026-08-04T13:00:00Z",
                expected_previous_hash=None,
            )
            _, documents, _ = store.read_normalized_sources("pdf-input")
        self.assertEqual(manifest["sources"][0]["normalized_source_ref"]["status"], "needs_ocr")
        self.assertEqual(documents["gdd-pdf"]["fragments"], [])
        self.assertEqual(documents["gdd-pdf"]["diagnostics"][0]["code"], "source.pdf_needs_ocr")

    def test_rejects_symlink_input_and_tampered_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "GDD.md"
            source.write_text("# Game\n", encoding="utf-8")
            symlink = root / "Linked.md"
            symlink.symlink_to(source)
            store = SourceBundleStore(SchemaRegistry(), root / "sources")
            with self.assertRaisesRegex(SourceNormalizationError, "symlink"):
                store.normalize(
                    "linked-input",
                    [{"source_id": "linked", "role": "gdd", "path": str(symlink)}],
                    normalized_at="2026-08-04T13:00:00Z",
                    expected_previous_hash=None,
                )
            _, reference = store.normalize(
                "safe-input",
                [{"source_id": "safe", "role": "gdd", "path": str(source)}],
                normalized_at="2026-08-04T13:00:01Z",
                expected_previous_hash=None,
            )
            fragment = next((Path(reference.path) / "sources" / "safe" / "fragments").iterdir())
            fragment.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(SourceNormalizationError, "mismatch"):
                store.read("safe-input")

    def test_cli_normalizes_with_one_machine_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "GDD.md"
            source.write_text("# Game\n\nRequirement.\n", encoding="utf-8")
            request = root / "request.json"
            request.write_text(
                json.dumps(
                    {
                        "project_root": str(root),
                        "store_root": str(root / ".forge-game/runtime/source-sets"),
                        "source_set_id": "cli-input",
                        "sources": [
                            {"source_id": "gdd", "role": "gdd", "path": str(source)}
                        ],
                        "normalized_at": "2026-08-04T13:00:00Z",
                        "expected_previous_hash": None,
                    }
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["source-normalize", "--request", str(request)])
        response = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(response["data"]["source_set"]["revision"], 1)


if __name__ == "__main__":
    unittest.main()
