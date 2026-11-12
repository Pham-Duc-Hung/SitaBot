"""Invoke deployed HTTP Lambda aliases without credentials or side effects."""

import argparse
import json
from pathlib import Path

import boto3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--outputs", type=Path, default=Path("dist/release-outputs.json"))
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name="ap-southeast-1")
    account = session.client("sts").get_caller_identity()["Account"]
    if account != args.expected_account:
        raise RuntimeError(f"Refusing to test account {account}; expected {args.expected_account}")
    aliases = json.loads(args.outputs.read_text(encoding="utf-8"))["SatiBotBackend"]
    client = session.client("lambda")
    for name in ("Chatbot", "Connector", "History"):
        arn = aliases[f"{name}LiveAliasArn"]
        if arn.split(":")[4] != account or not arn.endswith(":live"):
            raise RuntimeError(f"Unexpected {name} Lambda alias")
        response = client.invoke(FunctionName=arn, Payload=b"{}")
        body = json.loads(response["Payload"].read())
        if response.get("FunctionError") or body.get("statusCode") != 401:
            raise RuntimeError(
                f"{name} did not reject an unauthenticated request "
                f"(HTTP {body.get('statusCode')}, Lambda error {body.get('errorType')}: "
                f"{str(body.get('errorMessage', ''))[:200]}, "
                f"stack {body.get('stackTrace', [])[:14]})"
            )
        print(f"{name}: live alias runs and rejects unauthenticated request (401)")


if __name__ == "__main__":
    main()
