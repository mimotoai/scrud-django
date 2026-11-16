"""The README's own-models router is the tested one (`tests/own_models.py`)."""

import difflib
import re
from pathlib import Path

MARKER = "ARTICLE_WORKFLOW = workflow_check("
FIRST_LINE = "from django.http import HttpResponse"

# The lines that legitimately differ, and nothing else may. The tested module
# takes the type's IRI from the test workflow defaults; the README, which has
# none, defines it where it is used (an IRI of the reader's own).
IN_THE_README_ONLY = ['ARTICLE = "myapp://Article"']
IN_THE_TEST_ONLY = ["from .workflow_defaults import ARTICLE"]


def _lines(text, start, drop):
    text = text[text.index(start) :]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return [line for line in lines if line not in drop]


def test_the_readme_own_model_router_is_the_tested_one():
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    blocks = re.findall(r"```python\n(.*?)```", readme, re.S)
    (block,) = [block for block in blocks if MARKER in block]
    source = (Path(__file__).parent / "own_models.py").read_text()
    shown = _lines(block, FIRST_LINE, IN_THE_README_ONLY)
    tested = _lines(source, FIRST_LINE, IN_THE_TEST_ONLY)
    diff = "\n".join(
        difflib.unified_diff(tested, shown, "own_models.py", "README", lineterm="")
    )
    assert shown == tested, diff
