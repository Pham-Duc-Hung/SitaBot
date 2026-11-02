"""IAM Role/Policy operations. ``confirmed`` is supplied by a trusted backend.

This module does not authenticate callers. IAM validates policy semantics on write;
local validation checks structure and reports broad permissions without changing it.
"""
import hashlib
import json
import re
from datetime import date, datetime
from urllib.parse import unquote

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

try:
    from backend.shared.runtime import get_aws_region
except ImportError:
    from runtime import get_aws_region


POLICY_TEMPLATES = {
    "s3_read": {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:ListBucket"],
         "Resource": "arn:${PARTITION}:s3:::${BUCKET_NAME}"},
        {"Effect": "Allow", "Action": ["s3:GetObject"],
         "Resource": "arn:${PARTITION}:s3:::${BUCKET_NAME}/*"}]},
    "service_trust": {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"Service": "${SERVICE_PRINCIPAL}"},
         "Action": "sts:AssumeRole"}]},
    "role_trust": {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"AWS": "${SATIBOT_CALLER_ROLE_ARN}"},
         "Action": "sts:AssumeRole", "Condition": {
             "StringEquals": {"sts:ExternalId": "${EXTERNAL_ID}"}}}]},
}

READ_ACTIONS = {
    "list_roles", "get_role", "list_policies", "get_policy",
    "list_attached_role_policies", "list_role_policies", "get_role_policy",
    "preview_delete_role", "preview_delete_policy", "validate_policy",
}
WRITE_ACTIONS = {
    "create_role", "update_assume_role_policy", "delete_role", "create_policy",
    "update_policy", "delete_policy", "attach_role_policy", "detach_role_policy",
    "put_role_policy", "delete_role_policy",
}
ROLE_WARNING = (
    "Preview chỉ bao gồm liên kết IAM trực tiếp. Code hoặc dịch vụ ngoài IAM có thể "
    "vẫn tham chiếu Role. Gỡ Role khỏi instance profile có thể làm ứng dụng EC2 mất "
    "quyền gọi AWS; thay đổi có độ trễ."
)
RECOVERY = {
    "AccessDenied": "Kiểm tra quyền credential, permissions boundary và SCP; cấp đúng quyền còn thiếu trước khi thử lại.",
    "AccessDeniedException": "Kiểm tra quyền credential và các policy giới hạn quyền.",
    "DeleteConflict": "Đọc lại liên kết/phụ thuộc hiện tại, xử lý phần còn lại rồi yêu cầu xác nhận mới.",
    "NoSuchEntity": "Tra cứu lại tên hoặc ARN; tài nguyên có thể đã bị xóa bởi thao tác khác.",
    "EntityAlreadyExists": "Tra cứu tài nguyên hiện hữu hoặc chọn tên khác.",
    "LimitExceeded": "Kiểm tra quota và policy versions; chỉ dọn tài nguyên sau khi được xác nhận.",
    "MalformedPolicyDocument": "Sửa policy theo lỗi AWS trả về rồi kiểm tra lại trước khi xác nhận.",
    "InvalidInput": "Kiểm tra lại tham số theo yêu cầu API IAM.",
    "Throttling": "Chờ rồi thử lại có giới hạn; kiểm tra tiến độ trước khi lặp thao tác ghi.",
    "ServiceFailure": "Kiểm tra trạng thái tài nguyên trước khi thử lại để tránh lặp thao tác đã thành công.",
}


def _response(action, status="SUCCESS", message="Đã xử lý yêu cầu IAM.", data=None,
              error_code=None, warnings=None):
    return {"status": status, "service": "iam", "action": action,
            "message": message, "data": data if data is not None else {},
            "error_code": error_code, "warnings": warnings or []}


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if k != "ResponseMetadata"}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _name(p, key):
    value = p.get(key)
    if not isinstance(value, str) or not re.fullmatch(r"[\w+=,.@-]{1,128}", value, re.ASCII):
        raise ValueError(f"{key} phải là tên IAM hợp lệ.")
    if key == "role_name" and len(value) > 64:
        raise ValueError("role_name vượt quá 64 ký tự.")
    return value


