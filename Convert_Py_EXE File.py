import subprocess
import sys
from pathlib import Path

import re

# Hàm tăng version trong version.txt
def increase_version(version_file: Path):
    text = version_file.read_text(encoding="utf-8")

    match = re.search(
        r'filevers=\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)',
        text
    )

    if not match:
        raise Exception("Không tìm thấy filevers")

    major, minor, build, revision = map(int, match.groups())

    # tăng version
    revision += 1

    if revision > 9:
        revision = 0
        build += 1

    if build > 9:
        build = 0
        minor += 1

    if minor > 9:
        minor = 0
        major += 1

    version_tuple = f"({major}, {minor}, {build}, {revision})"
    version_string = f"{major}.{minor}.{build}.{revision}"

    # filevers
    text = re.sub(
        r'filevers=\([^)]+\)',
        f'filevers={version_tuple}',
        text
    )

    # prodvers
    text = re.sub(
        r'prodvers=\([^)]+\)',
        f'prodvers={version_tuple}',
        text
    )

    # FileVersion
    text = re.sub(
        r"StringStruct\(u'FileVersion', u'[^']*'\)",
        f"StringStruct(u'FileVersion', u'{version_string}')",
        text
    )

    # ProductVersion
    text = re.sub(
        r"StringStruct\(u'ProductVersion', u'[^']*'\)",
        f"StringStruct(u'ProductVersion', u'{version_string}')",
        text
    )

    version_file.write_text(text, encoding="utf-8")

    print(f"Version mới: {version_string}")
# Tăng version khi build xong
def build_exe():

    current_dir = Path.cwd()

    main_file = current_dir / "drop_ui.py"
    version_file = current_dir / "version.txt"
    icon_file = current_dir / "icon.ico"
    app_name = "Encode_File_App"
    if not main_file.exists():
        print("Không tìm thấy drop_ui.py")
        sys.exit(1)

    if not version_file.exists():
        print("Không tìm thấy version.txt")
        sys.exit(1)

    if not icon_file.exists():
        print("Không tìm thấy icon.ico")
        sys.exit(1)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",

        "--noconfirm",
        "--clean",

        "--onefile",

        "--noupx",

        "--windowed",

        "--noconsole",

        "--icon",
        str(icon_file),

        "--version-file",
        str(version_file),

        "--name",
        str(app_name),

        str(main_file)
    ]

    try:

        print("Đang build EXE...\n")

        subprocess.run(
            cmd,
            check=True
        )

        print("\nBuild thành công")
        print(f"EXE: dist/{app_name}.exe")
        increase_version(version_file)
    except subprocess.CalledProcessError:
        print("\nBuild thất bại")
        sys.exit(1)


if __name__ == "__main__":
    build_exe()