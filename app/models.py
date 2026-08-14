from __future__ import annotations

from pydantic import BaseModel, Field


class Contact(BaseModel):
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    linkedin: str | None = None
    github: str | None = None
    website: str | None = None
    other: list[str] = Field(default_factory=list)


class SkillCategory(BaseModel):
    category: str = ""
    items: list[str] = Field(default_factory=list)


class ExperienceEntry(BaseModel):
    title: str
    organization: str
    location: str = ""
    dates: str = ""
    bullets: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)


class EducationEntry(BaseModel):
    degree: str
    institution: str
    location: str = ""
    dates: str = ""


class ProjectEntry(BaseModel):
    name: str
    link: str = ""
    dates: str = ""
    bullets: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)


class Resume(BaseModel):
    name: str
    contact: Contact
    summary: str
    experience: list[ExperienceEntry] = Field(default_factory=list)
    education: list[EducationEntry] = Field(default_factory=list)
    skill_categories: list[SkillCategory] = Field(default_factory=list)
    projects: list[ProjectEntry] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)


class CoverLetter(BaseModel):
    """The written body of a cover letter. The applicant's name and contact details are never
    part of this model — they're taken from the saved resume when the .docx is rendered."""

    company: str = ""
    role: str = ""
    salutation: str = "Dear Hiring Manager,"
    paragraphs: list[str] = Field(default_factory=list)
    closing: str = "Sincerely,"
