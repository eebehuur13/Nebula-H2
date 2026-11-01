"""The eval harness points at the layer where an answer goes missing."""

from __future__ import annotations

from helpers import drain, drive_file
from nebula import access, evaluate

ASKER = "eval-asker@example.org"


async def test_eval_reports_the_first_layer_that_misses(mcp):
    await access.ensure_person(ASKER)
    await drive_file("Eval handbook", "Teachers in Guntur meet every Saturday morning.", emails=[ASKER])
    await drive_file("Eval salaries", "Teacher salaries for Guntur, every Saturday paid.",
                     emails=["eval-other@example.org"])
    await drain()

    report = await evaluate.run([
        {"question": "When do teachers in Guntur meet?", "expect": {"title": "Eval handbook", "text": "every Saturday"},
         "must_not_see": ["Eval salaries"]},
        {"question": "When do teachers in Guntur meet?", "expect": {"title": "Eval handbook", "text": "every Sunday"}},
        {"question": "Where is the Mars office?", "expect": {"absent": True}},
        {"question": "Who wrote the Eval report?", "expect": {"title": "Eval report"}},
    ], ASKER)
    first, wrong_text, absent, missing = report["results"]
    assert first["result"] == "pass" and first["checks"]["access"] == "pass"
    assert first["checks"]["retrieval"].startswith("pass: rank")
    assert wrong_text["result"] == "fail" and wrong_text["first_miss"] == "extraction"
    assert absent["result"] == "review"
    assert missing["first_miss"] == "extraction" and "no file" in missing["checks"]["extraction"]
    assert report["summary"] == {"pass": 1, "fail": 2, "review": 1}