def _arn(p):
    value = p.get("policy_arn")
    if not isinstance(value, str) or not re.fullmatch(r"arn:[\w-]+:iam::(?:\d{12}|aws):policy/[^\s${}]+", value):
        raise ValueError("policy_arn không hợp lệ hoặc chưa thay placeholder.")
    return value


def _strings(value):
    values = value if isinstance(value, list) else [value]
    if not values or any(not isinstance(v, str) or not v.strip() for v in values):
        raise ValueError("Giá trị policy phải là chuỗi hoặc danh sách chuỗi không rỗng.")
    return values


def _document(value, trust=False):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            raise ValueError("PolicyDocument không phải JSON hợp lệ.") from None
    if not isinstance(value, dict) or value.get("Version") != "2012-10-17":
        raise ValueError("Policy phải là object với Version 2012-10-17.")
    if set(value) - {"Version", "Id", "Statement"}:
        raise ValueError("Policy có trường cấp document không được hỗ trợ.")
    if re.search(r"\$\{|<[^>]+>", json.dumps(value)):
        raise ValueError("Policy còn placeholder chưa thay thế.")
    statements = value.get("Statement")
    statements = [statements] if isinstance(statements, dict) else statements
    if not isinstance(statements, list) or not statements:
        raise ValueError("Statement phải là object hoặc danh sách không rỗng.")
    warnings = []
    for s in statements:
        if not isinstance(s, dict) or s.get("Effect") not in ("Allow", "Deny"):
            raise ValueError("Mỗi Statement cần Effect Allow hoặc Deny.")
        if set(s) - {"Sid", "Effect", "Action", "NotAction", "Resource", "NotResource", "Principal", "NotPrincipal", "Condition"}:
            raise ValueError("Statement có trường không được hỗ trợ.")
        if ("Action" in s) == ("NotAction" in s):
            raise ValueError("Statement cần đúng một trong Action hoặc NotAction.")
        actions = _strings(s.get("Action", s.get("NotAction")))
        if any(a != "*" and not re.fullmatch(r"[a-zA-Z0-9-]+:[a-zA-Z0-9*?]+", a) for a in actions):
            raise ValueError("Action cần định dạng service:action hoặc wildcard.")
        if "Condition" in s and (not isinstance(s["Condition"], dict) or not s["Condition"]):
            raise ValueError("Condition phải là object không rỗng.")
        if any(not isinstance(v, dict) or not v for v in s.get("Condition", {}).values()):
            raise ValueError("Mỗi toán tử Condition cần object điều kiện không rỗng.")
        if trust:
            principal = s.get("Principal")
            if not isinstance(principal, dict) or not principal or set(principal) - {"AWS", "Service"}:
                raise ValueError("Trust chỉ hỗ trợ Principal AWS role hoặc Service.")
            if "Resource" in s or "NotResource" in s or "NotPrincipal" in s or "NotAction" in s:
                raise ValueError("Trust policy có trường ngoài phạm vi hỗ trợ.")
            if actions != ["sts:AssumeRole"]:
                raise ValueError("Trust chỉ hỗ trợ sts:AssumeRole.")
            for kind, entries in principal.items():
                for entry in _strings(entries):
                    if kind == "AWS" and not re.fullmatch(r"arn:[\w-]+:iam::\d{12}:role/[^\s*]+", entry):
                        raise ValueError("AWS Principal phải là ARN Role cụ thể.")
                    if kind == "Service" and not re.fullmatch(r"[a-z0-9.-]+\.amazonaws\.com(?:\.cn)?", entry):
                        raise ValueError("Service Principal không hợp lệ.")
            if "AWS" in principal and "sts:ExternalId" not in json.dumps(s.get("Condition", {})):
                warnings.append("Trust AWS Role chưa có điều kiện ExternalId; kiểm tra mục đích ủy quyền.")
        else:
            if "Principal" in s or "NotPrincipal" in s:
                raise ValueError("Permission policy không được chứa Principal.")
            if ("Resource" in s) == ("NotResource" in s):
                raise ValueError("Permission cần đúng một trong Resource hoặc NotResource.")
            resources = _strings(s.get("Resource", s.get("NotResource")))
            if s["Effect"] == "Allow" and (any("*" in a for a in actions + resources) or "NotAction" in s or "NotResource" in s):
                warnings.append("Allow có wildcard hoặc phép phủ định; cần xem xét phạm vi quyền trước khi xác nhận.")
    return value, sorted(set(warnings))


