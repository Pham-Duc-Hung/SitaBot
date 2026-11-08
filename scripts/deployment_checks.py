"""
Author: Trịnh Thiên Quang
Kiểm tra trạng thái triển khai và đường gọi API/workflow tới alias Lambda.
"""

import json


def pages(client, operation, key, **kwargs):
    token = None
    while True:
        result = getattr(client, operation)(
            **kwargs, **({"NextToken": token} if token else {})
        )
        yield from result.get(key, [])
        token = result.get("NextToken")
        if not token:
            return


def _matches_alias_uri(target: str, alias_arn: str) -> bool:
    return target == alias_arn or target.endswith(f"/{alias_arn}/invocations")


def verify_callers(session, parameters, aliases):
    source = parameters.get("ApiGatewaySourceArn", "")
    workflow = parameters.get("ActionStateMachineArn", "")
    if not source or not workflow:
        raise ValueError(
            "Provide ApiGatewaySourceArn and ActionStateMachineArn to verify caller wiring."
        )
    api_id = source.split(":", 5)[-1].split("/")[0].strip()
    if not api_id or "*" in api_id:
        raise ValueError(
            "ApiGatewaySourceArn must contain a concrete API ID (not wildcard '*') to verify caller wiring."
        )
    http = session.client("apigatewayv2")
    try:
        integrations = {
            item["IntegrationId"]: item.get("IntegrationUri", "")
            for item in pages(http, "get_integrations", "Items", ApiId=api_id)
        }
        routes = {
            item["RouteKey"]: integrations.get(
                item.get("Target", "").removeprefix("integrations/"), ""
            )
            for item in pages(http, "get_routes", "Items", ApiId=api_id)
        }
    except Exception as exc:
        if (
            getattr(exc, "response", {}).get("Error", {}).get("Code")
            != "NotFoundException"
        ):
            raise
        rest = session.client("apigateway")
        routes = {}
        position = None
        while True:
            result = rest.get_resources(
                restApiId=api_id, **({"position": position} if position else {})
            )
            for resource in result.get("items", []):
                for method in resource.get("resourceMethods", {}):
                    if method != "OPTIONS":
                        data = rest.get_integration(
                            restApiId=api_id,
                            resourceId=resource["id"],
                            httpMethod=method,
                        )
                        routes[f"{method} {resource['path']}"] = data.get("uri", "")
            position = result.get("position")
            if not position:
                break
    required = {
        "POST /chat": "chatbot",
        "POST /confirm": "chatbot",
        "GET /history": "history",
        "GET /connections": "connector",
        "POST /connections/external-id": "connector",
        "POST /connections/verify": "connector",
    }
    checked = {}
    for route, function in required.items():
        method, path = route.split(" ", 1)
        target = routes.get(route) or routes.get(f"{method} /api{path}", "")
        if not _matches_alias_uri(target, aliases[function]):
            raise ValueError(f"Gateway route {route} must invoke {function}:live.")
        checked[route] = target
    definition = json.loads(
        session.client("stepfunctions").describe_state_machine(
            stateMachineArn=workflow
        )["definition"]
    )
    targets = []

    def visit(value):
        if isinstance(value, dict):
            if value.get("Type") == "Task":
                resource = value.get("Resource", "")
                if (
                    isinstance(resource, str)
                    and ":lambda:" in resource
                    and ":function:" in resource
                ):
                    targets.append(resource)
                args = value.get("Parameters", value.get("Arguments", {}))
                if "FunctionName" in args:
                    targets.append(args["FunctionName"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(definition)
    for name in ("dispatcher", "formatter"):
        alias = aliases[name]
        accepted = {alias, alias.split(":function:")[1]}
        if not any(t in accepted for t in targets if isinstance(t, str)):
            raise ValueError(
                f"Workflow must statically invoke {name}:live; dynamic targets need explicit validation."
            )
        physical_name = alias.split(":function:")[1].split(":")[0]
        if any(
            t.split(":function:")[-1].split(":")[0] == physical_name
            and t not in accepted
            for t in targets
            if isinstance(t, str)
        ):
            raise ValueError(f"Workflow bypasses the {name}:live alias.")
    return {"gateway_routes": checked, "workflow_targets": targets}


def validate_live_evidence(evidence):
    for name, item in evidence["functions"].items():
        if item["alarm_state"] != "OK":
            raise ValueError(f"{name}: alarm is {item['alarm_state']}, expected OK.")
        if item.get("invoke_function_error"):
            raise ValueError(f"{name}: Lambda invocation failed.")
        reply = item["live_alias_smoke_response"]
        if not isinstance(reply, dict):
            raise ValueError(f"{name}: malformed validation response.")
        expected = (
            reply.get("statusCode") == 401
            if name in {"chatbot", "connector", "history"}
            else reply.get("error_code") in {"InvalidPayload", "InternalOnly"}
        )
        if not expected:
            raise ValueError(f"{name}: unexpected validation response from alias.")
        deployment = item.get("latest_deployment")
        if not deployment:
            raise ValueError(
                f"{name}: no CodeDeploy deployment evidence yet (initial stack create does not trigger CodeDeploy; run a canary update first)."
            )
        target = item.get("deployment_target", {})
        expected_name = item["live_alias_arn"].split(":function:")[1].split(":")[0]
        if (
            target.get("functionName") != expected_name
            or target.get("functionAlias") != "live"
        ):
            raise ValueError(f"{name}: deployment target is not this live alias.")
        if deployment["status"] == "Succeeded":
            expected_version = target.get("targetVersion")
        elif item.get("rollback_status") == "Succeeded":
            expected_version = target.get("currentVersion")
        else:
            raise ValueError(f"{name}: deployment/rollback has not succeeded.")
        if not expected_version or item["active_version"] != expected_version:
            raise ValueError(
                f"{name}: alias version does not match deployment/rollback target."
            )
        if item.get("routing_config", {}).get("AdditionalVersionWeights"):
            raise ValueError(f"{name}: traffic shifting is still in progress.")
