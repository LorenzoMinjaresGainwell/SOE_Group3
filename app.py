from __future__ import annotations

import json
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from services.auto_refresh import AutoRefresh, apply_changes
from services.csv_store import CsvStore


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DOCS_DIR = ROOT / "docs"
store = CsvStore(ROOT / "data")
auto_refresh = AutoRefresh(ROOT / "data")


class AppHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        today = datetime.now(timezone.utc).date()

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
        if path == "/static/gainwell-logo.png":
            return self._send_file(DOCS_DIR / "gainwell-logo.png", "image/png")
        if path == "/api/opportunities":
            return self._send_json(apply_changes(store.list_opportunities(today=today), auto_refresh.changes()))
        if path == "/api/contracts":
            return self._send_json(store.list_contracts(today=today))
        if path == "/api/updates":
            return self._send_json(store.list_updates(today=today))
        if path == "/api/rht-overview":
            return self._send_json(store.rht_overview(today=today, limit=self._query_limit(parsed.query)))
        if path == "/api/competitors":
            params = parse_qs(parsed.query, keep_blank_values=True)
            query = params.get("q", [""])[0][:200]
            return self._send_json(store.competitor_profiles(
                today=today, query=query, limit=self._query_limit(parsed.query)
            ))
        if path == "/api/federal-records":
            return self._send_json(apply_changes(store.list_federal_records(today=today), auto_refresh.changes()))
        if path == "/api/refresh/status":
            return self._send_json(auto_refresh.status())
        if path.startswith("/api/federal-records/"):
            record_id = unquote(path.rsplit("/", 1)[-1])
            record = store.get_federal_record(record_id, today=today)
            if not record:
                return self._send_json({"error": "Federal record not found"}, 404)
            return self._send_json(record)
        if path.startswith("/api/opportunities/"):
            opportunity_id = unquote(path.rsplit("/", 1)[-1])
            opportunity = store.get_opportunity(opportunity_id, today=today)
            if not opportunity:
                return self._send_json({"error": "Opportunity not found"}, 404)
            apply_changes([opportunity], auto_refresh.changes())
            return self._send_json(opportunity)
        if path.startswith("/api/contracts/"):
            contract = store.get_contract(unquote(path.rsplit("/", 1)[-1]), today=today)
            if not contract:
                return self._send_json({"error": "Contract not found"}, 404)
            return self._send_json(contract)
        if path.startswith("/api/updates/"):
            update = store.get_update(unquote(path.rsplit("/", 1)[-1]), today=today)
            if not update:
                return self._send_json({"error": "Update not found"}, 404)
            return self._send_json(update)
        if path == "/api/sources":
            return self._send_json(store.list_sources())
        if path == "/api/scoring-rules":
            return self._send_json(store.list_scoring_rules())

        return self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        try:
            return self._do_POST()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return self._send_json({"error": "Request body must be a valid JSON object"}, 400)

    def _do_POST(self):
        path = urlparse(self.path).path
        today = datetime.now(timezone.utc).date()

        if path.startswith("/api/opportunities/") and path.endswith("/status"):
            opportunity_id = unquote(path.split("/")[-2])
            payload = self._read_json()
            status_value = payload.get("status")
            status = status_value.strip() if isinstance(status_value, str) else ""
            if status not in {"Pursue", "Monitor", "Decline", "Unreviewed"}:
                return self._send_json({"error": "Invalid status"}, 400)
            opportunity = store.update_status(opportunity_id, status, payload.get("note", ""), today=today)
            if not opportunity:
                return self._send_json({"error": "Opportunity not found"}, 404)
            return self._send_json(opportunity)

        if path.startswith("/api/opportunities/") and path.endswith("/pin"):
            opportunity_id = unquote(path.split("/")[-2])
            payload = self._read_json()
            if not isinstance(payload.get("pinned"), bool):
                return self._send_json({"error": "Pinned must be true or false"}, 400)
            opportunity = store.update_pinned(opportunity_id, payload["pinned"], today=today)
            if not opportunity:
                return self._send_json({"error": "Opportunity not found"}, 404)
            return self._send_json(opportunity)

        if path == "/api/refresh":
            payload = self._read_json()
            return self._send_json(auto_refresh.start(force=bool(payload.get("force"))), 202)

        return self._send_json({"error": "Not found"}, 404)

    def _query_limit(self, query: str) -> int:
        raw = parse_qs(query).get("limit", ["20"])[0]
        try:
            return max(1, min(int(raw), 50))
        except ValueError:
            return 20

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length < 0:
            raise ValueError("Invalid Content-Length")
        if length == 0:
            return {}
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

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
