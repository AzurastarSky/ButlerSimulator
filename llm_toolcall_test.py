import requests
import json
import re

LLM_SERVER_URL = "http://192.168.0.222:8080/v1/chat/completions"
MODEL = "Qwen2.5-3B-Instruct"
WEB_APP_URL = "http://127.0.0.1:5000/api/device"

DEBUG = False

# Keep this small because your local context budget is limited
MAX_HISTORY_MESSAGES = 6   # roughly 3 user/assistant turns

SYSTEM_PROMPT = (
    "You are Butler, a smart-home assistant.\n"
    "Decide whether the user wants device control.\n"
    "If yes, reply with JSON only:\n"
    "{\"tool\":\"manage_device\",\"room\":\"<room>\",\"device\":\"<device>\",\"action\":\"turn_on|turn_off|toggle\"}\n"
    "If no, reply with JSON only:\n"
    "{\"tool\":null,\"reply\":\"<short reply>\"}\n"
    "Output JSON only. No extra text."
)


def execute_manage_device(room, device, action):
    payload = {
        "room": room,
        "device": device,
        "action": action
    }

    try:
        resp = requests.post(WEB_APP_URL, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        print("[Web page updated]")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result
    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


def build_device_reply(result):
    if not result.get("ok"):
        error = result.get("error", "Something went wrong.")
        return f"Sorry, I couldn't complete that. {error}"

    room = result.get("room", "the room")
    device = result.get("device", "device")
    new_state = result.get("new_state", "")

    if new_state:
        return f"Okay, the {room} {device} is now {new_state}."

    return f"Okay, I updated the {room} {device}."


def post_chat(history):
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + history[-MAX_HISTORY_MESSAGES:],
        "stream": False,
        "temperature": 0,
        "max_tokens": 96
    }

    resp = requests.post(LLM_SERVER_URL, json=payload, timeout=60)
    resp.raise_for_status()
    return resp.json()


def get_message_content(data):
    try:
        message = data["choices"][0]["message"]
        content = message.get("content", "")

        if content is None:
            return ""

        # Some backends return a list of content parts
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    if "text" in item:
                        parts.append(str(item["text"]))
                    elif "content" in item:
                        parts.append(str(item["content"]))
            return "\n".join(parts).strip()

        return str(content).strip()

    except Exception:
        return ""


def extract_json(text):
    if not text:
        return None

    text = text.strip()

    # First try direct JSON parse
    try:
        return json.loads(text)
    except Exception:
        pass

    # Fallback: extract first JSON object from text
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        candidate = match.group(0)
        try:
            return json.loads(candidate)
        except Exception:
            return None

    return None


def print_debug(data):
    if DEBUG:
        print("\n--- RAW RESPONSE ---")
        print(json.dumps(data, indent=2, ensure_ascii=False))


def handle_response(data):
    """
    Returns:
      assistant_json_text, printable_reply
    So we can BOTH:
      - store the assistant JSON in history
      - print a friendly reply to the user
    """
    content = get_message_content(data)

    if not content:
        print("[No assistant reply]")
        print_debug(data)
        return None, None

    parsed = extract_json(content)

    if not parsed:
        print("[Model reply was not valid JSON]")
        print(content)
        print_debug(data)
        return None, None

    tool = parsed.get("tool")

    if tool == "manage_device":
        room = parsed.get("room", "")
        device = parsed.get("device", "")
        action = parsed.get("action", "")

        print(
            "TOOL CALL: manage_device "
            + json.dumps(
                {
                    "room": room,
                    "device": device,
                    "action": action
                },
                ensure_ascii=False
            )
        )

        result = execute_manage_device(room, device, action)
        reply = build_device_reply(result)

        if reply:
            print(f"Butler: {reply}")

        # Store the original assistant JSON in history
        return content, reply

    if tool is None:
        reply = parsed.get("reply", "")
        if reply:
            print(f"Butler: {reply}")
            return content, reply
        else:
            print("[JSON returned but no reply field found]")
            print(content)
            return content, None

    print("[Unknown tool returned]")
    print(content)
    return content, None


def main():
    print("Butler is ready.")
    print("Type /exit to quit, /clear to clear chat history.\n")

    history = []

    while True:
        user_message = input("You: ").strip()

        if not user_message:
            continue

        if user_message.lower() in ["/exit", "exit", "quit"]:
            print("Butler: Goodbye.")
            break

        if user_message.lower() == "/clear":
            history = []
            print("Butler: Chat history cleared.")
            continue

        # Add user turn
        history.append({"role": "user", "content": user_message})

        try:
            data = post_chat(history)
            assistant_json_text, friendly_reply = handle_response(data)

            # Store assistant turn if we got one
            if assistant_json_text:
                history.append({"role": "assistant", "content": assistant_json_text})

        except requests.exceptions.RequestException as e:
            print("[HTTP error contacting LLM server]")
            print(e)
        except Exception as e:
            print("[Unexpected error]")
            print(e)


if __name__ == "__main__":
    main()