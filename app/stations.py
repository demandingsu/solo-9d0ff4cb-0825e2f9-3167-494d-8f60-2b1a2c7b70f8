"""桩号换算工具。

约定桩号形如 ``K12+345``：K 后为公里数，加号后为米数（0 <= 米数 < 1000）。
``+`` 后的米数允许小数（如 ``K12+345.5``）；K 前缀可省略（``12+345``）。
换算结果统一为自起点起算的米数，便于区间比较与求交。
"""
from __future__ import annotations

import math
import re

_STATION_RE = re.compile(
    r"^\s*(?:[Kk]\s*)?(\d+)\s*\+\s*(\d+(?:\.\d+)?)\s*$"
)


class StationError(ValueError):
    """桩号格式不合法。"""


def parse_station(value: str) -> float:
    """把 ``K12+345`` 换算为 ``12345.0`` 米。"""
    if not isinstance(value, str):
        raise StationError(f"桩号必须是字符串: {value!r}")
    match = _STATION_RE.match(value)
    if match is None:
        raise StationError(f"无法识别的桩号格式: {value!r}（应形如 K12+345）")
    km = int(match.group(1))
    meters = float(match.group(2))
    if not math.isfinite(meters) or meters >= 1000:
        raise StationError(f"桩号 {value!r} 加号后的米数应在 0 到 1000 之间")
    return km * 1000.0 + meters


def format_station(meters: float) -> str:
    """把米数格式化回桩号（用于缺段/重叠区间展示）。"""
    if not math.isfinite(meters):
        raise StationError(f"无法格式化的米数: {meters!r}")
    negative = meters < 0
    value = abs(meters)
    km = int(value // 1000)
    rest = value - km * 1000.0
    if abs(rest - round(rest)) < 1e-9:
        rest_text = f"{int(round(rest)):03d}"
    else:
        rest_text = f"{rest:.3f}".rstrip("0").rstrip(".")
        if "." in rest_text:
            whole, frac = rest_text.split(".")
            rest_text = f"{whole.zfill(3)}.{frac}"
        else:
            rest_text = rest_text.zfill(3)
    sign = "-" if negative else ""
    return f"{sign}K{km}+{rest_text}"
