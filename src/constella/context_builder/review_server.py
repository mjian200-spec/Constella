"""Small local server for reviewing Context Builder outputs in a browser."""

from __future__ import annotations

import json
import mimetypes
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Type
from urllib.parse import unquote, urlparse


DATA_FILES = {
    "document_graph.json",
    "context_packages.jsonl",
    "ontology_candidates.jsonl",
    "ambiguities.jsonl",
    "run_report.json",
}


def make_review_handler(review_dir: Path, output_dir: Path) -> Type[SimpleHTTPRequestHandler]:
    """Create a handler limited to the review app and one output directory."""
    graph_path = output_dir / "document_graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8")) if graph_path.exists() else {"units": {}}
    input_path = Path(graph.get("metadata", {}).get("input_path", ""))
    source_dir = input_path.parent if input_path.is_absolute() else (Path.cwd() / input_path).parent
    source_dir = source_dir.resolve()
    units = graph.get("units", {})

    class ReviewHandler(SimpleHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib method name
            request_path = unquote(urlparse(self.path).path)
            if request_path == "/":
                self._send_file(review_dir / "index.html")
                return
            if request_path in {"/app.js", "/styles.css"}:
                self._send_file(review_dir / request_path.lstrip("/"))
                return
            if request_path.startswith("/data/"):
                filename = request_path.removeprefix("/data/")
                self._send_file(output_dir / filename if filename in DATA_FILES else None)
                return
            if request_path.startswith("/assets/"):
                self._send_asset(request_path.removeprefix("/assets/"))
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown review resource")

        def _send_asset(self, unit_id: str) -> None:
            unit = units.get(unit_id, {})
            asset_path = unit.get("source", {}).get("asset_path")
            if not asset_path:
                self.send_error(HTTPStatus.NOT_FOUND, "Asset is not available")
                return
            candidate = (source_dir / asset_path).resolve()
            try:
                candidate.relative_to(source_dir)
            except ValueError:
                self.send_error(HTTPStatus.FORBIDDEN, "Asset path is outside the source document")
                return
            self._send_file(candidate)

        def _send_file(self, path: Path | None) -> None:
            if path is None or not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "Review resource not found")
                return
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            print(f"[review] {self.address_string()} - {format % args}")

    return ReviewHandler


def serve_review(review_dir: str | Path, output_dir: str | Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    handler = make_review_handler(Path(review_dir).resolve(), Path(output_dir).resolve())
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Context Builder review: http://{host}:{port}/")
    print(f"Output directory: {Path(output_dir).resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nReview server stopped.")
    finally:
        server.server_close()
