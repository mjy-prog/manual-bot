import os
import threading
from flask import Flask, request, jsonify
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
import google.generativeai as genai
from dotenv import load_dotenv

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

# Gemini API 초기화
genai.configure(api_key=os.environ["GEMINI_API_KEY"])
model = genai.GenerativeModel("gemini-2.0-flash-lite")

# 중복 이벤트 방지용 처리된 이벤트 ID 저장소
processed_events = set()
processed_events_lock = threading.Lock()

def load_manual():
    """manual.txt 파일을 읽어서 반환. 없으면 빈 문자열 반환."""
    try:
        with open("manual.txt", "r", encoding="utf-8") as f:
            content = f.read().strip()
            return content if content else ""
    except FileNotFoundError:
        return ""

def ask_gemini(question: str) -> str:
    """Gemini에게 매뉴얼 기반 질문을 전달하고 답변을 받음."""
    manual_content = load_manual()

    if manual_content:
        system_prompt = f"""당신은 회사 내부 업무 매뉴얼 기반 질문 답변 봇입니다.
아래 매뉴얼 내용만을 참고하여 팀원의 질문에 답변해주세요.

규칙:
- 매뉴얼에 있는 내용만 답변할 것
- 매뉴얼에 없는 내용이면 반드시 "매뉴얼에 해당 내용이 없습니다. 담당자에게 문의해주세요" 라고 답변
- 답변은 간결하고 명확하게
- 친절하지만 너무 격식없지 않게

=== 회사 매뉴얼 ===
{manual_content}
===================

질문: {question}"""
    else:
        # 매뉴얼이 없을 경우
        system_prompt = f"""당신은 회사 내부 업무 매뉴얼 기반 질문 답변 봇입니다.
현재 등록된 매뉴얼이 없습니다. 모든 질문에 "매뉴얼에 해당 내용이 없습니다. 담당자에게 문의해주세요" 라고 답변하세요.

질문: {question}"""

    response = model.generate_content(system_prompt)
    return response.text

def is_duplicate_event(event_id: str) -> bool:
    """중복 이벤트 여부 확인. 이미 처리된 이벤트면 True 반환."""
    with processed_events_lock:
        if event_id in processed_events:
            return True
        processed_events.add(event_id)
        # 메모리 누수 방지: 1000개 초과시 오래된 이벤트 정리
        if len(processed_events) > 1000:
            processed_events.clear()
        return False

def handle_message(event, say, client):
    """공통 메시지 처리 함수."""
    # 봇 자신의 메시지는 무시
    if event.get("bot_id"):
        return
    if event.get("subtype") == "bot_message":
        return

    user_message = event.get("text", "").strip()
    if not user_message:
        return

    # @멘션 제거 (채널 멘션인 경우 <@BOTID> 형태 제거)
    import re
    user_message = re.sub(r"<@[A-Z0-9]+>", "", user_message).strip()

    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")

    try:
        # Gemini에게 질문
        answer = ask_gemini(user_message)

        # 슬랙 스레드에 답변 전송
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=answer
        )
    except Exception as e:
        print(f"[오류] 답변 생성 실패: {e}")
        # 에러 발생 시 사용자에게 알림
        try:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="잠시 문제가 생겼어요. 다시 시도해주세요 🙏"
            )
        except Exception as inner_e:
            print(f"[오류] 에러 메시지 전송 실패: {inner_e}")

# DM 메시지 이벤트 처리
@slack_app.event("message")
def handle_dm_message(event, say, client, body):
    # 중복 이벤트 방지
    event_id = body.get("event_id", "")
    if event_id and is_duplicate_event(event_id):
        return

    handle_message(event, say, client)

# 채널 @멘션 이벤트 처리
@slack_app.event("app_mention")
def handle_mention(event, say, client, body):
    # 중복 이벤트 방지
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
    port = int(os.environ.get("PORT", 3000))
    print(f"[서버 시작] 포트 {port}에서 실행 중...")
    flask_app.run(host="0.0.0.0", port=port)
