# Adaptive Red Team — Super Red Team

프론트 연동(3모델, 인증/CORS, API 어댑터 패치, 로컬 시연과 서버 배포)은 [연동 인계서](docs/frontend-integration.md)를 먼저 참고하세요. 새 기본 실행은 3모델이며 기존 2모델 요청과 저장 기록도 읽을 수 있습니다.

허가된 로컬 주문 API에서 서로 다른 두 모델을 **Independent** 방식으로 실행하는 AI Orchestrator Backend MVP입니다. API 키 없이 Mock으로 실행할 수 있습니다. 프론트엔드 시안은 변경하지 않았습니다.

## 실제 로컬 웹 서버의 접근 권한 점검

`web-access-v1`은 백엔드 운영자가 등록한 실제 루프백 웹 서버에 HTTP 요청을 보냅니다. `http://127.0.0.1:18880` 같은 명시적 IP와 포트만 지원하며, 일반 도메인·원격 IP는 이번 버전에서 지원하지 않습니다. 임의 URL을 실행 요청으로 받지 않습니다. 기존 `orders-v1`은 그대로 동작합니다.

1. `backend/targets.example.json`을 `backend/targets.local.json`으로 복사합니다.
2. 실제 청구서 상세 경로를 `allowed_paths`와 `resources`에 등록합니다. 예시의 `invoice` 경로와 ID는 **자리표시자이며 실제 경로를 확인한 값이 아닙니다**. GET으로 상태를 바꾸는 경로는 등록하지 마세요.
3. 각 리소스의 `owner`를 A/B로 지정하고, 해당 청구서에만 나오는 고유한 비공개 문구를 `marker`로 지정합니다(8자 이상). 두 계정에 각각 소유 리소스가 필요합니다. 공통 메뉴나 로그인 페이지 문구, 계정 비밀번호를 사용하지 마세요.
4. 로그인 후 확인할 `verify_path`와 계정별 `success_marker`를 설정합니다. 성공 문구는 비로그인 페이지에 나타나면 안 됩니다. 폼의 고정값은 `fields`, 자격 증명 환경변수 이름은 `fields_env`, 숨김 CSRF 필드 이름은 `csrf_field`에 둡니다. CSRF가 없으면 `csrf_field`를 생략합니다.
5. 아래 환경변수를 설정한 터미널에서 백엔드를 시작하거나 재시작합니다. 파일 경로는 시작 시 읽습니다. 설정 파일과 자격 증명은 실행 결과나 모델 프롬프트에 넣지 않습니다.

저장소 루트 기준 PowerShell:

```powershell
$env:REDTEAM_TARGETS_FILE = (Resolve-Path backend/targets.local.json).Path
$env:PAPER_CUP_USER_A = '<test-user-a>'
$env:PAPER_CUP_PASSWORD_A = '<test-password-a>'
$env:PAPER_CUP_USER_B = '<test-user-b>'
$env:PAPER_CUP_PASSWORD_B = '<test-password-b>'
.venv/Scripts/python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 --workers 1
```

