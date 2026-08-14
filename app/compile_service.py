from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from app.config import Settings


class LatexCompileError(Exception):
    def __init__(self, message: str, log: str = ""):
        self.log = log
        super().__init__(message)


def compile_pdf(
    tex_source: str,
    settings: Settings,
    resources: dict[str, bytes] | None = None,
) -> bytes:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        tex_path = tmp / "main.tex"
        tex_path.write_text(tex_source, encoding="utf-8")

        for name, data in (resources or {}).items():
            (tmp / name).write_bytes(data)

        try:
            result = subprocess.run(
                [settings.tectonic_bin, "--untrusted", "-o", str(tmp), str(tex_path)],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            raise LatexCompileError("LaTeX compilation timed out") from exc
        except FileNotFoundError as exc:
            raise LatexCompileError(
                f"Could not find the `{settings.tectonic_bin}` binary — is Tectonic installed?"
            ) from exc

        pdf_path = tmp / "main.pdf"
        if result.returncode != 0 or not pdf_path.exists():
            log_tail = (result.stdout + result.stderr)[-4000:]
            raise LatexCompileError("LaTeX compilation failed", log=log_tail)

        return pdf_path.read_bytes()
