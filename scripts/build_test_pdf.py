"""Generate the test PDF used by PDFDetector.

Run once after editing the magic string or layout:

    ./venv/bin/python scripts/build_test_pdf.py

The output is committed under src/relay_detector/data/test_document.pdf so the
package data is shipped together with the wheel.
"""

from __future__ import annotations

import sys
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

# Keep this in sync with relay_detector.detectors.pdf.MAGIC.
MAGIC = "MAGIC-7F3K-VERIFY-CLAUDE-RELAY-DETECTOR"


def build(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(out_path), pagesize=letter)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(72, 720, "TEST DOCUMENT")
    c.setFont("Helvetica", 12)
    c.drawString(72, 690, "This PDF is the test fixture for the Claude relay-station detector.")
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, 640, f"Magic identifier: {MAGIC}")
    c.setFont("Helvetica", 11)
    c.drawString(72, 600, "If a model can read the magic identifier above, the relay correctly")
    c.drawString(72, 585, "forwarded the document content to a model with PDF/vision capability.")
    c.drawString(72, 555, "If the relay strips multimodal content or returns an error, this test")
    c.drawString(72, 540, "fails — flagging an aleat against the upstream model promised by the relay.")
    c.showPage()
    c.save()


def main() -> int:
    here = Path(__file__).resolve().parent
    out = here.parent / "src" / "relay_detector" / "data" / "test_document.pdf"
    build(out)
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
