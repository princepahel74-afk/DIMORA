"""DIMORA backend — Flask REST API + serves the web frontend.

Run:  python app.py          (then open http://127.0.0.1:8000)
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from dimora_core import pipeline
from dimora_core.ai import services
from dimora_core.ai.client import AIError, AIUnavailable, client_from_request
from dimora_core.config import settings
from dimora_core.planner.analysis import analyze, tour_order
from dimora_core.vision.loader import UploadError, data_url_to_image, to_jpeg_b64

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
VERSION = "2.3.0"
log = logging.getLogger("dimora")


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = settings.max_upload_mb * 1024 * 1024

    # ---------- plumbing ----------
    @app.after_request
    def cors(resp):  # the frontend may be opened from file:// or another port
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-AI-Provider, X-OpenRouter-Key, X-OpenRouter-Model, X-Ollama-Model, X-Ollama-Url"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return resp

    @app.route("/api/<path:_any>", methods=["OPTIONS"])
    def preflight(_any):
        return ("", 204)

    def body():
        return request.get_json(silent=True) or {}

    def visitor():
        # Behind a host's proxy (Render, Railway, Fly…) the real client is the first X-Forwarded-For hop.
        fwd = request.headers.get("X-Forwarded-For", "")
        return (fwd.split(",")[0].strip() if fwd and settings.public else "") or request.remote_addr or "?"

    def ai_client():
        return client_from_request(request.headers, body(), visitor=visitor())

    @app.errorhandler(UploadError)
    @app.errorhandler(ValueError)
    def bad_request(e):
        return jsonify(error="bad_request", message=str(e)), 400

    @app.errorhandler(AIUnavailable)
    def no_ai(e):
        return jsonify(error="ai_unavailable", message=str(e)), 503

    @app.errorhandler(AIError)
    def ai_failed(e):
        return jsonify(error="ai_failed", message=str(e)[:400]), 502

    @app.errorhandler(413)
    def too_big(_e):
        return jsonify(error="too_large", message=f"Upload is larger than {settings.max_upload_mb} MB"), 413

    # ---------- frontend ----------
    @app.get("/")
    def index():
        return send_from_directory(FRONTEND, "index.html")

    @app.get("/<path:path>")
    def static_files(path):
        if path.startswith("api/"):
            return jsonify(error="not_found"), 404
        return send_from_directory(FRONTEND, path)

    # ---------- API ----------
    @app.get("/api/health")
    def health():
        return jsonify(ok=True, version=VERSION, ai=ai_client().status())

    @app.post("/api/ai/test")
    def ai_test():
        ai = ai_client()
        t0 = time.time()
        out = ai.ask_json('Reply with exactly this JSON: {"ok":"DIMORA OK"}', max_tokens=40, timeout=60)
        return jsonify(ok=out.get("ok") == "DIMORA OK", reply=out, model=ai.used, ms=round((time.time() - t0) * 1000))

    @app.post("/api/plans/detect")
    def detect():
        files = request.files.getlist("files") or request.files.getlist("file")
        if not files:
            raise UploadError("Attach at least one floor plan (PNG, JPG, WEBP or PDF) as 'files'.")
        uploads = [(f.filename or "plan.png", f.read()) for f in files]
        form = request.form
        res = pipeline.detect(
            uploads,
            width_m=float(form.get("width_m") or 0),
            forced_floors=int(form.get("floors") or 0),
            use_ai=form.get("use_ai", "1") not in ("0", "false", "off"),
            ai=ai_client(),
            default_width=float(form.get("default_width") or 12),
        )
        return jsonify(res)

    @app.post("/api/plans/restack")
    def restack():
        b = body()
        floors, offs = pipeline.restack(b["floors"], b.get("offsets") or [[0, 0]] * len(b["floors"]))
        return jsonify(floors=floors, offsets=offs)

    @app.post("/api/analyze")
    def analyze_ep():
        return jsonify(analyze(body()["project"]))

    @app.post("/api/tour")
    def tour_ep():
        return jsonify(order=tour_order(body()["project"]))

    @app.post("/api/assistant")
    def assistant():
        b, ai = body(), ai_client()
        out = services.assistant(ai, b["text"], b.get("home", "{}"), b.get("action_doc", ""), b.get("history"))
        return jsonify(**out, via=ai.used)

    @app.post("/api/narration")
    def narration():
        b, ai = body(), ai_client()
        out = services.narration(ai, b.get("theme", ""), b.get("household", ""), b.get("floors", []), b.get("rooms", []))
        return jsonify(**out, via=ai.used)

    @app.post("/api/inspiration")
    def inspiration():
        b, ai = body(), ai_client()
        img = data_url_to_image(b["image"])
        out = services.inspiration(ai, to_jpeg_b64(img, 900), b.get("styles", []), b.get("floors", []), b.get("finishes", []))
        return jsonify(**out, via=ai.used)

    @app.post("/api/report-summary")
    def report_summary():
        b, ai = body(), ai_client()
        return jsonify(summary=services.report_summary(ai, b.get("facts", ""), b.get("room_facts", "")), via=ai.used)

    return app


app = create_app()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(f"DIMORA backend v{VERSION} → http://{settings.host}:{settings.port}")
    st = app.test_client().get("/api/health").get_json()["ai"]
    print(f"AI: provider={st['provider']} active={st['active']} | Ollama running={st['ollama']['running']} "
          f"model {st['ollama']['model']} installed={st['ollama']['model_installed']} | OpenRouter key={'yes' if st['openrouter']['configured'] else 'no'}")
    app.run(host=settings.host, port=settings.port, debug=False, threaded=True)
