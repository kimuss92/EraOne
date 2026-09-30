"""actions/design_studio.py — natural language -> professional prompt -> image / video.

The user describes a design in their own words (voice or typed): the item (kitchen,
wardrobe, living room, product...), the layout shape (L-shape, I-shape, U-shape,
island...), materials, textures, colours, decoration, mood. Era:

  1. PROMPT-ENGINEERS: a fast Gemini call turns that loose description into a
     professional, render-ready generation prompt (layout, materials, lighting,
     camera, style). Kitchen/wardrobe descriptions automatically obey the
     Delta Cuisine material-harmony laws from brands.yaml (one material family,
     1-2 cabinet textures, tall glass cabinetry, harmonious combination) unless
     the caller passes apply_brand_rules=False.
  2. GENERATES: an image via the Gemini image model, saved under runtime/designs/
     and opened on screen; or a video via Veo (image-first: the approved still is
     used as the video's first frame when available).

Every step degrades gracefully: if the prompt-engineering call fails we generate
from the raw description; if video generation is unavailable (quota/entitlement)
the user still gets the image and the pro prompt to reuse elsewhere.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from core.utils import get_api_key, log

_REPO_ROOT = Path(__file__).resolve().parent.parent
DESIGNS_DIR = _REPO_ROOT / "runtime" / "designs"

DEFAULT_IMAGE_MODEL = "gemini-2.5-flash-image"
DEFAULT_VIDEO_MODEL = "veo-3.0-fast-generate-001"
FALLBACK_VIDEO_MODELS = ("veo-2.0-generate-001",)
VIDEO_POLL_S = 10
VIDEO_TIMEOUT_S = 360

_KITCHEN_WORDS = ("kitchen", "cuisine", "مطبخ")
_WARDROBE_WORDS = ("wardrobe", "dressing", "placard", "armoire", "closet")

_PROMPT_ENGINEER_SYSTEM = (
    "You are a senior interior-design and product-visualization prompt engineer. "
    "You turn a client's loose, spoken description of a design into ONE professional "
    "text-to-image prompt for a photorealistic render.\n\n"
    "Extract and make explicit when present (never invent contradictions):\n"
    "- item type (kitchen, wardrobe, living room, bathroom, product, ...)\n"
    "- layout shape EXACTLY as stated: L-shape, I-shape (single line), U-shape, "
    "G-shape, parallel/galley, island, corner...\n"
    "- materials and textures (quartz, ceramic, laminated wood, lacquer, marble, ...)\n"
    "- colours and finishes (matte/gloss), hardware\n"
    "- decoration, accessories, plants, styling details the client mentioned\n"
    "- room context, windows/natural light\n"
    "Then ADD professional craft: photorealistic architectural render quality, "
    "camera angle (eye-level three-quarter view unless told otherwise), realistic "
    "global illumination, 8k detail, physically-plausible proportions.\n\n"
    "If DESIGN LAWS are provided, the prompt MUST obey them — they override any "
    "conflicting detail in the description.\n\n"
    "Respond with STRICT JSON only:\n"
    '{"title": <short filename-friendly title>, "prompt": <the final prompt, one '
    'paragraph, max 180 words>, "video_motion": <one short sentence describing a '
    "slow cinematic camera move suited to this scene, e.g. 'slow dolly-in across "
    "the island'>}"
)


# ── brand design laws ────────────────────────────────────────────────────────


def _load_design_rules() -> dict:
    """Delta Cuisine's material-harmony laws from brands.yaml (never raises)."""
    try:
        import yaml

        cfg = yaml.safe_load((_REPO_ROOT / "brands.yaml").read_text(encoding="utf-8"))
        return (cfg.get("brands", {}).get("delta_cuisine", {}) or {}).get("design_rules", {}) or {}
    except Exception:
        return {}


def _design_laws_block(description: str, apply_brand_rules: bool) -> str:
    """Return the DESIGN LAWS text to inject for kitchens/wardrobes, else ''."""
    if not apply_brand_rules:
        return ""
    d = description.lower()
    rules = _load_design_rules()
    if not rules:
        return ""
    if any(w in d for w in _KITCHEN_WORDS) and rules.get("kitchen"):
        return "DESIGN LAWS (kitchen — MUST obey):\n" + json.dumps(
            rules["kitchen"], ensure_ascii=False, indent=1
        )
    if any(w in d for w in _WARDROBE_WORDS) and rules.get("wardrobe"):
        return "DESIGN LAWS (wardrobe — MUST obey):\n" + json.dumps(
            rules["wardrobe"], ensure_ascii=False, indent=1
        )
    return ""


# ── step 1: professional prompt ──────────────────────────────────────────────


def build_pro_prompt(description: str, *, apply_brand_rules: bool = True) -> dict:
    """description -> {"title", "prompt", "video_motion"}. Falls back to a static
    template around the raw description if the model call fails."""
    from core.ai_provider import generate_json

    laws = _design_laws_block(description, apply_brand_rules)
    user_prompt = (laws + "\n\n" if laws else "") + f"Client description:\n{description}"
    raw = generate_json(user_prompt, system_instruction=_PROMPT_ENGINEER_SYSTEM, fallback={})
    prompt = str(raw.get("prompt", "")).strip()
    if len(prompt) > 40:
        return {
            "title": _slug(str(raw.get("title", "")) or description),
            "prompt": prompt,
            "video_motion": str(raw.get("video_motion", "")).strip() or "slow cinematic dolly-in",
        }
    # Model path down (quota/offline) — still produce something render-worthy.
    return {
        "title": _slug(description),
        "prompt": (
            f"Photorealistic architectural interior render: {description}. "
            "Eye-level three-quarter view, realistic global illumination, natural "
            "window light, physically plausible proportions, harmonious materials, "
            "8k detail." + (f" Obey these design laws: {laws}" if laws else "")
        ),
        "video_motion": "slow cinematic dolly-in",
    }


