"""Stamp the app identity into a CycloneDX SBOM and check its SDK line (#545).

`cyclonedx-py environment` describes the build environment's packages but
leaves `metadata.component` (the thing the SBOM is *for*) empty when there is
no pyproject.toml, which this app has none of. The release workflow runs

    python scripts/stamp_sbom.py <sbom.cdx.json> --name Open-Motion \
        --version <tag> [--expect-sdk <version from sdk-version.txt>]

to set that component to the app name and release tag, and, on rc and
production builds, to fail the build unless the SBOM's openmotion-sdk
component is exactly the pinned release. That makes the SBOM itself the
evidence that the pin held, not just the pip log.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SDK_COMPONENT = "openmotion-sdk"


def stamp(sbom: dict, name: str, version: str) -> dict:
    """Set the SBOM's main component in place and return it."""
    metadata = sbom.setdefault("metadata", {})
    metadata["component"] = {
        "type": "application",
        "name": name,
        "version": version,
        "bom-ref": f"{name}@{version}",
    }
    return sbom


def sdk_version(sbom: dict) -> str | None:
    for component in sbom.get("components", []):
        if component.get("name", "").lower() == SDK_COMPONENT:
            return component.get("version")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sbom", type=Path, help="CycloneDX JSON file, rewritten in place")
    parser.add_argument("--name", required=True, help="main component name")
    parser.add_argument("--version", required=True, help="main component version (the release tag)")
    parser.add_argument(
        "--expect-sdk", metavar="X.Y.Z",
        help="fail unless the SBOM's openmotion-sdk component has this version",
    )
    args = parser.parse_args(argv)

    with args.sbom.open(encoding="utf-8") as fh:
        sbom = json.load(fh)
    if sbom.get("bomFormat") != "CycloneDX":
        print(f"::error ::{args.sbom} is not a CycloneDX document")
        return 1

    stamp(sbom, args.name, args.version)

    sdk = sdk_version(sbom)
    components = len(sbom.get("components", []))
    print(f"{args.sbom.name}: {args.name} {args.version}, {components} components, "
          f"{SDK_COMPONENT} {sdk or 'MISSING'}")
    if sdk is None:
        print(f"::error ::{SDK_COMPONENT} is not among the SBOM components")
        return 1
    if args.expect_sdk and sdk != args.expect_sdk:
        print(f"::error ::SBOM records {SDK_COMPONENT} {sdk}, expected the pinned {args.expect_sdk}")
        return 1

    with args.sbom.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(sbom, fh, indent=2)
        fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
