# 受け入れ基準の grep 系(AC-17・AC-18)と、import 時に重い物を読まないこと。ソースの文字列を調べるだけ。
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "deskkit" / "modules" / "mojifix"


def _grep(pattern: str) -> list[str]:
    rx = re.compile(pattern)
    hits: list[str] = []
    for p in sorted(SRC.glob("*.py")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if rx.search(line):
                hits.append(f"{p.name}:{i}:{line.strip()}")
    return hits


def test_ac17_no_network_no_detectors() -> None:
    assert _grep(r"import socket|urllib|http\.client|requests|QNetwork|chardet|charset_normalizer|nkf") == []


def test_ac18_no_overwrite_move_extract_or_recycle() -> None:
    assert _grep(r"os\.replace|shutil\.(move|copy|copy2|copyfile)\(|\.extractall\(|\.extract\(|send2trash|recycle\(") == []


def test_ac18_removal_only_in_cleanup() -> None:
    hits = _grep(r"os\.remove|os\.unlink|\.unlink\(|shutil\.rmtree")
    assert [h.split(":")[0] for h in hits] == ["owned.py"]
    text = (SRC / "owned.py").read_text(encoding="utf-8")
    fn = text[text.index("def _remove_own_file"):text.index("def _excl_create")]
    assert "os.remove(p)" in fn and "FR-4 の後片づけ" in fn


def test_no_rename_outside_renamer_and_extract() -> None:
    hits = [h for h in _grep(r"os\.rename\(") if h.split(":")[0] not in ("renamer.py", "zipextract.py")]
    assert hits == []


def test_create_does_not_import_heavy_modules() -> None:
    code = ("import sys; import deskkit.modules.mojifix as m; "
            "print(any(k.startswith('deskkit.modules.mojifix.') for k in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         cwd=str(SRC.parents[2]))
    assert out.stdout.strip() == "False"
