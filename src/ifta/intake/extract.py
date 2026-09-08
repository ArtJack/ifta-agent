"""Vision extraction: a dirty fuel-receipt photo -> a ReceiptCandidate.

This is the missing front half of the receipt pipeline. `receipts.py` already
turns a ReceiptCandidate into a reviewed, reconciled, human-gated proposal;
this module *produces* those candidates from real-world photos using Claude
vision. Output is written as the same ``{"receipts": [...]}`` JSON that
``ifta intake`` already consumes, so nothing downstream changes.

Safety: receipts are evidence, not filing truth. The prompt forbids guessing —
unreadable fields come back ``null`` with low confidence — and every candidate
then flows through ``review_receipt()``, which gates low-confidence or
unverified data behind human approval before it can touch the filing math.
"""

from __future__ import annotations

import base64
import io
import re
from collections.abc import Callable
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from ifta.intake.receipts import ReceiptCandidate

# HEIC/HEIF (the iPhone default) needs this plugin; importing it registers the
# decoder with Pillow. It's a declared dependency — the guard is just defensive
# so non-HEIC formats keep working even if the wheel is somehow missing.
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ModuleNotFoundError:  # pragma: no cover - pillow-heif is a hard dependency
    pass

# Phone/scanner suffixes we treat as receipt photos.
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif", ".tif", ".tiff"}

# Formats the Anthropic vision API accepts directly (no conversion needed).
_NATIVE_MEDIA = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_MAX_DIRECT_BYTES = 3_500_000  # downscale anything larger before sending
_JPEG_LONG_EDGE = 1600  # Claude's vision sweet spot is ~1568px on the long edge
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_FLOAT_FIELDS = {"gallons", "amount"}
_PAYMENT_METHODS = {"fleet_card", "personal_card", "cash", "unknown"}

# One photo's worth of work: path in, model JSON payload out. Injectable for tests.
ExtractCall = Callable[[Path], dict[str, Any]]


EXTRACTION_PROMPT = """You read a photo of a US/Canada trucking fuel receipt and output structured data.

Return ONLY one JSON object. No prose, no markdown fences.

Fields (use null when a value is not clearly legible):
  date            ISO "YYYY-MM-DD"
  vendor          truck stop / brand, e.g. "Pilot", "Love's", "TA", "Petro"
  address         street address if printed
  city            city
  state           2-letter US state / Canadian province code of the station
  gallons         number — US gallons of DIESEL pumped (the pump quantity)
  amount          number — total USD charged for the fuel
  fuel_type       e.g. "diesel", "DEF", "reefer"  ("DSL" means diesel)
  truck_id        unit / truck (or vehicle) number if written on the receipt
  driver          driver name if present
  card_last4      last 4 digits of the card used
  invoice         the receipt reference number
  payment_method  one of: fleet_card, personal_card, cash, unknown
  confidence      object mapping each field you filled to a number 0.0-1.0

CRITICAL — this feeds a government tax filing. NEVER guess or invent a value: if a field
is not clearly legible, set it to null with confidence <= 0.3. Real receipts are messy
(faded thermal, crumpled, glare, blur, torn, handwriting). Prefer FEWER fields with honest
low confidence over plausible-looking data.

How a human reads these — apply the same rules:
- gallons = the DIESEL pump quantity, never DEF and never a dollar figure. A trailing "G"
  means gallons (e.g. "178.557G"). Report what you read — do not recompute it.
- VOID / returned lines: ignore the voided amount; use the amount actually charged.
- state = where the STATION is. If the 2-letter code is not printed, infer it from the
  city/address; if location is unreadable too, leave state null but fill city/address.
- date: accept MM/DD/YYYY, "04 2026", short "'26" (= 2026), "Apr 2026". The current year is
  2026 — a far-future or very old year is suspicious, so keep the most plausible reading at
  lower confidence rather than discarding it.
- invoice: use "Invoice #" if present, else "Receipt #", else "Transaction #".
- payment_method: a truck #/driver ID, or "TCH"/"WEX"/"Comdata"/"EFS"/"TCHPFJ" -> fleet_card.
  "Company: PERSONAL", "VISA CREDIT", or no truck #/driver ID -> personal_card. Else unknown.
- MULTIPLE RECEIPTS in one image: if you see more than one distinct receipt, extract the
  single most complete one and set EVERY confidence <= 0.4 so a human reviews it.

Return the JSON object now."""


def discover_images(folder: Path) -> list[Path]:
    """Receipt photos in a drop folder, sorted, skipping dotfiles and subdirs."""
    if not folder.exists():
        return []
    return sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in IMAGE_SUFFIXES
    )


