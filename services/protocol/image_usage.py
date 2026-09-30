"""Usage accounting is optional metadata and must preserve generated images."""
from __future__ import annotations

import logging
from typing import Any

from services.protocol.conversation import count_text_tokens
from utils.image_tokens import count_image_inputs_tokens, count_image_output_items_tokens, image_usage
from utils.tokenizer import encoding_for_model, estimate_text_tokens

log = logging.getLogger(__name__)


def safe_image_usage(prompt: str, model: str, items: list, size: Any, quality: str, images=None) -> dict:
    estimated_fields = []
    try:
        text_tokens = count_text_tokens(prompt, model)
        if getattr(encoding_for_model(model), "estimated", False):
            estimated_fields.append("input_text_tokens")
    except Exception as exc:
        log.warning("Image text accounting failed (%s); preserving image response", type(exc).__name__)
        text_tokens = estimate_text_tokens(prompt)
        estimated_fields.append("input_text_tokens")
    try:
        input_tokens = count_image_inputs_tokens(images, model)
    except Exception as exc:
        log.warning("Image input accounting failed (%s); preserving image response", type(exc).__name__)
        input_tokens = 0
        estimated_fields.append("input_image_tokens")
    try:
        output_tokens = count_image_output_items_tokens(items, size, quality)
    except Exception as exc:
        log.warning("Image output accounting failed (%s); preserving image response", type(exc).__name__)
        output_tokens = 0
        estimated_fields.append("output_image_tokens")
    usage = image_usage(text_tokens, input_tokens, output_tokens)
    if estimated_fields:
        usage["estimated"] = True
        usage["estimated_fields"] = estimated_fields
        unavailable = [field for field in estimated_fields if field != "input_text_tokens"]
        if unavailable:
            usage["unavailable_fields"] = unavailable
    return usage
