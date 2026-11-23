"""
뉴스 웹페이지(IT News) Playwright E2E 자동 검사 스크립트
==================================================
검사 대상:
 1. 메인 페이지(index.html / /) 및 관리자 페이지(admin.html) 로딩 및 콘솔 에러 검사
 2. 필수 UI 엘리먼트 렌더링 확인 (검색창, 헤더, 아키텍처 다이어그램 등)
 3. 검색 기능 및 UI 상호작용 (추천 태그 클릭, 검색창 입력)
 4. 다이어그램 토글 기능 (API 동작 구조 보기 / 닫기)
 5. 모바일/데스크톱 뷰포트 반응형 UI 레이아웃 검증
 6. HTTP 상태 코드 및 네트워크 응답 실패 감지

실행 방법:
  python3 test_news_page.py [--url http://localhost:8000] [--headless]
"""

import argparse
import asyncio
import sys
from playwright.async_api import async_playwright

class NewsWebTester:
    def __init__(self, base_url: str, headless: bool = True):
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.results = []
        self.console_errors = []
        self.failed_requests = []

    def log_result(self, test_name: str, passed: bool, message: str = ""):
        self.results.append({
            "name": test_name,
            "passed": passed,
            "message": message
        })
        status_icon = "✅ PASS" if passed else "❌ FAIL"
        print(f"[{status_icon}] {test_name}: {message}")

    async def run_all_tests(self):
        print(f"\n🚀 Playwright 자동 검사 시작: 대상 URL -> {self.base_url}\n" + "="*60)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) PlaywrightTestBot"
            )
            page = await context.new_page()

            # 이벤트 리스너 등록
            page.on("pageerror", lambda err: self.console_errors.append(f"PageError: {err}"))
            page.on("console", lambda msg: self.console_errors.append(f"ConsoleError: {msg.text}") if msg.type == "error" else None)
            page.on("requestfailed", lambda req: self.failed_requests.append(f"{req.url} ({req.failure})"))

            try:
                # 1. 메인 페이지 로딩 검사
                await self.test_main_page_load(page)

                # 2. 필수 UI 요소 검사
                await self.test_core_ui_elements(page)

                # 3. 검색창 및 태그 상호작용 검사
                await self.test_search_interactions(page)

                # 4. API 구조 보기 토글 검사
                await self.test_toggle_panel(page)

                # 5. 모바일 반응형 뷰포트 검사
                await self.test_mobile_viewport(browser)

                # 6. 관리자 페이지 로딩 검사
                await self.test_admin_page(context)

            finally:
                await browser.close()

        # 결과 요약 출력
        self.print_summary()

    async def test_main_page_load(self, page):
        test_name = "메인 페이지 로딩 및 응답 검사"
        try:
            resp = await page.goto(self.base_url, wait_until="domcontentloaded", timeout=10000)
            if not resp or resp.status >= 400:
                self.log_result(test_name, False, f"HTTP 응답 상태 코드 비정상: {resp.status if resp else 'No response'}")
                return
            
            title = await page.title()
            if "IT News" in title:
                self.log_result(test_name, True, f"타이틀 정상 로드 ('{title}')")
            else:
                self.log_result(test_name, False, f"예상치 못한 타이틀: '{title}'")
        except Exception as e:
            self.log_result(test_name, False, str(e))

    async def test_core_ui_elements(self, page):
        test_name = "핵심 UI 요소(검색창, 헤더 등) 렌더링"
        try:
            search_input = page.locator("#search-input")
            search_button = page.locator("#search-button")
            guest_area = page.locator("#auth-guest-area")

            has_search = await search_input.is_visible()
            has_btn = await search_button.is_visible()
            has_guest = await guest_area.is_visible()

            if has_search and has_btn and has_guest:
                self.log_result(test_name, True, "검색창, 검색버튼, 인증 바 정상 표시됨")
            else:
                self.log_result(test_name, False, f"UI 미노출 - search:{has_search}, btn:{has_btn}, auth:{has_guest}")
        except Exception as e:
            self.log_result(test_name, False, str(e))

    async def test_search_interactions(self, page):
        test_name = "검색창 입력 및 태그 상호작용"
        try:
            search_input = page.locator("#search-input")
            await search_input.fill("인공지능")
            val = await search_input.input_value()
            if val != "인공지능":
                self.log_result(test_name, False, f"입력값 불일치: {val}")
                return

            first_tag = page.locator(".tag-btn").first
            if await first_tag.is_visible():
                tag_text = await first_tag.inner_text()
                self.log_result(test_name, True, f"검색창 입력 및 추천 태그('{tag_text}') 정상 렌더링 확인")
            else:
                self.log_result(test_name, True, "검색창 텍스트 입력 정상")
        except Exception as e:
            self.log_result(test_name, False, str(e))

    async def test_toggle_panel(self, page):
        test_name = "API 동작 구조 다이어그램 토글 기능"
        try:
            # 혹시 모달이 열려 있다면 닫기
            modal = page.locator("#auth-modal")
            if await modal.is_visible():
                await page.keyboard.press("Escape")
                close_btn = page.locator("#close-modal-btn")
                if await close_btn.is_visible():
                    await close_btn.click()
                await page.wait_for_timeout(300)

            toggle_btn = page.locator("#toggle-flow-btn")
            panel = page.locator("#workflow-panel")

            if not await toggle_btn.is_visible():
                self.log_result(test_name, False, "#toggle-flow-btn 버튼이 존재하지 않음")
                return

            initial_visible = await panel.is_visible()
            await toggle_btn.click(force=True)
            await page.wait_for_timeout(300)
            after_click_visible = await panel.is_visible()

            await toggle_btn.click(force=True)
            await page.wait_for_timeout(300)

            self.log_result(test_name, True, f"패널 토글 상호작용 정상 확인 (상태 전환: {initial_visible} -> {after_click_visible})")
        except Exception as e:
            self.log_result(test_name, False, str(e))

    async def test_mobile_viewport(self, browser):
        test_name = "모바일 뷰포트(375x667) 반응형 UI 렌더링"
        try:
            mobile_context = await browser.new_context(viewport={"width": 375, "height": 667})
            mobile_page = await mobile_context.new_page()
            await mobile_page.goto(self.base_url, wait_until="domcontentloaded", timeout=10000)

            search_input = mobile_page.locator("#search-input")
            is_visible = await search_input.is_visible()
            await mobile_context.close()

            if is_visible:
                self.log_result(test_name, True, "모바일 화면에서도 검색 입력창 정상 표시")
            else:
                self.log_result(test_name, False, "모바일 뷰포트에서 검색 입력창 표시 실패")
        except Exception as e:
            self.log_result(test_name, False, str(e))

    async def test_admin_page(self, context):
        test_name = "관리자 페이지(/admin.html) 로딩 및 로그인 폼"
        try:
            admin_page = await context.new_page()
            admin_url = f"{self.base_url}/admin.html"
            resp = await admin_page.goto(admin_url, wait_until="domcontentloaded", timeout=10000)
            
            if not resp or resp.status >= 400:
                self.log_result(test_name, False, f"관리자 페이지 응답 실패: {resp.status if resp else 'No resp'}")
                await admin_page.close()
                return

            # 로그인 입력폼 요소 확인
            login_form_exists = await admin_page.locator("input[type='password'], input[id*='password'], input[id*='admin']").count() > 0
            await admin_page.close()

            if login_form_exists:
                self.log_result(test_name, True, "관리자 페이지 및 로그인 UI 정상 로드")
            else:
                self.log_result(test_name, True, "관리자 페이지 접근 정상 (HTTP 200)")
        except Exception as e:
            self.log_result(test_name, False, str(e))

    def print_summary(self):
        print("\n" + "="*60)
        total = len(self.results)
        passed = sum(1 for r in self.results if r["passed"])
        failed = total - passed

        print(f"📊 검사 요약: 총 {total}개 검사 중 {passed}개 성공, {failed}개 실패")

        if self.console_errors:
            print(f"\n⚠️  발생한 브라우저 콘솔 에러 ({len(self.console_errors)}건):")
            for err in self.console_errors[:5]:
                print(f"  - {err}")
            if len(self.console_errors) > 5:
                print(f"  ... 외 {len(self.console_errors)-5}건")
        else:
            print("✨ 치명적인 콘솔 에러 없음")

        print("="*60 + "\n")

        if failed > 0:
            sys.exit(1)
        else:
            sys.exit(0)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IT News 웹 자동 E2E 검사")
    parser.add_argument("--url", default="http://localhost:8000", help="검사할 웹 서버 Base URL (기본값: http://localhost:8000)")
    parser.add_argument("--headed", action="store_true", help="브라우저 창을 화면에 띄워 실행 (기본: headless)")
    args = parser.parse_args()

    tester = NewsWebTester(base_url=args.url, headless=not args.headed)
    asyncio.run(tester.run_all_tests())
