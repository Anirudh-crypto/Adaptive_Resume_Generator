from __future__ import annotations

import re
from pathlib import Path

import jinja2

from app.models import Contact, Resume

TEMPLATE_DIR = Path(__file__).parent.parent / "latex_templates"
TEMPLATE_NAME = "resume.tex.jinja"
PHOTO_RESOURCE_NAME = "pic.JPG"

_ESCAPE_MAP = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "$": r"\$",
    "&": r"\&",
    "#": r"\#",
    "_": r"\_",
    "%": r"\%",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_ESCAPE_PATTERN = re.compile("|".join(re.escape(ch) for ch in _ESCAPE_MAP))


def latex_escape(text: object) -> str:
    text = "" if text is None else str(text)
    return _ESCAPE_PATTERN.sub(lambda m: _ESCAPE_MAP[m.group()], text)


def _with_scheme(url: str) -> str:
    return url if re.match(r"^[a-zA-Z]+://", url) else f"https://{url}"


def _build_header_fields(contact: Contact) -> dict[str, str]:
    location = latex_escape(contact.location) if contact.location else ""

    line2_parts = []
    if contact.phone:
        line2_parts.append(latex_escape(contact.phone))
    if contact.email:
        line2_parts.append(f"\\href{{mailto:{contact.email}}}{{{latex_escape(contact.email)}}}")
    line2 = " \\quad | \\quad ".join(line2_parts)

    linkedin = ""
    if contact.linkedin:
        url = _with_scheme(contact.linkedin)
        linkedin = f"\\href{{{url}}}{{{latex_escape(contact.linkedin)}}}"

    website = ""
    if contact.website:
        url = _with_scheme(contact.website)
        website = f"\\href{{{url}}}{{{latex_escape(contact.website)}}}"

    return {
        "header_location": location,
        "header_line2": line2,
        "header_linkedin": linkedin,
        "header_website": website,
    }


def _build_env() -> jinja2.Environment:
    env = jinja2.Environment(
        block_start_string="\\BLOCK{",
        block_end_string="}",
        variable_start_string="\\VAR{",
        variable_end_string="}",
        comment_start_string="\\#{",
        comment_end_string="}",
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
        loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)),
    )
    env.filters["latex_escape"] = latex_escape
    return env


_ENV = _build_env()


def render_resume_latex(resume: Resume, has_photo: bool = False) -> str:
    template = _ENV.get_template(TEMPLATE_NAME)

    projects = []
    for entry in resume.projects:
        data = entry.model_dump()
        if data.get("link"):
            data["link"] = _with_scheme(data["link"])
        projects.append(data)

    languages_line = " \\quad | \\quad ".join(latex_escape(lang) for lang in resume.languages)

    return template.render(
        name=resume.name,
        has_photo=has_photo,
        summary=resume.summary,
        skill_categories=[c.model_dump() for c in resume.skill_categories],
        experience=[e.model_dump() for e in resume.experience],
        projects=projects,
        education=[e.model_dump() for e in resume.education],
        languages_line=languages_line,
        **_build_header_fields(resume.contact),
    )
