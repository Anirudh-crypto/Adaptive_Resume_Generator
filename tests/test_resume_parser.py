from pathlib import Path

import pytest

from app.resume_parser import ResumeParseError, parse_resume, parse_resume_text, resume_to_markdown

FIXTURE = Path(__file__).parent / "fixtures" / "sample_resume.md"


def test_parses_name_and_contact():
    resume = parse_resume(FIXTURE)
    assert resume.name == "Jane Doe"
    assert resume.contact.email == "jane.doe@example.com"
    assert resume.contact.phone == "(555) 123-4567"
    assert resume.contact.location == "Seattle, WA"
    assert resume.contact.linkedin == "linkedin.com/in/janedoe"
    assert resume.contact.github == "github.com/janedoe"


def test_parses_summary():
    resume = parse_resume(FIXTURE)
    assert "distributed systems" in resume.summary


def test_parses_experience_entries():
    resume = parse_resume(FIXTURE)
    assert len(resume.experience) == 2
    first = resume.experience[0]
    assert first.title == "Senior Software Engineer"
    assert first.organization == "Acme Corp"
    assert first.location == "Seattle, WA"
    assert first.dates == "Jan 2021 - Present"
    assert len(first.bullets) == 2
    assert "p99 latency" in first.bullets[0]
    assert first.technologies == ["FastAPI", "PostgreSQL", "Docker"]

    second = resume.experience[1]
    assert second.technologies == []


def test_parses_education():
    resume = parse_resume(FIXTURE)
    assert len(resume.education) == 1
    edu = resume.education[0]
    assert edu.degree == "B.S. Computer Science"
    assert edu.institution == "University of Washington"
    assert edu.dates == "2018"


def test_parses_skill_categories():
    resume = parse_resume(FIXTURE)
    assert len(resume.skill_categories) == 2
    assert resume.skill_categories[0].category == "Programming Languages"
    assert resume.skill_categories[0].items == ["Python", "Go"]
    assert resume.skill_categories[1].category == "Data & Cloud"
    assert resume.skill_categories[1].items == ["PostgreSQL", "Docker", "AWS"]


def test_parses_projects():
    resume = parse_resume(FIXTURE)
    assert len(resume.projects) == 1
    project = resume.projects[0]
    assert project.name == "Resume Builder"
    assert project.link == "github.com/janedoe/resume-builder"
    assert len(project.bullets) == 1
    assert project.technologies == ["Python", "FastAPI"]


def test_parses_languages():
    resume = parse_resume(FIXTURE)
    assert resume.languages == ["English (Native)", "Spanish (B2)"]


def test_missing_name_heading_raises(tmp_path):
    bad_file = tmp_path / "bad.md"
    bad_file.write_text("Not a heading\n## Summary\nhi\n", encoding="utf-8")
    with pytest.raises(ResumeParseError):
        parse_resume(bad_file)


def test_missing_summary_raises(tmp_path):
    bad_file = tmp_path / "bad.md"
    bad_file.write_text("# Name\nEmail: a@b.com\n## Skills\nPython\n", encoding="utf-8")
    with pytest.raises(ResumeParseError):
        parse_resume(bad_file)


def test_resume_to_markdown_round_trip():
    resume = parse_resume(FIXTURE)
    markdown = resume_to_markdown(resume)
    reparsed = parse_resume_text(markdown)
    assert reparsed == resume


def test_flat_skills_fallback(tmp_path):
    resume_file = tmp_path / "resume.md"
    resume_file.write_text(
        "# Name\nEmail: a@b.com\n## Summary\nHi\n## Skills\nPython, Go, SQL\n",
        encoding="utf-8",
    )
    resume = parse_resume(resume_file)
    assert len(resume.skill_categories) == 1
    assert resume.skill_categories[0].category == ""
    assert resume.skill_categories[0].items == ["Python", "Go", "SQL"]
