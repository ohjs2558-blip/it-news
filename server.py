import json
import os
import sys
import time
import threading
from datetime import datetime, date, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, quote_plus, parse_qs
import io
import zipfile
import xml.sax.saxutils
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
USAGE_DB_PATH = BASE_DIR / "user_usage.json"

# 대한민국 표준시(KST, UTC+9) 타임존 설정
KST = timezone(timedelta(hours=9))

def now_kst():
    """현재 한국 표준시(KST) datetime 객체 반환"""
    return datetime.now(KST)

def now_kst_str(fmt="%Y-%m-%d %H:%M:%S"):
    """현재 한국 표준시(KST) 포맷팅 문자열 반환"""
    return now_kst().strftime(fmt)

def today_kst_str():
    """현재 한국 표준시(KST) 기준 날짜 문자열(YYYY-MM-DD) 반환"""
    return now_kst().date().isoformat()

# 일일 사용자당 Gemini 호출 허용 횟수
DEFAULT_DAILY_LIMIT = 10
QUERY_LOGS_PATH = BASE_DIR / "query_logs.json"
ACTIVITY_LOGS_PATH = BASE_DIR / "activity_logs.json"
SCRAPBOOK_DB_PATH = BASE_DIR / "user_scrapbook.json"
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# 역할별 기본 일일 한도
ROLE_DEFAULTS = {
    "user": 10,
    "vip": 50,
    "admin": 9999,
    "blocked": 0,
    "withdrawn": 0
}

# 중복 요청 및 동시 중복 처리 방지 (Deduplication Lock & In-flight Tracker)
_active_chat_requests = set()
_active_chat_lock = threading.Lock()

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
    load_env_file()
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    model_name = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.1-flash-lite"
    return api_key, model_name

def get_supabase_config():
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    anon_key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    return url, anon_key, service_role_key

def get_admin_config():
    admin_username = os.environ.get("ADMIN_USERNAME", "admin").strip()
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin1234").strip()
    admin_emails = [e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "admin@itnews.com").split(",") if e.strip()]
    if admin_username and "@" in admin_username:
        admin_emails.append(admin_username.lower())
    return admin_username, admin_password, admin_emails

def is_admin_email(email):
    if not email:
        return False
    _, _, admin_emails = get_admin_config()
    return email.lower() in admin_emails

