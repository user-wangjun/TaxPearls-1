"""图片人机验证码（内存态，单进程）。

MVP 自建方案：后端签发 4 位扭曲字符图（Pillow 渲染），答案只存 SHA-256
摘要，10 分钟有效、一次性（无论答对答错，校验后即销毁，不给爆破机会）。
字符集剔除 0/O/1/I/L 等易混字形，校验不区分大小写。商用阶段建议升级为
Cloudflare Turnstile（免费、用户无感），接口形态不变。
"""
from __future__ import annotations

from hashlib import sha256
from hmac import compare_digest
from secrets import choice, randbelow, token_hex
from threading import RLock
import base64
import io
import time

from PIL import Image, ImageDraw, ImageFilter, ImageFont

MAX_KEYS = 8192
LIFETIME_SECONDS = 10 * 60
CODE_LENGTH = 4
# 剔除易混字形：0/O、1/I/L
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

_WIDTH, _HEIGHT = 200, 60
_INK = ("#1f3a5f", "#c3942d", "#34557f", "#7a5c1e", "#2f6f8f")
_NOISE = ("#dbe4ee", "#c9d4e2", "#b7c6d9", "#e3d9bf")
_NOISE_STRONG = ("#aebfd4", "#d3c08c", "#9fb2c9")


class _Puzzle:
    __slots__ = ("answer_hash", "expires_at")

    def __init__(self, answer_hash: str, expires_at: float) -> None:
        self.answer_hash = answer_hash
        self.expires_at = expires_at


_store: dict[str, _Puzzle] = {}
_lock = RLock()


class CaptchaCapacityError(RuntimeError):
    """Fail closed without evicting another user's unexpired challenge."""


def _digest(answer: str) -> str:
    return sha256(answer.strip().upper().encode("utf-8")).hexdigest()


def _load_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """优先取系统无衬线粗体，兜底 Pillow 内置可缩放字体（>=10.1）。"""
    for path in (
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ):
        try:
            return ImageFont.truetype(path, 34)
        except OSError:
            continue
    return ImageFont.load_default(34)


def _render(code: str) -> str:
    """把字符画成带噪点、弧线、旋转扰动的 PNG，返回 data URL。"""
    canvas = Image.new("RGB", (_WIDTH, _HEIGHT), "#f6f9fc")
    draw = ImageDraw.Draw(canvas)
    for _ in range(_WIDTH * _HEIGHT // 55):  # 底噪
        draw.point((randbelow(_WIDTH), randbelow(_HEIGHT)), fill=choice(_NOISE))
    for _ in range(5):  # 背景干扰弧
        x0, y0 = randbelow(_WIDTH - 60), randbelow(_HEIGHT)
        box = (x0, y0 - 28, x0 + 70 + randbelow(80), y0 + 28)
        draw.arc(box, randbelow(360), randbelow(360) + 130, fill=choice(_NOISE_STRONG), width=2)
    canvas = canvas.filter(ImageFilter.GaussianBlur(0.6))

    font = _load_font()
    step = (_WIDTH - 24) // CODE_LENGTH
    for i, ch in enumerate(code):
        tile = Image.new("RGBA", (46, 50), (0, 0, 0, 0))
        ImageDraw.Draw(tile).text((4, 4), ch, font=font, fill=choice(_INK))
        rotated = tile.rotate(randbelow(37) - 18, resample=Image.BICUBIC, expand=True)
        canvas.paste(rotated, (12 + i * step + randbelow(9) - 4, randbelow(6) + 2), rotated)

    draw = ImageDraw.Draw(canvas)
    for _ in range(2):  # 覆盖字符的细干扰线
        draw.line((0, randbelow(_HEIGHT), _WIDTH, randbelow(_HEIGHT)),
                  fill=choice(_NOISE_STRONG), width=1)

    buffer = io.BytesIO()
    canvas.save(buffer, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def issue(text: str | None = None) -> dict[str, str]:
    """签发一张图片验证码，返回 ``{"captcha_id": ..., "image": <data URL>}``。

    ``text`` 仅供测试注入固定明文；生产路径不传，随机生成。
    """
    code = (text or "".join(choice(ALPHABET) for _ in range(CODE_LENGTH))).upper()
    captcha_id = token_hex(8)
    now = time.monotonic()
    with _lock:
        for stale in [k for k, v in _store.items() if now >= v.expires_at]:
            del _store[stale]
        if len(_store) >= MAX_KEYS:
            raise CaptchaCapacityError("验证码容量暂满，请稍后重试。")
        _store[captcha_id] = _Puzzle(_digest(code), now + LIFETIME_SECONDS)
    try:
        return {"captcha_id": captcha_id, "image": _render(code)}
    except Exception:
        with _lock:
            _store.pop(captcha_id, None)
        raise


def verify(captcha_id: str, answer: str) -> bool:
    """校验并销毁。id 未知 / 已过期 / 答案错误均返回 False（不区分大小写）。"""
    if not captcha_id:
        return False
    with _lock:
        puzzle = _store.pop(captcha_id, None)  # 一次性：无论对错都销毁
    if not puzzle or time.monotonic() >= puzzle.expires_at or not (answer or "").strip():
        return False
    return compare_digest(puzzle.answer_hash, _digest(answer))
