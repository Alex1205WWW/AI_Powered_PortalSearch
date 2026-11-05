"""Deliverable 16 is "manual em português com imagens" - so the images have to be there, and be hers.

The manual spent two weeks saying the screenshots were still to be taken. They were taken from the real
panel on 2026-11-05 (`scripts/capture_manual_images.py`), and this is what stops the deliverable from
regressing quietly: a renamed file or a moved folder turns every picture in the manual into a broken link,
which is not something any other test would notice.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

MANUAL = Path("docs/manual_pt/manual.md")
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


@pytest.fixture(scope="module")
def references() -> list[tuple[str, str]]:
    return IMAGE_RE.findall(MANUAL.read_text(encoding="utf-8"))


def test_the_manual_still_illustrates_the_pages_she_uses(references):
    """One image per page she is told to open. Fewer than this and the manual is back to being text."""
    targets = {Path(src).name for _, src in references}
    assert {"painel.png", "imoveis-filtros.png", "mediadoras.png", "configuracoes.png"} <= targets


def test_every_image_the_manual_points_at_exists(references):
    assert references, "the manual has no images at all"
    for alt, src in references:
        path = MANUAL.parent / src
        assert path.is_file(), f"{src} is referenced by the manual and not on disk"
        assert path.stat().st_size > 5_000, f"{src} is too small to be a real screenshot"
        assert alt.strip(), f"{src} has no caption; she reads these to know which page she is looking at"


def test_the_images_are_real_png_files(references):
    """A zero-byte or half-written capture still satisfies "the file exists"."""
    for _, src in references:
        head = (MANUAL.parent / src).read_bytes()[:8]
        assert head == b"\x89PNG\r\n\x1a\n", f"{src} is not a PNG"


def test_the_manual_no_longer_promises_images_that_now_exist():
    """The old text said the screenshots were "por recolher". Leaving that in beside real screenshots
    would read as an unfinished deliverable."""
    text = MANUAL.read_text(encoding="utf-8")
    assert "por recolher" not in text
    assert "images/" in text
