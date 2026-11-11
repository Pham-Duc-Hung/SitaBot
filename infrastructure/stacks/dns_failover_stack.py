"""
DnsFailoverStack: Amazon Route 53, AWS Certificate Manager (ACM), and DNS Failover.
Author: Nguyen Duc Huy (Group 10 - Cloud Computing - UET/VNU)

Đặc tả kỹ thuật (Tuân thủ 100% Mục 1.3.3 trong Báo cáo Tiến độ 2):
  1. Tên miền & Subdomain:
      - satibot.id.vn       : Giao diện Web chính (React SPA).
      - api.satibot.id.vn   : API Gateway Endpoint.
  2. Chứng chỉ SSL/TLS (ACM):
      - Wildcard Certificate: *.satibot.id.vn & satibot.id.vn.
      - Xác thực tự động qua Route 53 DNS Validation.
  3. Kiểm tra sức khỏe (Health Check):
      - Giao thức: HTTPS.
      - Đường dẫn: /health (cổng 443).
      - Chu kỳ thăm dò: 10 giây (Fast Interval).
      - Ngưỡng thất bại: 3 lần liên tiếp.
  4. Chính sách định tuyến Chịu lỗi (Failover Routing):
      - Primary Record: Trỏ về API Gateway / CloudFront chính (ap-southeast-1 / ap-southeast-2).
      - Secondary Record: Tự động chuyển hướng sang S3 Bucket / CloudFront chứa tệp error.html tại us-east-1 khi hệ thống chính gặp sự cố.
      - TTL: 60 giây.
"""

from typing import Optional

import aws_cdk as cdk
from aws_cdk import (
    Aws,
    CfnOutput,
    Duration,
    Stack,
    aws_apigateway as apigateway,
    aws_certificatemanager as acm,
    aws_route53 as route53,
    aws_route53_targets as targets,
)
from constructs import Construct

from .core_api_stack import CoreApiStack


