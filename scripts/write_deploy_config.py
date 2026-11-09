"""
Author: Trịnh Thiên Quang
Sinh cấu hình CloudFormation và fingerprint từ tham số triển khai thực tế.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path


def build_configuration(template, environment):
    params = {}
    definitions = template["Parameters"]
    for name, definition in definitions.items():
        if name in {"ConfigurationVersion", "BootstrapVersion"}:
            continue
        value = environment.get(f"PARAM_{name}", definition.get("Default"))
        if name in {"ChatbotRoleName", "FormatterRoleName"} and not value:
            role = environment.get(f"PARAM_{name.removesuffix('Name')}Arn", "")
            value = role.rsplit("/", 1)[-1] if role else value
        if value is None:
            raise ValueError(f"Missing deployment parameter: {name}")
        params[name] = str(value)
    release = environment.get("PARAM_ConfigurationReleaseId", "v1")
    digest = hashlib.sha256(
        json.dumps({"release": release, "parameters": params}, sort_keys=True).encode()
    ).hexdigest()
    params["ConfigurationVersion"] = digest[:32]
    return {"Parameters": params}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--template", default="cdk.out/SatiBotBackendSprint2.template.json"
    )
    parser.add_argument("--output", default="deploy-config/template-configuration.json")
    args = parser.parse_args()
    config = build_configuration(
        json.loads(Path(args.template).read_text()), os.environ
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(config, indent=2), encoding="utf-8")
