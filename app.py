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

def load_manual():
    """manual.txt 파일을 읽어서 반환."""
    try:
        with open("manual.txt", "r", encoding="utf-8") as f:
            content = f.read().strip()
            return content if content else ""
    except FileNotFoundError:
        return ""

def clean_markdown(text: str) -> str:
    """Gemini가 출력한 마크다운을 슬랙 포맷으로 변환."""
    # **굵게** → *굵게*
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    # ### 제목 → *제목*
    text = re.sub(r'#{1,6}\s*(.+)', r'*\1*', text)
    # 빈 줄 정리 (3줄 이상 연속 빈줄 → 2줄로)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def ask_gemini(question: str) -> str:
    """Gemini에게 매뉴얼 기반 질문을 전달하고 답변을 받음."""
    manual_content = load_manual()

    if manual_content:
        prompt = f"""너는 회사 업무 매뉴얼을 기반으로 팀원 질문에 답변해주는 귀엽고 친절한 업무 도우미야! 🐾

아래 매뉴얼 내용만 참고해서 답변해줘.

=== 답변 규칙 ===
- 매뉴얼에 없는 내용이면 "앗, 매뉴얼에 해당 내용이 없어요 😢 담당자에게 문의해 주세요!" 라고 답변
- 정확한 내용을 친절하고 귀엽게 설명하기
- 이모지 적극 활용하기 🎉✅📌
- 단계가 있으면 번호로 정리해주기
- 중요한 내용은 슬랙 볼드(*중요내용*) 로 강조하기
- 마지막엔 담당자 정보 있으면 꼭 추가하기

=== 슬랙 포맷 규칙 (반드시 지켜!) ===
- 볼드 강조는 반드시 별표 1개로: *이렇게* (절대 **두개** 쓰지 말 것!)
- ## 이나 ### 같은 해시태그 제목 절대 사용 금지
- HTML 태그 사용 금지
- [ ] 대괄호 안에 ** 절대 사용 금지

=== 말투 예시 ===
나쁜 예: "연차 신청은 그룹웨어에서 하시면 됩니다."
좋은 예: "연차 신청은 *그룹웨어 → 근태관리 → 연차신청* 메뉴에서 할 수 있어요! 📅 사용 3일 전까지 신청해야 하는 거 잊지 마세요 😊"

=== 회사 매뉴얼 ===
{manual_content}
===================

질문: {question}"""
    else:
        prompt = f"""너는 회사 업무 매뉴얼을 기반으로 팀원 질문에 답변해주는 귀엽고 친절한 업무 도우미야! 🐾
현재 등록된 매뉴얼이 없어. 모든 질문에 "앗, 아직 매뉴얼이 등록되지 않았어요 😢 담당자에게 문의해 주세요!" 라고 답변해줘.

질문: {question}"""

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt
    )
    # 마크다운 → 슬랙 포맷 변환
    return clean_markdown(response.text)

def log_question(api_client, user_id: str, question: str):
    """질문 내용을 로그 채널에 기록."""
    log_channel = os.environ.get("LOG_CHANNEL_ID", "")
    if not log_channel:
        return
    try:
        kst = timezone(timedelta(hours=9))
        now = datetime.now(kst).strftime("%Y-%m-%d %H:%M")
        api_client.chat_postMessage(
            channel=log_channel,
            text=f"*👤 질문자:* <@{user_id}>\n*💬 질문:* {question}\n*🕐 시간:* {now}"
        )
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
    # 봇 자신의 메시지는 무시
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

    # 로그 채널에 질문 기록
    log_question(api_client, user_id, user_message)

    try:
        answer = ask_gemini(user_message)
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
                text="앗, 잠시 문제가 생겼어요 😢 다시 시도해 주세요!"
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
