from __future__ import annotations

import logging
from pathlib import Path

from app.models import (
    Contact,
    EducationEntry,
    ExperienceEntry,
    ProjectEntry,
    Resume,
    SkillCategory,
)

logger = logging.getLogger(__name__)

SECTION_ALIASES: dict[str, str] = {
    "summary": "summary",
    "objective": "summary",
    "experience": "experience",
    "work experience": "experience",
    "professional experience": "experience",
    "education": "education",
    "skills": "skills",
    "technical skills": "skills",
    "projects": "projects",
    "personal projects": "projects",
    "languages": "languages",
}

CONTACT_KEYS = {"email", "phone", "location", "linkedin", "github", "website"}


class ResumeParseError(Exception):
    def __init__(self, message: str, line_number: int | None = None):
        self.line_number = line_number
        if line_number is not None:
            message = f"{message} (line {line_number})"
        super().__init__(message)


def parse_resume(path: Path) -> Resume:
    return parse_resume_text(Path(path).read_text(encoding="utf-8"))


def parse_resume_text(text: str) -> Resume:
    lines = text.splitlines()

    name, contact, body_start = _parse_header(lines)
    sections = _split_top_level_sections(lines, body_start)

    if "summary" not in sections:
        raise ResumeParseError("Missing required '## Summary' section")
    summary = "\n".join(sections["summary"]).strip()

    experience = (
        _parse_entries(sections["experience"], kind="experience")
        if "experience" in sections
        else []
    )
    education = (
        _parse_entries(sections["education"], kind="education")
        if "education" in sections
        else []
    )
    projects = (
        _parse_entries(sections["projects"], kind="project")
        if "projects" in sections
        else []
    )
    skill_categories = (
        _parse_skill_categories(sections["skills"]) if "skills" in sections else []
    )
    languages = _parse_list(sections["languages"]) if "languages" in sections else []

    return Resume(
        name=name,
        contact=contact,
        summary=summary,
        experience=[ExperienceEntry(**e) for e in experience],
        education=[EducationEntry(**e) for e in education],
        projects=[ProjectEntry(**e) for e in projects],
        skill_categories=[SkillCategory(**c) for c in skill_categories],
        languages=languages,
    )


def _parse_header(lines: list[str]) -> tuple[str, Contact, int]:
    name = None
    name_line_idx = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# ") and not stripped.startswith("## "):
            name = stripped[2:].strip()
            name_line_idx = idx
            break
        raise ResumeParseError(
            "Expected the file to start with a single '# Name' heading", idx + 1
        )

    if name is None:
        raise ResumeParseError("Missing '# Name' heading at top of file")

    contact_line = None
    contact_line_idx = None
    for idx in range(name_line_idx + 1, len(lines)):
        stripped = lines[idx].strip()
        if not stripped:
            continue
        if stripped.startswith("## "):
            break
        contact_line = stripped
        contact_line_idx = idx
        break

    if contact_line is None:
        raise ResumeParseError(
            "Missing contact line after name heading", name_line_idx + 1
        )

    contact = _parse_contact_line(contact_line, contact_line_idx + 1)
    return name, contact, contact_line_idx + 1


def _parse_contact_line(line: str, line_number: int) -> Contact:
    fields = {"other": []}
    for part in line.split("|"):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            fields["other"].append(part)
            continue
        key, _, value = part.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key in CONTACT_KEYS:
            fields[key] = value
        else:
            fields["other"].append(part)
    return Contact(**fields)


def _split_top_level_sections(lines: list[str], body_start: int) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current_key: str | None = None
    current_lines: list[str] = []

    def flush():
        if current_key is not None:
            sections.setdefault(current_key, [])
            sections[current_key].extend(current_lines)

    for idx in range(body_start, len(lines)):
        line = lines[idx]
        stripped = line.strip()
        if stripped.startswith("## "):
            flush()
            heading = stripped[3:].strip().lower()
            normalized = SECTION_ALIASES.get(heading)
            if normalized is None:
                logger.warning(
                    "Unknown section heading '%s' at line %d — skipping", heading, idx + 1
                )
            current_key = normalized
            current_lines = []
        else:
            current_lines.append(line)
    flush()
    return sections


