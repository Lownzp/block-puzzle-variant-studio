from __future__ import annotations

import json
import math
from pathlib import Path

import cv2
import numpy as np


MODEL_PATH = Path(__file__).resolve().parent / "models" / "cell_occupancy_model.json"


def extract_cell_features(hsv_cell, gray_cell) -> list[float]:
    """Return compact visual features for one board cell crop."""
    if hsv_cell.size == 0 or gray_cell.size == 0:
        return [0.0] * 15
    hsv = hsv_cell.reshape(-1, 3).astype(np.float32)
    gray = gray_cell.astype(np.float32)
    sat = hsv[:, 1]
    val = hsv[:, 2]
    h, w = gray_cell.shape[:2]
    border = max(1, round(min(h, w) * 0.18))
    center = gray[border:max(border + 1, h - border), border:max(border + 1, w - border)]
    if center.size == 0:
        center = gray
    return [
        float(np.mean(sat)) / 255.0,
        float(np.std(sat)) / 128.0,
        float(np.mean(val)) / 255.0,
        float(np.std(val)) / 128.0,
        float(np.percentile(sat, 85)) / 255.0,
        float(np.percentile(val, 85)) / 255.0,
        float(np.mean(sat >= 65)),
        float(np.mean(val >= 125)),
        float(np.mean((sat >= 65) & (val >= 125))),
        float(np.std(gray)) / 128.0,
        float(np.mean(center)) / 255.0,
        float(np.std(center)) / 128.0,
        float(np.mean(center) - np.mean(gray)) / 255.0,
        float(np.percentile(val, 95) - np.percentile(val, 15)) / 255.0,
        float(np.percentile(sat, 95) - np.percentile(sat, 15)) / 255.0,
    ]


def train_gaussian_model(features: list[list[float]], labels: list[int]) -> dict:
    if len(features) != len(labels) or len(features) < 20:
        raise ValueError("not enough labeled cell samples")
    x = np.asarray(features, dtype=np.float32)
    y = np.asarray(labels, dtype=np.int32)
    if not np.any(y == 0) or not np.any(y == 1):
        raise ValueError("training samples must contain both empty and occupied cells")
    empty = x[y == 0]
    occupied = x[y == 1]
    mean_empty = np.mean(empty, axis=0)
    mean_occupied = np.mean(occupied, axis=0)
    var_empty = np.var(empty, axis=0) + 1e-4
    var_occupied = np.var(occupied, axis=0) + 1e-4
    return {
        "schemaVersion": 1,
        "classifier": "diagonal_gaussian_occupancy",
        "featureCount": int(x.shape[1]),
        "classStats": {
            "empty": {
                "count": int(empty.shape[0]),
                "mean": mean_empty.tolist(),
                "var": var_empty.tolist(),
            },
            "occupied": {
                "count": int(occupied.shape[0]),
                "mean": mean_occupied.tolist(),
                "var": var_occupied.tolist(),
            },
        },
    }


def save_model(model: dict, path: Path = MODEL_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")


def load_model(path: Path = MODEL_PATH) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def predict_occupied(model: dict, features: list[float]) -> tuple[bool, float]:
    stats = model.get("classStats") or {}
    empty = stats.get("empty") or {}
    occupied = stats.get("occupied") or {}
    x = np.asarray(features, dtype=np.float32)
    mean_empty = np.asarray(empty.get("mean"), dtype=np.float32)
    mean_occupied = np.asarray(occupied.get("mean"), dtype=np.float32)
    var_empty = np.asarray(empty.get("var"), dtype=np.float32)
    var_occupied = np.asarray(occupied.get("var"), dtype=np.float32)
    if x.shape != mean_empty.shape or x.shape != mean_occupied.shape:
        return False, 0.0

    def log_prob(mean, var, count):
        prior = math.log(max(1, int(count)))
        return float(prior - 0.5 * np.sum(np.log(var) + ((x - mean) ** 2) / var))

    empty_log = log_prob(mean_empty, var_empty, empty.get("count", 1))
    occupied_log = log_prob(mean_occupied, var_occupied, occupied.get("count", 1))
    margin = occupied_log - empty_log
    return margin > 0.0, margin
