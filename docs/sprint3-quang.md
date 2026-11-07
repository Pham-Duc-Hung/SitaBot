# Sprint 3 — Backend của Trịnh Thiên Quang

## Phân tích report2 và ranh giới thực hiện

Nguồn: `Report/Report progress 2/report2.tex`, bảng phân công hàng 11 và đặc tả Gemini/API/quyền của năm Lambda. Thay đổi PDF có sẵn trong workspace được giữ nguyên.

| Phụ trách | Hạng mục Sprint 3 | Xử lý trong phiên này |
|---|---|---|
| Quang | Chatbot/Gemini, SessionState/PendingAction, Dispatcher STS/Boto3, Formatter Gemini/ActionHistory/AppSync, Connector/History | Triển khai backend, kiểm thử tích hợp giả lập, đóng gói, cấu hình Lambda và tài liệu |
| Minh | IAM Prompt/Tool Schema, State Machine và IAM/hạ tầng chung | Không chỉnh sửa; cung cấp hợp đồng workflow, biến cấu hình và quyền cần ghép |
| Hùng | S3 schema/prompt, UI xác nhận, lịch sử và subscription | Đọc schema hiện có để tích hợp; không sửa module S3, schema S3 hay frontend |
| Huy | EC2 schema/LLM và DNS/failover | Đọc schema/module EC2 để tích hợp; không sửa EC2, DNS, Cognito hay shared/db.py |

Report2 có đoạn mô tả SQS và đoạn mô tả Step Functions. Backend mới bám theo kế hoạch Sprint 3 và đặc tả API/kiến trúc chính: xác nhận → Step Functions STANDARD → Dispatcher → Formatter. Không tự bổ sung SQS.

## Luồng mới và chế độ chạy

Các entrypoint Chatbot, Dispatcher, Formatter tự dùng luồng cloud khi `SATIBOT_MODE=cloud` hoặc đang chạy trong Lambda (`AWS_LAMBDA_FUNCTION_NAME` có giá trị). Lambda đã deploy không thể rơi sang heuristic chỉ vì thiếu Gemini key. Code PoC/AgentExecutor vẫn phục vụ demo local khi không bật cloud; hợp đồng xác nhận PoC không dùng cho production.

1. Gateway xác thực Cognito. Chatbot lấy `sub` từ authorizer, bỏ qua `user_id`, history, workspace, credentials, agent_state và model trong JSON client.
2. Chatbot đọc SessionState theo `(user_id, session_id)`, gắn kết quả server của tác vụ trước nếu còn tồn tại, gọi Gemini với các schema đã đăng ký.
3. Tool call phải đúng một công cụ đã đăng ký; JSON arguments phải đúng JSON Schema. Không tự sửa JSON lỗi thành `{}` hoặc thực thi công cụ lạ. Nhiều tool call được từ chối để xử lý từng hành động rõ ràng.
4. Tác vụ lưu vào PendingAction, TTL 900 giây, gắn dấu định danh kết nối RoleArn/ExternalId; phiên ghi với optimistic version. Nếu phiên xung đột, tác vụ chưa được trả cho người dùng và chưa thực thi; bản ghi mồ côi tự hết TTL.
5. Với thao tác thay đổi/kích hoạt tài nguyên: trả CONFIRMATION_REQUIRED. Client gửi `confirmed=true` và `action_id`; công cụ có confirmation_mode=explicit_phrase còn cần confirmation_text nguyên văn do user nhập. Server kiểm tra chủ sở hữu, thời hạn và trạng thái, ghi approved, StartExecution với tên action_id và input xác định. Tham số/role/credential gửi kèm xác nhận không có hiệu lực.
6. Với thao tác chỉ đọc: Chatbot tự StartExecution (không cần approved), trả HTTP 202. Dispatcher và Formatter chạy bất đồng bộ để không giữ Gateway request qua hai Lambda và một lần Gemini nữa. Trạng thái/kết quả cuối nhận qua AppSync/History; không coi ACCEPTED là thành công của AWS.
7. Dispatcher đọc lại PendingAction, đối chiếu schema, claim PENDING → EXECUTING bằng conditional update. Chỉ một worker claim được. Sau claim, đọc AwsConnections của người dùng và đối soát với kết nối lúc đề xuất; nếu user đã đổi role/ExternalId thì yêu cầu tạo tác vụ mới, không thực thi sang tài khoản khác. AssumeRole với ExternalId do server lưu. EC2/S3 nhận tham số phẳng; Lambda/IAM nhận tham số lồng theo hợp đồng module hiện hữu.
8. Kết quả được chuẩn hóa, che secret và lưu vào PendingAction trước khi trả envelope cho Formatter. Lần gọi lại một tác vụ COMPLETED chỉ nhận kết quả đã lưu. Lỗi kết nối STS được ghi thành kết quả lỗi, không chạy service. Không có quyền/không có kết nối cũng tạo kết quả lịch sử được nếu tác vụ đã claim hợp lệ.
9. Formatter gọi Gemini chỉ để tạo lời diễn giải; trạng thái, mã lỗi và data vẫn từ AWS. Nếu Gemini lỗi/sai cấu trúc thì dùng message của service. Conditional PutItem lưu một bản ActionHistory theo `(user_id, created_at)`; retry dùng lại bản đã lưu, không ghi đè kết quả.
10. Formatter ký IAM SigV4 để gọi mutation AppSync. Lỗi phát thông báo được ghi publication_status=FAILED và không đổi kết quả AWS. Workflow mẫu thử lại Formatter tối đa hai vòng thông báo; UI vẫn truy vấn History được. Sự kiện trùng có thể xảy ra khi publish thành công nhưng ghi cờ PUBLISHED thất bại; UI deduplicate theo action_id.

