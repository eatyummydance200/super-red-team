# 프론트 연동 인계

대상 프론트: https://super-red-team-frontend.vercel.app/

현재 공개 사이트에는 변경을 배포하지 않았습니다. 연결된 GitHub 계정은 `5ceansdeep/super-red-team-frontend`에 읽기 권한만 있으며, 연결된 Vercel 계정에도 이 프로젝트가 없습니다. 백엔드 호스팅도 아직 없습니다. 아래 로컬 검증과 공개 연결을 구분해야 합니다.

## 로컬 시연

백엔드는 저장소 루트에서 `./start.ps1`로 실행합니다. `backend/.env`를 읽고 127.0.0.1:8000으로 바인딩합니다. 키는 이미 로컬 파일에 저장돼 있으며 Git에서 제외됩니다.

실제 프론트 소스를 기준으로 만든 연동 패치는 `integration/frontend.patch`입니다. 프론트 담당자가 프론트 저장소에서 적용합니다:

```powershell
git apply --check ../super-red-team/integration/frontend.patch
git apply ../super-red-team/integration/frontend.patch
```

프론트의 `.env.local`:

```dotenv
VITE_USE_MOCK=false
VITE_API_BASE_URL=/api
VITE_EXECUTION_MODE=mock
BACKEND_PROXY_URL=http://127.0.0.1:8000
BACKEND_PROXY_TOKEN=<backend/.env의 BACKEND_API_TOKEN 값>
```

`BACKEND_PROXY_TOKEN`은 Vite 개발 서버에서만 사용합니다. `VITE_` 접두사를 붙이면 브라우저 번들에 공개되므로 붙이지 않습니다. 개발 프록시는 `/api`를 제거하고 로컬 백엔드로 전달합니다. `npm run dev -- --host 127.0.0.1` 실행 후 http://127.0.0.1:5173 에 접속합니다.

이 작업에서 실행한 검증용 프론트 복사본은 `.integration/frontend`에 있으며 원본 프론트 저장소는 수정하지 않았습니다. 현재 이 복사본의 개발 서버로 로컬 시연이 가능합니다. 원본에 패치를 적용한 다음에는 복사본의 서버를 종료하고 원본에서 개발 서버를 실행하면 됩니다.

`VITE_USE_MOCK=false`는 프론트의 가짜 API를 끈다는 뜻입니다. 모델 호출 여부는 별도입니다. 기본 `VITE_EXECUTION_MODE=mock`는 실제 백엔드와 HTTP Runner를 사용하되 모델의 요청 선택만 고정합니다. 실제 모델은 연결 설정에서 `실제 AI 점검`을 선택하면 됩니다. 등록된 웹 시나리오는 Mock 모드에서도 실제 서버에 로그인하고 요청합니다.

패치는 요청 필드 변환, 시나리오 환경 선택, 에이전트 상태와 로그 응답 변환, 보고서 표시, 세션 접근 토큰 입력, 서버의 최근 실행 목록 조회를 포함합니다. 보고서는 백엔드가 계산하며 프론트는 단위 변환 외에 점수나 재현율을 계산하지 않습니다. 프롬프트 버전은 실제 지원하는 `v1`만 표시합니다.

## 공개 사이트 연결

1. 백엔드를 단일 프로세스로 계속 실행할 수 있는 Docker/VM 호스팅에 올립니다. `Dockerfile`과 `compose.yaml`을 제공합니다. SQLite와 증거 파일은 `/app/data` 영속 볼륨에 저장합니다. 현재 모델 실행은 프로세스 안의 백그라운드 작업이므로 재시작·다중 worker·수평 확장에 의존하면 안 됩니다.
2. 서버 환경변수에 모델 키, 모델 설정과 단가, 충분히 긴 임의 `BACKEND_API_TOKEN`, `REQUIRE_API_AUTH=true`, 정확한 `CORS_ALLOWED_ORIGINS=https://super-red-team-frontend.vercel.app`를 설정합니다. Docker는 인증 키가 없으면 시작을 거부합니다. Health 외 API는 Bearer 인증을 요구합니다.
3. 백엔드 앞에 HTTPS 리버스 프록시/호스팅 TLS를 설정합니다. Compose의 포트는 127.0.0.1:8000만 열므로 리버스 프록시가 전달해야 합니다. 모델 키는 서버 비밀 설정에만 저장합니다.
4. 프론트 소유자가 패치를 적용하고 Vercel에 `VITE_USE_MOCK=false`, `VITE_API_BASE_URL=https://<백엔드 주소>`, `VITE_EXECUTION_MODE=mock`을 설정해 다시 빌드·배포합니다. 이후 사용자가 화면의 연결 설정에 **백엔드 접근 토큰**을 입력합니다. 토큰은 탭의 sessionStorage에만 저장되며, 모델 API 키를 입력하면 안 됩니다. 접근 토큰을 아는 사용자는 실행 및 보고서 전체에 접근할 수 있으므로 팀 내부에서만 공유합니다.
5. Mock의 시작→상태 polling→보고서→취소를 확인한 뒤 실제 AI 실행을 선택합니다. 공개 프록시에 서버 토큰을 고정해 인증 없이 전달하는 구조는 만들지 마세요. 누구나 유료 모델을 호출할 수 있습니다.

