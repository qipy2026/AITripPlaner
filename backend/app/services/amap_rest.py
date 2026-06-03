"""高德地图 REST API（替代 MCPTool / amap-mcp-server）。"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx

from ..config import get_settings

_BASE = "https://restapi.amap.com/v3"


class AmapRestClient:
    """封装行程规划与地图 API 所需的高德接口。"""

    def __init__(self, api_key: Optional[str] = None) -> None:
        settings = get_settings()
        self.api_key = api_key or settings.amap_api_key
        if not self.api_key:
            raise ValueError("高德地图 API Key 未配置，请在 .env 中设置 AMAP_API_KEY")

    def _get(self, path: str, params: Dict[str, Any]) -> dict:
        params = {**params, "key": self.api_key, "output": "json"}
        with httpx.Client(timeout=30.0) as client:
            r = client.get(f"{_BASE}{path}", params=params)
            r.raise_for_status()
            data = r.json()
        if str(data.get("status")) != "1":
            raise RuntimeError(data.get("info") or "高德 API 错误")
        return data

    def text_search(self, keywords: str, city: str, citylimit: bool = True) -> str:
        """返回含 pois[] 的 JSON 字符串（与行程规划压缩逻辑兼容）。"""
        data = self._get(
            "/place/text",
            {
                "keywords": keywords,
                "city": city,
                "citylimit": "true" if citylimit else "false",
                "offset": 20,
                "page": 1,
            },
        )
        pois: List[dict] = []
        for p in data.get("pois") or []:
            if not isinstance(p, dict):
                continue
            pois.append(
                {
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "address": p.get("address"),
                    "location": p.get("location"),
                    "typecode": p.get("typecode"),
                }
            )
        return json.dumps({"pois": pois}, ensure_ascii=False)

    def weather(self, city: str) -> str:
        """返回含 forecasts[] 的 JSON 字符串。"""
        # 先查城市 adcode
        geo = self._get("/config/district", {"keywords": city, "subdistrict": 0})
        districts = geo.get("districts") or []
        adcode = districts[0].get("adcode") if districts else city

        data = self._get(
            "/weather/weatherInfo",
            {"city": adcode, "extensions": "all"},
        )
        forecasts: List[dict] = []
        for cast in data.get("forecasts") or []:
            for c in cast.get("casts") or []:
                forecasts.append(
                    {
                        "date": c.get("date"),
                        "day_weather": c.get("dayweather"),
                        "night_weather": c.get("nightweather"),
                        "day_temp": c.get("daytemp"),
                        "night_temp": c.get("nighttemp"),
                        "wind_direction": c.get("daywind"),
                        "wind_power": c.get("daypower"),
                    }
                )
        return json.dumps({"forecasts": forecasts}, ensure_ascii=False)

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """兼容原 MCP 工具名。"""
        if tool_name == "maps_text_search":
            return self.text_search(
                keywords=str(arguments.get("keywords", "")),
                city=str(arguments.get("city", "")),
                citylimit=str(arguments.get("citylimit", "true")).lower() == "true",
            )
        if tool_name == "maps_weather":
            return self.weather(city=str(arguments.get("city", "")))
        raise ValueError(f"不支持的高德工具: {tool_name}")


_client: Optional[AmapRestClient] = None


def get_amap_client() -> AmapRestClient:
    global _client
    if _client is None:
        _client = AmapRestClient()
    return _client