def _pages(client, method, key, **kwargs):
    result, marker, seen = [], None, set()
    while True:
        response = getattr(client, method)(**kwargs, **({"Marker": marker} if marker else {}))
        result.extend(response.get(key, []))
        if not response.get("IsTruncated"):
            return result
        marker = response.get("Marker")
        if not marker or marker in seen:
            raise ValueError("AWS trả phân trang thiếu hoặc lặp Marker; chưa đọc đầy đủ dữ liệu.")
        seen.add(marker)


def _decode(value):
    return json.loads(unquote(value)) if isinstance(value, str) else value


def _role_preview(client, role):
    metadata = client.get_role(RoleName=role)["Role"]
    if metadata.get("Path", "").startswith("/aws-service-role/"):
        raise ValueError("Service-linked role cần quy trình dịch vụ riêng, ngoài phạm vi module.")
    return {"role": metadata,
            "managed_policies": _pages(client, "list_attached_role_policies", "AttachedPolicies", RoleName=role),
            "inline_policies": _pages(client, "list_role_policies", "PolicyNames", RoleName=role),
            "instance_profiles": _pages(client, "list_instance_profiles_for_role", "InstanceProfiles", RoleName=role),
            "limitations": ROLE_WARNING}


def _policy_preview(client, arn):
    metadata = client.get_policy(PolicyArn=arn)["Policy"]
    entities = {}
    for usage in ("PermissionsPolicy", "PermissionsBoundary"):
        entities[usage] = {key: _pages(client, "list_entities_for_policy", key,
                                     PolicyArn=arn, PolicyUsageFilter=usage)
                           for key in ("PolicyRoles", "PolicyUsers", "PolicyGroups")}
    return {"policy": metadata, "entities": entities,
            "versions": _pages(client, "list_policy_versions", "Versions", PolicyArn=arn)}


def _fingerprint(value):
    return hashlib.sha256(json.dumps(_clean(value), sort_keys=True).encode()).hexdigest()


def _verify_profiles_unused(profiles, options):
    """Check all enabled EC2 regions. Any unreadable region aborts deletion."""
    regional = boto3.client("ec2", **options)
    regions = regional.describe_regions(AllRegions=False)["Regions"]
    if not regions:
        raise ValueError("Không xác minh được các region EC2 để kiểm tra profile.")
    for region in regions:
        ec2 = boto3.client("ec2", **{**options, "region_name": region["RegionName"]})
        token = None
        seen = set()
        while True:
            response = ec2.describe_iam_instance_profile_associations(**({"NextToken": token} if token else {}))
            for item in response.get("IamInstanceProfileAssociations", []):
                if item.get("State") != "disassociated" and item.get("IamInstanceProfile", {}).get("Arn") in profiles:
                    raise ValueError("Instance profile đang liên kết EC2; giữ lại và xử lý trên EC2 Console trước.")
            token = response.get("NextToken")
            if not token:
                break
            if token in seen:
                raise ValueError("Phân trang kiểm tra EC2 bị lặp; không xóa profile.")
            seen.add(token)