로컬 `127.0.0.1:18880`은 배포된 서버에서 사용자의 PC를 가리키지 않습니다. 기본 Compose는 로컬 대상 등록을 끄고 내장 주문 시나리오만 제공합니다. 실제 웹 대상은 해당 백엔드가 도달할 수 있는 허가된 환경에서 따로 등록해야 하며, 현재 Runner는 운영자가 등록한 루프백 IP 대상만 지원합니다.

## 모델과 비용

기본 live 모델은 `deepseek-flash`, `qwen3.8-flash`, `glm-5.3-flash`입니다. 각 `POST /chat/completions` 호출은 인계표의 추론 옵션을 그대로 전송합니다. Qwen 주소는 `MODEL_B_BASE_URL`로 운영자가 변경할 수 있습니다. 키 이름은 `DEEPSEEK_API_KEY`, `QWEN_API_KEY`, `ZAI_API_KEY`입니다. 기존 OpenAI·Anthropic·Gemini 접두사 모델도 유지합니다.

단가는 환경변수의 USD/1M 값을 사용합니다. 이번 로컬 설정에는 인계표 값인 DeepSeek 0.30/1.20, Qwen 0.15/0.47, GLM 0.15/0.50을 입력했습니다. 캐시·혼잡 할인이나 실제 청구액을 조회하는 기능은 아닙니다. 요금을 모르면 변수를 비워 `null`로 표시합니다. `usage.completion_tokens`를 과금 출력량으로 사용하고 그 안에 포함된 reasoning 상세 토큰을 중복 가산하지 않습니다. 실패한 호출에서 usage를 받지 못하면 토큰과 비용 합계를 `null`로 둡니다.

실제 연결 확인은 각 공급자에 민감한 데이터 없는 짧은 JSON 요청을 1회씩 보내 수행했습니다. 세 모델 모두 JSON과 usage를 반환했습니다. 이 확인은 서버 전체를 세 모델로 분석했다는 뜻은 아닙니다. 비공개 송장 본문의 외부 전송은 앞선 자동 승인 검토에서 거부됐으며, 이번 확인에도 전송하지 않았습니다.

## 보고서 규칙

- CVSS와 severity는 평가 엔진이 명시적으로 제공할 때만 기록합니다. 현재 기본 엔진은 수치를 만들지 않아 둘 다 `null`입니다. 사람의 CVSS 검토 입력 UI/API는 아직 없습니다.
- 확정 취약점만 점수에 포함합니다. `reproduce_rate`는 동일한 발견 키에 대한 판정 가능한 성공/차단 시도를 모두 분모에 넣고, 오류·기준 조회·판정 보류를 제외합니다. 명시적으로 받은 `false_positive`와 `needs_review` 레이블은 구분해서 집계하며, 재현되지 않았다는 이유만으로 오탐으로 바꾸지 않습니다.
- 조합은 **같은 실행의 모델 부분 집합**입니다. 3모델이면 7개, 전체 3모델 조합만 `is_current=true`입니다. 다른 시나리오·환경·과거 실행을 섞은 순위는 제공하지 않습니다.
- 점수는 Σ(CVSS × 해당 조합의 재현율)입니다. CVSS/재현율을 모르거나 조합에 미완료 점검이 있으면 `null`입니다. 완전히 점검했고 확정 발견이 없는 조합의 점수 0은 알려진 값입니다.
- 방법별 토큰·비용·시간은 해당 요청을 선택한 모델 호출과 HTTP 시도에 귀속합니다. 종료 결정과 요청 전 실패의 사용량은 방법에 임의 배분하지 않고 실행 합계에만 포함합니다. 구버전 기록에서 방법별 사용량이 없으면 `null`입니다.
- 진행률은 등록 체크리스트의 관측 완료 비율입니다. 취약점 탐지율이나 남은 시간 예측이 아닙니다. `phase`는 실제 수행 단계를 나타냅니다.
- 3모델이 기본이며 기존 2모델 요청과 저장 기록도 호환됩니다. 4개 이상·중복 모델·미지원 협업 방식은 422입니다.

추가 API `GET /executions?limit=20&offset=0`은 `executions[]`, `next_offset`을 반환합니다. 로그는 `events`, `next_after` 구조를 유지하고 `message`, `level`을 추가했습니다. 모델 메타데이터 단가는 `input_per_mtok`, `output_per_mtok`이며 단위는 USD/1M입니다. 시나리오는 `prompt_versions`, `roles`, `environment_versions`, `allowed_paths`를 제공합니다.
