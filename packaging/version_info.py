"""Generate the Windows version resource for the packaged executable.

This is what the Details tab of the file's properties shows, and what
SmartScreen quotes back to someone who downloads the binary. Without it the
.exe is anonymous, which is the worst thing an unsigned download can be.

Called by the spec file, and standalone as: version_info.py <version> <out>
"""
import io
import sys

TEMPLATE = """\
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({major}, {minor}, {patch}, 0),
    prodvers=({major}, {minor}, {patch}, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', 'Lemon Zest'),
         StringStruct('FileDescription', 'Sync a local music library to portable players'),
         StringStruct('FileVersion', '{version}'),
         StringStruct('InternalName', 'lemon-zest'),
         StringStruct('OriginalFilename', 'lemon-zest.exe'),
         StringStruct('ProductName', 'Lemon Zest'),
         StringStruct('ProductVersion', '{version}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def write(version, out):
    # A prerelease or build suffix is not a number; the resource fields are.
    numeric = version.split("-")[0].split("+")[0]
    parts = (numeric.split(".") + ["0", "0", "0"])[:3]
    major, minor, patch = (int(p) if p.isdigit() else 0 for p in parts)
    text = TEMPLATE.format(major=major, minor=minor, patch=patch,
                           version=version)
    io.open(out, "w", encoding="utf-8", newline="\n").write(text)
    return out


if __name__ == "__main__":
    print(write(sys.argv[1], sys.argv[2]))
