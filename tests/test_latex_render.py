from pathlib import Path

import pytest

from app.latex_render import LAYOUTS, latex_escape, render_resume_latex
from app.resume_parser import parse_resume

FIXTURE = Path(__file__).parent / "fixtures" / "sample_resume.md"


def test_latex_escape_individual_chars():
    assert latex_escape("100% & $10k #1 _score_ ~tilde^caret\\slash") == (
        r"100\% \& \$10k \#1 \_score\_ \textasciitilde{}tilde"
        r"\textasciicircum{}caret\textbackslash{}slash"
    )


def test_latex_escape_combined_case():
    assert latex_escape("50% increase & $10k saved") == r"50\% increase \& \$10k saved"


def test_latex_escape_none_returns_empty():
    assert latex_escape(None) == ""


def test_render_resume_produces_valid_looking_tex():
    resume = parse_resume(FIXTURE)
    tex = render_resume_latex(resume)
    assert "\\documentclass" in tex
    assert "\\begin{document}" in tex
    assert "\\end{document}" in tex
    assert "Jane Doe" in tex
    assert "p99 latency by 30\\%" in tex  # the fixture's literal '%' must be escaped, not left raw
    assert "Programming Languages" in tex
    assert "Technologies: FastAPI, PostgreSQL, Docker" in tex


def test_render_without_photo_omits_includegraphics():
    resume = parse_resume(FIXTURE)
    tex = render_resume_latex(resume, has_photo=False)
    assert "\\includegraphics" not in tex


def test_render_with_photo_includes_includegraphics():
    resume = parse_resume(FIXTURE)
    tex = render_resume_latex(resume, has_photo=True)
    assert "\\includegraphics" in tex
    assert "pic.JPG" in tex


# --- regional layouts ------------------------------------------------------------------------


@pytest.mark.parametrize("region", sorted(LAYOUTS))
def test_every_layout_renders_the_fixture(region):
    resume = parse_resume(FIXTURE)
    tex = render_resume_latex(resume, region=region)
    assert "\\documentclass" in tex
    assert "\\end{document}" in tex
    assert "Jane Doe" in tex
    # The escaping contract holds in every layout, not just the default one.
    assert "p99 latency by 30\\%" in tex


def test_indian_layout_never_includes_a_photo():
    """has_photo is forced True here on purpose: the guarantee has to come from the template
    itself, not from the caller remembering to pass has_photo=False."""
    resume = parse_resume(FIXTURE)
    tex = render_resume_latex(resume, has_photo=True, region="india")
    assert "\\includegraphics" not in tex
    assert "pic.JPG" not in tex


def test_layouts_declare_whether_they_support_a_photo():
    assert LAYOUTS["germany"].supports_photo is True
    assert LAYOUTS["india"].supports_photo is False


@pytest.mark.parametrize("region", ["france", "", "resume.tex.jinja", "../../etc/passwd"])
def test_unknown_region_is_rejected_before_touching_the_filesystem(region):
    """The Jinja loader is pointed at the whole latex_templates/ directory, so resolving a
    caller-supplied name would be an arbitrary file read."""
    resume = parse_resume(FIXTURE)
    with pytest.raises(ValueError, match="Unknown resume region"):
        render_resume_latex(resume, region=region)
