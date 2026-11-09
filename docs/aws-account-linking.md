# Liên kết tài khoản AWS của người dùng

SatiBot đăng nhập người dùng bằng Cognito; kết nối đến tài khoản AWS của họ là bước riêng. Không thu thập AWS password, access key hoặc secret key trên web.

## Luồng triển khai

1. API Gateway xác thực JWT và chuyển `sub` của người dùng tới Connector Lambda. Bảo vệ cả ba route `GET /api/connections`, `POST /api/connections/external-id` và `POST /api/connections/verify` bằng cùng authorizer. Không lấy `user_id` từ body do trình duyệt gửi.
2. Web gọi `/api/connections/external-id`. Connector lưu ExternalId theo `sub` trong `AwsConnections` và trả về ExternalId cùng trust policy. Backend CDK truyền ARN role của Connector và Dispatcher qua `SATIBOT_CONNECTOR_ROLE_ARN` và `SATIBOT_DISPATCHER_ROLE_ARN`; nếu thiếu hai biến này, trang web báo chưa cấu hình thay vì đoán tài khoản AWS.
3. Trong tài khoản AWS cần liên kết, tạo IAM Role với path `/satibot/`, tên `SatiBotAccessRole`, trust policy do web hiển thị và permission policy chỉ cấp các thao tác được phép. Role ARN có dạng `arn:aws:iam::<AWS_ACCOUNT_ID>:role/satibot/SatiBotAccessRole`.
4. Người dùng gửi Role ARN tới `/api/connections/verify`. Connector gọi STS `AssumeRole` với ExternalId được lưu phía máy chủ. Chỉ sau khi AWS chấp nhận, Connector mới lưu trạng thái `CONNECTED`. Dispatcher đọc kết nối theo `sub` và lấy credential tạm thời mỗi khi thực hiện tác vụ.

Frontend `aws-connection.js` dùng API cùng origin. Nếu tích hợp Cognito bằng JWT trên trình duyệt, module đăng nhập cung cấp `window.SatiBotAuth.getIdToken(): Promise<string>`; nếu dùng phiên cùng origin, trình duyệt gửi cookie theo cấu hình API. API Gateway phải thực sự xác thực người dùng và cung cấp claim `sub`. Máy chủ demo cục bộ chỉ có `/api/chat` và `/api/health`, nên màn hình liên kết sẽ báo thiếu route cho đến khi API Gateway/Connector được nối vào web.

## Kiểm tra trước khi nghiệm thu

- Người dùng A và B nhận ExternalId khác nhau, chỉ xem kết nối của mình.
- Role ARN sai hoặc trust policy/ExternalId không khớp trả lỗi và không đánh dấu `CONNECTED`.
- Sau khi xác minh, reload trang vẫn thấy đúng Account ID/Role ARN từ `GET /api/connections`.
- Một thao tác S3 đọc dùng credential tạm thời của đúng tài khoản; thao tác ghi vẫn đòi xác nhận.
