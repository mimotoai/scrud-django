"""The README's own-models router is the tested one (`tests/own_models.py`)."""

import re
from pathlib import Path

MARKER = "ARTICLE_WORKFLOW = workflow_check("


def _lines(text):
    return [line.strip() for line in text.splitlines() if line.strip()]


def test_the_readme_own_model_router_is_the_tested_one():
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    blocks = re.findall(r"```python\n(.*?)```", readme, re.S)
    (block,) = [block for block in blocks if MARKER in block]
    source = (Path(__file__).parent / "own_models.py").read_text()
    shown = block[block.index(MARKER) :]
    tested = source[source.index(MARKER) :]
    assert _lines(shown) == _lines(tested)
