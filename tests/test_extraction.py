"""What text extraction misses: pictures and scans, and Claude looking at them."""

from __future__ import annotations

import base64
import io

from helpers import MEMBER, OUTSIDER, call, drain, drive_file, raw_call
from nebula.ingest import images, parse
from nebula.ingest.quality import assess


def _png(w: int = 800, h: int = 600) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (w, h), (40, 120, 200))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _docx(build) -> bytes:
    import docx

    d = docx.Document()
    build(d)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _pdf_with_picture_page() -> bytes:
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    from PIL import Image

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 750, "Centre visits in Guntur: the field team visited every centre in the district this quarter "
                          "and recorded attendance, nutrition and the state of each building in detail. " * 3)
    c.showPage()
    c.drawString(72, 750, "Attendance chart")
    c.drawImage(ImageReader(Image.open(io.BytesIO(_png()))), 72, 300, 400, 300)
    c.showPage()
    c.save()
    return buf.getvalue()


def _gaps(doc) -> dict[str, str]:
    return {g["part"]: g["state"] for g in assess(doc)["gaps"]}


def test_google_doc_images_are_numbered_and_can_be_shown():
    png = base64.b64encode(_png(400, 300)).decode()
    md = (f"# Field visit\n\nWe visited the centre.\n\n![][image1]\n\nAttendance below.\n\n"
          f"[image1]: <data:image/png;base64,{png}>\n")
    doc = parse(md.encode(), filename="Field visit.md")
    assert [b.locator for b in doc.blocks if b.kind == "image"] == [{"image": 1}]
    assert _gaps(doc) == {"image:1": "uncertain"}
    pictures = images.render_part(md.encode(), "Field visit.md", "image:1")
    assert len(pictures) == 1 and pictures[0][1] == "png" and pictures[0][0][:4] == b"\x89PNG"


def test_word_images_small_logos_are_not_gaps():
    from docx.shared import Inches

    def build(d):
        d.add_picture(io.BytesIO(_png(100, 100)), width=Inches(0.6))   # a logo
        d.add_paragraph("Monthly report")
        d.add_picture(io.BytesIO(_png()), width=Inches(5))              # a chart
    data = _docx(build)
    doc = parse(data, filename="monthly.docx")
    assert _gaps(doc) == {"image:2": "uncertain"}
    assert images.render_part(data, "monthly.docx", "image:2")[0][1] == "png"


def test_pdf_picture_pages_and_slides_are_flagged():
    doc = parse(_pdf_with_picture_page(), filename="visits.pdf")
    assert _gaps(doc) == {"page:2": "needs_extraction"}
    page = images.render_part(_pdf_with_picture_page(), "visits.pdf", "page:2")
    assert page[0][1] == "png" and len(page[0][0]) > 1000

    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[5])
    s1.shapes.title.text = "Enrolment by district"
    s1.shapes.add_picture(io.BytesIO(_png()), Inches(1), Inches(1.5), Inches(8), Inches(5))
    buf = io.BytesIO()
    prs.save(buf)
    deck = parse(buf.getvalue(), filename="review.pptx")
    assert _gaps(deck) == {"slide:1": "needs_extraction"}
    assert images.render_part(buf.getvalue(), "review.pptx", "slide:1")[0][1] == "png"


async def test_claude_can_look_at_a_picture_page(mcp, monkeypatch):
    """Nebula keeps no copy of the file, so showing a page as a picture downloads it from Drive."""
    from nebula import sync
    from nebula.connectors import gdrive

    pdf = _pdf_with_picture_page()
    item_id = await drive_file("Guntur visits", data=pdf, filename="visits.pdf",
                               mime="application/pdf", kind="pdf", emails=[MEMBER], external_id="visits-pdf")
    await drain()
    item = f"item:{item_id}"
    downloads = []

    class FakeDrive:
        def __init__(self, config):
            pass

        async def download(self, file_id):
            downloads.append(file_id)
            meta = {"id": file_id, "name": "visits.pdf", "mimeType": "application/pdf", "size": str(len(pdf))}
            return meta, gdrive.classify(meta), "application/pdf", pdf

        async def close(self):
            return None

    monkeypatch.setattr(sync, "get_connector", lambda kind, config: FakeDrive(config))
    monkeypatch.setattr(gdrive, "service_account_email", lambda: "nebula-drive@example.iam.gserviceaccount.com")

    r = await call(mcp, MEMBER, "open", id=item)
    ext = r["extraction"]
    assert ext["status"] == "needs_extraction" and [g["part"] for g in ext["parts_not_read"]] == ["page:2"]
    assert "as_image" in ext["how_to_fix"]

    pic = await raw_call(mcp, MEMBER, "open", id=item, part="page:2", as_image=True)
    assert not pic.is_error and [c.type for c in pic.content] == ["text", "image"]
    assert pic.content[1].mime_type == "image/png"
    assert downloads == ["visits-pdf"]
    assert (await raw_call(mcp, OUTSIDER, "open", id=item, part="page:2", as_image=True)).is_error
    assert downloads == ["visits-pdf"]  # access is checked before anything is downloaded

    st = await call(mcp, MEMBER, "sources", action="status", id=item)
    assert st["extraction"]["parts_not_read"] == 1
