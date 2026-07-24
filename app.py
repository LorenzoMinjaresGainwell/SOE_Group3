from __future__ import annotations

import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from services.csv_store import CsvStore
from services.gov_api_client import refresh_opportunities
from services.neural_model import analyze_opportunity
from services.scoring import explain_fit_score


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
store = CsvStore(ROOT / "data")


class AppHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" and parsed.query == "view=records":
            return self._send_file(STATIC_DIR / "federal-records.html", "text/html")
        if path == "/":
            return self._send_file(STATIC_DIR / "index.html", "text/html")
        if path == "/records":
            return self._send_file(STATIC_DIR / "federal-records.html", "text/html")
        if path == "/static/styles.css":
            return self._send_file(STATIC_DIR / "styles.css", "text/css")
        if path == "/static/app.js":
            return self._send_file(STATIC_DIR / "app.js", "application/javascript")
        if path == "/static/federal-records.js":
            return self._send_file(STATIC_DIR / "federal-records.js", "application/javascript")
        if path == "/api/opportunities":
            return self._send_json(store.list_opportunities())
        if path == "/api/federal-records":
            return self._send_json(store.list_federal_records())
        if path.startswith("/api/federal-records/"):
            record_id = path.rsplit("/", 1)[-1]
            record = store.get_federal_record(record_id)
            if not record:
                return self._send_json({"error": "Federal record not found"}, 404)
            return self._send_json(record)
        if path.startswith("/api/opportunities/"):
            opportunity_id = path.rsplit("/", 1)[-1]
            opportunity = store.get_opportunity(opportunity_id)
            if not opportunity:
                return self._send_json({"error": "Opportunity not found"}, 404)
            rules = store.list_scoring_rules()
            opportunity["fit_breakdown"] = explain_fit_score(opportunity, rules)
            opportunity["analysis"] = analyze_opportunity(opportunity)
            return self._send_json(opportunity)
        if path == "/api/sources":
            return self._send_json(store.list_sources())
        if path == "/api/scoring-rules":
            return self._send_json(store.list_scoring_rules())

        return self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path.startswith("/api/opportunities/") and path.endswith("/status"):
            opportunity_id = path.split("/")[-2]
            payload = self._read_json()
            status = payload.get("status", "").strip()
            if status not in {"Pursue", "Monitor", "Decline", "Unreviewed"}:
                return self._send_json({"error": "Invalid status"}, 400)
            opportunity = store.update_status(opportunity_id, status, payload.get("note", ""))
            if not opportunity:
                return self._send_json({"error": "Opportunity not found"}, 404)
            rules = store.list_scoring_rules()
            opportunity["fit_breakdown"] = explain_fit_score(opportunity, rules)
            opportunity["analysis"] = analyze_opportunity(opportunity)
            return self._send_json(opportunity)

        if path == "/api/refresh":
            return self._send_json(refresh_opportunities())

        return self._send_json({"error": "Not found"}, 404)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str):
        if not path.exists():
            return self._send_json({"error": "File not found"}, 404)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    server = ThreadingHTTPServer(("localhost", 8000), AppHandler)
    print("Rural Health Opportunity Dashboard")
    print("Open http://localhost:8000")
    server.serve_forever()


if __name__ == "__main__":
    main()
