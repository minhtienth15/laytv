#!/usr/bin/env python3
"""Fetch SB live-room JSON and generate an M3U playlist.

Expected API response: a JSON array containing objects with `id`, `path`,
 and `stream` fields. Signed stream URLs are copied verbatim.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def fetch_json(api_url: str, timeout: int, headers: list[str]) -> Any:
    request_headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"}

    for header in headers:
        if ":" not in header:
            raise ValueError(f"Header không hợp lệ: {header!r}. Dùng dạng 'Tên: Giá trị'.")
        name, value = header.split(":", 1)
        request_headers[name.strip()] = value.strip()

    request = Request(api_url, headers=request_headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read().decode(charset)
    except HTTPError as exc:
        raise RuntimeError(f"API trả về HTTP {exc.code}: {exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(f"Không thể kết nối API: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"Hết thời gian chờ API sau {timeout} giây.") from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        preview = body[:300].replace("\n", " ")
        raise RuntimeError(f"API không trả JSON hợp lệ. Nội dung đầu: {preview!r}") from exc


def read_json_file(input_path: Path) -> Any:
    try:
        return json.loads(input_path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise RuntimeError(f"Không đọc được file {input_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"File {input_path} không phải JSON hợp lệ: {exc}") from exc


def safe_attribute(value: str) -> str:
    """Keep an M3U attribute on one line and prevent quote termination."""
    return value.replace("\r", "").replace("\n", "").replace('"', "%22")


def build_playlist(
    data: Any,
    group_title: str,
) -> tuple[str, int, int]:
    if not isinstance(data, list):
        raise ValueError("JSON gốc phải là một array như livingRoom.txt.")

    lines = ["#EXTM3U"]
    skipped = 0

    for index, item in enumerate(data):
        if not isinstance(item, dict):
            print(f"Bỏ qua phần tử #{index + 1}: không phải object.", file=sys.stderr)
            skipped += 1
            continue

        raw_id = item.get("id")
        raw_stream = item.get("stream")
        if raw_id is None or not str(raw_id).strip():
            print(f"Bỏ qua phần tử #{index + 1}: thiếu id.", file=sys.stderr)
            skipped += 1
            continue
        if not isinstance(raw_stream, str) or not raw_stream.strip():
            print(f"Bỏ qua id={raw_id}: thiếu stream.", file=sys.stderr)
            skipped += 1
            continue

        channel_id = str(raw_id).strip()
        stream = raw_stream.strip()
        parsed = urlparse(stream)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            print(f"Bỏ qua id={channel_id}: stream không phải URL HTTP(S).", file=sys.stderr)
            skipped += 1
            continue

        channel_name = f"SB-{channel_id}"
        attributes = [
            f'tvg-id="{safe_attribute(channel_id)}"',
            f'tvg-name="{safe_attribute(channel_name)}"',
            'tvg-logo=""',
        ]
        if group_title:
            attributes.append(f'group-title="{safe_attribute(group_title)}"')

        lines.append(f"#EXTINF:-1 {' '.join(attributes)},{channel_name}")
        # Preserve the complete signed URL, including auth_key / txSecret / txTime.
        lines.append(stream)

    valid_count = (len(lines) - 1) // 2
    if valid_count == 0:
        raise ValueError("Không tìm thấy stream hợp lệ; không tạo playlist rỗng.")

    return "\n".join(lines) + "\n", valid_count, skipped


def write_atomic(output_path: Path, content: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=output_path.parent,
            prefix=f".{output_path.name}.", suffix=".tmp", delete=False,
        ) as temp_file:
            temp_name = temp_file.name
            temp_file.write(content)
        os.replace(temp_name, output_path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Lấy danh sách SB live room từ API và tạo playlist M3U."
    )
    source = parser.add_mutually_exclusive_group(required=False)
    source.add_argument("--api-url", default=os.getenv("API_URL"),
                        help="URL API trả về JSON array (hoặc đặt biến môi trường API_URL).")
    source.add_argument("--input", type=Path,
                        help="Đọc JSON từ file có sẵn, ví dụ livingRoom.txt.")
    parser.add_argument("-o", "--output", type=Path, default=Path("livingRoom.m3u"),
                        help="File playlist đầu ra (mặc định: livingRoom.m3u).")
    parser.add_argument("--group-title", default="SB",
                        help="Tên group-title trong M3U (mặc định: SB).")
    parser.add_argument("--timeout", type=int, default=30,
                        help="Timeout gọi API theo giây (mặc định: 30).")
    parser.add_argument("--header", action="append", default=[],
                        help="HTTP header bổ sung, có thể dùng nhiều lần: 'Tên: Giá trị'.")
    args = parser.parse_args()

    # argparse's mutually exclusive group considers --api-url present even
    # when it comes from the environment only if the option was supplied.
    if not args.input and not args.api_url:
        parser.error("Cần --api-url hoặc biến môi trường API_URL, hoặc dùng --input.")
    if args.timeout <= 0:
        parser.error("--timeout phải lớn hơn 0.")
    return args


def main() -> int:
    args = parse_args()
    try:
        data = read_json_file(args.input) if args.input else fetch_json(
            args.api_url, args.timeout, args.header
        )
        playlist, valid_count, skipped_count = build_playlist(
            data=data,
            group_title=args.group_title,
        )
        write_atomic(args.output, playlist)
    except (ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Đã tạo: {args.output}")
    print(f"Số kênh hợp lệ: {valid_count}")
    print(f"Số phần tử bỏ qua: {skipped_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
