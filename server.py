from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime
import json
import os

app = Flask(__name__)
CORS(app)

TOKENS_FILE = "tokens.json"
MAX_TOKENS = 10  # Maximum number of tokens to store


def load_tokens():
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {"tokens": []}
    return {"tokens": []}


def save_tokens(data):
    with open(TOKENS_FILE, "w") as f:
        json.dump(data, f, indent=2)

MODELS_JSON_PATH = 'models.json'

def load_models_data():
    """
    Load models data from JSON file with error handling
    Always returns a tuple (data, error_message)
    """
    try:
        if not os.path.exists(MODELS_JSON_PATH):
            return None, f"Models file not found at {MODELS_JSON_PATH}"
        
        with open(MODELS_JSON_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Validate that data is a list
        if not isinstance(data, list):
            return None, "Invalid data format: expected array"
        
        return data, None
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON format: {str(e)}"
    except Exception as e:
        return None, f"Error loading models: {str(e)}"

@app.route('/models', methods=['GET'])
def get_models():
    """
    Endpoint to serve all models
    Always returns a response
    """
    # Load the data
    models_data, error = load_models_data()
    
    # Handle error case
    if error:
        return jsonify({"error": error}), 500
    
    # Handle empty data case
    if models_data is None:
        return jsonify({"error": "No models data available"}), 404
    
    # Return the data
    return jsonify(models_data)

@app.route("/api", methods=["POST"])
def receive_token():
    body = request.get_json(force=True)
    token = body.get("token")

    if not token:
        return jsonify({"status": "error", "message": "No token provided"}), 400

    tokens_data = load_tokens()
    tokens_list = tokens_data.get("tokens", [])

    version = body.get("version", "v3")

    entry = {
        "token": token,
        "version": version,
        "timestamp_utc": datetime.utcnow().isoformat() + "Z",
        "timestamp_local": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "token_length": len(token),
        "token_preview": token[:50] + "..."
    }

    # Add new token
    tokens_list.append(entry)

    # If more than MAX_TOKENS, remove oldest
    if len(tokens_list) > MAX_TOKENS:
        removed = tokens_list.pop(0)
        print(f"🗑 Removed oldest token (Length: {removed['token_length']})")

    # Update metadata
    tokens_data["tokens"] = tokens_list
    tokens_data["total_count"] = len(tokens_list)
    tokens_data["last_updated"] = entry["timestamp_local"]

    save_tokens(tokens_data)

    print(f"\n{'='*60}")
    print(f"✅ TOKEN RECEIVED at {entry['timestamp_local']}")
    print(f"   Version: {entry['version']}")
    print(f"   Length: {entry['token_length']} chars")
    print(f"   Preview: {entry['token_preview']}")
    print(f"   Total stored: {tokens_data['total_count']} (Max: {MAX_TOKENS})")
    print(f"{'='*60}\n")

    return jsonify({
        "status": "success",
        "message": f"Token stored (rolling max {MAX_TOKENS})",
        "entry_index": len(tokens_list) - 1,
        "total_count": tokens_data["total_count"]
    }), 200


@app.route("/api/tokens", methods=["GET"])
def get_tokens():
    tokens_data = load_tokens()
    return jsonify(tokens_data), 200


@app.route("/api/tokens/latest", methods=["GET"])
def get_latest_token():
    tokens_data = load_tokens()
    if tokens_data.get("tokens"):
        return jsonify(tokens_data["tokens"][-1]), 200
    return jsonify({"message": "No tokens stored yet"}), 404


@app.route("/api/tokens/clear", methods=["DELETE"])
def clear_tokens():
    save_tokens({"tokens": []})
    return jsonify({"status": "cleared"}), 200


if __name__ == "__main__":
    print("\n🚀 reCAPTCHA Token Receiver running on http://localhost:5000")
    print("   POST /api                 → store a token (max 7 rolling)")
    print("   GET  /api/tokens          → view all tokens")
    print("   GET  /api/tokens/latest   → view latest token")
    print("   GET  /models   → view latest models")
    print("   DELETE /api/tokens/clear  → wipe all tokens\n")
    app.run(host="0.0.0.0", port=5000, debug=True)
