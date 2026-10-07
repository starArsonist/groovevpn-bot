import shutil
import subprocess
from pathlib import Path

import pytest

PAGE_TESTS = Path(__file__).resolve().parent.parent / "web" / "connect" / "validate.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js не найден: проверьте страницу вручную по чек-листу web/connect/README.md")
def test_connect_page_logic_passes_node_tests():
    result = subprocess.run(
        ["node", "--test", str(PAGE_TESTS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )

    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]
