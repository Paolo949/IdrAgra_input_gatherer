import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Manifest:
    """Small append-only provenance manifest for one staging directory."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / "manifest.json"

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "schema_version": SCHEMA_VERSION,
                "created_at": _now(),
                "aoi": None,
                "date_window": None,
                "assets": [],
            }
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported manifest schema: {data.get('schema_version')!r}"
            )
        return data

    def configure(
        self,
        *,
        aoi: dict[str, Any] | None = None,
        date_window: dict[str, str] | None = None,
    ) -> None:
        data = self.read()
        if aoi is not None:
            data["aoi"] = aoi
        if date_window is not None:
            data["date_window"] = date_window
        self._write(data)

    def add_asset(
        self,
        path: Path,
        *,
        category: str,
        provider: str,
        dataset: str | None = None,
        source: str | None = None,
        request: dict[str, Any] | None = None,
    ) -> None:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("manifest assets must be inside the staging directory") from exc

        data = self.read()
        entry = {
            "path": relative.as_posix(),
            "category": category,
            "provider": provider,
            "dataset": dataset,
            "source": source,
            "request": request,
            "size_bytes": resolved.stat().st_size,
            "sha256": sha256_file(resolved),
            "recorded_at": _now(),
        }
        # Re-running a resumable acquisition refreshes the same asset instead
        # of growing duplicate manifest entries.
        data["assets"] = [item for item in data["assets"] if item["path"] != entry["path"]]
        data["assets"].append(entry)
        data["assets"].sort(key=lambda item: item["path"])
        self._write(data)

    def remove_assets(self, paths) -> None:
        """Forget assets that were intentionally removed or superseded."""

        relative_paths = set()
        for path in paths:
            resolved = Path(path).resolve()
            try:
                relative_paths.add(resolved.relative_to(self.root).as_posix())
            except ValueError as exc:
                raise ValueError(
                    "manifest assets must be inside the staging directory"
                ) from exc
        data = self.read()
        data["assets"] = [
            item for item in data["assets"] if item["path"] not in relative_paths
        ]
        self._write(data)

    def _write(self, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = _now()
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
