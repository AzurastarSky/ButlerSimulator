from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# Super simple in-memory state
STATE = {
    "living room": {
        "light": "off"
    }
}

HTML = """
<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="1">
    <title>Butler Light Demo</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            background: #111827;
            color: white;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
        }
        .card {
            background: #1f2937;
            padding: 30px;
            border-radius: 16px;
            width: 320px;
            text-align: center;
            box-shadow: 0 10px 30px rgba(0,0,0,0.35);
        }
        .room {
            font-size: 28px;
            font-weight: bold;
            margin-bottom: 20px;
        }
        .bulb {
            width: 80px;
            height: 80px;
            margin: 0 auto 20px auto;
            border-radius: 50%;
            background: {{ bulb_color }};
            box-shadow: 0 0 25px {{ bulb_glow }};
            border: 3px solid #d1d5db;
        }
        .status {
            font-size: 22px;
            font-weight: bold;
            color: {{ text_color }};
        }
        .label {
            margin-top: 10px;
            font-size: 14px;
            color: #9ca3af;
        }
    </style>
</head>
<body>
    <div class="card">
        <div class="room">Living Room</div>
        <div class="bulb"></div>
        <div class="status">Light is {{ status.upper() }}</div>
        <div class="label">Page refreshes every second</div>
    </div>
</body>
</html>
"""


@app.route("/")
def home():
    status = STATE["living room"]["light"]

    bulb_color = "#facc15" if status == "on" else "#374151"
    bulb_glow = "rgba(250, 204, 21, 0.85)" if status == "on" else "rgba(0,0,0,0)"
    text_color = "#facc15" if status == "on" else "#9ca3af"

    return render_template_string(
        HTML,
        status=status,
        bulb_color=bulb_color,
        bulb_glow=bulb_glow,
        text_color=text_color
    )


@app.route("/api/device", methods=["POST"])
def device():
    data = request.get_json(force=True) or {}

    room = str(data.get("room", "")).strip().lower()
    device = str(data.get("device", "")).strip().lower()
    action = str(data.get("action", "")).strip().lower()

    # Tiny bit of normalisation to make the demo less brittle
    if room == "lounge":
        room = "living room"
    if device == "lamp":
        device = "light"

    if room != "living room" or device != "light":
        return jsonify({
            "ok": False,
            "error": "This simple demo only supports the living room light."
        }), 400

    if action == "turn_on":
        STATE["living room"]["light"] = "on"
    elif action == "turn_off":
        STATE["living room"]["light"] = "off"
    elif action == "toggle":
        STATE["living room"]["light"] = (
            "off" if STATE["living room"]["light"] == "on" else "on"
        )
    else:
        return jsonify({
            "ok": False,
            "error": f"Unknown action: {action}"
        }), 400

    return jsonify({
        "ok": True,
        "room": room,
        "device": device,
        "action": action,
        "new_state": STATE["living room"]["light"]
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)