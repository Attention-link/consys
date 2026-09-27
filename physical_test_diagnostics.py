#!/usr/bin/env python3
"""Collect read-only physical-test evidence without changing a WFB deployment."""

import argparse
import csv
import hashlib
import json
import re
import socket
import struct
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


MAX_API_FRAME = 8 * 1024 * 1024
SENSITIVE_NAME = re.compile(r"(key|secret|password|token|private)", re.IGNORECASE)
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(key|secret|password|token|private[_-]?key)\b\s*[:=]\s*\S+"
)


def timestamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def redact_text(value):
    """Retain diagnostic failures while removing assignment-style secrets."""
    return SENSITIVE_ASSIGNMENT.sub(r"\1=<redacted>", str(value))


def sanitize(value, field_name=""):
    if SENSITIVE_NAME.search(str(field_name)):
        return "<redacted>"
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(key): sanitize(item, str(key)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    return value


def run_command(arguments, timeout=10):
    try:
        completed = subprocess.run(
            arguments, check=False, shell=False, text=True, capture_output=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, redact_text(error)
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    if completed.returncode:
        return None, redact_text(
            "command exited with {}: {}".format(completed.returncode, output or "no output")
        )
    return redact_text(output), None


def record(records, source, status, data=None, error=None):
    entry = {"recorded_at": timestamp(), "source": source, "status": status}
    if data is not None:
        entry["data"] = sanitize(data)
    if error is not None:
        entry["error"] = redact_text(error)
    records.append(entry)


def systemd_state(records, role):
    unit = "wifibroadcast@{}.service".format(role)
    output, error = run_command(
        ["systemctl", "show", unit, "--no-pager", "-p", "LoadState", "-p", "ActiveState",
         "-p", "SubState", "-p", "Result", "-p", "MainPID", "-p", "ExecMainStatus"]
    )
    if error:
        record(records, "systemd_service", "not_recorded", error=error)
        return
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    record(records, "systemd_service", "recorded", values)


def read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip(), None
    except OSError as error:
        return None, redact_text(error)


def nic_identity(records):
    root = Path("/sys/class/net")
    try:
        interfaces = sorted(item.name for item in root.iterdir() if item.name != "lo")
    except OSError as error:
        record(records, "nic_identity", "not_recorded", error=error)
        return
    devices = []
    for interface in interfaces:
        base = root / interface
        address, address_error = read_text(base / "address")
        driver_link = base / "device" / "driver"
        try:
            driver = driver_link.resolve().name if driver_link.exists() else None
        except OSError as error:
            driver = None
            address_error = address_error or redact_text(error)
        modalias, modalias_error = read_text(base / "device" / "modalias")
        item = {
            "interface": interface,
            "mac_address": address,
            "driver": driver,
            "modalias": modalias,
        }
        errors = [message for message in (address_error, modalias_error) if message]
        if errors:
            item["read_errors"] = errors
        devices.append(item)
    record(records, "nic_identity", "recorded", {"interfaces": devices})


def config_hash(records, config_path):
    try:
        digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    except FileNotFoundError:
        record(records, "wfb_config_hash", "not_recorded", error="configuration file does not exist")
    except OSError as error:
        record(records, "wfb_config_hash", "not_recorded", error=error)
    else:
        record(records, "wfb_config_hash", "recorded", {
            "path": str(config_path), "sha256": digest,
            "contents_recorded": False,
        })


class MsgpackReader:
    """Small read-only MessagePack decoder for the WFB statistics API."""

    def __init__(self, payload):
        self.payload = payload
        self.position = 0

    def take(self, size):
        if self.position + size > len(self.payload):
            raise ValueError("truncated MessagePack value")
        result = self.payload[self.position:self.position + size]
        self.position += size
        return result

    def integer(self, size, signed=False):
        return int.from_bytes(self.take(size), "big", signed=signed)

    def value(self):
        code = self.integer(1)
        if code <= 0x7F:
            return code
        if 0x80 <= code <= 0x8F:
            return self.mapping(code & 0x0F)
        if 0x90 <= code <= 0x9F:
            return [self.value() for _ in range(code & 0x0F)]
        if 0xA0 <= code <= 0xBF:
            return self.take(code & 0x1F).decode("utf-8", "replace")
        if code >= 0xE0:
            return code - 256
        if code == 0xC0:
            return None
        if code == 0xC2:
            return False
        if code == 0xC3:
            return True
        if code == 0xC4:
            return self.take(self.integer(1))
        if code == 0xC5:
            return self.take(self.integer(2))
        if code == 0xC6:
            return self.take(self.integer(4))
        if code == 0xCA:
            return struct.unpack(">f", self.take(4))[0]
        if code == 0xCB:
            return struct.unpack(">d", self.take(8))[0]
        unsigned = {0xCC: 1, 0xCD: 2, 0xCE: 4, 0xCF: 8}
        signed = {0xD0: 1, 0xD1: 2, 0xD2: 4, 0xD3: 8}
        if code in unsigned:
            return self.integer(unsigned[code])
        if code in signed:
            return self.integer(signed[code], signed=True)
        if code in (0xD9, 0xDA, 0xDB):
            size = self.integer({0xD9: 1, 0xDA: 2, 0xDB: 4}[code])
            return self.take(size).decode("utf-8", "replace")
        if code in (0xDC, 0xDD):
            return [self.value() for _ in range(self.integer({0xDC: 2, 0xDD: 4}[code]))]
        if code in (0xDE, 0xDF):
            return self.mapping(self.integer({0xDE: 2, 0xDF: 4}[code]))
        raise ValueError("unsupported MessagePack type 0x{:02x}".format(code))

    def mapping(self, size):
        result = {}
        for _ in range(size):
            key = self.value()
            if not isinstance(key, (str, int, float, bool)):
                key = repr(key)
            result[str(key)] = self.value()
        return result


def receive_exact(sock, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise OSError("connection closed before a complete API frame arrived")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def api_statistics(records, ports, timeout, sample_index=1):
    failures = []
    for port in ports:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
                sock.settimeout(timeout)
                size = struct.unpack(">I", receive_exact(sock, 4))[0]
                if size > MAX_API_FRAME:
                    raise ValueError("API frame is too large: {} bytes".format(size))
                value = MsgpackReader(receive_exact(sock, size)).value()
                record(records, "wfb_statistics_api", "recorded", {
                    "host": "127.0.0.1", "port": port, "sample_index": sample_index,
                    "message": value,
                })
                return
        except (OSError, ValueError, struct.error) as error:
            failures.append("127.0.0.1:{}: {}".format(port, redact_text(error)))
    record(records, "wfb_statistics_api", "not_recorded", {
        "sample_index": sample_index,
    }, error="; ".join(failures))


def flatten(value, prefix=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from flatten(item, "{}.{}".format(prefix, key) if prefix else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from flatten(item, "{}[{}]".format(prefix, index))
    else:
        yield prefix, value


def write_evidence(output_dir, stem, document, records):
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / (stem + ".json")
    jsonl_path = output_dir / (stem + ".jsonl")
    csv_path = output_dir / (stem + ".csv")
    json_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for entry in records:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("recorded_at", "source", "status", "field", "value"))
        for entry in records:
            for field, value in flatten(entry):
                if field not in ("recorded_at", "source", "status"):
                    writer.writerow((entry["recorded_at"], entry["source"], entry["status"], field,
                                     json.dumps(value, sort_keys=True) if isinstance(value, (dict, list))
                                     else value))
    return json_path, csv_path, jsonl_path


def parse_metadata(items):
    metadata = {}
    for item in items:
        if "=" not in item or not item.split("=", 1)[0]:
            raise argparse.ArgumentTypeError("--metadata requires NAME=VALUE")
        name, value = item.split("=", 1)
        metadata[name] = redact_text(value)
    return metadata


def api_port(value):
    """Return a valid TCP/UDP port or raise an argparse-friendly error."""
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "API port must be an integer from 1 to 65535"
        ) from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("API port must be from 1 to 65535")
    return port


def main():
    parser = argparse.ArgumentParser(
        description="Collect opt-in, read-only WFB physical-test diagnostic evidence."
    )
    parser.add_argument("--run-id", required=True, help="Operator-supplied physical-test run identifier.")
    parser.add_argument("--role", required=True, choices=("gs", "drone"), help="Endpoint role under test.")
    parser.add_argument("--output-dir", required=True, type=Path, help="Directory for JSON, CSV, and JSONL evidence.")
    parser.add_argument("--metadata", action="append", default=[], metavar="NAME=VALUE",
                        help="Additional operator-supplied run metadata; may be repeated.")
    parser.add_argument("--config-path", type=Path, default=Path("/etc/wifibroadcast.cfg"),
                        help="WFB configuration to hash; contents are never read into evidence.")
    parser.add_argument("--api-port", action="append", type=api_port, metavar="PORT",
                        help="WFB statistics API port (1-65535); may be repeated.")
    parser.add_argument("--api-timeout", type=float, default=2.0,
                        help="Timeout in seconds for each local API attempt (default: 2).")
    parser.add_argument("--sample-count", type=int, default=1,
                        help="Number of WFB statistics snapshots to record (default: 1).")
    parser.add_argument("--sample-interval", type=float, default=1.0,
                        help="Seconds between WFB statistics snapshots (default: 1).")
    arguments = parser.parse_args()
    if arguments.api_timeout <= 0:
        parser.error("--api-timeout must be positive")
    if arguments.sample_count < 1:
        parser.error("--sample-count must be at least one")
    if arguments.sample_interval <= 0:
        parser.error("--sample-interval must be positive")

    run_metadata = parse_metadata(arguments.metadata)
    records = []
    record(records, "run_metadata", "recorded", {
        "run_id": arguments.run_id, "role": arguments.role, "operator_metadata": run_metadata,
    })
    systemd_state(records, arguments.role)
    nic_identity(records)
    config_hash(records, arguments.config_path)
    for sample_index in range(1, arguments.sample_count + 1):
        api_statistics(
            records, arguments.api_port or (8002, 8003), arguments.api_timeout, sample_index
        )
        if sample_index < arguments.sample_count:
            time.sleep(arguments.sample_interval)

    collected_at = timestamp()
    document = {
        "schema_version": 1,
        "collected_at": collected_at,
        "run_id": arguments.run_id,
        "role": arguments.role,
        "records": records,
    }
    safe_run_id = re.sub(r"[^A-Za-z0-9._-]+", "_", arguments.run_id).strip("._") or "run"
    stem = "physical-test-{}-{}".format(safe_run_id, collected_at.replace(":", "").replace("-", ""))
    paths = write_evidence(arguments.output_dir, stem, document, records)
    print("\n".join(str(path) for path in paths))


if __name__ == "__main__":
    main()
