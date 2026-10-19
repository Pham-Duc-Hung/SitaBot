from aws_cdk import Stack
from constructs import Construct

class CoreApiStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Định nghĩa các tài nguyên AWS của bạn ở đây
        # Ví dụ: Tạo Cognito User Pool, DynamoDB Table...
