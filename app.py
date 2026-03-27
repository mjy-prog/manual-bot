import os
import threading
from flask import Flask, request, jsonify
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from google import genai
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
import re

# 환경변수 로드
load_dotenv()

# Flask 앱 초기화
flask_app = Flask(__name__)

# Slack Bolt 앱 초기화
slack_app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.environ["SLACK_SIGNING_SECRET"]
)
handler = SlackRequestHandler(slack_app)

# Gemini 클라이언트 초기화
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
GEMINI_MODEL = "gemini-2.5-flash"

# 중복 이벤트 방지용 저장소
processed_events = set()
processed_events_lock = threading.Lock()

# 스레드별 대화 히스토리 저장소 {thread_ts: [{"role": "user"/"model", "parts": [{"text": "..."}]}]}
thread_histories = {}
thread_histories_lock = threading.Lock()

def load_manual():
    """manual.txt 파일을 읽어서 반환."""
    try:
        with open("manual.txt", "r", encoding="utf-8") as f:
            content = f.read().strip()
            return content if content else ""
    except FileNotFoundError:
        return ""

def clean_format(text: str) -> str:
    """불필요한 마크다운 제거 및 슬랙 포맷 정리."""
    # **텍스트** → [텍스트]
    text = re.sub(r'\*\*(.+?)\*\*', r'[\1]', text)
    # *텍스트* (볼드) → [텍스트]
    text = re.sub(r'(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)', r'[\1]', text)
    # ## 제목 → 제목 (해시 제거)
    text = re.sub(r'#{1,6}\s*(.+)', r'\1', text)
    # 이모지 전부 제거
    text = re.sub(r'[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0000FE00-\U0000FEFF]', '', text)
    # 3줄 이상 빈줄 → 2줄로
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def build_system_prompt():
    """시스템 프롬프트 생성."""
    manual_content = load_manual()
    if manual_content:
        return f"""당신은 회사 내부 업무 매뉴얼을 기반으로 질문에 답변하는 업무 도우미입니다.
아래 매뉴얼 내용만을 참고하여 답변해주세요.

답변 규칙:
- 매뉴얼에 없는 내용이면 "매뉴얼에 해당 내용이 없습니다. 담당자에게 문의해주세요." 라고 답변
- 정확하고 간결하게 답변할 것
- 말투는 친절하되 차분하고 정중하게
- 호칭은 일절 사용하지 말 것 (고객님, 담당자님, 이름 등 모든 호칭 금지)
- 이모지, 이모티콘 절대 사용 금지
- 중요한 내용 강조 시 [대괄호]로 표시할 것 (예: [환급결정], [대응필요])
- ** 또는 * 같은 마크다운 기호 절대 사용 금지
- ## 같은 헤더 기호 절대 사용 금지
- 단계나 항목은 숫자(1. 2. 3.) 또는 - 로 정리

=== 회사 매뉴얼 ===
{manual_content}
==================="""
    else:
        return """당신은 회사 내부 업무 매뉴얼을 기반으로 질문에 답변하는 업무 도우미입니다.
현재 등록된 매뉴얼이 없습니다. "매뉴얼에 해당 내용이 없습니다. 담당자에게 문의해주세요." 라고 답변해주세요.
호칭은 일절 사용하지 말 것."""

def ask_gemini_with_history(thread_ts: str, user_message: str) -> str:
    """스레드 히스토리를 포함하여 Gemini에게 질문."""
    system_prompt = build_system_prompt()

    with thread_histories_lock:
        if thread_ts not in thread_histories:
            thread_histories[thread_ts] = []
        history = thread_histories[thread_ts].copy()

    # 현재 질문 추가
    current_contents = history + [{"role": "user", "parts": [{"text": user_message}]}]

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=current_contents,
        config={"system_instruction": system_prompt}
    )
    answer = response.text

    # 히스토리 업데이트
    with thread_histories_lock:
        if thread_ts not in thread_histories:
            thread_histories[thread_ts] = []
        thread_histories[thread_ts].append({"role": "user", "parts": [{"text": user_message}]})
        thread_histories[thread_ts].append({"role": "model", "parts": [{"text": answer}]})
        # 히스토리 최대 20턴 유지 (메모리 관리)
        if len(thread_histories[thread_ts]) > 40:
            thread_histories[thread_ts] = thread_histories[thread_ts][-40:]
        # 스레드 수 최대 500개 유지
        if len(thread_histories) > 500:
            oldest_key = next(iter(thread_histories))
            del thread_histories[oldest_key]

    return answer

def log_question(api_client, user_id: str, question: str):
    """질문 내용을 로그 채널에 기록."""
    log_channel = os.environ.get("LOG_CHANNEL_ID", "")
    if not log_channel:
        print("[로그] LOG_CHANNEL_ID 환경변수가 설정되지 않았습니다.")
        return
    try:
        kst = timezone(timedelta(hours=9))
        now = datetime.now(kst).strftime("%Y-%m-%d %H:%M")
        result = api_client.chat_postMessage(
            channel=log_channel,
            text=f"질문자: <@{user_id}>\n질문: {question}\n시간: {now}"
        )
        if not result["ok"]:
            print(f"[오류] 로그 채널 전송 실패: {result}")
    except Exception as e:
        print(f"[오류] 로그 채널 전송 실패: {e}")

def is_duplicate_event(event_id: str) -> bool:
    """중복 이벤트 여부 확인."""
    with processed_events_lock:
        if event_id in processed_events:
            return True
        processed_events.add(event_id)
        if len(processed_events) > 1000:
            processed_events.clear()
        return False

def handle_message(event, say, api_client):
    """공통 메시지 처리 함수."""
    if event.get("bot_id"):
        return
    if event.get("subtype") == "bot_message":
        return

    user_message = event.get("text", "").strip()
    if not user_message:
        return

    # @멘션 제거
    user_message = re.sub(r"<@[A-Z0-9]+>", "", user_message).strip()
    if not user_message:
        return

    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")
    user_id = event.get("user", "unknown")

    # 로그 채널에 질문 기록 (스레드 첫 질문만)
    if not event.get("thread_ts"):
        log_question(api_client, user_id, user_message)

    try:
        answer = ask_gemini_with_history(thread_ts, user_message)
        answer = clean_format(answer)
        api_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=answer
        )
    except Exception as e:
        print(f"[오류] 답변 생성 실패: {e}")
        try:
            api_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="잠시 문제가 생겼습니다. 다시 시도해주세요."
            )
        except Exception as inner_e:
            print(f"[오류] 에러 메시지 전송 실패: {inner_e}")

# DM 메시지 이벤트 처리
@slack_app.event("message")
def handle_dm_message(event, say, client, body):
    event_id = body.get("event_id", "")
    if event_id and is_duplicate_event(event_id):
        return
    handle_message(event, say, client)

# 채널 @멘션 이벤트 처리
@slack_app.event("app_mention")
def handle_mention(event, say, client, body):
    event_id = body.get("event_id", "")
    if event_id and is_duplicate_event(event_id):
        return
    handle_message(event, say, client)

# Flask 라우트: 슬랙 이벤트 수신
@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)

# Flask 라우트: 서버 상태 확인용
@flask_app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"[서버 시작] 포트 {port}에서 실행 중...")
    flask_app.run(host="0.0.0.0", port=port)
