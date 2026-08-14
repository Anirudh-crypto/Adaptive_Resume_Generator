from __future__ import annotations

import re

from google.genai import types

from app.config import Settings
from app.gemini import build_client
from app.models import CoverLetter, Resume

SYSTEM_PROMPT = """You are writing a cover letter for a job applicant, based on their resume (which
has already been tailored to this job description) and the job description itself.

Write 3 or 4 body paragraphs of flowing prose:
1. An opening that names the role (and the company, if the job description states one) and says in
   one or two sentences why this applicant is a strong fit.
2. One or two middle paragraphs connecting the applicant's actual experience, projects, and skills
   to the specific requirements in the job description. Draw a concrete line from a requirement in
   the job description to evidence that already exists in the resume.
3. A short closing paragraph expressing interest and inviting a conversation.

You must NEVER:
- Invent employers, job titles, dates, degrees, institutions, certifications, metrics, skills, or
  technologies that are not present in the resume given to you.
- Claim years of experience, seniority, or availability that the resume does not support.
- Claim a requirement is met when the resume shows no evidence for it. If the applicant does not
  meet some requirement, simply write about what they do have instead — never apologize for gaps,
  and never fabricate.
- Restate the resume as a list. This is prose, not bullet points.

Style rules:
- Plain, confident, specific. No flattery of the company, no clichés ("I am writing to express my
  keen interest", "team player", "fast-paced environment"), no em dashes.
- Each paragraph is plain text: no markdown, no bullet characters, no headings, no line breaks
  inside a paragraph.
- Never emit a placeholder like [Company Name], [Date], or [Your Name]. If a detail is not in the
  job description, write the sentence so it is not needed.
- Do not write the applicant's name, address, contact details, the date, or a signature line. Those
  are added automatically around what you write.

Fields:
- "company": the hiring company's name exactly as written in the job description, or "" if the job
  description does not name one.
- "role": the job title exactly as written in the job description, or "" if it does not state one.
- "salutation": address a named hiring manager if the job description names one, otherwise
  "Dear Hiring Manager,".
- "paragraphs": the body paragraphs, in order.
- "closing": a sign-off such as "Sincerely,".
"""

# Models occasionally fall back to mail-merge placeholders ("[Company Name]") despite the prompt.
# Rather than shipping a letter with visible brackets, drop the placeholder and tidy the spacing.
_PLACEHOLDER_PATTERN = re.compile(r"\[[^\[\]]{0,60}\]")


class CoverLetterError(Exception):
    pass


def _resume_digest(resume: Resume) -> str:
    """A compact plain-text view of the tailored resume — this is the only evidence the model is
    allowed to draw on, so it has to carry everything a cover letter might legitimately cite."""
    parts = [f"NAME: {resume.name}", f"SUMMARY: {resume.summary}"]

    if resume.experience:
        blocks = []
        for entry in resume.experience:
            header = f"{entry.title} at {entry.organization}"
            if entry.dates:
                header += f" ({entry.dates})"
            bullets = "\n".join(f"  - {b}" for b in entry.bullets)
            if entry.technologies:
                bullets += f"\n  Technologies: {', '.join(entry.technologies)}"
            blocks.append(f"{header}\n{bullets}")
        parts.append("EXPERIENCE:\n" + "\n\n".join(blocks))

    if resume.projects:
        blocks = []
        for project in resume.projects:
            bullets = "\n".join(f"  - {b}" for b in project.bullets)
            if project.technologies:
                bullets += f"\n  Technologies: {', '.join(project.technologies)}"
            blocks.append(f"{project.name}\n{bullets}")
        parts.append("PROJECTS:\n" + "\n\n".join(blocks))

    if resume.education:
        lines = [
            f"  - {e.degree}, {e.institution}" + (f" ({e.dates})" if e.dates else "")
            for e in resume.education
        ]
        parts.append("EDUCATION:\n" + "\n".join(lines))

    if resume.skill_categories:
        lines = [
            f"  - {c.category}: {', '.join(c.items)}" if c.category else f"  - {', '.join(c.items)}"
            for c in resume.skill_categories
        ]
        parts.append("SKILLS:\n" + "\n".join(lines))

    if resume.languages:
        parts.append(f"LANGUAGES: {', '.join(resume.languages)}")

    return "\n\n".join(parts)


def _build_prompt(resume: Resume, jd_text: str) -> str:
    return f"""JOB DESCRIPTION:
{jd_text}

APPLICANT'S RESUME (already tailored to this job description — the only facts you may use):
{_resume_digest(resume)}

Write the cover letter, following the rules given in the system instructions."""


def _clean_paragraph(text: str) -> str:
    text = _PLACEHOLDER_PATTERN.sub("", text)
    text = text.replace("\r\n", " ").replace("\n", " ")
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _clean(letter: CoverLetter) -> CoverLetter:
    paragraphs = [p for p in (_clean_paragraph(p) for p in letter.paragraphs) if p]
    if not paragraphs:
        raise CoverLetterError("Gemini returned a cover letter with no body paragraphs")

    salutation = _clean_paragraph(letter.salutation) or "Dear Hiring Manager,"
    closing = _clean_paragraph(letter.closing) or "Sincerely,"

    return letter.model_copy(
        update={
            "company": _clean_paragraph(letter.company),
            "role": _clean_paragraph(letter.role),
            "salutation": salutation,
            "paragraphs": paragraphs,
            "closing": closing,
        }
    )


def generate_cover_letter(resume: Resume, jd_text: str, settings: Settings) -> CoverLetter:
    client = build_client(settings)
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=_build_prompt(resume, jd_text),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=CoverLetter,
        ),
    )
    letter: CoverLetter | None = response.parsed
    if letter is None:
        raise CoverLetterError("Gemini response could not be parsed into the cover letter schema")
    return _clean(letter)
