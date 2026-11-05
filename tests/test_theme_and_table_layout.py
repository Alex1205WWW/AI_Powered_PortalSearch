"""Two styling facts that are easy to get wrong again, and invisible when they are.

Both were found by looking at a screenshot of the live panel on 2026-11-05, not by any check that reads
what the server sent - which is the only thing the rest of the suite can do.

1. Pico 2 renamed its colour variables. `--pico-primary` is the TEXT/link colour; a button is painted from
   `--pico-primary-background`. Mapping only the old name is silent: the stylesheet is valid, every test
   passes, and every primary button renders in pico's blue (#0172ad, measured) on a green panel.
2. The Mediadoras table has nine columns and no fixed layout, so the browser sized it to its content and
   it ran 264px past its container: "Estado" clipped, the last column off-screen.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = Path("app/web/static/app.css")
AGENCIES = Path("app/web/templates/agencies.html")

# The three blocks every theme token must be declared in, per the rule at the top of app.css: bare
# :root (light), the OS-preference block, and the explicit [data-theme="dark"] override - so the toggle
# wins in both directions.
THEME_BLOCKS = 3


@pytest.fixture(scope="module")
def css() -> str:
    return CSS.read_text(encoding="utf-8")


def test_buttons_are_painted_from_the_variable_pico_actually_uses(css):
    assert "--pico-primary-background" in css, (
        "pico 2 paints buttons from --pico-primary-background; --pico-primary alone leaves them blue")
    assert "--pico-primary-border" in css, "without the border the button keeps a blue outline"


def test_the_button_colour_is_declared_in_every_theme_block(css):
    """Declared once, the toggle would be right in one direction and wrong in the other."""
    for name in ("--pico-primary-background", "--pico-primary-hover-background"):
        found = len(re.findall(rf"{re.escape(name)}\s*:", css))
        assert found == THEME_BLOCKS, f"{name} is declared {found}x, expected {THEME_BLOCKS}"


def test_no_pico_colour_variable_is_hardcoded(css):
    """Every one of these must resolve to a theme token, or the two themes drift apart again."""
    for declaration in re.findall(r"--pico-primary[\w-]*\s*:\s*([^;]+);", css):
        assert declaration.strip().startswith("var(--"), f"hardcoded pico colour: {declaration.strip()}"


def test_the_agencies_table_declares_its_layout(css):
    """Nine columns without `table-layout: fixed` is what overflowed the container by 264px."""
    assert 'class="table-agencies"' in AGENCIES.read_text(encoding="utf-8")
    assert "table.table-agencies { table-layout: fixed" in css


def test_the_agency_column_widths_add_up(css):
    """They are percentages of a fixed layout: over 100 and the table overflows again, under and it
    leaves a gap at the right."""
    widths = re.findall(r"\.table-agencies th:nth-child\(\d\) \{ width: (\d+)%", css)
    assert len(widths) == 9, f"expected 9 column widths, found {len(widths)}"
    assert sum(int(w) for w in widths) == 100, f"widths sum to {sum(int(w) for w in widths)}%"


def test_the_action_button_is_allowed_to_shrink_inside_its_cell(css):
    """The last 26px of the overflow were not the columns: a "Desativar" button needed 133px in a 108px
    cell, and a button does not shrink to fit the way text does."""
    assert ".table-agencies td:last-child button" in css
