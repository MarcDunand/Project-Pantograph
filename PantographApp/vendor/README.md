# Vendored: the AxiDraw Python API (pyaxidraw) 3.9.6

Source: `https://cdn.evilmadscientist.com/dl/ad/public/AxiDraw_API.zip`,
downloaded 2026-09-19. It contained `AxiDraw_API_396`, dated 2023-12-12.
Copyright Windell H. Oskay / Evil Mad Scientist Laboratories, licensed
**GPL-2.0-or-later**. The licence text is in `AxiDraw_API_396/pyaxidraw/LICENSE.txt`,
and the headers are in every source file.

**Why it's vendored:** that URL always serves the latest version, so a lockfile
hash would break the moment a new version comes out, and every new install
would fail. A copy here pins it.

**How it differs from the download** (the files themselves are unmodified):
- Only the parts needed to install and run were kept (`pyaxidraw/`, `axicli/`,
  `axicli.py`, `setup.py`, `pyproject.toml`, `requirements/`, `README.txt`,
  `Installation.txt`). The documentation, examples and tests were left out.
- The zip bundled a pre-built `axidrawinternal` wheel in
  `prebuilt_dependencies/`. It now sits here on its own
  (`axidrawinternal-3.9.6-py2.py3-none-any.whl`, also GPL-2.0-or-later).
  With it inside the package folder, the package's `setup.py` added it as a
  dependency by *absolute temporary path*, which leaked a path from one
  machine's cache into `uv.lock` and broke installs everywhere else.
  `pyproject.toml` at the repo root now lists both pieces separately.

**The names:** the distribution is `axicli`, and it provides the `pyaxidraw`
module (`from pyaxidraw import axidraw`).

**To upgrade:** download the new zip, repeat the two steps above, update the
version in the file and folder names and in `pyproject.toml`, run `uv lock`,
and test on the AxiDraw.