def execute(credentials, params):
    """Dispatcher contract: action, parameters, confirmed (strict bool), region.

    All writes require confirmation. Delete actions automatically preview on an
    unconfirmed request; optional preview_fingerprint rejects changed dependencies.
    """
    action = params.get("action") if isinstance(params, dict) else None
    warnings, completed, step, plan = [], [], None, None
    try:
        if not isinstance(params, dict) or not isinstance(params.get("parameters", {}), dict):
            raise ValueError("params và parameters phải là object.")
        p = params.get("parameters", {})
        confirmed = params.get("confirmed", False)
        if type(confirmed) is not bool:
            raise ValueError("confirmed phải là boolean do backend đáng tin cậy cấp.")
        if credentials is not None and not isinstance(credentials, dict):
            raise ValueError("credentials phải là object hoặc null.")
        if action not in READ_ACTIONS | WRITE_ACTIONS:
            if isinstance(action, str) and any(x in action for x in ("user", "group")):
                return _response(action, "ERROR", "Module chỉ quản lý Role/Policy. User/Group cần quy trình credential, MFA và vòng đời riêng. Hãy dùng AWS Console → IAM → Users/Groups.", error_code="UnsupportedTool")
            return _response(action, "ERROR", "Thao tác IAM không được hỗ trợ.", error_code="UnsupportedTool")
        role = _name(p, "role_name") if "role" in action and action != "list_roles" else None
        arn_actions = {"get_policy", "update_policy", "delete_policy", "preview_delete_policy", "attach_role_policy", "detach_role_policy"}
        arn = _arn(p) if action in arn_actions else None
        policy_name = _name(p, "policy_name") if action in {"create_policy", "put_role_policy", "get_role_policy", "delete_role_policy"} else None
        doc = None
        if action in {"create_role", "update_assume_role_policy", "create_policy", "update_policy", "put_role_policy", "validate_policy"}:
            trust = action in {"create_role", "update_assume_role_policy"}
            if action == "validate_policy":
                if p.get("policy_type", "permission") not in {"permission", "trust"}:
                    raise ValueError("policy_type phải là permission hoặc trust.")
                trust = p.get("policy_type") == "trust"
            doc, warnings = _document(p.get("trust_policy" if trust and action != "validate_policy" else "policy_document"), trust)
        if arn and ":iam::aws:" in arn:
            if action in {"delete_policy", "update_policy", "preview_delete_policy"}:
                raise ValueError("AWS managed policy không được sửa hoặc xóa.")
            if action == "attach_role_policy" and arn.endswith(("/AdministratorAccess", "/PowerUserAccess", "/IAMFullAccess")):
                warnings.append("Policy được gắn cấp quyền quản trị rộng; cần xác nhận.")
        if action == "validate_policy":
            return _response(action, data={"valid_structure": True, "policy_document": doc,
                             "validation_scope": "Kiểm tra cục bộ; AWS đánh giá ngữ nghĩa khi ghi."}, warnings=warnings)
        if action in WRITE_ACTIONS - {"delete_role", "delete_policy", "attach_role_policy"} and not confirmed:
            return _response(action, "CONFIRMATION_REQUIRED", "Cần xác nhận trước khi thay đổi IAM.", warnings=warnings)
        creds = credentials or {}
        if creds and not all(isinstance(creds.get(k), str) and creds[k] for k in ("AccessKeyId", "SecretAccessKey", "SessionToken")):
            raise ValueError("Cần đủ AccessKeyId, SecretAccessKey, SessionToken tạm thời.")
        options = {"region_name": get_aws_region(params.get("region")),
                   "config": Config(retries={"mode": "standard", "max_attempts": 2})}
        if creds:
            options.update(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"], aws_session_token=creds["SessionToken"])
        client = boto3.client("iam", **options)

        if action == "attach_role_policy":
            metadata = client.get_policy(PolicyArn=arn)["Policy"]
            existing = client.get_policy_version(PolicyArn=arn, VersionId=metadata["DefaultVersionId"])["PolicyVersion"]
            _, policy_warnings = _document(_decode(existing["Document"]))
            warnings.extend(policy_warnings)
            if not confirmed:
                return _response(action, "CONFIRMATION_REQUIRED", "Đã kiểm tra policy hiện tại; cần xác nhận trước khi gắn.",
                                 {"policy_arn": arn, "policy_document": _decode(existing["Document"])}, warnings=warnings)

        def write(method, **kwargs):
            nonlocal step
            step = {"operation": method, "target": {k: v for k, v in kwargs.items() if k not in {"PolicyDocument", "AssumeRolePolicyDocument"}}}
            result = getattr(client, method)(**kwargs)
            completed.append(step)
            step = None
            return result

        if action in {"delete_role", "preview_delete_role", "delete_policy", "preview_delete_policy"}:
            is_role = action.endswith("role")
            plan = _role_preview(client, role) if is_role else _policy_preview(client, arn)
            if is_role:
                warnings.append(ROLE_WARNING)
            fingerprint = _fingerprint(plan)
            if action.startswith("preview_") or not confirmed:
                return _response(action, "SUCCESS" if action.startswith("preview_") else "CONFIRMATION_REQUIRED",
                                 "Đã đọc liên kết hiện tại; xem tác động và xác nhận trước khi xóa.",
                                 _clean({"preview": plan, "preview_fingerprint": fingerprint}), warnings=warnings)
            if p.get("preview_fingerprint") and p["preview_fingerprint"] != fingerprint:
                return _response(action, "CONFIRMATION_REQUIRED", "Liên kết đã thay đổi; cần xem và xác nhận lại.",
                                 _clean({"preview": plan, "preview_fingerprint": fingerprint}), warnings=warnings)
            if is_role:
                selected = p.get("delete_instance_profiles", [])
                if not isinstance(selected, list) or any(not isinstance(v, str) for v in selected):
                    raise ValueError("delete_instance_profiles phải là danh sách tên profile chọn xóa.")
                profiles = {v["InstanceProfileName"]: v for v in plan["instance_profiles"]}
                if set(selected) - set(profiles):
                    raise ValueError("Profile chọn xóa không thuộc preview của Role.")
                # Explicit selection + no current EC2 association in enabled regions.
                # This does not prove absence of references in external code/IaC.
                if selected:
                    _verify_profiles_unused({profiles[n]["Arn"] for n in selected}, options)
                for policy in plan["managed_policies"]:
                    write("detach_role_policy", RoleName=role, PolicyArn=policy["PolicyArn"])
                for name in plan["inline_policies"]:
                    write("delete_role_policy", RoleName=role, PolicyName=name)
                for name in profiles:
                    write("remove_role_from_instance_profile", RoleName=role, InstanceProfileName=name)
                    if name in selected:
                        current = client.get_instance_profile(InstanceProfileName=name)["InstanceProfile"]
                        if current.get("Roles"):
                            raise ValueError("Profile vẫn còn Role sau khi tháo; chưa thể xác nhận profile rỗng để xóa.")
                        _verify_profiles_unused({profiles[name]["Arn"]}, options)
                        write("delete_instance_profile", InstanceProfileName=name)
                write("delete_role", RoleName=role)
            else:
                entities = plan["entities"]
                if entities["PermissionsPolicy"]["PolicyUsers"] or entities["PermissionsPolicy"]["PolicyGroups"] or any(entities["PermissionsBoundary"].values()):
                    return _response(action, "ERROR", "Policy còn liên kết User/Group hoặc được dùng làm permissions boundary. Xử lý liên kết đó trên IAM Console trước; module chưa thay đổi tài nguyên.", _clean({"preview": plan}), "DependenciesOutsideScope")
                for entity in entities["PermissionsPolicy"]["PolicyRoles"]:
                    write("detach_role_policy", RoleName=entity["RoleName"], PolicyArn=arn)
                for version in plan["versions"]:
                    if not version["IsDefaultVersion"]:
                        write("delete_policy_version", PolicyArn=arn, VersionId=version["VersionId"])
                write("delete_policy", PolicyArn=arn)
            return _response(action, data=_clean({"completed_steps": completed}), warnings=warnings)
        if action == "list_roles":
            data = {"roles": _pages(client, "list_roles", "Roles")}
        elif action == "get_role":
            data = {"role": client.get_role(RoleName=role)["Role"],
                    "managed_policies": _pages(client, "list_attached_role_policies", "AttachedPolicies", RoleName=role),
                    "inline_policies": _pages(client, "list_role_policies", "PolicyNames", RoleName=role)}
        elif action == "list_policies":
            scope = p.get("scope", "Local")
            if scope not in {"Local", "AWS", "All"}:
                raise ValueError("scope phải là Local, AWS hoặc All.")
            data = {"policies": _pages(client, "list_policies", "Policies", Scope=scope)}
        elif action == "get_policy":
            metadata = client.get_policy(PolicyArn=arn)["Policy"]
            version = client.get_policy_version(PolicyArn=arn, VersionId=metadata["DefaultVersionId"])["PolicyVersion"]
            data = {"policy": metadata, "policy_document": _decode(version["Document"])}
        elif action in {"list_attached_role_policies", "list_role_policies"}:
            key = "AttachedPolicies" if action == "list_attached_role_policies" else "PolicyNames"
            data = {key: _pages(client, action, key, RoleName=role)}
        elif action == "get_role_policy":
            data = client.get_role_policy(RoleName=role, PolicyName=policy_name)
            data["PolicyDocument"] = _decode(data["PolicyDocument"])
        elif action == "create_role":
            data = write(action, RoleName=role, AssumeRolePolicyDocument=json.dumps(doc))
        elif action == "update_assume_role_policy":
            data = write(action, RoleName=role, PolicyDocument=json.dumps(doc))
        elif action == "create_policy":
            data = write(action, PolicyName=policy_name, PolicyDocument=json.dumps(doc))
        elif action == "update_policy":
            data = write("create_policy_version", PolicyArn=arn, PolicyDocument=json.dumps(doc), SetAsDefault=True)
        elif action in {"attach_role_policy", "detach_role_policy"}:
            data = write(action, RoleName=role, PolicyArn=arn)
        elif action == "put_role_policy":
            data = write(action, RoleName=role, PolicyName=policy_name, PolicyDocument=json.dumps(doc))
        else:
            data = write("delete_role_policy", RoleName=role, PolicyName=policy_name)
        return _response(action, data=_clean(data), warnings=warnings)
    except ValueError as exc:
        return _response(action, "ERROR", str(exc), data=_clean({"completed_steps": completed,
                         "preview": plan, "recovery_suggestion": "Đọc lại trạng thái và sửa điều kiện được báo trước khi xác nhận lại."}),
                         error_code="InvalidParameters", warnings=warnings)
    except (ClientError, BotoCoreError) as exc:
        code = exc.response.get("Error", {}).get("Code", "AwsClientError") if isinstance(exc, ClientError) else type(exc).__name__
        message = exc.response.get("Error", {}).get("Message", "Lỗi kết nối hoặc credential AWS.") if isinstance(exc, ClientError) else "Lỗi kết nối hoặc credential AWS."
        for value in (credentials or {}).values():
            if isinstance(value, str) and value:
                message = message.replace(value, "<redacted>")
        return _response(action, "ERROR", message[:2000], _clean({
            "completed_steps": completed, "failed_step": step,
            "preview": plan, "recovery_suggestion": RECOVERY.get(code, "Kiểm tra mã lỗi AWS và trạng thái thực tế trước khi thử lại."),
            "execution_note": "Các bước chưa được API xác nhận có thể chưa thực hiện hoặc có kết quả chưa xác định. Không tự động rollback."}), code, warnings)
