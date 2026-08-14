import pytest

from app.compile_service import LatexCompileError, compile_pdf
from app.config import Settings

# Every test in this module shells out to the real Tectonic binary and, on a cold cache,
# downloads the LaTeX support bundle over the network. Deselect with `-m "not tectonic"`.
pytestmark = pytest.mark.tectonic


def test_compile_pdf_success():
    settings = Settings()
    tex = r"\documentclass{article}\begin{document}Hello Test\end{document}"
    pdf_bytes = compile_pdf(tex, settings)
    assert pdf_bytes[:4] == b"%PDF"


def test_compile_pdf_failure_raises_with_log():
    settings = Settings()
    tex = r"\documentclass{article}\begin{document}\undefinedcommand\end{document}"
    with pytest.raises(LatexCompileError) as exc_info:
        compile_pdf(tex, settings)
    assert "Undefined control sequence" in exc_info.value.log


def test_compile_pdf_with_resource(photo_jpeg_bytes):
    settings = Settings()
    tex = (
        r"\documentclass{article}\usepackage{graphicx}\begin{document}"
        r"\includegraphics[width=1cm]{photo.jpg}\end{document}"
    )
    pdf_bytes = compile_pdf(tex, settings, resources={"photo.jpg": photo_jpeg_bytes})
    assert pdf_bytes[:4] == b"%PDF"
