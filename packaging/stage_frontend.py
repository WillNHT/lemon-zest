"""Stage the interface's assets for packaging, stamping the version in.

The frontend is hand-written HTML, CSS and JS with no bundler, so "building"
it means copying it somewhere clean and filling in the one thing the source
cannot know: which version is running. The header's version chip is the
placeholder; everything else is copied byte for byte.

Called as: stage_frontend.py <source-web-dir> <out-dir> <version>
"""
import io
import os
import re
import shutil
import sys

STAMPED = "index.html"
VERSION_CHIP = re.compile(r'(<span class="ver" id="ver">)([^<]*)(</span>)')


def stage(src, out, version):
    if os.path.isdir(out):
        shutil.rmtree(out)
    shutil.copytree(src, out)

    target = os.path.join(out, STAMPED)
    if os.path.isfile(target):
        html = io.open(target, encoding="utf-8").read()
        html, hits = VERSION_CHIP.subn(r"\g<1>%s\g<3>" % version, html)
        if not hits:
            print(f"warning: no version chip in {STAMPED}", file=sys.stderr)
        io.open(target, "w", encoding="utf-8", newline="\n").write(html)

    count = sum(len(files) for _, _, files in os.walk(out))
    print(f"staged {count} frontend files -> {out}")


if __name__ == "__main__":
    stage(sys.argv[1], sys.argv[2], sys.argv[3])