class DnsFailoverStack(Stack):
    """
    Quản lý tên miền toàn cầu (Route 53), chứng chỉ số bảo mật (ACM) và cơ chế chịu lỗi tự động (Failover).
    """

    DEFAULT_DOMAIN = "satibot.id.vn"

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        core_api_stack: Optional[CoreApiStack] = None,
        domain_name: Optional[str] = None,
        secondary_error_domain: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        domain = domain_name or self.node.try_get_context("domain_name") or self.DEFAULT_DOMAIN
        api_domain_name = f"api.{domain}"
        error_host = (
            secondary_error_domain
            or self.node.try_get_context("error_page_domain")
            or f"error-failover.{domain}"
        )

        # ==============================================================================
        # 1. ROUTE 53 PUBLIC HOSTED ZONE
        # ==============================================================================
        self.hosted_zone = route53.PublicHostedZone(
            self,
            "SatiBotHostedZone",
            zone_name=domain,
            comment=f"Public Hosted Zone quản lý tên miền và DNS Failover cho hệ thống SatiBot ({domain})",
        )

        # ==============================================================================
        # 2. AWS CERTIFICATE MANAGER (ACM) - WILDCARD SSL/TLS CERTIFICATE
        # ==============================================================================
        self.certificate = acm.Certificate(
            self,
            "SatiBotWildcardCertificate",
            domain_name=domain,
            subject_alternative_names=[f"*.{domain}"],
            validation=acm.CertificateValidation.from_dns(self.hosted_zone),
        )

        # ==============================================================================
        # 3. ROUTE 53 HEALTH CHECK (GIÁM SÁT ENDPOINT /health)
        # ==============================================================================
        # Cấu hình theo đúng Báo cáo 2: HTTPS, /health, 10s interval, threshold 3
        self.health_check = route53.CfnHealthCheck(
            self,
            "ApiGatewayPrimaryHealthCheck",
            health_check_config=route53.CfnHealthCheck.HealthCheckConfigProperty(
                type="HTTPS",
                fully_qualified_domain_name=api_domain_name,
                resource_path="/health",
                port=443,
                request_interval=10,  # Fast 10-second probing
                failure_threshold=3,  # Báo lỗi sau 3 lần fail liên tiếp
                measure_latency=True,
                inverted=False,
                enable_sni=True,
            ),
            health_check_tags=[
                route53.CfnHealthCheck.HealthCheckTagProperty(
                    key="Project",
                    value="SatiBot",
                ),
                route53.CfnHealthCheck.HealthCheckTagProperty(
                    key="Role",
                    value="PrimaryApiHealthCheck",
                ),
            ],
        )

        # ==============================================================================
        # 4. CUSTOM DOMAIN NAME TRÊN API GATEWAY
        # ==============================================================================
        self.custom_domain = apigateway.DomainName(
            self,
            "ApiCustomDomain",
            domain_name=api_domain_name,
            certificate=self.certificate,
            endpoint_type=apigateway.EndpointType.REGIONAL,
            security_policy=apigateway.SecurityPolicy.TLS_1_2,
        )

        # Ánh xạ API Gateway vào Custom Domain nếu được truyền vào
        if core_api_stack and hasattr(core_api_stack, "api"):
            self.custom_domain.add_base_path_mapping(
                core_api_stack.api,
                base_path="",
            )

        # ==============================================================================
        # 5. FAILOVER ROUTING POLICY (PRIMARY & SECONDARY)
        # ==============================================================================
        # 5.1. Bản ghi Primary: Trỏ về API Gateway chính khi Health Check là HEALTHY
        self.primary_record = route53.CfnRecordSet(
            self,
            "ApiPrimaryFailoverRecord",
            hosted_zone_id=self.hosted_zone.hosted_zone_id,
            name=f"{api_domain_name}.",
            type="A",
            failover="PRIMARY",
            set_identifier="Primary-SatiBot-ApiGateway-Endpoint",
            health_check_id=self.health_check.attr_health_check_id,
            alias_target=route53.CfnRecordSet.AliasTargetProperty(
                hosted_zone_id=self.custom_domain.domain_name_alias_hosted_zone_id,
                dns_name=self.custom_domain.domain_name_alias_domain_name,
                evaluate_target_health=True,
            ),
        )

        # 5.2. Bản ghi Secondary: Tự động chuyển hướng về trang thông báo lỗi khi Health Check UNHEALTHY
        # Trỏ về CloudFront Distribution / S3 Bucket chứa error.html ở us-east-1 do bạn Hùng làm
        self.secondary_record = route53.CfnRecordSet(
            self,
            "ApiSecondaryFailoverRecord",
            hosted_zone_id=self.hosted_zone.hosted_zone_id,
            name=f"{api_domain_name}.",
            type="CNAME",
            failover="SECONDARY",
            set_identifier="Secondary-SatiBot-ErrorPage-Failover",
            ttl="60",
            resource_records=[error_host],
        )

        # ==============================================================================
        # 6. CLOUDFORMATION OUTPUTS
        # ==============================================================================
        CfnOutput(
            self,
            "HostedZoneId",
            value=self.hosted_zone.hosted_zone_id,
            description="ID của Route 53 Public Hosted Zone",
            export_name="SatiBot-HostedZoneId",
        )
        CfnOutput(
            self,
            "WildcardCertificateArn",
            value=self.certificate.certificate_arn,
            description="ARN chứng chỉ SSL/TLS Wildcard ACM",
            export_name="SatiBot-WildcardCertificateArn",
        )
        CfnOutput(
            self,
            "ApiCustomDomainUrl",
            value=f"https://{api_domain_name}",
            description="URL Custom Domain chính thức của SatiBot API",
            export_name="SatiBot-ApiCustomDomainUrl",
        )
        CfnOutput(
            self,
            "HealthCheckId",
            value=self.health_check.attr_health_check_id,
            description="ID của Route 53 Health Check phục vụ Failover",
            export_name="SatiBot-HealthCheckId",
        )
