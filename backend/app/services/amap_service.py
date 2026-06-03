"""高德地图服务（REST API，已脱离 HelloAgents MCPTool）。"""

from typing import List, Dict, Any, Optional
import json

from .amap_rest import get_amap_client
from ..models.schemas import Location, POIInfo, WeatherInfo


class AmapService:
    """高德地图服务封装类"""

    def __init__(self):
        self.client = get_amap_client()

    def search_poi(self, keywords: str, city: str, citylimit: bool = True) -> List[POIInfo]:
        try:
            raw = self.client.text_search(keywords, city, citylimit)
            data = json.loads(raw)
            pois = []
            for p in data.get("pois") or []:
                loc = p.get("location") or "0,0"
                lng, lat = 0.0, 0.0
                if isinstance(loc, str) and "," in loc:
                    a, b = loc.split(",", 1)
                    lng, lat = float(a), float(b)
                pois.append(
                    POIInfo(
                        id=str(p.get("id") or ""),
                        name=str(p.get("name") or ""),
                        type=str(p.get("typecode") or ""),
                        address=str(p.get("address") or ""),
                        location=Location(longitude=lng, latitude=lat),
                        tel="",
                    )
                )
            return pois
        except Exception as e:
            print(f"❌ POI搜索失败: {str(e)}")
            return []

    def get_weather(self, city: str) -> List[WeatherInfo]:
        try:
            raw = self.client.weather(city)
            data = json.loads(raw)
            out = []
            for f in data.get("forecasts") or []:
                out.append(
                    WeatherInfo(
                        date=str(f.get("date") or ""),
                        day_weather=str(f.get("day_weather") or ""),
                        night_weather=str(f.get("night_weather") or ""),
                        day_temp=int(f.get("day_temp") or 0) if f.get("day_temp") else 0,
                        night_temp=int(f.get("night_temp") or 0) if f.get("night_temp") else 0,
                        wind_direction=str(f.get("wind_direction") or ""),
                        wind_power=str(f.get("wind_power") or ""),
                    )
                )
            return out
        except Exception as e:
            print(f"❌ 天气查询失败: {str(e)}")
            return []

    def plan_route(
        self,
        origin_address: str,
        destination_address: str,
        origin_city: Optional[str] = None,
        destination_city: Optional[str] = None,
        route_type: str = "walking",
    ) -> Dict[str, Any]:
        # 路线规划可后续接 direction API；暂返回空结构
        return {}

    def geocode(self, address: str, city: Optional[str] = None) -> Optional[Location]:
        return None

    def get_poi_detail(self, poi_id: str) -> Dict[str, Any]:
        return {}


_amap_service: Optional[AmapService] = None


def get_amap_service() -> AmapService:
    global _amap_service
    if _amap_service is None:
        _amap_service = AmapService()
    return _amap_service


def get_amap_mcp_tool():
    """兼容旧调用方：返回 REST 客户端。"""
    return get_amap_client()