def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", (text or "design")).strip("_").lower()
    return s[:40] or "design"


# ── step 2: generation ───────────────────────────────────────────────────────


def _client():
    from core.ai_provider import get_client

    return get_client(get_api_key())


def _save_bytes(data: bytes, title: str, ext: str) -> Path:
    DESIGNS_DIR.mkdir(parents=True, exist_ok=True)
    path = DESIGNS_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{title}.{ext}"
    path.write_bytes(data)
    return path


def _open_file(path: Path) -> None:
    try:
        os.startfile(str(path))  # Windows viewer
    except Exception:
        pass


def generate_image(
    prompt: str, title: str, *, model: str | None = None, player=None
) -> tuple[Path | None, str]:
    """Generate one image. Returns (saved_path_or_None, message)."""
    from google.genai import types as gtypes

    model = model or DEFAULT_IMAGE_MODEL
    try:
        resp = _client().models.generate_content(
            model=model,
            contents=prompt,
            config=gtypes.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]),
        )
        for cand in resp.candidates or []:
            for part in (cand.content.parts or []) if cand.content else []:
                inline = getattr(part, "inline_data", None)
                if inline and inline.data:
                    path = _save_bytes(inline.data, title, "png")
                    log("DESIGN", f"image saved: {path}", player)
                    return path, f"Image saved to {path}"
        return None, "The image model returned no image (it may have refused the prompt)."
    except Exception as e:
        msg = str(e)
        if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
            return None, "Image generation is out of quota right now — try again later."
        return None, f"Image generation failed: {msg[:180]}"


def generate_video(
    prompt: str,
    title: str,
    *,
    image_path: Path | None = None,
    model: str | None = None,
    player=None,
) -> tuple[Path | None, str]:
    """Generate a short video via Veo, optionally animating a still image.
    Bounded polling; returns (saved_path_or_None, message)."""
    from google.genai import types as gtypes

    client = _client()
    models = [model] if model else [DEFAULT_VIDEO_MODEL, *FALLBACK_VIDEO_MODELS]
    last_err = ""
    for m in models:
        try:
            kwargs: dict = {"model": m, "prompt": prompt}
            if image_path and image_path.exists():
                kwargs["image"] = gtypes.Image(
                    image_bytes=image_path.read_bytes(), mime_type="image/png"
                )
            op = client.models.generate_videos(**kwargs)
            deadline = time.time() + VIDEO_TIMEOUT_S
            while not op.done:
                if time.time() > deadline:
                    return (
                        None,
                        "Video generation timed out — it may still finish server-side; try again in a minute.",
                    )
                time.sleep(VIDEO_POLL_S)
                op = client.operations.get(op)
            vids = getattr(op.response, "generated_videos", None) or []
            if not vids:
                last_err = f"{m}: operation finished with no video"
                continue
            video = vids[0].video
            DESIGNS_DIR.mkdir(parents=True, exist_ok=True)
            path = DESIGNS_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{title}.mp4"
            client.files.download(file=video)
            video.save(str(path))
            log("DESIGN", f"video saved: {path}", player)
            return path, f"Video saved to {path}"
        except Exception as e:
            last_err = f"{m}: {str(e)[:160]}"
            log("DESIGN", f"video model {m} unavailable: {last_err}", player)
            continue
    return None, (
        "Video generation isn't available on this account right now "
        f"({last_err}). The image and the professional prompt are ready to reuse."
    )


# ── the action entry point (matches Era's actions/ calling convention) ───────


def design_studio(parameters: dict | None = None, player=None, speak=None) -> str:
    """Synchronous worker — main.py runs it in a background thread and Era speaks
    the returned summary when it finishes."""
    params = parameters or {}
    description = (params.get("description") or "").strip()
    if not description:
        return "I need a description of the design first."
    output = (params.get("output") or "image").strip().lower()
    apply_rules = params.get("apply_brand_rules")
    apply_rules = True if apply_rules is None else bool(apply_rules)

    log("DESIGN", f"request: output={output} desc={description[:80]!r}", player)
    spec = build_pro_prompt(description, apply_brand_rules=apply_rules)
    log("DESIGN", f"pro prompt ready: {spec['prompt'][:120]}...", player)

    # Image first, always: it's fast, and Veo animates better from an approved frame.
    img_path, img_msg = generate_image(spec["prompt"], spec["title"], player=player)
    if img_path:
        _open_file(img_path)

    if output != "video":
        if img_path:
            return f"Design ready — {img_msg}. I opened it on screen."
        return img_msg

    if img_path and speak:
        try:
            speak(
                "The still frame is ready and on your screen. Now generating the video — this can take a few minutes."
            )
        except Exception:
            pass
    video_prompt = f"{spec['prompt']} Camera: {spec['video_motion']}."
    vid_path, vid_msg = generate_video(
        video_prompt, spec["title"], image_path=img_path, player=player
    )
    if vid_path:
        _open_file(vid_path)
        return f"Design video ready — {vid_msg}. I opened it on screen."
    if img_path:
        return f"{vid_msg} The image is at {img_path}."
    return vid_msg