별도 터미널에서 등록 확인 및 실행:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/scenarios
$body = @{ scenario = 'web-access-v1'; target_id = 'paper-cup'; mode = 'mock' } | ConvertTo-Json
$run = Invoke-RestMethod http://127.0.0.1:8000/executions -Method Post -ContentType application/json -Body $body
Invoke-RestMethod "http://127.0.0.1:8000/executions/$($run.execution_id)"
Invoke-RestMethod "http://127.0.0.1:8000/executions/$($run.execution_id)/report"
```

**웹 시나리오의 `mock`도 실제 서버에 로그인하고 HTTP 요청을 보냅니다.** Mock은 요청 순서를 결정하는 모델만 대체합니다. 실제 AI 탐색은 기존 live 설정과 두 live 모델을 지정해 사용합니다. 모델에는 허용 GET 경로와 리소스 소유 계정만 전달하며, 자격 증명·판정용 marker는 전달하지 않습니다.

계정별·에이전트별 세션을 분리하고, 익명 요청은 로그인 쿠키를 사용하지 않습니다. 로그인 POST만 예외적으로 허용하며 리다이렉트는 따라가지 않습니다. 로그인은 설정된 확인 페이지를 직접 GET해서 검증합니다. 각 응답은 스트리밍 중 32KiB로 제한합니다. 요청 예산은 모델이 선택한 GET 횟수이며, 첫 로그인에는 계정당 최대 3회의 추가 요청이 발생합니다. 전체 에이전트 시간 제한과 도구 호출당 5초 제한은 로그인에도 적용됩니다.

소유 계정의 정상 조회에서 marker를 확인한 뒤, 다른 계정 또는 익명 응답에 같은 marker가 있으면 노출로 판정합니다. HTTP 상태만으로 노출을 판단하지 않습니다. 등록 marker가 없거나 정상 조회가 실패하면 `needs_review`로 남깁니다. 리소스별 소유자·타 계정·익명 검사를 모두 판정하지 못하면 보고서의 `comparison.complete=false`입니다.

SQL injection·XSS·전체 사이트 자동 탐색·브라우저 실행은 포함하지 않습니다. 두 에이전트는 실제 서버 데이터를 공유합니다. 증거에는 응답 본문이 저장되므로 청구서 등 비공개 내용이 포함될 수 있습니다. 계정 자격 증명과 수집한 세션 쿠키 값은 본문에서 가리고, 응답 헤더와 로그인 본문은 저장하지 않습니다. 로컬 백엔드 API 자체의 인증은 기존과 같이 없으므로 루프백 바인딩으로 실행합니다.

## 설치와 실행

Python 3.11 이상, PowerShell, 저장소 루트 기준:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e "./backend[dev]"
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Swagger: <http://127.0.0.1:8000/docs>. **단일 프로세스, 동시 실험 1건**, 실험 내 두 에이전트는 비동기 병렬 실행합니다. 다른 실행이 진행 중이면 409입니다. 여러 worker나 서버 인스턴스로 실행하지 마세요. API 인증은 구현하지 않았으므로 로컬 루프백에서 사용합니다.

Mock 실행·조회·보고서 저장:

```powershell
$base = 'http://127.0.0.1:8000'
$created = Invoke-RestMethod "$base/executions" -Method Post -ContentType 'application/json' -Body '{"mode":"mock","environment":"vulnerable"}'
$id = $created.execution_id
Invoke-RestMethod "$base/executions/$id"
Invoke-RestMethod "$base/executions/$id/logs?after=0&limit=100"
Invoke-RestMethod "$base/executions/$id/report" | ConvertTo-Json -Depth 50 | Set-Content -Encoding utf8 report.json
# 필요할 때 취소:
Invoke-RestMethod "$base/executions/$id/cancel" -Method Post
# 수정 버전의 독립적인 새 실험:
Invoke-RestMethod "$base/executions" -Method Post -ContentType 'application/json' -Body '{"environment":"fixed"}'
```

POST는 레코드 저장 후 **202**와 ID를 반환합니다. 프론트는 1~2초마다 상태를 polling하고 종료 상태가 되면 보고서를 조회합니다. 실행 중 보고서도 조회 가능하며 `is_final=false`입니다. 별도 프론트 개발 서버는 같은 origin의 개발 프록시로 `/executions`, `/models`, `/scenarios`, `/health`를 연결하세요. CORS 전체 허용은 하지 않습니다.

## 실제 모델 연결

LiteLLM 어댑터는 OpenAI, Anthropic, Gemini 모델을 지원합니다. 모델 ID는 **서버 시작 전** 환경변수 `MODEL_A`, `MODEL_B`로 등록하며 요청에서 임의 공급자나 URL을 등록할 수 없습니다. `openai/`는 `OPENAI_API_KEY`, `anthropic/`는 `ANTHROPIC_API_KEY`, `gemini/`는 `GEMINI_API_KEY`를 사용합니다. 기본 ID의 사용 가능 여부는 공급자 계정에서 확인해야 합니다.

`backend/.env.example`은 환경변수 목록입니다. `.env`를 자동으로 읽지는 않습니다. PowerShell에서 서버를 시작하기 전에 설정하세요. 빈 요금 변수를 설정하지 않거나 제거하면 비용은 알 수 없음으로 처리합니다.

로컬 비밀 설정은 Git에서 제외되는 `backend/.env`에 저장합니다. 저장소 루트에서 `./start.ps1`을 실행하면 이 파일을 명시적으로 읽고 `127.0.0.1:8000`으로 시작합니다. 기존에 실행 중인 프로세스는 재시작해야 반영됩니다. 이미 터미널에 설정한 환경변수가 있으면 `.env`보다 우선합니다.

Gemini 두 모델을 같은 키로 비교하려면 다음 항목을 설정합니다:

```dotenv
GEMINI_API_KEY=<your-key>
MODEL_A=gemini/gemini-3.6-flash
MODEL_B=gemini/gemini-3.5-flash-lite
LITELLM_LOCAL_MODEL_COST_MAP=True
```

실행 요청도 등록한 두 모델을 지정합니다. `mode=live`는 유료 API를 호출할 수 있으며, 모델 목록은 `GET /models`에서 확인합니다. 아래 요청은 내장 주문 시나리오이며, 실제 웹 서버를 점검하려면 위의 대상 등록 설정을 완료하고 `scenario=web-access-v1`, `target_id=paper-cup`을 추가하세요.

```powershell
$body = @{
  mode = 'live'
  agents = @(
    @{ model = 'gemini/gemini-3.6-flash' },
    @{ model = 'gemini/gemini-3.5-flash-lite' }
  )
} | ConvertTo-Json -Depth 5
Invoke-RestMethod http://127.0.0.1:8000/executions -Method Post -ContentType application/json -Body $body
```

OpenAI와 Anthropic을 사용하는 경우의 수동 환경변수 설정 예시:

```powershell
$env:OPENAI_API_KEY = '<your-key>'
$env:ANTHROPIC_API_KEY = '<your-key>'
$env:MODEL_A = 'openai/gpt-4o-mini'
$env:MODEL_B = 'anthropic/claude-haiku-4-5'
# 현재 계약 요금을 확인한 경우에만 아래 네 변수를 설정 (단위: USD / 1M tokens)
# $env:MODEL_A_INPUT_USD_PER_MILLION = '...'
# $env:MODEL_A_OUTPUT_USD_PER_MILLION = '...'
# $env:MODEL_B_INPUT_USD_PER_MILLION = '...'
# $env:MODEL_B_OUTPUT_USD_PER_MILLION = '...'
```

서버 실행 후 별도 터미널에서:

```powershell
$body = @{
  mode = 'live'
  environment = 'vulnerable'
  agents = @(
    @{ model = 'openai/gpt-4o-mini'; role = 'access-control tester'; prompt_version = 'v1'; instructions = 'Start with legitimate access, then explore object ownership.' },
    @{ model = 'anthropic/claude-haiku-4-5'; role = 'access-control tester'; prompt_version = 'v1'; instructions = 'Check authentication and cross-account visibility.' }
  )
  budget = @{ seconds = 60; requests = 12; model_calls = 12; tokens = 100000; max_output_tokens = 1000 }
} | ConvertTo-Json -Depth 10
Invoke-RestMethod 'http://127.0.0.1:8000/executions' -Method Post -ContentType 'application/json' -Body $body
```

역할·추가 지시·모델·프롬프트 버전을 에이전트별로 설정합니다. `prompt_version=v1`만 구현되어 있습니다. `app/prompts.py`의 시스템 프롬프트와 모델 출력 스키마, 각자의 관찰·이전 가설을 매 호출에 전달합니다. 모델은 다음 HTTP 검증을 선택하거나 종료합니다. 고정 목록은 **Mock 모델 시뮬레이터에만** 있으며 실제 모델은 새 숫자 주문 ID 등 허용된 경로를 선택할 수 있습니다. 모델의 계획·가설은 권한을 변경하지 못합니다.

Mock과 live 모두 같은 실행 루프, 시나리오별 HTTP Runner와 평가기, 저장소, 집계기를 사용합니다. live는 **실제 모델 API 호출**을 의미하며, 실제 웹 서버 연결 여부는 `scenario=web-access-v1`으로 선택합니다. [LiteLLM 공식 문서](https://docs.litellm.ai/docs/)의 비동기 completion API를 사용합니다. SDK의 자동 재시도는 끄고 오케스트레이터가 일시적 오류에 한 번만 재시도합니다.

## API와 계약

| API | 동작 |
| --- | --- |
| `GET /health` | 상태·운영 제한 |
| `GET /models` | 등록 ID, 모드, 키 설정 여부, 비용 제한 지원 여부; 키 값은 미반환 |
| `GET /scenarios` | 등록 환경·도구·점검·미지원 옵션 |
| `POST /executions` | `ExecutionRequest`, 202 `{execution_id,status}` |
| `GET /executions/{id}` | 설정, 실행/에이전트 상태, 사용량, 테스트, 증거, 판정 |
| `GET /executions/{id}/logs?after=0&limit=100` | 순번 기반 이벤트와 `next_after` |
| `POST /executions/{id}/cancel` | 취소 처리 후 현재 결과. 종료된 실행에는 멱등 |
| `GET /executions/{id}/report` | 저장 기록 기반 JSON 집계 |

잘못된 ID는 404, 미지원 옵션·알 수 없는 필드·모델은 422입니다. `Execution`, `AgentResult`, `TestAttempt`, `Evidence`, `Finding`, `CheckResult` 계약은 `backend/app/contracts.py`, 요청 스키마는 `/openapi.json`에서 확인합니다. AgentRun은 AgentResult의 상태·추적 필드로 함께 표현합니다.

모델 출력 계약:

```json
{"hypothesis":"An authenticated account may read a foreign order","action":"http","request":{"method":"GET","path":"/orders/202","account":"A"}}
```

종료: `{"hypothesis":"Enough evidence collected","action":"stop"}`. 여분 필드, 자유 URL, 임의 헤더, GET 외 메서드, 셸 명령은 허용되지 않습니다. JSON 형식 오류는 해당 에이전트만 실패시킵니다.

교체 가능한 연동 지점은 다음과 같습니다. 현재 원격 Runner·평가 엔진의 담당자/계약은 확정되지 않았으므로 이 계약은 **로컬 MVP 제안**입니다.

* `ToolGateway.execute(ToolRequest) -> {request,status_code,body,duration}`: 계정 선택자를 도구 계층에서만 인증 헤더로 바꿉니다. `close()`로 정리합니다. 실제 Runner 연결 시 그 Runner에서도 네트워크·리다이렉트·크기·시간 제한을 유지해야 합니다.
* `evaluate(TestAttempt, observation) -> {check,verdict,reproduced,reason}`: 오케스트레이터에서 동기 호출하며 결과는 `apply_evaluation`으로 연결합니다. 모델과 Runner에는 평가용 소유권 맵이나 판정을 전달하지 않습니다. 원격 비동기 평가 엔진이 확정되면 이 호출 지점을 비동기로 교체해야 합니다.
* 공급자 `complete(spec,messages,output_tokens,timeout) -> Reply(content,input_tokens,output_tokens)`: 테스트에서는 네트워크 없이 대체 공급자를 주입합니다.

## 격리·예산·실패

`orders-v1`에서는 각 에이전트마다 **새 ASGI 애플리케이션과 주문 데이터**를 만듭니다. HTTPX의 ASGITransport가 실제 HTTP 요청/응답 의미론으로 로컬 앱을 호출하며 해당 Runner는 소켓을 열지 않습니다. 모델이 받을 정보·도구·예산은 동일하고, 관찰/가설 컨텍스트는 분리됩니다. 주문 fixture의 상태 변경은 전부 거부합니다. `web-access-v1`의 실제 네트워크·로그인·공유 데이터 제한은 위의 웹 서버 점검 절을 참고하세요.

* 주문 fixture의 Gateway는 `/orders/{1~6자리 숫자}`만 받습니다. URL·쿼리·인코딩 우회·리다이렉트를 거부하고 응답 크기를 제한합니다. 로컬 fixture의 GET만 도달 가능합니다.
* 테스트 계정 토큰은 `gateway.py`/fixture 내부에만 존재합니다. 모델에는 A/B 선택자만 제공합니다. 증거는 Authorization 헤더를 저장하지 않고 계정 선택자와 본문을 저장합니다. 오류도 원문 대신 유형을 기록해 공급자 키 유출을 피합니다. 모델 API 키는 Runner에 전달하지 않습니다.
* **에이전트별 동일 예산**: 경과 시간, HTTP 요청, 모델 호출(재시도 포함), 토큰, 선택적 비용 한도. 각 에이전트는 작업을 하나씩 수행하므로 검사·예약 구간에 await가 없어 중복 소비가 없습니다. 서로의 예산을 빌려 쓰지 않습니다. 시간/요청/모델 호출/토큰/비용 상한은 입력 스키마로도 제한합니다.
* 호출 전에 UTF-8 바이트 수와 프레이밍 여유분으로 입력 토큰을 보수적으로 예약하고 최대 출력 토큰을 더합니다. 성공 후 실제 사용량으로 정산합니다. 실패·취소된 호출은 예약을 소비한 것으로 유지하며 알 수 없는 사용량은 합계에서 추측하지 않고 `token_usage_complete=false`로 표시합니다.
* `cost_usd` 한도는 두 모델의 서버 요금이 설정된 경우에만 수락합니다. 비용은 설정한 토큰 요금 기반 **추정치**이며 공급자 과금 상한을 보증하지 않습니다. 요금 미설정이나 청구 불명 호출이 있으면 비용은 `null`과 이유를 반환합니다. Mock 정상 호출만 토큰/비용이 0입니다.
* 호출 timeout 15초, HTTP timeout 5초, 에이전트 총 시간 기본 60초. 모델 일시 오류만 최대 한 번 재시도합니다. 시간초과·형식 오류·도구/평가 오류는 미재현/통과가 아니라 실패/판정 미정입니다. 모델 한쪽이 실패해도 다른 쪽은 계속 실행하며 전체 `failed`, `partial_failure=true`로 표시합니다.
* 예산 소진은 `completed`와 `termination_reason=budget_exhausted:...`로 기록합니다. 미실시 점검은 판정 없이 이유를 남기고, 보고서 `comparison.complete=false`로 비교 불완전성을 표시합니다. 모델 자발 종료 시에만 완전 비교로 표시합니다.
* 취소는 신규 발행을 중단하고 진행 중 await를 취소한 뒤 부분 증거를 보존합니다. `cancellation_processed` 이벤트에 로컬 취소 여부와 **공급자 청구 취소 미보장**을 기록합니다.
* SQLite에 설정·상태·이벤트·사용량·증거 메타데이터를 저장하고, `DATA_DIR/{execution_id}/{agent_run_id}/*.json`에 요청/응답과 SHA-256을 저장합니다. 기본 데이터 경로는 실행 디렉터리의 `data`입니다. 백엔드 재시작 시 미완료 실행만 실패로 정리하고 재실행하지 않습니다. 정상 종료 중 작업도 `server_shutdown` 실패로 남깁니다.

## 평가와 보고서

Fixture는 A의 주문 101, B의 주문 202를 동일한 초기 상태로 제공합니다. 취약 버전은 인증 후 소유권 검사를 생략하고, 수정 버전은 타인 주문을 403으로 막습니다. 별도의 평가 맵은 `evaluator.py`에 있으며 모델 컨텍스트에 포함하지 않습니다. 정상 자기 주문 조회, 타인 주문의 실제 필드 노출, 익명 접근을 평가합니다. HTTP 200만으로 재현 성공이라 하지 않습니다.

추적: 모델 가설 → attempt → evidence ID/파일/해시 → evaluation → finding 및 check. `reproduced`는 실제 비인가 데이터 노출이면 true, 해당 시도의 명시적 차단이면 false, 정상 접근 기준선·판정 불가·오류이면 null입니다. 체크 진행(`not_run/completed`)과 판정(`vulnerable/pass/needs_review/not_applicable`)을 구분합니다. 오류는 통과로 바꾸지 않습니다.

보고서는 범위/설정, 시도별 방법/요청/증거/판정, 영향/개선 방법, 체크리스트/미실시 이유, 방법별 재현/미재현/오류 수, 전체 벽시계 시간, 모델별 시간/토큰/추정 비용을 포함합니다. 같은 점검의 노출을 하나의 취약점 키로 합치되 **모든 모델·시도·증거 ID**를 보존하고 A 전용/B 전용/공통 발견을 집계합니다. 재현률의 분모는 판정 가능한 비인가 접근 시도이며 기준선·오류·판정 불가는 제외합니다.

미재현은 안전 보장이 아닙니다. 소유권 oracle은 개별 관찰 평가용이며 전체 취약점 정답 집합은 정의하지 않았으므로 탐지율/오탐/F1은 계산하지 않습니다. `environment=fixed`로 새 실행은 가능하지만 전후 보고서를 자동 연결하는 기능은 없습니다. JSON 문자열은 신뢰할 수 없는 데이터이므로 프론트에서 `textContent` 등으로 렌더링하고 `innerHTML`에 넣지 마세요.

## 검증

저장소 루트에서:

```powershell
.venv\Scripts\python.exe -m pytest backend\tests -q
```

테스트는 Mock → API → 두 독립 루프 → 로컬 ASGI HTTP → 증거 파일/SQLite → 평가 → JSON 보고서 전체 흐름, 취약/수정 환경, 재시작 복구, 부분 실패, 취소, timeout, 예산, 미지원 옵션/범위 우회 거부를 검증합니다. LiteLLM은 설치된 SDK의 비동기 호출을 가짜 응답으로 검증합니다. **실제 공급자 API 호출·과금·품질은 검증하지 않았습니다.**

웹 대상 확장 후 **47 passed**: 기존 35개와 웹 실행·CSRF/세션·취약/차단 판정·인증 실패·대상/경로 제한·응답 크기 제한 등을 검증했습니다. 별도 임시 루프백 HTTP 서버에 실제 소켓으로 연결하는 로그인·인증/익명 요청 테스트도 포함합니다. 사용자의 18880 서버에 대한 전체 점검 결과를 의미하지 않습니다.

2026-10-31 로컬 검증 결과: Python 3.13.3, FastAPI 0.162.0, Pydantic 2.14.2, LiteLLM 1.106.6에서 **35 passed**. Uvicorn을 루프백 포트 8765로 실제 실행해 health/OpenAPI, 실행 202, polling, 보고서 HTTP 200도 확인했습니다. 두 환경 각각 총 10회 도구 호출 후 취약 환경은 고유 취약점 1건, 수정 환경은 0건이었습니다. 검증용 서버는 종료했으며 보고서 예시는 git에서 제외되는 `backend/data/smoke-vulnerable-report.json`, `backend/data/smoke-fixed-report.json`에 남겼습니다.

## 미구현 범위

Sequential/Debate, Black/White-box, 외부 임의 대상, 브라우저/코드 도구, 쓰기/파괴적 테스트, 예약, 계획 승인 UI, 분산 실행/재개, 자동 최적화, 전후 자동 비교, HTML/PDF 보고서, 운영용 인증/권한과 프론트엔드는 제외합니다. 미지원 요청 필드는 무시하지 않고 422로 반환합니다. 원격 Runner/평가 엔진 연동은 위의 교체 지점까지만 제공합니다. SQLite 문서 전체 갱신은 제한된 MVP 규모용이며 대규모 이벤트/증거에 최적화하지 않았습니다.