def _parse_entries(lines: list[str], kind: str) -> list[dict]:
    entries: list[dict] = []
    current: dict | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("### "):
            if current is not None:
                entries.append(current)
            current = _parse_entry_header(stripped[4:].strip(), kind)
        elif stripped.startswith("- ") or stripped.startswith("* "):
            if current is not None:
                current["bullets"].append(stripped[2:].strip())
        elif stripped.lower().startswith("technologies:") and current is not None and "technologies" in current:
            tech_text = stripped.split(":", 1)[1]
            current["technologies"] = [t.strip() for t in tech_text.split(",") if t.strip()]
    if current is not None:
        entries.append(current)
    return entries


def _parse_entry_header(header: str, kind: str) -> dict:
    parts = [p.strip() for p in header.split("|")]
    while len(parts) < 4:
        parts.append("")

    if kind == "experience":
        return {
            "title": parts[0],
            "organization": parts[1],
            "location": parts[2],
            "dates": parts[3],
            "bullets": [],
            "technologies": [],
        }
    if kind == "education":
        return {
            "degree": parts[0],
            "institution": parts[1],
            "location": parts[2],
            "dates": parts[3],
            "bullets": [],
        }
    # project: Name | Link | Dates
    return {
        "name": parts[0],
        "link": parts[1],
        "dates": parts[2],
        "bullets": [],
        "technologies": [],
    }


def _parse_list(lines: list[str]) -> list[str]:
    items: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") or stripped.startswith("* "):
            items.append(stripped[2:].strip())
        else:
            items.extend(s.strip() for s in stripped.split(",") if s.strip())
    return items


def _parse_skill_categories(lines: list[str]) -> list[dict]:
    non_empty = [line.strip() for line in lines if line.strip()]
    if not non_empty:
        return []

    if any(":" in line for line in non_empty):
        categories: list[dict] = []
        for line in non_empty:
            stripped = line
            if stripped.startswith("- ") or stripped.startswith("* "):
                stripped = stripped[2:].strip()
            if ":" not in stripped:
                continue
            name, _, rest = stripped.partition(":")
            items = [s.strip() for s in rest.split(",") if s.strip()]
            categories.append({"category": name.strip(), "items": items})
        return categories

    return [{"category": "", "items": _parse_list(lines)}]


_CONTACT_LABELS = [
    ("email", "Email"),
    ("phone", "Phone"),
    ("location", "Location"),
    ("linkedin", "LinkedIn"),
    ("github", "GitHub"),
    ("website", "Website"),
]


def resume_to_markdown(resume: Resume) -> str:
    lines: list[str] = [f"# {resume.name}", "", _contact_line(resume.contact), ""]

    lines += ["## Summary", "", resume.summary.strip(), ""]

    if resume.skill_categories:
        lines.append("## Skills")
        lines.append("")
        for cat in resume.skill_categories:
            items = ", ".join(cat.items)
            if cat.category:
                lines.append(f"{cat.category}: {items}")
            else:
                lines.append(items)
        lines.append("")

    if resume.experience:
        lines.append("## Experience")
        lines.append("")
        for entry in resume.experience:
            lines.append(f"### {entry.title} | {entry.organization} | {entry.location} | {entry.dates}")
            lines.append("")
            for bullet in entry.bullets:
                lines.append(f"- {bullet}")
            if entry.technologies:
                lines.append("")
                lines.append(f"Technologies: {', '.join(entry.technologies)}")
            lines.append("")

    if resume.projects:
        lines.append("## Projects")
        lines.append("")
        for entry in resume.projects:
            lines.append(f"### {entry.name} | {entry.link} | {entry.dates}")
            lines.append("")
            for bullet in entry.bullets:
                lines.append(f"- {bullet}")
            if entry.technologies:
                lines.append("")
                lines.append(f"Technologies: {', '.join(entry.technologies)}")
            lines.append("")

    if resume.education:
        lines.append("## Education")
        lines.append("")
        for entry in resume.education:
            lines.append(f"### {entry.degree} | {entry.institution} | {entry.location} | {entry.dates}")
        lines.append("")

    if resume.languages:
        lines.append("## Languages")
        lines.append("")
        lines.append(", ".join(resume.languages))
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def _contact_line(contact: Contact) -> str:
    parts = []
    for field, label in _CONTACT_LABELS:
        value = getattr(contact, field)
        if value:
            parts.append(f"{label}: {value}")
    parts.extend(contact.other)
    return " | ".join(parts)
