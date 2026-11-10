"""Inventory embedded Maven metadata of the actual fat JAR; not license clearance."""
import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


def inventory(jar):
    components = []
    ns = {"m": "http://maven.apache.org/POM/4.0.0"}
    with zipfile.ZipFile(jar) as archive:
        for name in sorted(archive.namelist()):
            if not name.startswith("META-INF/maven/") or not name.endswith("/pom.properties"):
                continue
            values = {}
            for line in archive.read(name).decode("utf-8").splitlines():
                if "=" in line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    values[key] = value
            pom = name.removesuffix("pom.properties") + "pom.xml"
            licenses = []
            if pom in archive.namelist():
                document = ET.fromstring(archive.read(pom))
                for item in document.findall("m:licenses/m:license", ns):
                    licenses.append({key: item.findtext("m:" + key, namespaces=ns) for key in ("name", "url")})
            components.append({"group": values.get("groupId"), "artifact": values.get("artifactId"),
                               "version": values.get("version"), "declared_licenses": licenses,
                               "evidence": name, "status": "embedded_metadata_only_not_distribution_clearance"})
    with jar.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"artifact_sha256": digest,
            "coverage": "Embedded Maven metadata; not exhaustive native/container SBOM or dependency license approval",
            "components": components}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("jar", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(inventory(args.jar), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
