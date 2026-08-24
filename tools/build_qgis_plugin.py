import argparse
import shutil
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SOURCE = ROOT / "src" / "IdrAgraGather"
REQUIRED = (
    "__init__.py",
    "metadata.txt",
    "plugin.py",
    "dialog.py",
    "cell_dialog.py",
    "map_tool.py",
)
PACKAGE_NAME = "IdrAgraGather"


def build(output: Path) -> Path:
    for relative in REQUIRED:
        if not (PLUGIN_SOURCE / relative).is_file():
            raise FileNotFoundError(PLUGIN_SOURCE / relative)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as temporary:
        package = Path(temporary) / PACKAGE_NAME
        shutil.copytree(
            PLUGIN_SOURCE,
            package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            for path in sorted(package.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(package.parent))
    _validate_installable_archive(output)
    return output


def _validate_installable_archive(output: Path) -> None:
    """Reject archives QGIS could interpret as a nested or invalid package."""

    with ZipFile(output) as archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]
    roots = {name.split("/", 1)[0] for name in names}
    if roots != {PACKAGE_NAME}:
        raise ValueError(f"plugin ZIP must contain only the {PACKAGE_NAME!r} root")
    required = {f"{PACKAGE_NAME}/{name}" for name in REQUIRED}
    missing = sorted(required - set(names))
    if missing:
        raise ValueError(f"plugin ZIP is missing: {', '.join(missing)}")


def main():
    parser = argparse.ArgumentParser(description="Build the installable QGIS plugin ZIP")
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=ROOT / "dist" / "IdrAgraGather_qgis.zip",
    )
    args = parser.parse_args()
    print(build(args.output))


if __name__ == "__main__":
    main()