### Giới hạn cố ý của cơ chế chống lặp

Không tuyên bố exactly-once cho API AWS. Nếu Lambda chết sau khi AWS nhận thao tác nhưng trước khi lưu kết quả, tác vụ giữ EXECUTING; retry không tự chạy lại. Vận hành phải kiểm tra AWS/CloudWatch rồi tạo yêu cầu mới nếu cần. Không tự xóa lock hoặc “reset” trạng thái để làm check xanh. SDK bên trong module dịch vụ vẫn có chính sách retry riêng của tác giả.

## Files và trách nhiệm

| File/nhóm | Nội dung |
|---|---|
| backend/functions/chatbot/cloud_handler.py | HTTP /chat và /confirm, phiên server, Gemini, intent và xác nhận |
| backend/functions/dispatcher/cloud_handler.py | Claim, STS, adapter service, cache kết quả |
| backend/functions/formatter/cloud_handler.py | Lời đáp/fallback, conditional history, publication |
| backend/functions/*/index.py | Điểm vào giữ demo local và route sang cloud; Connector/History có API bổ sung |
| backend/shared/workflow_store.py | DynamoDB conditional writes, TTL checks, bounded sessions |
| backend/shared/tool_registry.py | Normalize schema có sẵn và JSON Schema validation |
| backend/shared/gemini.py | Secrets Manager cache 5 phút, HTTPS, tối đa 3 lần/6 giây mỗi lần, không lộ raw lỗi/key |
| backend/shared/appsync.py | Mutation với IAM SigV4, kiểm tra lỗi GraphQL dù HTTP 200 |
| backend/shared/http_api.py, cloud_errors.py, runtime.py | Claims, JSON validation, lỗi HTTP và masking secret |
| backend/graphql/* | GraphQL schema + JS NONE resolvers để đội hạ tầng/UI ghép; chưa tạo API AWS |
| scripts/package_sprint2.py | Năm ZIP, namespace đầy đủ, tùy chọn vendor runtime deps đúng Linux x86_64/Python 3.12 |
| scripts/verify_backend_artifacts.py | Import cloud handler và thư viện từ ZIP đã extract, chạy Python 3.12 isolated để phát hiện thiếu module/dependency |
| scripts/package_backend.py | Alias tương thích satibot_backend.zip và index.lambda_handler shim |
| infrastructure/backend_sprint2_app.py | Năm Lambda và cấu hình Sprint 3; không tạo/sửa các role, bảng hay workflow |
| tests/test_sprint3_backend.py | Test tích hợp DynamoDB/STS/Secrets Manager giả lập và mock Gemini/AppSync |

Registry đọc Lambda/EC2 schema hiện hữu và chuyển S3 input_schema/action sang tool call chung. Module IAM hiện chưa cung cấp `IAM_TOOL_SCHEMAS`; vì Prompt/Tool Schema IAM thuộc Minh, backend chỉ tự đăng ký IAM sau khi hằng này được bàn giao. Không tự suy diễn một tool IAM từ tên bất kỳ của LLM. Dispatcher vẫn có adapter cho module IAM khi schema được cung cấp.

## Hợp đồng API cho Hùng/Huy

Mọi HTTP route nghiệp vụ cần Gateway Cognito authorizer. Gateway cần cấu hình CORS cho origin frontend, Authorization/Content-Type và xử lý OPTIONS; không expose Dispatcher/Formatter thành route công khai. Backend không tự verify token từ header và không tin client truyền user_id.

### POST /chat hoặc /api/chat

```json
{"message":"Tăng RAM hàm demo lên 512 MB","session_id":"session-demo","region":"ap-southeast-1"}
```

Với tác vụ chỉ đọc cũng trả ACCEPTED/HTTP 202 và cùng hợp đồng cập nhật kết quả AppSync/History.

session_id được server sinh nếu bỏ trống. Client giữ giá trị server trả về cho các lượt tiếp. Nội dung tối đa 8000 ký tự; tối đa 20 messages và khoảng 48 KB history lưu trong một phiên, TTL phiên 24 giờ. Một lượt có tối đa một đề xuất thao tác. Client không gửi history làm nguồn tin cho cloud.

```json
{"status":"CONFIRMATION_REQUIRED","type":"action_confirmation","session_id":"session-demo","action_id":"act_...","expires_at":1794000000,"service":"lambda","tool_name":"update_function_resources","region":"ap-southeast-1","target_account_id":"123456789012","parameters":{"function_name":"demo","memory_mb":512},"message":"..."}
```

### POST /confirm hoặc /api/confirm

```json
{"confirmed":true,"action_id":"act_..."}
```

Với `s3_delete_bucket_safe`, response đề xuất trả `confirmation_mode=explicit_phrase` và `required_confirmation=DELETE <bucket-name>`. UI phải yêu cầu user nhập câu này rồi gửi thêm `confirmation_text` trong POST /confirm. Server đối chiếu cả câu user nhập lẫn tham số đã lưu; chỉ confirmed=true hoặc câu do LLM tự điền không đủ để xóa bucket. Không sửa schema/service S3 của Hùng để bỏ yêu cầu này.

Trả HTTP 202 với status ACCEPTED/type action_queued khi workflow được tiếp nhận. Không coi 202 là AWS thực thi thành công. Tác vụ đã COMPLETED trả HTTP 200 với kết quả đã lưu, không thực thi lại. Không rõ action/chủ khác → 404; hết hạn → 410; xung đột phiên/tác vụ → 409. JSON sai/schema sai → 400; Gemini/backend chưa cấu hình → 503; Gemini lỗi → 502. Xác nhận chỉ làm việc với action server đã phát, không nhận trực tiếp tool_name/parameters để thực thi.

### Kết nối

- POST /connections/external-id: ExternalId ổn định theo user. Tạo trust policy với Principal là role Connector/Dispatcher của SatiBot và điều kiện sts:ExternalId.
- POST /connections/verify: `{ "role_arn": "arn:aws:iam::123456789012:role/satibot/SatiBotAccessRole" }`. STS thành công mới ghi CONNECTED/account_id/verified_at. Không lưu credentials tạm.
- GET /connections: trả các trường công khai status, role_arn, account_id, verified_at của user.
- Các route cũng hỗ trợ /api/ prefix. Tên/path role phải được quyền sts:AssumeRole hiện có của Minh cho phép; ví dụ trên khớp policy hiện tại.

### GET /history hoặc /api/history

Query `limit=1..100`, `cursor`, `service`, `status`, `session_id`, `from`, `to`. Timestamp là ISO UTC kết thúc bằng Z. `from`/`to` dùng sort key, các trường còn lại dùng FilterExpression; limit giới hạn số item đánh giá nên trang có thể rỗng nhưng vẫn có cursor. Tiếp tục tới khi cursor=null. Cursor phải cùng user; client giữ nguyên bộ lọc trong quá trình phân trang.

Chi tiết: `/history?created_at=<timestamp được trả trong lịch sử>` dùng GetItem với partition luôn lấy từ sub. Summary không trả raw data; detail trả data đã mask. Không có route scan toàn bộ tài khoản.

### AppSync

Import schema.graphql; gắn publish.js vào Mutation.publishActionResult và subscribe.js vào Subscription.onActionResult với NONE data source. Mutation chỉ AWS_IAM và cấp appsync:GraphQL cho Formatter đúng field ARN. Subscription chỉ Cognito User Pool; owner lấy từ identity.sub trong resolver, không nhận user_id làm bộ lọc tin cậy từ client.

```graphql
subscription Results($session: ID!) {
  onActionResult(session_id: $session) {
    user_id session_id action_id created_at service tool_name status message data
  }
}
```

Frontend parse AWSJSON data; dedup bằng action_id; reconnect thì gọi History để bù sự kiện bị lỡ. Mutation publisher luôn chọn đủ các trường event để subscription có dữ liệu.

## Bàn giao tích hợp cho Minh — không thay đổi phần hạ tầng của Minh

### CDK parameters của backend assembly

| Parameter | Nguồn/ý nghĩa |
|---|---|
| ConnectionsTableName | AwsConnections, PK user_id |
| HistoryTableName | ActionHistory, PK user_id + SK created_at |
| SessionStateTableName | SessionState, PK user_id + SK session_id, TTL expires_at |
| PendingActionTableName | PendingAction, PK action_id, TTL expires_at |
| ChatbotRoleArn/DispatcherRoleArn/FormatterRoleArn/ConnectorRoleArn/HistoryRoleArn | Role từ IamStack |
| GeminiSecretArn | SecretString là khóa thuần hoặc JSON api_key/GEMINI_API_KEY; chỉ Chatbot/Formatter được đọc |
| ActionStateMachineArn | Workflow STANDARD; không dùng Express vì không có cùng tính chất idempotency StartExecution |
| AppSyncGraphqlUrl | https://<id>.appsync-api.ap-southeast-1.amazonaws.com/graphql |

Assembly tự gắn SATIBOT_MODE và tên bảng/env cho từng Lambda. GEMINI_MODEL có thể cấu hình riêng; mặc định gemini-2.5-flash. Không sử dụng key LLM_API_KEY trong cloud. Chưa có key/ARN thì request lỗi rõ ràng, không mock kết quả.

### Quyền cần ghép

Các quyền DynamoDB/STS/secret hiện có giữ đúng bảng phân quyền report2. Ngoài ra, luồng mới cần Minh cấp:

- Chatbot: states:StartExecution đúng State Machine; không cần quyền gọi Boto3 resource hoặc invoke Dispatcher trong process.
- Workflow role: lambda:InvokeFunction đúng Dispatcher và Formatter, không cấp vào mọi Lambda.
- Formatter: appsync:GraphQL đúng field Mutation.publishActionResult. Không mở quyền mutation Cognito.
- Formatter cần dynamodb:PutItem/UpdateItem trên ActionHistory. Replay lấy bản cũ bằng ReturnValuesOnConditionCheckFailure, không cần mở quyền Scan/GetItem cho Formatter.
- Tất cả Lambda ghi log dùng prefix tên hàm tương thích role hiện có; không cấp policy IAM mới trong backend assembly.

File `docs/sprint3-workflow-contract.asl.json` là ví dụ hợp đồng hoàn chỉnh để Minh ghép với State Machine do Minh sở hữu. Thay DispatcherArn/FormatterArn bằng ARN thật trước khi triển khai. Input workflow chỉ `{user_id, action_id}`. Không đưa credentials/parameters do client gửi vào state input.

Dispatcher trả envelope `{user_id,session_id,action_id,created_at,service,tool_name,result}`. Nếu không có result (từ chối trước claim), không gọi Formatter và không tự chạy lại mutation. Formatter trả `publication_status` để workflow retry riêng việc thông báo. Lỗi ghi lịch sử có thể retry Formatter; AWS resource task không chạy lại chỉ để sửa lời đáp hoặc thông báo.

### Thứ tự ghép để tránh phụ thuộc vòng

1. Bảng/role/secret từ hạ tầng chung.
2. Backend assembly (workflow/AppSync ARN có thể để trống khi synth lần đầu, request sẽ fail closed).
3. Minh tạo workflow/AppSync bằng ARN Lambda đã xuất; ghép schema/resolvers.
4. Cập nhật ARN workflow/AppSync và quyền invoke/publish vào role tương ứng; ghép API routes/authenticator.
5. Hùng ghép UI confirmation/subscription/history; nghiệm thu bằng hai Cognito users và AWS sandbox.

## Kiểm thử và trạng thái nghiệm thu

```bash
python -m pip install -r ci/requirements.txt
AWS_DEFAULT_REGION=ap-southeast-1 AWS_EC2_METADATA_DISABLED=true python -m pytest -q
python scripts/package_sprint2.py --with-dependencies
python scripts/verify_backend_artifacts.py
python scripts/build_frontend_ci.py
npx --yes aws-cdk@2 synth --app "python infrastructure/backend_sprint2_app.py" --output dist/cdk.out
```

CI thực hiện cùng các bước; dependency jsonschema được vendor cho CPython 3.12/Linux x86_64 để tránh đóng gói nhầm native rpds wheel theo Python của máy dev. Không đưa boto3, moto hay secret từ máy dev vào ZIP. Boto3 dùng runtime Lambda.

Các ca đã có test: schema lỗi/tool lạ/nhiều calls; client giả user/history/credentials; owner/TTL/approval; optimistic session conflict; hai worker claim; replay tác vụ hoàn tất; timeout sau khi AWS có thể nhận request; completion write failure giữ lock; STS dùng role/ExternalId từ server; đổi kết nối sau duyệt không đổi đích thực thi; xóa S3 cần câu xác nhận từ người dùng, không chỉ từ LLM; Gemini secret/retry; fallback; history một bản; publish fail/replay; History detail/filter theo owner; read-only qua đủ Chatbot→Dispatcher→Formatter; ngữ cảnh lượt sau; phiên không hỏng khi tác vụ cũ bị TTL xóa.

Chưa nghiệm thu live: Cognito/Gateway, trust policy và quyền target AWS thật; secret/key và Gemini quota/model trong tài khoản nhóm; State Machine/AppSync deployment; subscription hai người dùng và UI reconnect; GitHub checks sau push. Đây là các điểm cần ghép phần của thành viên khác và thông tin môi trường, không phải kết quả đã được giả lập thành thành công.

Tài liệu đối chiếu: [Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai), [Step Functions StartExecution](https://docs.aws.amazon.com/step-functions/latest/apireference/API_StartExecution.html), [AppSync enhanced subscription filters](https://docs.aws.amazon.com/appsync/latest/devguide/aws-appsync-real-time-enhanced-filtering.html).

### Kết quả kiểm tra cục bộ

- Python 3.12.14 và dependencies từ ci/requirements.txt: 349 tests và 8 subtests đều qua.
- Build năm ZIP có dependency đúng ABI CPython 3.12/Linux x86_64; verify_backend_artifacts.py import đủ năm cloud handlers và xác nhận jsonschema được nạp từ artifact, không dựa vào source workspace.
- Build frontend tĩnh, CDK synth backend thành công; template có đúng năm Lambda cloud và không tạo IAM/DynamoDB/State Machine của thành viên khác.
- GraphQL parse và node --check cho hai resolver JS thành công; JSON workflow mẫu có các tham chiếu state hợp lệ. Chưa coi đây là kiểm chứng runtime AppSync/Step Functions trên AWS.
- git diff --check không lỗi. Chưa triển khai AWS hoặc xác minh CI GitHub sau push.
