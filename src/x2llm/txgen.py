"""Faithful Python port of x.com's client-side x-client-transaction-id
generator (algorithm as reverse-engineered by the community; JS float
semantics preserved deliberately - do not "clean up" the rounding).

Inputs come from an anonymous fetch of x.com: the twitter-site-verification
meta key, the loading-x-anim SVG frames and byte indices parsed out of the
ondemand.s webpack chunk. Output validates byte-for-byte against captured
browser txids (scripts/validate_txgen.py).
"""
from __future__ import annotations

import base64
import hashlib
import math
import os
import re
import time

import httpx

EPOCH_MS = 1_682_924_400_000  # 2023-05-01, per the web client
DEFAULT_KEYWORD = "obfiowerehiring"
ADDITIONAL_RANDOM_NUMBER = 3
ON_DEMAND_CHUNK_NAME = "ondemand.s"
INDICES_REGEX = re.compile(r"\(\w\[(\d{1,2})\],\s*16\)")
ON_DEMAND_FILE_HASH_REGEX = re.compile(
    r"(\d+):\s*[\"']ondemand\.s[\"'][\s\S]*?\}\)\[e\]\s*\|\|\s*e\)\s*\+\s*[\"']\.[\"']"
    r"\s*\+\s*\(\{[\s\S]*?\b\1:\s*[\"']([a-zA-Z0-9_-]+)[\"']",
    re.DOTALL,
)
HOME_URL = "https://x.com/i/jf/"
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)


def _js_round(x: float) -> int:  # JS Math.round: half away from zero
    return math.floor(x + 0.5)


def _float_to_hex(x: float) -> str:  # port of utils.js floatToHex, bug-for-bug
    result: list[str] = []
    quotient = math.floor(x)
    fraction = x - quotient
    while quotient > 0:
        quotient = math.floor(x / 16)
        remainder = math.floor(x - quotient * 16)
        result.insert(0, chr(remainder + 55) if remainder > 9 else str(remainder))
        x = quotient
    if fraction == 0:
        return "".join(result)
    result.append(".")
    guard = 0
    while fraction > 0 and guard < 64:
        fraction *= 16
        integer = math.floor(fraction)
        fraction -= integer
        result.append(chr(integer + 55) if integer > 9 else str(integer))
        guard += 1
    return "".join(result)


class _Cubic:
    def __init__(self, curves: list[float]):
        self.curves = curves

    def get_value(self, t: float) -> float:
        c = self.curves
        start = end_gradient = 0.0
        if t <= 0.0:
            if c[0] > 0.0:
                start = c[1] / c[0]
            elif c[1] == 0.0 and c[2] > 0.0:
                start = c[3] / c[2]
            return start * t
        if t >= 1.0:
            if c[2] < 1.0:
                end_gradient = (c[3] - 1.0) / (c[2] - 1.0)
            elif c[2] == 1.0 and c[0] < 1.0:
                end_gradient = (c[1] - 1.0) / (c[0] - 1.0)
            return 1.0 + end_gradient * (t - 1.0)
        lo, hi = 0.0, 1.0
        mid = 0.0
        while lo < hi:
            mid = (lo + hi) / 2
            x_est = self._calc(c[0], c[2], mid)
            if abs(t - x_est) < 0.00001:
                return self._calc(c[1], c[3], mid)
            if x_est < t:
                lo = mid
            else:
                hi = mid
        return self._calc(c[1], c[3], mid)

    @staticmethod
    def _calc(a: float, b: float, m: float) -> float:
        return 3.0 * a * (1 - m) * (1 - m) * m + 3.0 * b * (1 - m) * m * m + m * m * m


def _solve(value: int, min_val: float, max_val: float, floor: bool) -> float:
    result = value * (max_val - min_val) / 255 + min_val
    return math.floor(result) if floor else _js_round(result * 100) / 100


def _is_odd(num):
    return -1.0 if num % 2 else 0.0


def _animate(frames: list[int], target_time: float) -> str:
    from_color = list(frames[0:3]) + [1]
    to_color = list(frames[3:6]) + [1]
    to_rotation = [_solve(frames[6], 60.0, 360.0, True)]
    curves = [_solve(v, _is_odd(i), 1.0, False) for i, v in enumerate(frames[7:])]
    val = _Cubic(curves).get_value(target_time)
    color = [max(f + (t - f) * val, 0) for f, t in zip(from_color, to_color)]
    rotation = 0.0 + (to_rotation[0] - 0.0) * val
    rad = rotation * math.pi / 180
    matrix = [math.cos(rad), -math.sin(rad), math.sin(rad), math.cos(rad)]
    str_arr = [format(_js_round(v), "x") for v in color[:3]]
    for v in matrix:
        rounded = _js_round(v * 100) / 100
        if rounded < 0:
            rounded = -rounded
        h = _float_to_hex(rounded)
        str_arr.append(("0" + h).lower() if h.startswith(".") else (h or "0"))
    str_arr.extend(["0", "0"])
    return re.sub(r"[.-]", "", "".join(str_arr))


