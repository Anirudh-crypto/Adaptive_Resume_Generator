from __future__ import annotations

from google import genai
from google.genai import types
from pydantic import BaseModel

from app.config import Settings
from app.models import Resume

SYSTEM_PROMPT = """You are helping a job applicant tailor their resume to a specific job description.

You may ONLY do the following:
- Rewrite the professional summary to emphasize relevant experience for this job.
- Reorder, select a subset of, and lightly rephrase the bullet points already present under each
  experience entry, to emphasize what matters most for this job description.
- Within each skill category, reorder and select a subset of the skills already present.

You must NEVER:
- Invent employers, job titles, dates, locations, degrees, or institutions.
- Invent skill categories, skills, technologies, or tools not already present in the source resume.
- Move a skill into a different category than the one it was given in.
- Change the underlying facts of what was accomplished — only rephrase for emphasis and clarity.

For each experience entry you return, echo back the "organization" field EXACTLY as given in the
input (same spelling/casing), so it can be matched back to the original entry. Return exactly one
tailored entry per experience entry given, in the same order.

For each skill category you return, echo back the "category" field EXACTLY as given in the input,
so it can be matched back. Return exactly one entry per category given, in the same order.
"""


class TailoredExperienceEntry(BaseModel):
    organization: str
    bullets: list[str]


class TailoredSkillCategory(BaseModel):
    category: str
    items: list[str]


class TailoredResumeContent(BaseModel):
    summary: str
    experience: list[TailoredExperienceEntry]
    skill_categories: list[TailoredSkillCategory]


class TailoringMismatchError(Exception):
    pass


def _build_prompt(resume: Resume, jd_text: str) -> str:
    experience_blocks = []
    for entry in resume.experience:
        bullets = "\n".join(f"  - {b}" for b in entry.bullets)
        experience_blocks.append(f"organization: {entry.organization}\nbullets:\n{bullets}")
    experience_block = "\n\n".join(experience_blocks)

    skills_blocks = []
    for cat in resume.skill_categories:
        skills_blocks.append(f"category: {cat.category}\nitems: {', '.join(cat.items)}")
    skills_block = "\n\n".join(skills_blocks)

    return f"""JOB DESCRIPTION:
{jd_text}

CURRENT RESUME SUMMARY:
{resume.summary}

CURRENT EXPERIENCE (organization + bullets only — titles/dates/locations are fixed and not shown):
{experience_block}

CURRENT SKILLS (grouped by category):
{skills_block}

Tailor the summary, experience bullets, and per-category skill selection to this job description,
following the rules given in the system instructions."""


def tailor_resume(resume: Resume, jd_text: str, settings: Settings) -> Resume:
    client = genai.Client(api_key=settings.gemini_api_key)
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=_build_prompt(resume, jd_text),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=TailoredResumeContent,
        ),
    )
    tailored: TailoredResumeContent = response.parsed
    if tailored is None:
        raise TailoringMismatchError("Gemini response could not be parsed into the expected schema")
    return _merge_tailored(resume, tailored)


def _merge_tailored(original: Resume, tailored: TailoredResumeContent) -> Resume:
    if len(tailored.experience) != len(original.experience):
        raise TailoringMismatchError(
            f"Tailored response had {len(tailored.experience)} experience entries, "
            f"expected {len(original.experience)}"
        )
    if len(tailored.skill_categories) != len(original.skill_categories):
        raise TailoringMismatchError(
            f"Tailored response had {len(tailored.skill_categories)} skill categories, "
            f"expected {len(original.skill_categories)}"
        )

    by_org = {e.organization.strip().lower(): e for e in original.experience}
    merged_experience = []
    for i, tailored_entry in enumerate(tailored.experience):
        key = tailored_entry.organization.strip().lower()
        source = by_org.get(key, original.experience[i])
        merged_experience.append(source.model_copy(update={"bullets": tailored_entry.bullets}))

    by_category = {c.category.strip().lower(): c for c in original.skill_categories}
    merged_skill_categories = []
    for i, tailored_cat in enumerate(tailored.skill_categories):
        key = tailored_cat.category.strip().lower()
        source = by_category.get(key, original.skill_categories[i])
        source_items_lower = {s.strip().lower(): s for s in source.items}
        filtered_items = []
        for item in tailored_cat.items:
            canonical = source_items_lower.get(item.strip().lower())
            if canonical is not None and canonical not in filtered_items:
                filtered_items.append(canonical)
        if not filtered_items:
            filtered_items = list(source.items)
        merged_skill_categories.append(source.model_copy(update={"items": filtered_items}))

    return original.model_copy(
        update={
            "summary": tailored.summary,
            "experience": merged_experience,
            "skill_categories": merged_skill_categories,
        }
    )
