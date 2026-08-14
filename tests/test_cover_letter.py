from __future__ import annotations

from pathlib import Path

import pytest

from app.cover_letter import CoverLetterError, _build_prompt, _clean, _resume_digest
from app.models import CoverLetter
from app.resume_parser import parse_resume

FIXTURE = Path(__file__).parent / "fixtures" / "sample_resume.md"


def _letter(**overrides) -> CoverLetter:
    base = {
        "company": "Globex",
        "role": "Staff Backend Engineer",
        "salutation": "Dear Hiring Manager,",
        "paragraphs": ["First paragraph.", "Second paragraph."],
        "closing": "Sincerely,",
    }
    return CoverLetter(**{**base, **overrides})


def test_digest_carries_the_evidence_a_letter_may_cite():
    digest = _resume_digest(parse_resume(FIXTURE))

    assert "Senior Software Engineer at Acme Corp (Jan 2021 - Present)" in digest
    assert "improving p99 latency by 30%" in digest
    assert "B.S. Computer Science, University of Washington" in digest
    assert "Programming Languages: Python, Go" in digest
    assert "Resume Builder" in digest
    assert "English (Native), Spanish (B2)" in digest


def test_prompt_includes_the_job_description():
    prompt = _build_prompt(parse_resume(FIXTURE), "We need a Go engineer who knows Kubernetes.")
    assert "We need a Go engineer who knows Kubernetes." in prompt


def test_clean_strips_unresolved_placeholders():
    cleaned = _clean(
        _letter(paragraphs=["I am excited to join [Company Name] as an engineer."])
    )
    assert cleaned.paragraphs == ["I am excited to join as an engineer."]


def test_clean_flattens_line_breaks_within_a_paragraph():
    cleaned = _clean(_letter(paragraphs=["First line\nsecond   line."]))
    assert cleaned.paragraphs == ["First line second line."]


def test_clean_drops_empty_paragraphs():
    cleaned = _clean(_letter(paragraphs=["Real content.", "   ", "[Placeholder]"]))
    assert cleaned.paragraphs == ["Real content."]


def test_clean_falls_back_to_defaults_for_blank_salutation_and_closing():
    cleaned = _clean(_letter(salutation="  ", closing=""))
    assert cleaned.salutation == "Dear Hiring Manager,"
    assert cleaned.closing == "Sincerely,"


def test_clean_rejects_a_letter_with_no_body():
    with pytest.raises(CoverLetterError):
        _clean(_letter(paragraphs=["", "[Company Name]"]))
