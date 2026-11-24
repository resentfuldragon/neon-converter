# Third-party software

The Windows build of Neon Converter includes the programs and libraries below. Each keeps its own
license; the GPL/AGPL ones are redistributed unmodified, and their complete source code is available
from the links given.

| Component | License | Source |
|---|---|---|
| calibre (`_internal/engines/calibre`) | GPL-3.0 | https://download.calibre-ebook.com (exact version and tarball link in `engines/calibre/SOURCE.txt`) · https://github.com/kovidgoyal/calibre |
| FFmpeg (`_internal/bin/ffmpeg.exe`, gyan.dev "essentials" build via imageio-ffmpeg) | GPL-3.0 | https://ffmpeg.org/download.html · https://www.gyan.dev/ffmpeg/builds/ |
| Pandoc (`_internal/bin/pandoc.exe`, via pypandoc_binary) | GPL-2.0-or-later | https://github.com/jgm/pandoc |
| PyMuPDF / MuPDF | AGPL-3.0 | https://github.com/pymupdf/PyMuPDF |
| PyMuPDF4LLM | AGPL-3.0 | https://github.com/pymupdf/pymupdf4llm |
| mobi (KindleUnpack) | GPL-3.0 | https://github.com/iscc/mobi |
| pdf2docx | MIT | https://github.com/ArtifexSoftware/pdf2docx |
| python-docx | MIT | https://github.com/python-openxml/python-docx |
| CustomTkinter | MIT | https://github.com/TomSchimansky/CustomTkinter |
| tkinterdnd2 / tkdnd | MIT / BSD | https://github.com/Eliav2/tkinterdnd2 |
| Pillow | MIT-CMU | https://github.com/python-pillow/Pillow |
| pillow-heif / libheif | BSD-3-Clause / LGPL-3.0 | https://github.com/bigcat88/pillow_heif |
| OpenCV (headless), NumPy, fontTools, lxml | Apache-2.0 / BSD / MIT | https://pypi.org |
| Python | PSF License | https://www.python.org |

The optional `pymupdf-layout` add-on is deliberately **not** included, as it is not distributed under an
open-source license.
