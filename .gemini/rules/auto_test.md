# 프로젝트 규칙: 웹페이지 업데이트 시 Playwright 자동 검사 및 자가 치유(Self-Healing)

사용자가 뉴스 웹페이지 관련 코드(`index.html`, `news.html`, `admin.html`, `server.py`, CSS, JS 등)를 수정하거나 업데이트를 요청할 때마다 다음 절차를 **의무적으로 자동 수행**합니다:

### 1. 업데이트 완료 직후 자동 검사 (Playwright 실행)
- 웹 파일 수정이 끝나면, 사용자가 별도 명령어를 입력하지 않아도 에이전트가 백그라운드/직접 실행으로 다음 명령을 수행합니다:
  ```bash
  python3 test_news_page.py --url http://localhost:8000
  ```

### 2. 자동 자가 수정 (Self-Healing / 오류 발견 시 자동 복구)
- 검사 결과에서 `[❌ FAIL]` 항목이 발생하거나 브라우저 콘솔 에러(`PageError`, `ConsoleError`)가 감지될 경우:
  1. 에러 로그 및 실패 원인을 즉시 분석합니다.
  2. 에러가 발생한 원인 파일(HTML/JS/CSS/서버 로직)을 즉시 스스로 수정합니다.
  3. 수정한 후 다시 `python3 test_news_page.py --url http://localhost:8000`를 재실행하여 모든 테스트가 `[✅ PASS]`가 될 때까지 검증을 완료합니다.

### 3. 사용자 보고
- 모든 검사가 정상 통과(`[✅ PASS]`)되었음을 확인한 후, 발생했던 오류 내역과 어떻게 자동으로 수정했는지(또는 이상이 없었는지) 요약 결과를 보고합니다.