class TxidFactory:
    """Mints x-client-transaction-id values. init() = two anonymous fetches;
    generate() is pure CPU (sha256 + base64)."""

    def __init__(self, http: httpx.AsyncClient | None = None):
        self._http = http
        self._own_http = http is None
        self.key_bytes: list[int] | None = None
        self.animation_key: str | None = None

    async def init(self) -> None:
        if self.key_bytes:
            return
        if self._own_http:
            self._http = httpx.AsyncClient(
                headers={
                    "user-agent": CHROME_UA,
                    "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "accept-language": "en",
                },
                timeout=20,
                follow_redirects=True,
            )
        html = (await self._http.get(HOME_URL)).text
        key = re.search(
            r'name="twitter-site-verification"\s+content="([^"]+)"', html
        ) or re.search(r'content="([^"]+)"\s+name="twitter-site-verification"', html)
        if not key:
            raise RuntimeError("twitter-site-verification not found in home page")
        self.key_bytes = list(base64.b64decode(key.group(1)))
        ondemand = ON_DEMAND_FILE_HASH_REGEX.search(html)
        if not ondemand:
            raise RuntimeError("ondemand.s chunk URL not found in home page")
        url = f"https://abs.twimg.com/responsive-web/client-web/{ON_DEMAND_CHUNK_NAME}.{ondemand.group(2)}a.js"
        chunk = (await self._http.get(url)).text
        indices = [int(m) for m in INDICES_REGEX.findall(chunk)]
        if not indices:
            raise RuntimeError("key byte indices not found in ondemand chunk")
        row_index, key_indices = indices[0], indices[1:]

        def parse_frames() -> list[list[list[int]]]:
            # one entry per loading-x-anim svg; each is the <g>'s SECOND path
            per_svg = []
            for m in re.finditer(
                r'<svg[^>]*id="loading-x-anim[^"]*"[^>]*>.*?</svg>', html, re.DOTALL
            ):
                g = re.search(r"<g>(.*?)</g>", m.group(0), re.DOTALL)
                if not g:
                    continue
                ds = re.findall(r'd="([^"]+)"', g.group(1))
                if len(ds) < 2:
                    continue
                # browser: firstChild.children[1] -> second path inside <g>
                items = ds[1][9:].split("C")
                per_svg.append(
                    [[int(n) for n in re.sub(r"\D+", " ", it).split()] for it in items]
                )
            return per_svg

        frames = parse_frames()
        if len(frames) < 4:
            raise RuntimeError("loading-x-anim frames not found in home page")
        rows = frames[self.key_bytes[5] % 4]  # browser: frames[keyBytes[5] % 4]
        row = rows[self.key_bytes[row_index] % 16]
        frame_time = 1
        for idx in key_indices:
            frame_time = frame_time * (self.key_bytes[idx] % 16)
        frame_time = _js_round(frame_time / 10) * 10
        self.animation_key = _animate(row, frame_time / 4096)

    async def aclose(self) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()

    def generate(self, method: str, path: str, time_now_s: int | None = None) -> str:
        assert self.key_bytes and self.animation_key, "init() first"
        if time_now_s is None:
            time_now_s = int((time.time() * 1000 - EPOCH_MS) // 1000)
        t = time_now_s
        time_bytes = [t & 0xFF, (t >> 8) & 0xFF, (t >> 16) & 0xFF, (t >> 24) & 0xFF]
        data = f"{method}!{path}!{t}{DEFAULT_KEYWORD}{self.animation_key}"
        digest = hashlib.sha256(data.encode()).digest()[:16]
        rand = int.from_bytes(os.urandom(1), "big")
        body = self.key_bytes + time_bytes + list(digest) + [ADDITIONAL_RANDOM_NUMBER]
        return base64.b64encode(bytes([rand] + [b ^ rand for b in body])).decode().rstrip("=")

    def brute(self, method: str, path: str, time_now_s: int, expected: str) -> int | None:
        """Validation helper: find the random byte that reproduces `expected`."""
        t = time_now_s
        time_bytes = [t & 0xFF, (t >> 8) & 0xFF, (t >> 16) & 0xFF, (t >> 24) & 0xFF]
        data = f"{method}!{path}!{t}{DEFAULT_KEYWORD}{self.animation_key}"
        digest = hashlib.sha256(data.encode()).digest()[:16]
        body = self.key_bytes + time_bytes + list(digest) + [ADDITIONAL_RANDOM_NUMBER]
        for rand in range(256):
            candidate = base64.b64encode(bytes([rand] + [b ^ rand for b in body])).decode().rstrip("=")
            if candidate == expected:
                return rand
        return None