def _convert_to_jpeg(path: Path) -> bytes:
    """Convert/downscale any image to a reasonable JPEG using Pillow.

    Used for HEIC/TIFF (which the vision API does not accept) and for oversized
    photos. Cross-platform — this replaces the old macOS-only ``sips`` call so
    the pipeline runs in a Linux container too.
    """
    try:
        with Image.open(path) as src:
            im = ImageOps.exif_transpose(src)  # honor camera rotation before EXIF is dropped
            im.thumbnail((_JPEG_LONG_EDGE, _JPEG_LONG_EDGE))  # downscale only, keep aspect
            if im.mode != "RGB":
                im = im.convert("RGB")  # JPEG has no alpha/palette channel
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85, optimize=True)
            return buf.getvalue()
    except UnidentifiedImageError as exc:  # e.g. HEIC when pillow-heif isn't installed
        raise RuntimeError(
            f"Cannot read {path.name}: unsupported or unreadable image format. "
            "HEIC/HEIF requires the `pillow-heif` package."
        ) from exc
    except OSError as exc:  # truncated/corrupt file or decode failure
        raise RuntimeError(f"Failed to convert {path.name}: {exc}") from exc


def image_block(path: Path) -> dict[str, Any]:
    """Build an Anthropic image content block, converting/downscaling as needed."""
    suffix = path.suffix.lower()
    media = _NATIVE_MEDIA.get(suffix)
    if media is not None and path.stat().st_size <= _MAX_DIRECT_BYTES:
        data = path.read_bytes()
    else:
        data = _convert_to_jpeg(path)
        media = "image/jpeg"
    b64 = base64.standard_b64encode(data).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}}


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = _NUM_RE.search(value.replace(",", ""))
        if match:
            return float(match.group())
    return None


def _clamp01(value: Any) -> float | None:
    f = _to_float(value)
    if f is None:
        return None
    return max(0.0, min(1.0, f))


def candidate_from_payload(payload: dict[str, Any], source_file: str) -> ReceiptCandidate:
    """Map a model JSON payload to a ReceiptCandidate, coercing/validating safely.

    Unknown keys are dropped, numbers are parsed out of stray text, confidences are
    clamped to 0..1, and an out-of-range payment method degrades to "unknown".
    """
    allowed = {f.name for f in fields(ReceiptCandidate)} - {"source_file", "confidence"}
    kwargs: dict[str, Any] = {"source_file": source_file}
    for key in allowed:
        if key not in payload or payload[key] is None:
            continue
        value = payload[key]
        if key in _FLOAT_FIELDS:
            coerced = _to_float(value)
            if coerced is not None:
                kwargs[key] = coerced
        else:
            kwargs[key] = value.strip() if isinstance(value, str) else str(value)

    if kwargs.get("payment_method") not in _PAYMENT_METHODS:
        kwargs.pop("payment_method", None)  # fall back to the dataclass default

    confidence = payload.get("confidence")
    if isinstance(confidence, dict):
        cleaned = {
            str(k): clamped
            for k, v in confidence.items()
            if (clamped := _clamp01(v)) is not None
        }
        if cleaned:
            kwargs["confidence"] = cleaned

    return ReceiptCandidate(**kwargs)


def _extract_one_live(path: Path, *, model: str, max_tokens: int) -> dict[str, Any]:
    """Send one image to Claude vision and parse the JSON it returns."""
    from ifta.agent.runner import _client, _extract_review_json

    client = _client()
    # SDK block-param typing is strict; mirror runner.py and hand it an Any payload.
    messages: Any = [
        {"role": "user", "content": [image_block(path), {"type": "text", "text": EXTRACTION_PROMPT}]}
    ]
    message = client.messages.create(model=model, max_tokens=max_tokens, messages=messages)
    text = "".join(block.text for block in message.content if block.type == "text")
    return _extract_review_json(text)


def extract_one(
    path: Path,
    *,
    model: str,
    max_tokens: int = 1024,
    call: ExtractCall | None = None,
) -> ReceiptCandidate:
    """Extract a single receipt photo into a ReceiptCandidate.

    `call` overrides the live model call (used in tests). The default sends the
    image to Claude vision.
    """
    runner = call or (lambda p: _extract_one_live(p, model=model, max_tokens=max_tokens))
    return candidate_from_payload(runner(path), source_file=path.name)


def write_candidates_json(candidates: list[ReceiptCandidate], out_path: Path) -> Path:
    """Write candidates in the ``{"receipts": [...]}`` shape ``ifta intake`` reads."""
    import json

    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [asdict(c) for c in candidates]
    out_path.write_text(
        json.dumps({"receipts": rows}, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return out_path