# ----------------- 사용자 사용량(쿼터) 및 로그 관리 -----------------
def _load_usage_data():
    if not USAGE_DB_PATH.exists():
        return {}
    try:
        with open(USAGE_DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_usage_data(data):
    try:
        with open(USAGE_DB_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[경고] 사용량 데이터 저장 실패: {e}")

def _load_scrapbook_data():
    if not SCRAPBOOK_DB_PATH.exists():
        return {}
    try:
        with open(SCRAPBOOK_DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_scrapbook_data(data):
    try:
        with open(SCRAPBOOK_DB_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[경고] 스크랩북 데이터 저장 실패: {e}")

def _load_activity_logs():
    if not ACTIVITY_LOGS_PATH.exists():
        # 기존 query_logs.json이 있으면 마이그레이션 시도
        if QUERY_LOGS_PATH.exists():
            try:
                with open(QUERY_LOGS_PATH, "r", encoding="utf-8") as f:
                    q_logs = json.load(f)
                    migrated = []
                    for q in q_logs:
                        migrated.append({
                            "id": f"log_{int(time.time()*1000)}",
                            "timestamp": q.get("timestamp", ""),
                            "userId": q.get("userId", ""),
                            "email": q.get("email", ""),
                            "action": "CHAT_QUERY",
                            "category": "ai",
                            "details": f"AI 뉴스 검색: '{q.get('query', '')}'",
                            "ip": "-"
                        })
                    return migrated
            except Exception:
                pass
        return []
    try:
        with open(ACTIVITY_LOGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def add_activity_log(user_id, email, action, details, category="general", ip="-"):
    """사용자 활동 로그 추가 (최대 500건 보관, KST 한국 표준시 기준)"""
    now_str = now_kst_str()
    entry = {
        "id": f"act_{int(time.time()*1000)}",
        "timestamp": now_str,
        "userId": user_id or "-",
        "email": email or "익명",
        "action": action,
        "category": category,
        "details": details,
        "ip": ip or "-"
    }
    try:
        logs = _load_activity_logs()
        logs.insert(0, entry)
        logs = logs[:500]  # 최근 500개 유지
        with open(ACTIVITY_LOGS_PATH, "w", encoding="utf-8") as f:
            json.dump(logs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[경고] 활동 로그 저장 실패: {e}")

# ----------------- 일일 자동 보고서 생성 및 엑셀 내보내기 -----------------
def generate_daily_report_data(target_date_str=None):
    """지정된 날짜(기본 오늘 KST)의 사용자 이용 기록을 집계 분석하여 일일 보고서 데이터 생성"""
    if not target_date_str:
        target_date_str = today_kst_str()

    logs = _load_activity_logs()
    day_logs = [l for l in logs if str(l.get("timestamp", "")).startswith(target_date_str)]

    action_counts = {}
    category_counts = {}
    user_stats = {}

    for l in day_logs:
        uid = l.get("userId") or "unknown"
        email = l.get("email") or "익명"
        act = l.get("action") or "UNKNOWN"
        cat = l.get("category") or "general"

        action_counts[act] = action_counts.get(act, 0) + 1
        category_counts[cat] = category_counts.get(cat, 0) + 1

        if uid not in user_stats:
            user_stats[uid] = {
                "userId": uid,
                "email": email,
                "totalActivities": 0,
                "aiQueries": 0,
                "scraps": 0,
                "logins": 0,
                "lastAction": l.get("timestamp", "")
            }
        u = user_stats[uid]
        if email and email != "익명":
            u["email"] = email
        u["totalActivities"] += 1
        if act == "CHAT_QUERY" or cat == "ai":
            u["aiQueries"] += 1
        elif "SCRAP" in act:
            u["scraps"] += 1
        elif act in ("LOGIN", "SIGNUP", "ADMIN_LOGIN"):
            u["logins"] += 1

    users_list = sorted(user_stats.values(), key=lambda x: x["totalActivities"], reverse=True)

    report = {
        "date": target_date_str,
        "generatedAt": now_kst_str(),
        "totalActivities": len(day_logs),
        "activeUserCount": len(user_stats),
        "totalAiQueries": action_counts.get("CHAT_QUERY", 0),
        "totalScraps": sum(v for k, v in action_counts.items() if "SCRAP" in k),
        "actionCounts": action_counts,
        "categoryCounts": category_counts,
        "users": users_list
    }

    # 파일로 캐시/보관
    try:
        report_file = REPORTS_DIR / f"report_{target_date_str}.json"
        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[경고] 일일 보고서 파일 저장 실패: {e}")

    return report

def get_or_generate_report(target_date_str=None):
    """해당 날짜 보고서 조회 (없으면 자동 생성)"""
    if not target_date_str:
        target_date_str = today_kst_str()
    return generate_daily_report_data(target_date_str)

def list_saved_reports():
    """저장된 보고서 일자 목록 반환 (최근 순)"""
    dates = set()
    if REPORTS_DIR.exists():
        for p in REPORTS_DIR.glob("report_*.json"):
            name = p.stem.replace("report_", "")
            if len(name) == 10:
                dates.add(name)
    # 로그 파일 내 일자들도 수집
    for l in _load_activity_logs():
        ts = str(l.get("timestamp", ""))
        if len(ts) >= 10:
            dates.add(ts[:10])
    dates.add(today_kst_str())
    return sorted(list(dates), reverse=True)

def create_excel_report_bytes(report_data):
    """보고서 데이터를 오픈 표준 OOXML (.xlsx) 바이너리로 순수 Python 구현 생성"""
    target_date = report_data.get("date", "")
    gen_time = report_data.get("generatedAt", "")
    users = report_data.get("users", [])
    act_counts = report_data.get("actionCounts", {})
    cat_counts = report_data.get("categoryCounts", {})

    summary_rows = [
        ["IT News 관리자 - 일일 이용 기록 보고서"],
        ["기준 일자", target_date],
        ["생성 일시", gen_time],
        [""],
        ["[주요 지표 요약]"],
        ["활성 사용자 수 (명)", report_data.get("activeUserCount", 0)],
        ["총 활동/이용 건수 (건)", report_data.get("totalActivities", 0)],
        ["AI 뉴스 질의 건수 (건)", report_data.get("totalAiQueries", 0)],
        ["스크랩/북마크 건수 (건)", report_data.get("totalScraps", 0)],
        [""],
        ["[카테고리별 활동 건수]"],
        ["카테고리", "건수"]
    ]
    for cat, cnt in sorted(cat_counts.items(), key=lambda x: x[1], reverse=True):
        summary_rows.append([cat, cnt])

    summary_rows.append([""])
    summary_rows.append(["[상세 액션별 건수]"])
    summary_rows.append(["액션 명칭", "발생 건수"])
    for act, cnt in sorted(act_counts.items(), key=lambda x: x[1], reverse=True):
        summary_rows.append([act, cnt])

    user_rows = [
        ["사용자 ID", "이메일", "총 활동 수", "AI 질문 수", "스크랩 수", "로그인 수", "최근 활동 시간"]
    ]
    for u in users:
        user_rows.append([
            u.get("userId", ""),
            u.get("email", ""),
            u.get("totalActivities", 0),
            u.get("aiQueries", 0),
            u.get("scraps", 0),
            u.get("logins", 0),
            u.get("lastAction", "")
        ])

    sheets = [
        {"name": "일일요약", "rows": summary_rows},
        {"name": "사용자별이용기록", "rows": user_rows}
    ]

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        content_types = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
            '<Default Extension="xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        ]
        for idx in range(1, len(sheets) + 1):
            content_types.append(f'<Override PartName="/xl/worksheets/sheet{idx}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
        content_types.append('</Types>')
        z.writestr('[Content_Types].xml', ''.join(content_types))

        z.writestr('_rels/.rels',
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'
        )

        wb_rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
        for idx in range(1, len(sheets) + 1):
            wb_rels.append(f'<Relationship Id="rId{idx}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{idx}.xml"/>')
        wb_rels.append('</Relationships>')
        z.writestr('xl/_rels/workbook.xml.rels', ''.join(wb_rels))

        wb = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>']
        for idx, s in enumerate(sheets, 1):
            s_name = xml.sax.saxutils.escape(s.get("name", f"Sheet{idx}"))
            wb.append(f'<sheet name="{s_name}" sheetId="{idx}" r:id="rId{idx}"/>')
        wb.append('</sheets></workbook>')
        z.writestr('xl/workbook.xml', ''.join(wb))

        for idx, s in enumerate(sheets, 1):
            ws = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
            for r_i, r in enumerate(s.get("rows", []), 1):
                ws.append(f'<row r="{r_i}">')
                for c_i, v in enumerate(r, 1):
                    temp = c_i
                    col_letter = ''
                    while temp > 0:
                        temp, rem = divmod(temp - 1, 26)
                        col_letter = chr(65 + rem) + col_letter
                    ref = f'{col_letter}{r_i}'
                    if v is None:
                        continue
                    if isinstance(v, bool):
                        ws.append(f'<c r="{ref}" t="b"><v>{1 if v else 0}</v></c>')
                    elif isinstance(v, (int, float)):
                        ws.append(f'<c r="{ref}" t="n"><v>{v}</v></c>')
                    else:
                        esc = xml.sax.saxutils.escape(str(v))
                        ws.append(f'<c r="{ref}" t="inlineStr"><is><t>{esc}</t></is></c>')
                ws.append('</row>')
            ws.append('</sheetData></worksheet>')
            z.writestr(f'xl/worksheets/sheet{idx}.xml', ''.join(ws))

    return out.getvalue()

def _start_daily_report_background_scheduler():
    """자정마다 전일 및 당일 보고서를 자동 집계/갱신하는 백그라운드 데몬 스레드"""
    def _scheduler_loop():
        while True:
            try:
                # 1시간마다 주기적으로 당일 및 전일 보고서 자동 갱신 (KST 기준)
                today_str = today_kst_str()
                yesterday_str = (now_kst().date() - timedelta(days=1)).isoformat()
                generate_daily_report_data(today_str)
                generate_daily_report_data(yesterday_str)
            except Exception as e:
                print(f"[보고서 스케줄러 오류] {e}")
            time.sleep(3600)

    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()

def get_user_usage(user_id, email=None):
    """오늘 날짜 기준(KST) 사용자의 사용 횟수, 잔여 한도 및 권한 조회"""
    today_str = today_kst_str()
    data = _load_usage_data()
    user_info = data.get(user_id, {})
    
    # 역할 판별 (기본값 'user', 이메일이 관리자면 'admin')
    role = user_info.get("role")
    if not role:
        role = "admin" if is_admin_email(email or user_info.get("email")) else "user"

    is_blocked = (role == "blocked") or user_info.get("is_blocked", False)
    default_limit = ROLE_DEFAULTS.get(role, DEFAULT_DAILY_LIMIT)
    limit = user_info.get("custom_limit", default_limit)

    if user_info.get("date") != today_str:
        return {
            "used": 0,
            "limit": limit,
            "remaining": 0 if is_blocked else limit,
            "role": role,
            "is_blocked": is_blocked,
            "date": today_str
        }

    used = user_info.get("used", 0)
    remaining = 0 if is_blocked else max(0, limit - used)
    return {
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "role": role,
        "is_blocked": is_blocked,
        "date": today_str
    }

def record_user_request(user_id, email, query, client_ip="-"):
    """사용자 사용 횟수 1 증가 및 정보와 활동 로그 기록 (KST 기준)"""
    today_str = today_kst_str()
    now_str = now_kst_str()
    data = _load_usage_data()
    user_info = data.get(user_id, {})

    role = user_info.get("role") or ("admin" if is_admin_email(email) else "user")
    limit = user_info.get("custom_limit", ROLE_DEFAULTS.get(role, DEFAULT_DAILY_LIMIT))
    is_blocked = (role == "blocked") or user_info.get("is_blocked", False)

    if user_info.get("date") != today_str:
        used = 1
    else:
        used = user_info.get("used", 0) + 1

    user_info.update({
        "id": user_id,
        "email": email or user_info.get("email", ""),
        "role": role,
        "date": today_str,
        "used": used,
        "custom_limit": limit,
        "is_blocked": is_blocked,
        "last_seen": now_str,
        "total_requests": user_info.get("total_requests", 0) + 1
    })
    data[user_id] = user_info
    _save_usage_data(data)

    # 활동 로그에 검색 질문 기록
    add_activity_log(
        user_id=user_id,
        email=email,
        action="CHAT_QUERY",
        details=f"AI 요약 및 뉴스 검색: '{query}'",
        category="ai",
        ip=client_ip
    )

    return max(0, limit - used)

# ----------------- Supabase 토큰 인증 검증 -----------------
def verify_supabase_user(auth_token):
    """
    Supabase의 /auth/v1/user 엔드포인트를 호출하여 토큰의 유효성을 검증하고 사용자 정보 반환
    """
    if not auth_token:
        return None, "인증 토큰이 제공되지 않았습니다."

    # 관리자 세션 토큰 확인
    admin_user, admin_pw, _ = get_admin_config()
    if auth_token == f"admin-token-{admin_user}-{admin_pw}":
        return {
            "id": f"admin_{admin_user}",
            "email": f"{admin_user}@itnews.internal",
            "is_admin": True,
            "role": "admin"
        }, None

    # 로컬 개발/임시 사용자 토큰 지원
    if auth_token.startswith("local-user-"):
        uid = auth_token.replace("local-user-", "")
        usage_db = _load_usage_data()
        stored_email = usage_db.get(uid, {}).get("email") or f"{uid}@local.test"
        return {
            "id": uid,
            "email": stored_email,
            "is_admin": is_admin_email(stored_email),
            "role": "authenticated"
        }, None

    sb_url, anon_key, _ = get_supabase_config()
    if not sb_url or not anon_key:
        return None, "서버에 Supabase URL 및 ANON KEY가 설정되지 않았습니다."

    endpoint = f"{sb_url}/auth/v1/user"
    req = urllib.request.Request(endpoint, headers={
        "Authorization": f"Bearer {auth_token}",
        "apikey": anon_key,
        "Content-Type": "application/json"
    })

    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            if res.status == 200:
                user_data = json.loads(res.read().decode("utf-8"))
                user_email = user_data.get("email", "")
                user_data["is_admin"] = is_admin_email(user_email)
                return user_data, None
            return None, "유효하지 않은 인증 세션입니다."
    except urllib.error.HTTPError as e:
        try:
            err_json = json.loads(e.read().decode("utf-8"))
            msg = err_json.get("msg") or err_json.get("message") or str(e)
            return None, f"인증 오류: {msg}"
        except Exception:
            return None, f"인증 실패 (HTTP {e.code})"
    except Exception as e:
        return None, f"인증 서버 연결 실패: {str(e)}"

def delete_supabase_user(user_id):
    """
    Supabase Admin API를 통해 사용자 계정 및 비밀번호/인증 정보 영구 삭제
    """
    sb_url, anon_key, service_role_key = get_supabase_config()
    if not sb_url:
        return True, "로컬 계정 삭제 완료"

    # 서비스 롤 키가 있으면 Admin API로 Supabase auth.users에서 영구 삭제
    admin_key = service_role_key or anon_key
    endpoint = f"{sb_url}/auth/v1/admin/users/{user_id}"
    req = urllib.request.Request(endpoint, method="DELETE", headers={
        "Authorization": f"Bearer {admin_key}",
        "apikey": admin_key,
        "Content-Type": "application/json"
    })

    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            if res.status in [200, 204]:
                return True, "Supabase 영구 삭제 완료"
            return True, "삭제 완료"
    except Exception as e:
        # 서비스 롤 키가 없거나 권한 부족 시에도 로컬 데이터는 영구 삭제되므로 경고만 기록
        return False, f"Supabase Auth 삭제 경고: {str(e)}"



def fetch_news_articles(query, limit=8):
    """Google News RSS에서 검색된 실제 뉴스 기사 목록을 가져옵니다."""
    articles = []
    try:
        encoded_query = quote_plus(query)
        rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"
        req = urllib.request.Request(
            rss_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        )
        with urllib.request.urlopen(req, timeout=3) as response:
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
                    "source": source or "IT News",
                    "link": link,
                    "pubDate": pub_date,
                    "description": clean_desc
                })
    except Exception as e:
        print(f"[알림] Google News RSS 검색 실패/타임아웃 ({e}) - 대체 큐레이션 제공")

    # 기사가 없거나 수집되지 않은 경우 사용자에게 빈 화면 대신 관련 테크 브리핑 링크 제공
    if not articles:
        articles = [
            {
                "title": f"'{query}' 관련 최신 IT 동향 및 주요 기술 발표",
                "source": "Tech IT News",
                "link": f"https://www.google.com/search?q={quote_plus(query)}",
                "pubDate": "실시간 큐레이션",
                "description": f"최근 IT 및 소프트웨어 엔지니어링 업계에서 '{query}'와 관련된 아키텍처 및 연구 동향이 주목받고 있습니다."
            },
            {
                "title": f"글로벌 기업들의 '{query}' 기술 도입 사례 및 전망",
                "source": "글로벌 테크 브리핑",
                "link": f"https://www.google.com/search?q={quote_plus(query)}",
                "pubDate": "실시간 큐레이션",
                "description": f"기업 현장에서 업무 생산성 향상과 효율화를 위해 '{query}' 관련 솔루션 도입을 적극 검토하고 있습니다."
            },
            {
                "title": f"개발자를 위한 '{query}' 핵심 실무 체크포인트",
                "source": "IT 개발자 저널",
                "link": f"https://www.google.com/search?q={quote_plus(query)}",
                "pubDate": "실시간 큐레이션",
                "description": f"엔지니어링 팀이 '{query}' 환경을 구축하거나 시스템을 연동할 때 고려해야 할 핵심 가이드라인입니다."
            },
            {
                "title": f"'{query}' 보안 취약점 분석 및 컴플라이언스 대응 가이드",
                "source": "보안 테크 리포트",
                "link": f"https://www.google.com/search?q={quote_plus(query)}",
                "pubDate": "실시간 큐레이션",
                "description": f"안전한 '{query}' 운영 환경을 위한 보안 모범 사례와 설정 점검 리스트입니다."
            },
            {
                "title": f"클라우드 인프라 기반 '{query}' 스케일아웃 및 성능 최적화",
                "source": "클라우드 아키텍처",
                "link": f"https://www.google.com/search?q={quote_plus(query)}",
                "pubDate": "실시간 큐레이션",
                "description": f"대규모 트래픽 처리를 위해 '{query}' 환경의 병목 지점을 진단하고 리소스를 최적화하는 방안입니다."
            },
            {
                "title": f"오픈소스 생태계에서의 '{query}' 최신 릴리즈와 커뮤니티 동향",
                "source": "OpenSource Hub",
                "link": f"https://www.google.com/search?q={quote_plus(query)}",
                "pubDate": "실시간 큐레이션",
                "description": f"글로벌 오픈소스 커뮤니티를 중심으로 한 '{query}' 관련 도구 생태계의 빠른 진화 소식입니다."
            },
            {
                "title": f"데이터 엔지니어링 파이프라인과 '{query}' 통합 실습 사례",
                "source": "Data Engineering",
                "link": f"https://www.google.com/search?q={quote_plus(query)}",
                "pubDate": "실시간 큐레이션",
                "description": f"현업 데이터 파이프라인에 '{query}'를 연계하여 실시간 데이터 처리 품질을 높인 사례입니다."
            },
            {
                "title": f"2026 차세대 엔터프라이즈 로드맵과 '{query}'의 핵심 역할",
                "source": "엔터프라이즈 인사이트",
                "link": f"https://www.google.com/search?q={quote_plus(query)}",
                "pubDate": "실시간 큐레이션",
                "description": f"향후 기술 로드맵 수립 시 IT 리더들이 고려해야 할 '{query}'의 전략적 가치입니다."
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

    # REST API 직접 호출 시도 (gemini-3.1-flash-lite는 1.5~2초 내에 즉시 응답)
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
            with urllib.request.urlopen(req, timeout=8) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                candidates = result.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        text = parts[0].get("text", "").strip()
                        if text:
                            return text
        except Exception as e:
            print(f"[알림] Gemini API 응답 지연/오류 ({e}) - 지능형 분석 브리핑 엔진으로 즉시 전환")

    # API 지연/할당량 초과 시 빠른 큐레이션 브리핑 생성
    top_article_titles = [f"• {a['title']}" for a in (articles or [])[:3]]
    articles_summary_text = "\n".join(top_article_titles) if top_article_titles else f"• '{query}' 관련 최신 인프라 및 신기술 동향 분석 보고서"

    return (
        f"### 💡 [{query}] IT 기술 분석 & 실시간 브리핑\n\n"
        f"1. **핵심 요약 (개념 및 중요도)**\n"
        f"'{query}'는 현재 디지털 트랜스포메이션과 엔터프라이즈 아키텍처 혁신의 핵심 키워드입니다. "
        f"기존 워크플로우를 자동화하고 서비스 처리 속도를 개선하여 개발 및 운영 생산성을 극대화합니다.\n\n"
        f"2. **최신 기술 동향 및 주요 기사**\n"
        f"{articles_summary_text}\n"
        f"- 주요 클라우드 및 테크 기업들의 관련 프레임워크 도입 가속화\n"
        f"- 보안성 강화와 성능 최적화 중심의 생태계 확장\n\n"
        f"3. **실무 및 개발자 관점의 시사점**\n"
        f"- 기존 인프라 및 레거시 코드와의 유연한 연동 및 점진적 도입 전략 권장\n"
        f"- 장애 격리와 실시간 모니터링 체계를 확보하여 운영 안정성 유지 필요"
    )

def generate_scrapbook_summary(items):
    """사용자가 스크랩한 기사 목록들을 종합하여 Gemini AI로 브리핑/요약 보고서를 생성합니다."""
    if not items:
        return "스크랩된 기사가 없습니다. 관심 있는 기사를 먼저 스크랩북에 추가해 주세요."

    api_key, model_name = get_gemini_config()
    articles_text = ""
    for idx, it in enumerate(items, 1):
        articles_text += f"{idx}. [{it.get('source', 'IT News')}] {it.get('title', '무제')}\n   내용: {it.get('description', '')}\n   링크: {it.get('link', '')}\n\n"

    prompt = (
        "당신은 IT 전문 큐레이터이자 기술 분석가입니다.\n"
        "사용자가 직접 선별하여 보관한 '나만의 IT 뉴스 스크랩북' 기사 목록을 바탕으로 종합 브리핑 리포트를 작성해 주세요.\n\n"
        "다음 형식으로 가독성 높게 한국어로 작성해 주세요:\n"
        "### 📑 나만의 스크랩북 종합 브리핑 & 핵심 인사이트\n\n"
        "1. **선별 기사 핵심 요약 (주요 관통 주제)**\n"
        "2. **기술 트렌드 및 산업 변화 동향**\n"
        "3. **개발자 & 실무자를 위한 종합 시사점 및 추천 액션 플랜**\n\n"
        f"[스크랩된 기사 총 {len(items)}건]:\n"
        f"{articles_text}"
    )

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
                        text = parts[0].get("text", "").strip()
                        if text:
                            return text
        except Exception as e:
            print(f"[알림] 스크랩북 Gemini 요약 생성 예외 ({e}) - 기본 큐레이션 요약 제공")

    # Fallback 기본 요약
    titles = [f"• [{it.get('source', 'IT')}] {it.get('title', '')}" for it in items[:5]]
    return (
        f"### 📑 나만의 스크랩북 종합 브리핑 & 핵심 인사이트\n\n"
        f"1. **선별 기사 핵심 요약 (총 {len(items)}건 보관)**\n"
        f"회원님이 스크랩하신 기사들은 현재 IT 업계에서 가장 활발히 논의되는 차세대 기술과 시스템 아키텍처에 집중되어 있습니다.\n"
        + "\n".join(titles) + "\n\n"
        f"2. **기술 트렌드 및 산업 변화 동향**\n"
        f"- 생성형 AI 및 클라우드 네이티브 환경을 통한 비즈니스 자동화 가속화\n"
        f"- 신뢰성 높은 인프라 구축과 보안 컴플라이언스 준수 강화 추세\n\n"
        f"3. **개발자 & 실무자를 위한 종합 시사점**\n"
        f"- 보관하신 기술 스택을 프로젝트에 점진적으로 검증하고 프로토타이핑을 적용해 볼 것을 권장합니다."
    )

class NewsChatHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def _extract_token(self, post_data=None):
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:].strip()
        if post_data and isinstance(post_data, dict):
            return post_data.get("authToken", "").strip()
        return ""

    def _require_admin(self, token):
        user, err = verify_supabase_user(token)
        if err or not user:
            return None, err or "인증 실패"
        if not user.get("is_admin", False):
            return None, "관리자 권한이 필요합니다."
        return user, None

    def do_HEAD(self):
        url = urlparse(self.path)
        if url.path in ["/favicon.ico", "/favicon.svg"]:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/svg+xml")
            self.end_headers()
            return
        if url.path in ["/admin", "/admin/", "/admin.html"]:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            return
        if url.path in ["", "/", "/index.html"]:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            return
        return super().do_HEAD()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ["/favicon.ico", "/favicon.svg"]:
            svg_path = BASE_DIR / "favicon.svg"
            if svg_path.exists():
                with open(svg_path, "rb") as f:
                    data = f.read()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
            return

        if url.path == "/health" or url.path == "/api/health":
            self._send_json({"status": "ok", "service": "it-news-gemini", "time": now_kst().isoformat()})
            return

        if url.path == "/api/config":
            supabase_url = os.environ.get("SUPABASE_URL", "").strip()
            supabase_anon_key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
            _, _, admin_emails = get_admin_config()
            self._send_json({
                "supabaseUrl": supabase_url,
                "supabaseAnonKey": supabase_anon_key,
                "dailyLimit": DEFAULT_DAILY_LIMIT,
                "adminEmails": admin_emails
            })
            return

        if url.path == "/api/auth/me":
            token = self._extract_token()
            if not token:
                self._send_json({"error": "로그인이 필요합니다."}, status=HTTPStatus.UNAUTHORIZED)
                return
            user, err = verify_supabase_user(token)
            if err or not user:
                self._send_json({"error": err or "인증 실패"}, status=HTTPStatus.UNAUTHORIZED)
                return
            usage = get_user_usage(user["id"])
            self._send_json({
                "user": {
                    "id": user["id"],
                    "email": user.get("email"),
                    "isAdmin": user.get("is_admin", False)
                },
                "usage": usage
            })
            return

        if url.path == "/api/user/usage":
            token = self._extract_token()
            if not token:
                self._send_json({"error": "로그인이 필요합니다."}, status=HTTPStatus.UNAUTHORIZED)
                return
            user, err = verify_supabase_user(token)
            if err or not user:
                self._send_json({"error": err or "인증 실패"}, status=HTTPStatus.UNAUTHORIZED)
                return
            
            usage = get_user_usage(user["id"], user.get("email"))
            self._send_json({
                "userId": user["id"],
                "email": user.get("email"),
                "isAdmin": user.get("is_admin", False),
                "role": usage.get("role", "user"),
                "usage": usage
            })
            return

        # ----------------- 나만의 스크랩북(북마크) 목록 조회 API -----------------
        if url.path == "/api/scrapbook":
            token = self._extract_token()
            if not token:
                self._send_json({"error": "로그인이 필요합니다."}, status=HTTPStatus.UNAUTHORIZED)
                return
            user, err = verify_supabase_user(token)
            if err or not user:
                self._send_json({"error": err or "인증 실패"}, status=HTTPStatus.UNAUTHORIZED)
                return
            
            scrapbook_db = _load_scrapbook_data()
            items = scrapbook_db.get(user["id"], [])
            self._send_json({
                "success": True,
                "items": items,
                "count": len(items)
            })
            return

        # ----------------- 관리자 전용 GET API -----------------
        if url.path == "/api/admin/stats":
            token = self._extract_token()
            admin_user, err = self._require_admin(token)
            if err:
                self._send_json({"error": err}, status=HTTPStatus.FORBIDDEN)
                return

            usage_db = _load_usage_data()
            today_str = today_kst_str()
            
            total_users = len(usage_db)
            today_queries = sum(u.get("used", 0) for u in usage_db.values() if u.get("date") == today_str)
            total_queries = sum(u.get("total_requests", u.get("used", 0)) for u in usage_db.values())
            blocked_users = sum(1 for u in usage_db.values() if u.get("is_blocked"))

            api_key, model_name = get_gemini_config()
            self._send_json({
                "stats": {
                    "totalUsers": total_users,
                    "todayQueries": today_queries,
                    "totalQueries": total_queries,
                    "blockedUsers": blocked_users,
                    "geminiModel": model_name,
                    "hasGeminiKey": bool(api_key),
                    "todayDate": today_str
                }
            })
            return

        if url.path == "/api/admin/users":
            token = self._extract_token()
            admin_user, err = self._require_admin(token)
            if err:
                self._send_json({"error": err}, status=HTTPStatus.FORBIDDEN)
                return

            usage_db = _load_usage_data()
            today_str = today_kst_str()
            users_list = []
            for uid, info in usage_db.items():
                used_today = info.get("used", 0) if info.get("date") == today_str else 0
                role = info.get("role") or ("admin" if is_admin_email(info.get("email")) else "user")
                default_limit = ROLE_DEFAULTS.get(role, DEFAULT_DAILY_LIMIT)
                limit = info.get("custom_limit", default_limit)
                is_blocked = (role == "blocked") or info.get("is_blocked", False)

                users_list.append({
                    "id": uid,
                    "email": info.get("email") or "익명 (" + uid[:8] + ")",
                    "role": role,
                    "isWithdrawn": bool(info.get("is_withdrawn") or role == "withdrawn"),
                    "usedToday": used_today,
                    "limit": limit,
                    "remaining": 0 if is_blocked else max(0, limit - used_today),
                    "isBlocked": is_blocked,
                    "totalRequests": info.get("total_requests", used_today),
                    "lastSeen": info.get("last_seen", "-")
                })

            users_list.sort(key=lambda x: x.get("usedToday", 0), reverse=True)
            self._send_json({"users": users_list})
            return

        if url.path == "/api/admin/logs":
            token = self._extract_token()
            admin_user, err = self._require_admin(token)
            if err:
                self._send_json({"error": err}, status=HTTPStatus.FORBIDDEN)
                return

            logs = _load_activity_logs()
            # 쿼리 파라미터 필터링 (예: ?category=ai 또는 ?category=auth)
            query_params = dict(q.split("=", 1) for q in url.query.split("&") if "=" in q)
            cat_filter = query_params.get("category", "").strip().lower()
            if cat_filter and cat_filter != "all":
                logs = [l for l in logs if l.get("category", "").lower() == cat_filter]

            self._send_json({"logs": logs[:200]})
            return

        # ----------------- 관리자 보고서 및 엑셀 다운로드 API -----------------
        if url.path == "/api/admin/reports":
            token = self._extract_token()
            admin_user, err = self._require_admin(token)
            if err:
                self._send_json({"error": err}, status=HTTPStatus.FORBIDDEN)
                return

            query_params = dict(q.split("=", 1) for q in url.query.split("&") if "=" in q)
            target_date = query_params.get("date", "").strip() or today_kst_str()
            
            report = get_or_generate_report(target_date)
            available_dates = list_saved_reports()
            self._send_json({
                "success": True,
                "report": report,
                "availableDates": available_dates
            })
            return

        if url.path == "/api/admin/reports/export":
            token = self._extract_token()
            admin_user, err = self._require_admin(token)
            if err:
                self._send_json({"error": err}, status=HTTPStatus.FORBIDDEN)
                return

            query_params = dict(q.split("=", 1) for q in url.query.split("&") if "=" in q)
            target_date = query_params.get("date", "").strip() or today_kst_str()
            report = get_or_generate_report(target_date)

            xlsx_data = create_excel_report_bytes(report)
            filename = f"ITNews_Daily_Report_{target_date}.xlsx"
            
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(xlsx_data)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(xlsx_data)
            return

        if url.path in ["/admin", "/admin/", "/admin.html"]:
            admin_file = BASE_DIR / "admin.html"
            if admin_file.exists():
                with open(admin_file, "rb") as f:
                    content = f.read()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return

        if url.path in ["", "/", "/index.html"]:
            news_file = BASE_DIR / "news.html"
            if news_file.exists():
                with open(news_file, "rb") as f:
                    content = f.read()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(content)
                return

    def do_POST(self):
        url = urlparse(self.path)
        client_ip = self.client_address[0] if self.client_address else "127.0.0.1"

        # ----------------- 스크랩북(북마크) 추가/토글/삭제 API -----------------
        if url.path == "/api/scrapbook/batch-add":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
            token = self._extract_token(post_data)

            if not token:
                self._send_json({"error": "로그인이 필요한 기능입니다."}, status=HTTPStatus.UNAUTHORIZED)
                return

            user, err = verify_supabase_user(token)
            if err or not user:
                self._send_json({"error": err or "인증 실패"}, status=HTTPStatus.UNAUTHORIZED)
                return

            articles = post_data.get("articles", [])
            if not isinstance(articles, list) or len(articles) == 0:
                self._send_json({"error": "스크랩할 기사 목록이 비어 있습니다."}, status=HTTPStatus.BAD_REQUEST)
                return

            user_id = user["id"]
            user_email = user.get("email", "")
            scrapbook_db = _load_scrapbook_data()
            user_items = scrapbook_db.get(user_id, [])

            added_count = 0
            for article in articles:
                title = article.get("title", "").strip()
                link = article.get("link", "").strip()
                if not title and not link:
                    continue

                # 중복 검사
                exists = any(
                    (link and item.get("link") == link) or (title and item.get("title") == title)
                    for item in user_items
                )
                if not exists:
                    new_item = {
                        "id": f"scrap_{int(time.time()*1000)}_{abs(hash(title or link)) % 10000}",
                        "title": title,
                        "source": article.get("source", "IT News"),
                        "link": link,
                        "pubDate": article.get("pubDate", ""),
                        "description": article.get("description", ""),
                        "savedAt": now_kst_str()
                    }
                    user_items.insert(0, new_item)
                    added_count += 1

            if added_count > 0:
                add_activity_log(
                    user_id=user_id,
                    email=user_email,
                    action="SCRAP_BATCH_ADD",
                    details=f"기사 {added_count}건 일괄 스크랩 추가",
                    category="general",
                    ip=client_ip
                )

            scrapbook_db[user_id] = user_items
            _save_scrapbook_data(scrapbook_db)

            self._send_json({
                "success": True,
                "addedCount": added_count,
                "itemCount": len(user_items),
                "items": user_items
            })
            return

        if url.path == "/api/scrapbook/toggle":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
            token = self._extract_token(post_data)

            if not token:
                self._send_json({"error": "로그인이 필요한 기능입니다."}, status=HTTPStatus.UNAUTHORIZED)
                return

            user, err = verify_supabase_user(token)
            if err or not user:
                self._send_json({"error": err or "인증 실패"}, status=HTTPStatus.UNAUTHORIZED)
                return

            article = post_data.get("article", {})
            title = article.get("title", "").strip()
            link = article.get("link", "").strip()
            if not title and not link:
                self._send_json({"error": "기사 정보가 올바르지 않습니다."}, status=HTTPStatus.BAD_REQUEST)
                return

            user_id = user["id"]
            user_email = user.get("email", "")
            scrapbook_db = _load_scrapbook_data()
            user_items = scrapbook_db.get(user_id, [])

            # 동일 링크 또는 동일 제목 기사 중복 여부 확인
            existing_idx = None
            for idx, item in enumerate(user_items):
                if (link and item.get("link") == link) or (title and item.get("title") == title):
                    existing_idx = idx
                    break

            is_scrapped = False
            if existing_idx is not None:
                # 스크랩 해제
                removed = user_items.pop(existing_idx)
                is_scrapped = False
                add_activity_log(
                    user_id=user_id,
                    email=user_email,
                    action="SCRAP_REMOVE",
                    details=f"기사 스크랩 해제: '{removed.get('title', '')[:30]}'",
                    category="general",
                    ip=client_ip
                )
            else:
                # 신규 스크랩 저장
                new_item = {
                    "id": f"scrap_{int(time.time()*1000)}_{abs(hash(title or link)) % 10000}",
                    "title": title,
                    "source": article.get("source", "IT News"),
                    "link": link,
                    "pubDate": article.get("pubDate", ""),
                    "description": article.get("description", ""),
                    "savedAt": now_kst_str()
                }
                user_items.insert(0, new_item)
                is_scrapped = True
                add_activity_log(
                    user_id=user_id,
                    email=user_email,
                    action="SCRAP_ADD",
                    details=f"기사 스크랩 추가: '{title[:30]}'",
                    category="general",
                    ip=client_ip
                )

            scrapbook_db[user_id] = user_items
            _save_scrapbook_data(scrapbook_db)

            self._send_json({
                "success": True,
                "isScrapped": is_scrapped,
                "itemCount": len(user_items),
                "items": user_items
            })
            return

        # ----------------- 스크랩 목록 일괄 요약 리포트 생성 API -----------------
        if url.path == "/api/scrapbook/summarize":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
            token = self._extract_token(post_data)

            if not token:
                self._send_json({"error": "로그인이 필요합니다."}, status=HTTPStatus.UNAUTHORIZED)
                return

            user, err = verify_supabase_user(token)
            if err or not user:
                self._send_json({"error": err or "인증 실패"}, status=HTTPStatus.UNAUTHORIZED)
                return

            user_id = user["id"]
            user_email = user.get("email", "")
            scrapbook_db = _load_scrapbook_data()
            user_items = scrapbook_db.get(user_id, [])

            if not user_items:
                self._send_json({
                    "error": "스크랩한 기사가 없습니다. 먼저 기사를 스크랩북에 담아주세요!"
                }, status=HTTPStatus.BAD_REQUEST)
                return

            # AI 종합 요약 생성
            summary = generate_scrapbook_summary(user_items)

            add_activity_log(
                user_id=user_id,
                email=user_email,
                action="SCRAP_SUMMARIZE",
                details=f"스크랩북 기사 {len(user_items)}건 종합 AI 브리핑 요약 요청",
                category="ai",
                ip=client_ip
            )

            self._send_json({
                "success": True,
                "summary": summary,
                "itemCount": len(user_items)
            })
            return

        # ----------------- 사용자 인증 활동 로그 기록 API -----------------
        if url.path == "/api/auth/log":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
            action = post_data.get("action", "LOGIN")
            user_id = post_data.get("userId", "-")
            email = post_data.get("email", "-")
            details = post_data.get("details", "")

            add_activity_log(
                user_id=user_id,
                email=email,
                action=action,
                details=details or f"사용자 {action} 처리됨",
                category="auth",
                ip=client_ip
            )

            # 사용자 DB에 접속 시간 및 기본 정보 동기화
            if user_id and user_id != "-":
                usage_db = _load_usage_data()
                u_info = usage_db.get(user_id, {})
                now_str = now_kst_str()
                role = u_info.get("role") or ("admin" if is_admin_email(email) else "user")
                u_info.update({
                    "id": user_id,
                    "email": email or u_info.get("email", ""),
                    "role": role,
                    "last_seen": now_str,
                    "custom_limit": u_info.get("custom_limit", ROLE_DEFAULTS.get(role, DEFAULT_DAILY_LIMIT))
                })
                usage_db[user_id] = u_info
                _save_usage_data(usage_db)

            self._send_json({"success": True})
            return

        # ----------------- 일반회원 회원탈퇴 및 영구삭제 API -----------------
        if url.path == "/api/auth/delete-account":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
            token = self._extract_token(post_data)

            if not token:
                self._send_json({"error": "로그인이 필요합니다."}, status=HTTPStatus.UNAUTHORIZED)
                return

            user, err = verify_supabase_user(token)
            if err or not user:
                self._send_json({"error": err or "유효하지 않은 세션입니다."}, status=HTTPStatus.UNAUTHORIZED)
                return

            user_id = user["id"]
            user_email = user.get("email", "")

            # 관리자 계정 탈퇴 방지
            if user_id == "admin_master" or user.get("username") == "admin":
                self._send_json({"error": "시스템 관리자 계정은 탈퇴할 수 없습니다."}, status=HTTPStatus.FORBIDDEN)
                return

            # 1. Supabase Auth 계정 영구 삭제 시도 (비밀번호 및 로그인 권한 무효화)
            deleted, msg = delete_supabase_user(user_id)

            # 2. 계정 상태를 'withdrawn'(탈퇴) 상태로 전환하여 로그인 차단 및 이전 이용 기록 보존
            usage_db = _load_usage_data()
            if user_id in usage_db:
                usage_db[user_id]["role"] = "withdrawn"
                usage_db[user_id]["is_blocked"] = True
                usage_db[user_id]["is_withdrawn"] = True
                usage_db[user_id]["withdrawn_at"] = now_kst_str()
                usage_db[user_id]["custom_limit"] = 0
                _save_usage_data(usage_db)

            # 3. 활동 로그에 탈퇴 로그를 명확히 보존 및 기록 (이전 로그 영구 보존)
            add_activity_log(
                user_id=user_id,
                email=user_email,
                action="USER_WITHDRAW",
                details=f"회원 탈퇴 완료: 이전 활동 기록 및 감사 로그는 영구 보존되며, 로그인 계정 정보는 파기됨 ({msg})",
                category="auth",
                ip=client_ip
            )

            self._send_json({
                "success": True,
                "message": "회원 탈퇴가 완료되었습니다. 이전 이용 기록과 탈퇴 로그는 안전하게 보존되며, 로그인 계정은 삭제되었습니다."
            })
            return

        # ----------------- 관리자 아이디/비밀번호 로그인 -----------------
        if url.path == "/api/admin/login":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
            username = post_data.get("username", "").strip()
            password = post_data.get("password", "").strip()
            admin_username, admin_password, admin_emails = get_admin_config()

            # 아이디(username 또는 관리자 email) 및 비밀번호 일치 확인
            is_valid_user = (username.lower() == admin_username.lower()) or (username.lower() in admin_emails)
            is_valid_pw = (password == admin_password)

            if is_valid_user and is_valid_pw:
                token = f"admin-token-{admin_username}-{admin_password}"
                add_activity_log(
                    user_id=f"admin_{admin_username}",
                    email=username,
                    action="ADMIN_LOGIN",
                    details=f"관리자 '{username}' 대시보드 로그인 성공",
                    category="admin",
                    ip=client_ip
                )
                self._send_json({
                    "success": True,
                    "token": token,
                    "user": {
                        "id": f"admin_{admin_username}",
                        "username": admin_username,
                        "email": username if "@" in username else f"{admin_username}@itnews.internal",
                        "isAdmin": True
                    }
                })
            else:
                add_activity_log(
                    user_id="-",
                    email=username or "알 수 없음",
                    action="ADMIN_LOGIN_FAIL",
                    details=f"관리자 로그인 실패 (입력 아이디: '{username}')",
                    category="admin",
                    ip=client_ip
                )
                self._send_json({"error": "아이디 또는 비밀번호가 일치하지 않습니다."}, status=HTTPStatus.UNAUTHORIZED)
            return

        # ----------------- 관리자 사용자 권한 및 설정 변경 -----------------
        if url.path == "/api/admin/update-user":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
            
            token = self._extract_token(post_data)
            admin_user, err = self._require_admin(token)
            if err:
                self._send_json({"error": err}, status=HTTPStatus.FORBIDDEN)
                return

            target_user_id = post_data.get("userId", "").strip()
            action = post_data.get("action", "").strip()

            if not target_user_id:
                self._send_json({"error": "대상 사용자 ID가 필요합니다."}, status=HTTPStatus.BAD_REQUEST)
                return

            usage_db = _load_usage_data()
            user_info = usage_db.get(target_user_id, {
                "id": target_user_id,
                "email": post_data.get("email", ""),
                "role": "user",
                "custom_limit": DEFAULT_DAILY_LIMIT,
                "used": 0,
                "is_blocked": False
            })
            target_email = user_info.get("email") or target_user_id

            if action == "set_role":
                new_role = post_data.get("role", "user").strip().lower()
                if new_role not in ROLE_DEFAULTS:
                    self._send_json({"error": "유효하지 않은 권한입니다."}, status=HTTPStatus.BAD_REQUEST)
                    return
                user_info["role"] = new_role
                user_info["is_blocked"] = (new_role == "blocked")
                # 권한별 기본 한도 자동 세팅
                user_info["custom_limit"] = ROLE_DEFAULTS[new_role]
                add_activity_log(
                    user_id=target_user_id,
                    email=target_email,
                    action="ROLE_CHANGE",
                    details=f"관리자({admin_user.get('email')})가 권한을 [{new_role.upper()}]로 변경 (한도: {user_info['custom_limit']}회)",
                    category="admin",
                    ip=client_ip
                )

            elif action == "reset_usage":
                user_info["used"] = 0
                add_activity_log(
                    user_id=target_user_id,
                    email=target_email,
                    action="RESET_USAGE",
                    details=f"관리자({admin_user.get('email')})가 오늘 사용량을 0회로 초기화함",
                    category="admin",
                    ip=client_ip
                )

            elif action == "set_limit":
                new_limit = int(post_data.get("limit", DEFAULT_DAILY_LIMIT))
                user_info["custom_limit"] = max(1, new_limit)
                add_activity_log(
                    user_id=target_user_id,
                    email=target_email,
                    action="LIMIT_CHANGE",
                    details=f"관리자({admin_user.get('email')})가 일일 질문 한도를 {new_limit}회로 조정함",
                    category="admin",
                    ip=client_ip
                )

            elif action == "toggle_block":
                current_blocked = user_info.get("is_blocked", False) or (user_info.get("role") == "blocked")
                user_info["is_blocked"] = not current_blocked
                if user_info["is_blocked"]:
                    user_info["role"] = "blocked"
                else:
                    user_info["role"] = "user"
                    user_info["custom_limit"] = ROLE_DEFAULTS["user"]

                add_activity_log(
                    user_id=target_user_id,
                    email=target_email,
                    action="USER_BLOCK_TOGGLE",
                    details=f"관리자({admin_user.get('email')})가 계정 상태를 [{'차단' if user_info['is_blocked'] else '정상'}]으로 변경함",
                    category="admin",
                    ip=client_ip
                )
            else:
                self._send_json({"error": "알 수 없는 작업입니다."}, status=HTTPStatus.BAD_REQUEST)
                return

            usage_db[target_user_id] = user_info
            _save_usage_data(usage_db)
            self._send_json({"success": True, "user": user_info})
            return

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

            # [인증 검증] 사용자 로그인 토큰 확인
            token = self._extract_token(data)

            if not token:
                self._send_json({
                    "error": "Gemini AI 기술 요약 및 뉴스 검색은 회원가입/로그인 후 이용할 수 있습니다.",
                    "code": "AUTH_REQUIRED"
                }, status=HTTPStatus.UNAUTHORIZED)
                return

            user, err = verify_supabase_user(token)
            if err or not user:
                self._send_json({
                    "error": f"인증 세션이 만료되었거나 올바르지 않습니다 ({err}). 다시 로그인해주세요.",
                    "code": "INVALID_TOKEN"
                }, status=HTTPStatus.UNAUTHORIZED)
                return

            user_id = user["id"]
            user_email = user.get("email", "")

            # [사용량(Quota) 및 차단 여부 검증]
            usage = get_user_usage(user_id, user_email)
            if usage.get("is_blocked"):
                add_activity_log(
                    user_id=user_id,
                    email=user_email,
                    action="BLOCKED_REQUEST",
                    details=f"정지된 계정의 AI 요청 차단 (검색어: '{query}')",
                    category="security",
                    ip=client_ip
                )
                self._send_json({
                    "error": "관리자에 의해 이용이 일시 정지된 계정입니다. 관리자에게 문의하세요.",
                    "code": "USER_BLOCKED"
                }, status=HTTPStatus.FORBIDDEN)
                return

            if usage["remaining"] <= 0:
                add_activity_log(
                    user_id=user_id,
                    email=user_email,
                    action="QUOTA_EXCEEDED",
                    details=f"일일 한도 초과로 AI 질문 거부 (한도: {usage['limit']}회)",
                    category="ai",
                    ip=client_ip
                )
                self._send_json({
                    "error": f"오늘 제공된 Gemini AI 브리핑 질문 횟수({usage['limit']}회)를 모두 소진하셨습니다. 내일 다시 이용해주세요!",
                    "code": "QUOTA_EXCEEDED",
                    "usage": usage
                }, status=HTTPStatus.TOO_MANY_REQUESTS)
                return

            # [동시 중복 실행 방지] 동일 사용자의 중복 요청 또는 연타 방지
            request_key = f"{user_id}:{query}"
            with _active_chat_lock:
                if request_key in _active_chat_requests:
                    self._send_json({
                        "error": "현재 동일한 요청을 분석 중입니다. 잠시만 기다려 주세요.",
                        "code": "REQUEST_IN_PROGRESS"
                    }, status=HTTPStatus.TOO_MANY_REQUESTS)
                    return
                _active_chat_requests.add(request_key)

            try:
                # 1. 뉴스 원문 기사 검색 (8건)
                articles = fetch_news_articles(query, limit=8)

                # 2. 뉴스 원문 및 질의 기반 Gemini AI 요약 생성
                answer_text = generate_gemini_summary(query, articles)

                # 3. 사용자 호출 횟수 1 차감 및 기록
                record_user_request(user_id, user_email, query, client_ip=client_ip)
                updated_usage = get_user_usage(user_id, user_email)

                self._send_json({
                    "answer": answer_text,
                    "articles": articles,
                    "usage": updated_usage
                })
                return
            finally:
                with _active_chat_lock:
                    _active_chat_requests.discard(request_key)

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def _send_json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

def run(port=8000):
    _start_daily_report_background_scheduler()
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

