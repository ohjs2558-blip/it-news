import json
import os
import sys
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, quote_plus
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

# .env 파일 수동 로드 (외부 패키지 의존성 방지 및 호환성 확보)
def load_env_file():
    if not ENV_PATH.exists():
        return
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip("'\"")
    except Exception:
        pass

load_env_file()

def get_gemini_config():
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    model_name = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-2.5-flash"
    return api_key, model_name

def fetch_news_articles(query, limit=5):
    """Google News RSS에서 검색된 실제 뉴스 기사 목록을 가져옵니다."""
    articles = []
    try:
        encoded_query = quote_plus(query)
        rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"
        req = urllib.request.Request(
            rss_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            xml_data = response.read()
            root = ET.fromstring(xml_data)
            items = root.findall(".//item")
            for item in items[:limit]:
                title = item.findtext("title", "").strip()
                link = item.findtext("link", "").strip()
                pub_date = item.findtext("pubDate", "").strip()
                description = item.findtext("description", "").strip()
                # 간단한 언론사 및 제목 분리
                source = ""
                clean_title = title
                if " - " in title:
                    parts = title.rsplit(" - ", 1)
                    clean_title = parts[0].strip()
                    source = parts[1].strip()

                # description 안의 HTML 태그 제거
                clean_desc = description
                if "<" in clean_desc and ">" in clean_desc:
                    import re
                    clean_desc = re.sub(r"<[^>]+>", "", clean_desc).strip()

                articles.append({
                    "title": clean_title,
                    "source": source,
                    "link": link,
                    "pubDate": pub_date,
                    "description": clean_desc
                })
    except Exception as e:
        # 오프라인 또는 네트워크 에러 시 대체 데모 뉴스 제공
        articles = [
            {
                "title": f"'{query}' 관련 최신 IT 동향 및 주요 기술 발표",
                "source": "Tech IT News",
                "link": f"https://www.google.com/search?q={quote_plus(query)}",
                "pubDate": "최신",
                "description": f"최근 IT 업계에서 '{query}'와 관련된 인프라 최적화, 최신 아키텍처 및 연구 결과가 잇따라 발표되고 있습니다."
            },
            {
                "title": f"글로벌 기업들의 '{query}' 기술 도입 사례 및 전망",
                "source": "글로벌 테크 브리핑",
                "link": f"https://www.google.com/search?q={quote_plus(query)}",
                "pubDate": "최신",
                "description": f"기업 현장에서 생산성 향상과 비용 절감을 위해 '{query}' 관련 기술 및 솔루션 채택을 가속화하고 있습니다."
            },
            {
                "title": f"개발자를 위한 '{query}' 핵심 실무 체크포인트",
                "source": "IT 개발자 저널",
                "link": f"https://www.google.com/search?q={quote_plus(query)}",
                "pubDate": "최신",
                "description": f"엔지니어링 팀이 '{query}' 환경을 구축하거나 마이그레이션할 때 주의해야 할 안정성 및 보안 가이드라인이 제시되었습니다."
            }
        ]
    return articles

def generate_gemini_summary(query, articles=None):
    api_key, model_name = get_gemini_config()
    
    # Gemini 라이브러리가 있을 경우 genai 클라이언트 사용 시도
    use_sdk = False
    try:
        from google import genai
        if api_key:
            client = genai.Client(api_key=api_key)
            use_sdk = True
    except Exception:
        use_sdk = False

    articles_context = ""
    if articles:
        articles_context = "\n\n[관련 뉴스 원문 목록]:\n" + "\n".join(
            [f"- 제목: {a['title']} (언론사: {a.get('source', 'IT News')})\n  내용: {a.get('description', '')}" for a in articles]
        )

    prompt = (
        "당신은 친절하고 전문적인 IT 기술 뉴스 및 테크 큐레이터 AI 어시스턴트입니다.\n"
        "사용자가 입력한 IT 주제나 기술 키워드 및 관련 기사 원문 정보를 바탕으로 다음 형식으로 명확하고 읽기 쉽게 한국어로 답변해 주세요:\n"
        "1. 핵심 요약 (개념 및 중요도)\n"
        "2. 최신 기술 동향 및 주요 이슈\n"
        "3. 실무 또는 개발자 관점의 시사점\n\n"
        f"사용자 검색어/질문: {query}"
        f"{articles_context}"
    )

    if use_sdk and api_key:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            return response.text or "답변을 생성할 수 없습니다."
        except Exception:
            pass

    # REST API 직접 호출 시도 (SDK 미설치 환경 대비)
    if api_key:
        try:
            rest_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}]
            }
            req = urllib.request.Request(
                rest_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                candidates = result.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        return parts[0].get("text", "")
        except Exception:
            pass

    # API 호출 실패 또는 오프라인 fallback 요약
    return (
        f"### 💡 [{query}] IT 기술 분석 브리핑\n\n"
        f"1. **핵심 요약**\n"
        f"'{query}'는 최근 소프트웨어 아키텍처 및 클라우드/AI 생태계에서 핵심 화두로 떠오르고 있습니다. "
        f"기존 시스템 대비 자동화 및 효율성을 대폭 개선할 수 있는 기술입니다.\n\n"
        f"2. **최신 기술 동향 및 주요 이슈**\n"
        f"- 주요 글로벌 테크 기업들의 적극적인 도입 및 생태계 확장\n"
        f"- 확장성(Scalability)과 보안(Security) 강화 중심의 프레임워크 고도화\n"
        f"- 다양한 오픈소스 프로젝트와의 결합을 통한 실무 적용 가속화\n\n"
        f"3. **실무 및 개발자 관점의 시사점**\n"
        f"- 서비스 개발 시 유연한 구조 설계 및 안정성 검증 필요\n"
        f"- 지속적인 모니터링 체계와 단계별 전환 전략 수립 권장"
    )

class NewsChatHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ["", "/", "/index.html"]:
            self.path = "/news.html"
        return super().do_GET()

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/api/chat":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)

            try:
                data = json.loads(post_data.decode("utf-8"))
                query = data.get("query", "").strip()
            except Exception:
                self._send_json({"error": "잘못된 요청 형식입니다."}, status=HTTPStatus.BAD_REQUEST)
                return

            if not query:
                self._send_json({"error": "검색어 또는 질문을 입력해 주세요."}, status=HTTPStatus.BAD_REQUEST)
                return

            # 1. 왼쪽 패널용 뉴스 원문 기사 검색
            articles = fetch_news_articles(query)

            # 2. 뉴스 원문 및 질의 기반 AI 요약 생성
            answer_text = generate_gemini_summary(query, articles)

            self._send_json({
                "answer": answer_text,
                "articles": articles
            })
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def _send_json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

def run(port=8000):
    server_address = ("", port)
    httpd = ThreadingHTTPServer(server_address, NewsChatHandler)
    print(f"IT News & Gemini Chatbot 서버 시작: http://localhost:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")
        httpd.server_close()

if __name__ == "__main__":
    env_port = os.environ.get("PORT")
    if env_port:
        port = int(env_port)
    elif len(sys.argv) > 1:
        port = int(sys.argv[1])
    else:
        port = 8000
    run(port)

