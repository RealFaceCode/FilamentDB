from __future__ import annotations

import argparse
import base64
from datetime import datetime
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import urllib.error
import urllib.request
from uuid import uuid4


def _build_multipart_form_data(fields: dict[str, str], file_field_name: str, file_path: Path) -> tuple[bytes, str]:
    boundary = f"----FilamentDBBoundary{uuid4().hex}"
    lines: list[bytes] = []

    for key, value in fields.items():
        lines.append(f"--{boundary}".encode("utf-8"))
        lines.append(f'Content-Disposition: form-data; name="{key}"'.encode("utf-8"))
        lines.append(b"")
        lines.append(str(value).encode("utf-8"))

    filename = file_path.name
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    file_content = file_path.read_bytes()

    lines.append(f"--{boundary}".encode("utf-8"))
    lines.append(
        f'Content-Disposition: form-data; name="{file_field_name}"; filename="{filename}"'.encode("utf-8")
    )
    lines.append(f"Content-Type: {content_type}".encode("utf-8"))
    lines.append(b"")
    lines.append(file_content)
    lines.append(f"--{boundary}--".encode("utf-8"))
    lines.append(b"")

    body = b"\r\n".join(lines)
    return body, f"multipart/form-data; boundary={boundary}"


def _compute_job_id(file_path: Path) -> str:
    stat = file_path.stat()
    fingerprint = f"{file_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:20]
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"sl-{timestamp}-{digest}"


def _append_log(log_file: Path | None, message: str) -> None:
    if not log_file:
        return
    log_file.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Auto deduct filament in Filament_Datenbank from slicer export")
    parser.add_argument("file", help="Path to the generated print file (.3mf, .gcode, .gco, .bgcode)")
    parser.add_argument(
        "--endpoint",
        default=str(os.getenv("FILAMENT_DB_ENDPOINT", "")).strip(),
        help="Filament_Datenbank API endpoint",
    )
    parser.add_argument("--project", default="private", choices=["private", "business"], help="Target project")
    parser.add_argument("--slicer", default="Slicer", help="Slicer name shown in usage history")
    parser.add_argument("--printer", default=None, help="Printer name for usage tracking")
    parser.add_argument("--ams-slots", default=None, help="Used AMS slots (for example: 1,2,4)")
    parser.add_argument("--job-id", default=None, help="Optional external job id for idempotency")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to DB")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout in seconds")
    parser.add_argument(
        "--log-file",
        default=None,
        help="Optional log file path (for example %%TEMP%%\\filament_slicer_auto_usage.log)",
    )
    parser.add_argument("--auth-user", default=None, help="Optional HTTP Basic auth username")
    parser.add_argument("--auth-password", default=None, help="Optional HTTP Basic auth password")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    file_path = Path(args.file).expanduser()
    log_file = Path(args.log_file).expanduser() if args.log_file else None

    if not file_path.exists() or not file_path.is_file():
        _append_log(log_file, f"ERROR File not found: {file_path}")
        print(f"ERROR: File not found: {file_path}")
        return 4

    suffixes = {suffix.lower() for suffix in file_path.suffixes}
    supported_suffixes = {".3mf", ".gcode", ".gco", ".bgcode"}
    if not (suffixes & supported_suffixes):
        _append_log(log_file, f"ERROR Unsupported file type: {file_path.name}")
        print("ERROR: Only .3mf, .gcode, .gco and .bgcode files are supported")
        return 4

    endpoint = str(args.endpoint or "").strip()
    if not endpoint:
        _append_log(log_file, "ERROR Missing endpoint. Set --endpoint or FILAMENT_DB_ENDPOINT.")
        print("ERROR: Missing endpoint. Set --endpoint or FILAMENT_DB_ENDPOINT.")
        return 4

    job_id = args.job_id or _compute_job_id(file_path)
    fields = {
        "project": args.project,
        "slicer": args.slicer,
        "job_id": job_id,
        "dry_run": "1" if args.dry_run else "0",
    }
    if args.printer:
        fields["printer"] = str(args.printer)
    if args.ams_slots:
        fields["ams_slots"] = str(args.ams_slots)

    body, content_type = _build_multipart_form_data(fields, "file", file_path)
    headers = {"Content-Type": content_type, "Accept": "application/json"}
    if args.auth_user and args.auth_password:
        token = base64.b64encode(f"{args.auth_user}:{args.auth_password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"

    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")

    _append_log(log_file, f"POST {endpoint} file={file_path.name} project={args.project} job_id={job_id}")

    try:
        with urllib.request.urlopen(request, timeout=max(1, int(args.timeout))) as response:
            payload = response.read().decode("utf-8", errors="replace")
            data = json.loads(payload) if payload else {}
    except urllib.error.HTTPError as error:
        body_text = error.read().decode("utf-8", errors="replace")
        _append_log(log_file, f"HTTP {error.code}: {body_text}")
        print(f"ERROR: API returned HTTP {error.code}")
        return 2
    except Exception as error:
        _append_log(log_file, f"ERROR Request failed: {error}")
        print(f"ERROR: Request failed: {error}")
        return 3

    if not data.get("ok"):
        _append_log(log_file, f"API error payload: {json.dumps(data, ensure_ascii=False)}")
        print(f"ERROR: API error: {data.get('error', 'unknown')}")
        return 2

    changed = int(data.get("changed_spools") or 0)
    deducted_g = float(data.get("deducted_g") or 0.0)
    already_applied = bool(data.get("already_applied"))
    dry_run = bool(data.get("dry_run"))

    if already_applied:
        message = f"OK (already applied) job_id={data.get('job_id', job_id)}"
    elif dry_run:
        message = f"OK dry-run changed_spools={changed} deducted_g={deducted_g:.3f}"
    else:
        message = f"OK changed_spools={changed} deducted_g={deducted_g:.3f}"

    _append_log(log_file, message)
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
