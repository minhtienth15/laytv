#!/usr/bin/env python3
"""
IPTV room scraper for JSONP/JSON APIs.

Flow:
  1. Fetch all_live_rooms endpoint.
  2. Extract unique roomNum values recursively.
  3. Fetch {API_BASE}/{roomNum}/{DETAIL_FILE} for each room.
  4. Export playlist.m3u and channels.json.

Configure API_BASE and proxy via CLI flags or environment variables.
Use only with streams and endpoints you are authorized to access.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests


DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}


def parse_json_or_jsonp(text: str) -> Any:
    """Parse JSON or JSONP such as callback({...});"""
    text = text.strip().lstrip("\ufeff")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.match(r"^[\w.$]+\s*\((.*)\)\s*;?\s*$", text, flags=re.S)
        if not match:
            raise ValueError("Response is neither valid JSON nor recognizable JSONP")
        return json.loads(match.group(1))


class ProxyPool:
    """Loads proxy endpoints and rotates them for API requests only."""
    def __init__(self, proxies: list[str]):
        self.proxies = proxies
        self._index = 0
        self._lock = threading.Lock()

    @classmethod
    def from_url(cls, url: str, timeout: float) -> "ProxyPool":
        response = requests.get(url, timeout=timeout, headers=DEFAULT_HEADERS)
        response.raise_for_status()
        proxies: list[str] = []
        seen: set[str] = set()
        for line in response.text.splitlines():
            value = line.strip()
            if not value or value.startswith(("#", "//")):
                continue
            if re.fullmatch(r"[^:/\s]+:\d+", value):
                value = "http://" + value
            elif not re.match(r"^https?://", value, flags=re.I):
                continue
            if value not in seen:
                seen.add(value)
                proxies.append(value)
        if not proxies:
            raise RuntimeError("Proxy list was downloaded but contained no supported IP:PORT/HTTP proxy entries")
        return cls(proxies)

    def next(self) -> str:
        with self._lock:
            proxy = self.proxies[self._index % len(self.proxies)]
            self._index += 1
            return proxy

    def count(self) -> int:
        return len(self.proxies)


def request_json(url: str, timeout: float, retries: int,
                 base_headers: dict[str, str], proxy_pool: ProxyPool | None = None,
                 fixed_proxy: str | None = None) -> Any:
    last_error = None
    attempts = (min(max(retries + 1, 1), proxy_pool.count()) if proxy_pool else retries + 1)
    for attempt in range(attempts):
        session = requests.Session()
        session.headers.update(base_headers)
        proxy = proxy_pool.next() if proxy_pool else fixed_proxy
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            return parse_json_or_jsonp(response.text)
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(min(0.5 * (attempt + 1), 2))
        finally:
            session.close()
    raise RuntimeError(f"Request failed after retries ({type(last_error).__name__})")


def iter_room_objects(node: Any):
    """Yield dictionaries containing roomNum, including nested data groups/hot lists."""
    if isinstance(node, dict):
        if node.get("roomNum") is not None:
            yield node
        for value in node.values():
            yield from iter_room_objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_room_objects(value)


def extract_rooms(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        raise ValueError("Unexpected list API response: expected an object")
    if payload.get("code") not in (None, 200, "200"):
        raise ValueError(f"API returned code={payload.get('code')}: {payload.get('msg', '')}")
    root = payload.get("data", payload)
    unique: dict[str, dict] = {}
    for item in iter_room_objects(root):
        room_num = str(item.get("roomNum", "")).strip()
        if room_num and room_num not in unique:
            unique[room_num] = item
    return list(unique.values())


def first_stream(stream: dict, preferred: str) -> tuple[str, str]:
    preference = {
        "hd": ["hdM3u8", "m3u8", "hdFlv", "flv"],
        "sd": ["m3u8", "hdM3u8", "flv", "hdFlv"],
        "flv": ["hdFlv", "flv", "hdM3u8", "m3u8"],
    }.get(preferred, ["hdM3u8", "m3u8", "hdFlv", "flv"])
    for key in preference:
        value = stream.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value, key
    return "", ""


def make_session(proxy: str | None) -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape room details into an IPTV M3U playlist.")
    parser.add_argument("--api-base", default=os.getenv("API_BASE", ""),
                        help="Base URL before /{roomNum}/{detail-file} (or use API_BASE env var)")
    parser.add_argument("--detail-file", default=os.getenv("DETAIL_FILE", "details.json"),
                        help="Detail filename (default: details.json; or use DETAIL_FILE env var)")
    parser.add_argument("--rooms-url", default=os.getenv("ROOMS_URL", ""),
                        help="Full all_live_rooms.json endpoint (or use ROOMS_URL env var)")
    parser.add_argument("--proxy", default="",
                        help="Use one fixed proxy instead of a proxy list (e.g. http://IP:PORT)")
    parser.add_argument("--proxy-list-url", default=os.getenv("PROXY_LIST_URL", "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=ipport&format=text&country=vn"),
                        help="URL returning proxies as IP:PORT or HTTP proxy URLs; set empty string to disable pool")
    parser.add_argument("--out", default="output", help="Output directory (default: output)")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent detail requests (default: 8)")
    parser.add_argument("--timeout", type=float, default=20, help="Per-request timeout in seconds")
    parser.add_argument("--retries", type=int, default=2, help="Retries per request")
    parser.add_argument("--quality", choices=["hd", "sd", "flv"], default="hd",
                        help="Preferred stream quality (default: hd)")
    parser.add_argument("--input-rooms", default="", help="Use a saved all_live_rooms JSON/JSONP file instead of fetching it")
    parser.add_argument("--headers-file", default="", help="Optional JSON file containing extra HTTP headers")
    args = parser.parse_args()

    if not args.input_rooms and not args.rooms_url:
        parser.error("Provide --rooms-url or --input-rooms")
    if not args.api_base:
        parser.error("Provide --api-base, the base URL used for /{roomNum}/{detail-file}")

    base_headers = dict(DEFAULT_HEADERS)
    if args.headers_file:
        with open(args.headers_file, "r", encoding="utf-8") as f:
            extra_headers = json.load(f)
        if not isinstance(extra_headers, dict):
            parser.error("--headers-file must contain a JSON object")
        base_headers.update({str(k): str(v) for k, v in extra_headers.items()})

    proxy_pool = None
    if args.proxy_list_url.strip() and not args.proxy.strip():
        try:
            proxy_pool = ProxyPool.from_url(args.proxy_list_url.strip(), args.timeout)
            print(f"Loaded {proxy_pool.count()} proxy candidates; checking them against the IPTV API as requests run.")
        except Exception as exc:
            print(f"[WARN] Could not load proxy list ({exc}).", file=sys.stderr)
            return 2
    fixed_proxy = args.proxy.strip() or None

    if args.input_rooms:
        raw = Path(args.input_rooms).read_text(encoding="utf-8")
        rooms_payload = parse_json_or_jsonp(raw)
    else:
        rooms_payload = request_json(args.rooms_url, args.timeout, args.retries, base_headers, proxy_pool, fixed_proxy)

    rooms = extract_rooms(rooms_payload)
    if not rooms:
        print("No roomNum values found. Check the rooms endpoint/response.", file=sys.stderr)
        return 2

    base = args.api_base.rstrip("/") + "/"
    def fetch_detail(room_stub: dict) -> dict | None:
        room_num = str(room_stub["roomNum"]).strip()
        detail_file = args.detail_file.strip().lstrip("/") or "details.json"
        detail_url = urljoin(base, f"{room_num}/{detail_file}")
        try:
            payload = request_json(detail_url, args.timeout, args.retries, base_headers, proxy_pool, fixed_proxy)
            if not isinstance(payload, dict) or payload.get("code") not in (None, 200, "200"):
                raise ValueError(f"Unexpected API payload/code: {payload.get('code') if isinstance(payload, dict) else type(payload).__name__}")
            data = payload.get("data", payload)
            room = data.get("room", {}) if isinstance(data, dict) else {}
            stream = data.get("stream", {}) if isinstance(data, dict) else {}
            url, stream_key = first_stream(stream if isinstance(stream, dict) else {}, args.quality)
            if not url:
                raise ValueError("No supported stream URL found in data.stream")
            anchor = room.get("anchor", {}) if isinstance(room, dict) else {}
            title = str(room.get("title") or room.get("detail") or room_stub.get("title") or f"Room {room_num}")
            nickname = str(anchor.get("nickName") or room_stub.get("anchor", {}).get("nickName") or "")
            return {
                "roomNum": room_num,
                "name": title,
                "group": "IPTV",
                "anchor": nickname,
                "stream": url,
                "streamField": stream_key,
                "cover": room.get("cover") or room.get("customCoverUrl") or room_stub.get("cover") or "",
                "liveStatus": room.get("liveStatus", room_stub.get("liveStatus")),
            }
        except Exception as exc:
            print(f"[WARN] room {room_num}: {exc}", file=sys.stderr)
            return None

    results = []
    workers = max(1, min(args.workers, 32))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_detail, room) for room in rooms]
        for future in concurrent.futures.as_completed(futures):
            item = future.result()
            if item:
                results.append(item)

    results.sort(key=lambda item: (item["name"].casefold(), item["roomNum"]))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    (out / "channels.json").write_text(
        json.dumps({"channels": results, "count": len(results)}, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    with (out / "playlist.m3u").open("w", encoding="utf-8", newline="\n") as f:
        f.write("#EXTM3U\n")
        for item in results:
            name = item["name"].replace("\r", " ").replace("\n", " ").replace('"', "'")
            group = item["group"].replace('"', "'")
            logo = item["cover"].replace('"', "'")
            f.write(f'#EXTINF:-1 tvg-id="{item["roomNum"]}" tvg-name="{name}" tvg-logo="{logo}" group-title="{group}",{name}\n')
            f.write(item["stream"] + "\n")

    print(f"Rooms discovered: {len(rooms)}")
    if proxy_pool:
        print(f"Proxy candidates loaded: {proxy_pool.count()} (only API requests use them; stream URLs are direct)")
    print(f"Streams extracted: {len(results)}")
    print(f"Playlist: {out / 'playlist.m3u'}")
    print(f"JSON:      {out / 'channels.json'}")
    return 0 if results else 3


if __name__ == "__main__":
    raise SystemExit(main())
