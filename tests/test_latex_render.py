from pathlib import Path

from app.latex_render import latex_escape, render_resume_latex
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
