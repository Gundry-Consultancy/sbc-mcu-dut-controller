"""Check enum compatibility against a saved job's actual imported ProtoMQ schema."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
from pathlib import Path

from hil_controller.adapters.protomq_launcher import ENCODE_JSON_JS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protomq-dir", type=Path, required=True)
    parser.add_argument(
        "--job", type=Path, default=Path(__file__).with_name("pr1014-i2c-modify.json")
    )
    args = parser.parse_args()
    job = json.loads(args.job.read_text(encoding="utf-8"))
    source = next(
        s["payload_json"] for s in job["params"]["stages"] if s.get("label") == "add_bme280_ref1000"
    )

    def encode(payload):
        return subprocess.run(
            ["node", "--input-type=module", "-e", ENCODE_JSON_JS, json.dumps(payload)],
            cwd=args.protomq_dir,
            text=True,
            capture_output=True,
            check=False,
        )

    named = encode(source)
    numeric_source = copy.deepcopy(source)
    numeric_source["i2c"]["add"]["types"] = {"0": 6, "1": 27}
    numeric = encode(numeric_source)
    if named.returncode or numeric.returncode:
        raise RuntimeError(named.stderr + numeric.stderr)
    if json.loads(named.stdout)["hex"] != json.loads(numeric.stdout)["hex"]:
        raise RuntimeError("enum names and numeric values encode differently")
    rejected = []
    for key, value in [
        ("types", {"0": "NOT_A_SENSOR"}),
        ("types", []),
        ("period", 1.5),
        ("unknownField", 1),
    ]:
        payload = copy.deepcopy(source)
        payload["i2c"]["add"][key] = value
        if encode(payload).returncode == 0:
            raise RuntimeError(f"invalid value accepted: {key}={value!r}")
        rejected.append({key: value})
    payload = copy.deepcopy(source)
    payload["i2c"]["add"]["descriptor"]["addressSpace"]["pinScl"] = 40
    if encode(payload).returncode == 0:
        raise RuntimeError("numeric pin accepted by string-pin schema")
    print(
        json.dumps(
            {
                "enum_equivalence": True,
                "names": ["T_PRESSURE", "T_ALTITUDE"],
                "values": [6, 27],
                "rejected": rejected,
                "numeric_pin_rejected": True,
                "bundle_sha256": hashlib.sha256(
                    (args.protomq_dir / "protobufs/bundle.json").read_bytes()
                ).hexdigest(),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
