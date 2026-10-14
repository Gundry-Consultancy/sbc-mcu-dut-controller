"""Build the Tachyon local plugin ZIP from explicit, non-secret source files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    source = repo / "plugins" / "tachyon-hil"
    files = {
        name: (source / name).read_bytes()
        for name in (
            ".claude-plugin/plugin.json",
            "plugin.json",
            "README.md",
            "skills/tachyon-bench/SKILL.md",
        )
    }
    config = json.loads((repo / "examples/mcp/configs/claude.mcp.json").read_text())
    files[".mcp.json"] = (json.dumps(config, indent=2) + "\n").encode()
    portable = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
        **config,
    }
    files["mcp.json"] = (json.dumps(portable, indent=2) + "\n").encode()
    for name in ("pr1014-i2c.json", "pr1014-i2c-modify.json"):
        files[f"jobs/{name}"] = (repo / "examples/mcp" / name).read_bytes()
    for name in ("circuitpython_wifi.py", "wippersnapper_python_wifi.py"):
        files[f"examples/{name}"] = (repo / "examples/mcp" / name).read_bytes()
    manifest = json.loads(files[".claude-plugin/plugin.json"])
    assert manifest["name"] == "tachyon-hil"
    assert manifest["version"] == json.loads(files["plugin.json"])["version"]
    for name, data in files.items():
        if name.endswith(".json"):
            json.loads(data)
    output = args.output.resolve()
    unpacked = output / "tachyon-hil"
    for name, data in files.items():
        path = unpacked / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    archive = output / f"tachyon-hil-{manifest['version']}.zip"
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zipped:
        for name, data in sorted(files.items()):
            entry = ZipInfo(name, date_time=(2026, 10, 14, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            zipped.writestr(entry, data)
    with ZipFile(archive) as zipped:
        assert zipped.testzip() is None
        assert set(zipped.namelist()) == set(files)
        for name, data in files.items():
            assert zipped.read(name) == data
    print(f"Built and verified {archive} ({len(files)} files)")


if __name__ == "__main__":
    main()
