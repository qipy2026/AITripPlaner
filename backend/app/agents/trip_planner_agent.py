"""多智能体旅行规划系统"""

import json
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..config import get_settings
from ..core.llm import invoke_chat
from ..graph.trip_graph import run_trip_planning_graph
from ..services.amap_rest import get_amap_client
from ..models.schemas import (
    Attraction,
    Budget,
    DayPlan,
    Hotel,
    Location,
    Meal,
    TripPlan,
    TripRequest,
    WeatherInfo,
)

# ============ 行程规划 LLM ============
# 说明：MCPTool 单一入口「amap」直接 call_tool；行程由 LLM 或 MCP 兜底拼装。
# 长 system + 巨型 JSON 样例易触发部分兼容接口 500，故用短 system。

PLANNER_SYSTEM_COMPACT = """你是旅行行程规划助手。根据用户给出的城市、日期、交通/住宿偏好，以及「景点POI / 天气 / 酒店POI」的 JSON 摘要，输出**仅一段**合法 JSON（可用 ```json 包裹），不要有其它说明文字。

顶层：city, start_date, end_date, days[], weather_info[], overall_suggestions, budget。
days[] 每天须含：date(YYYY-MM-DD)、day_index(从0递增)、description、transportation、accommodation、hotel、attractions[]、meals[]。
attractions：visit_duration 为整数分钟（禁止写「2小时」）；location 须为 {longitude,latitude}。
meals 须为数组：[{type,name,description,estimated_cost},…]，type 为 breakfast/lunch/dinner；禁止把三餐写成一个对象。
weather_info：覆盖行程每一天；day_temp/night_temp 为纯数字。
budget：各项为数字。坐标与 POI 摘要一致。"""


class MultiAgentTripPlanner:
    """多智能体旅行规划系统"""

    def __init__(self):
        """初始化多智能体系统"""
        print("🔄 开始初始化多智能体旅行规划系统...")

        try:
            get_settings()
            self.amap = get_amap_client()
            print("  - 高德 REST API + LangGraph 行程工作流（LangChain ChatOpenAI）")

            print("✅ 旅行规划系统初始化成功")

        except Exception as e:
            print(f"❌ 多智能体系统初始化失败: {str(e)}")
            import traceback
            traceback.print_exc()
            raise
    
    def plan_trip(self, request: TripRequest) -> TripPlan:
        """
        使用多智能体协作生成旅行计划

        Args:
            request: 旅行请求

        Returns:
            旅行计划
        """
        try:
            print(f"\n{'='*60}")
            print(f"🚀 LangGraph 行程规划: {request.city} ({request.travel_days}天)")
            print(f"{'='*60}\n")
            trip_plan = run_trip_planning_graph(self, request)
            print(f"{'='*60}\n✅ 旅行计划生成完成!\n{'='*60}\n")
            return trip_plan

        except Exception as e:
            print(f"❌ 生成旅行计划失败: {str(e)}")
            import traceback
            traceback.print_exc()
            raise

    def _call_amap(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """高德 REST（原 maps_* MCP 工具名保留兼容）。"""
        try:
            return self.amap.call_tool(tool_name, arguments)
        except Exception as e:
            return f"高德 API 调用失败 ({tool_name}): {e}"

    def _invoke_planner_llm(self, user_prompt: str) -> str:
        """直连 LLM：短 system、显式 max_tokens，并对 5xx/429 重试。"""
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": PLANNER_SYSTEM_COMPACT},
            {"role": "user", "content": user_prompt},
        ]
        sep = "=" * 60
        print(f"\n{sep}\n📤 发给大模型的消息\n{sep}")
        print(f"[system] ({len(PLANNER_SYSTEM_COMPACT)} 字符)\n{PLANNER_SYSTEM_COMPACT}")
        print(f"\n[user] ({len(user_prompt)} 字符)\n{user_prompt}")
        print(f"{sep}\n")

        last_err: Optional[Exception] = None
        for attempt in range(4):
            try:
                raw = invoke_chat(
                    messages,
                    temperature=0.35,
                    max_tokens=8192,
                )
                print(f"{sep}\n📥 大模型返回 ({len(raw)} 字符)\n{sep}\n{raw}\n{sep}\n")
                return raw
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                if any(
                    x in msg
                    for x in (
                        "500",
                        "502",
                        "503",
                        "429",
                        "timeout",
                        "timed out",
                        "connection",
                    )
                ):
                    wait = 1.0 * (2**attempt)
                    print(
                        f"   LLM 第 {attempt + 1} 次失败，{wait:.1f}s 后重试… ({str(e)[:100]})"
                    )
                    time.sleep(wait)
                    continue
                raise
        if last_err:
            raise last_err
        raise RuntimeError("LLM 调用失败")

    def _try_parse_llm_trip_plan(
        self, response: str, request: TripRequest
    ) -> Optional[TripPlan]:
        """从 LLM 输出解析 TripPlan；先做字段规范化以兼容常见模型输出格式。"""
        try:
            if "```json" in response:
                json_start = response.find("```json") + 7
                json_end = response.find("```", json_start)
                json_str = response[json_start:json_end].strip()
            elif "```" in response:
                json_start = response.find("```") + 3
                json_end = response.find("```", json_start)
                json_str = response[json_start:json_end].strip()
            elif "{" in response and "}" in response:
                json_start = response.find("{")
                json_end = response.rfind("}") + 1
                json_str = response[json_start:json_end]
            else:
                return None
            data = json.loads(json_str)
            normalized = self._normalize_llm_trip_payload(data, request)
            if normalized is None:
                return None
            return TripPlan(**normalized)
        except Exception as e:
            print(f"⚠️ 解析 LLM JSON 失败: {e}")
            return None

    def _normalize_llm_trip_payload(
        self, data: Any, request: TripRequest
    ) -> Optional[dict]:
        """将 LLM 返回的松散 JSON 规整为符合 TripPlan 的结构。"""
        if not isinstance(data, dict):
            return None
        out: Dict[str, Any] = json.loads(json.dumps(data, ensure_ascii=False))

        out.setdefault("city", request.city)
        out.setdefault("start_date", request.start_date)
        out.setdefault("end_date", request.end_date)
        out.setdefault("overall_suggestions", "祝您旅途愉快。")
        out.setdefault("weather_info", [])
        days_raw = out.get("days")
        if not isinstance(days_raw, list):
            return None
        days_raw = [d for d in days_raw if isinstance(d, dict)]
        if not days_raw:
            return None

        trip_start = datetime.strptime(request.start_date, "%Y-%m-%d")

        for i, day in enumerate(days_raw):
            if not isinstance(day, dict):
                continue
            day.setdefault("day_index", i)
            if not day.get("date"):
                alt = day.get("day_date") or day.get("calendar_date")
                if isinstance(alt, str):
                    alt = alt.strip()[:10]
                    if re.match(r"^\d{4}-\d{2}-\d{2}$", alt):
                        day["date"] = alt
                if not day.get("date"):
                    # 按列表顺序依次顺延，避免仅依赖可能错误的 day_index
                    day["date"] = (trip_start + timedelta(days=i)).strftime("%Y-%m-%d")
            day.setdefault(
                "description",
                day.get("summary") or f"第{i + 1}天行程",
            )
            day.setdefault("transportation", request.transportation)
            day.setdefault("accommodation", request.accommodation)

            attrs = day.get("attractions")
            if isinstance(attrs, list):
                for a in attrs:
                    if not isinstance(a, dict):
                        continue
                    a["visit_duration"] = self._coerce_visit_duration_minutes(
                        a.get("visit_duration")
                    )
                    loc = self._coerce_location_dict(a.get("location"))
                    a["location"] = loc
                    a.setdefault("address", "")
                    a.setdefault("description", a.get("name") or "")
                    a.setdefault("category", "景点")
                    a.setdefault("ticket_price", 0)

            day["meals"] = self._coerce_meals_list(day.get("meals"), i)

            h = day.get("hotel")
            if isinstance(h, dict) and h.get("location") is not None:
                h["location"] = self._coerce_location_dict(h.get("location"))

        out["days"] = days_raw
        return out

    @staticmethod
    def _coerce_visit_duration_minutes(val: Any) -> int:
        """「120」「2小时」「90分钟」等转为整数分钟。"""
        if val is None:
            return 90
        if isinstance(val, bool):
            return 90
        if isinstance(val, (int, float)):
            return max(1, int(val))
        s = str(val).strip()
        m = re.match(r"^(\d+(?:\.\d+)?)\s*小时", s)
        if m:
            return max(1, int(float(m.group(1)) * 60))
        m = re.match(r"^(\d+(?:\.\d+)?)\s*分钟", s)
        if m:
            return max(1, int(float(m.group(1))))
        m = re.match(r"^(\d+)", s)
        if m:
            return max(1, int(m.group(1)))
        return 90

    @staticmethod
    def _coerce_location_dict(loc: Any) -> Dict[str, float]:
        if loc is None:
            return {"longitude": 0.0, "latitude": 0.0}
        if isinstance(loc, dict):
            lng = loc.get("longitude", loc.get("lng"))
            lat = loc.get("latitude", loc.get("lat"))
            try:
                return {
                    "longitude": float(lng or 0),
                    "latitude": float(lat or 0),
                }
            except (TypeError, ValueError):
                return {"longitude": 0.0, "latitude": 0.0}
        return {"longitude": 0.0, "latitude": 0.0}

    @staticmethod
    def _coerce_meals_list(meals: Any, day_index: int) -> List[dict]:
        """meals 可能是数组，也可能是 {breakfast: '...', lunch: ...}。"""
        default_costs = {"breakfast": 35, "lunch": 60, "dinner": 80}
        labels = {
            "breakfast": "早餐",
            "lunch": "午餐",
            "dinner": "晚餐",
        }

        def one_meal(mtype: str, name: str, desc: str, cost: int) -> dict:
            return {
                "type": mtype,
                "name": name,
                "description": desc,
                "estimated_cost": cost,
            }

        if isinstance(meals, list):
            fixed: List[dict] = []
            for m in meals:
                if not isinstance(m, dict):
                    continue
                mt = str(m.get("type") or "lunch").lower()
                if mt not in labels:
                    if "早" in mt:
                        mt = "breakfast"
                    elif "晚" in mt or "夕" in mt:
                        mt = "dinner"
                    else:
                        mt = "lunch"
                fixed.append(
                    {
                        "type": mt,
                        "name": str(m.get("name") or labels.get(mt, "用餐")),
                        "description": str(m.get("description") or ""),
                        "estimated_cost": int(m.get("estimated_cost") or default_costs.get(mt, 50)),
                    }
                )
            if len(fixed) >= 3:
                return fixed
            # 不足三餐则补全
            have = {x["type"] for x in fixed}
            for mt in ("breakfast", "lunch", "dinner"):
                if mt not in have:
                    fixed.append(
                        one_meal(
                            mt,
                            f"第{day_index + 1}天{labels[mt]}",
                            "当地推荐",
                            default_costs[mt],
                        )
                    )
            _order = {"breakfast": 0, "lunch": 1, "dinner": 2, "snack": 3}
            return sorted(fixed, key=lambda x: _order.get(x["type"], 9))

        if isinstance(meals, dict):
            out: List[dict] = []
            for mt in ("breakfast", "lunch", "dinner"):
                if mt not in meals:
                    continue
                v = meals[mt]
                if isinstance(v, str):
                    out.append(
                        one_meal(
                            mt,
                            f"{labels[mt]}推荐",
                            v,
                            default_costs[mt],
                        )
                    )
                elif isinstance(v, dict):
                    out.append(
                        {
                            "type": mt,
                            "name": str(v.get("name") or labels[mt]),
                            "description": str(v.get("description") or ""),
                            "estimated_cost": int(
                                v.get("estimated_cost") or default_costs[mt]
                            ),
                        }
                    )
            if len(out) >= 3:
                return out

        # 缺省或无法识别
        return [
            one_meal("breakfast", f"第{day_index + 1}天早餐", "当地特色早餐", 35),
            one_meal("lunch", f"第{day_index + 1}天午餐", "当地午餐", 60),
            one_meal("dinner", f"第{day_index + 1}天晚餐", "当地晚餐", 80),
        ]

    def _loads_compact_section(self, raw: str) -> dict:
        obj = self._extract_json_object(raw)
        return obj if isinstance(obj, dict) else {}

    @staticmethod
    def _location_from_amap_field(loc: Any) -> Location:
        if loc is None:
            return Location(longitude=0.0, latitude=0.0)
        if isinstance(loc, str) and "," in loc:
            parts = loc.split(",")
            try:
                return Location(longitude=float(parts[0].strip()), latitude=float(parts[1].strip()))
            except (ValueError, IndexError):
                return Location(longitude=0.0, latitude=0.0)
        if isinstance(loc, dict):
            try:
                lng = float(loc.get("lng") or loc.get("longitude") or 0)
                lat = float(loc.get("lat") or loc.get("latitude") or 0)
                return Location(longitude=lng, latitude=lat)
            except (TypeError, ValueError):
                pass
        return Location(longitude=0.0, latitude=0.0)

    def _build_trip_plan_from_mcp_compact(
        self,
        request: TripRequest,
        attraction_json: str,
        weather_json: str,
        hotel_json: str,
    ) -> TripPlan:
        """LLM 不可用时，用已压缩的 MCP JSON 拼装合法 TripPlan。"""
        att = self._loads_compact_section(attraction_json)
        hot = self._loads_compact_section(hotel_json)
        wdata = self._loads_compact_section(weather_json)

        pois_a: List[dict] = [p for p in (att.get("pois") or []) if isinstance(p, dict)]
        pois_h: List[dict] = [p for p in (hot.get("pois") or []) if isinstance(p, dict)]
        forecasts: List[dict] = [
            f for f in (wdata.get("forecasts") or []) if isinstance(f, dict)
        ]

        if not pois_a and not pois_h:
            return self._create_fallback_plan(request)

        primary_hotel_poi = pois_h[0] if pois_h else None
        hotel_model: Optional[Hotel] = None
        if primary_hotel_poi:
            hotel_model = Hotel(
                name=str(primary_hotel_poi.get("name") or "推荐酒店"),
                address=str(primary_hotel_poi.get("address") or ""),
                location=self._location_from_amap_field(primary_hotel_poi.get("location")),
                price_range="",
                rating="",
                distance="",
                type=request.accommodation,
                estimated_cost=350,
            )

        start_dt = datetime.strptime(request.start_date, "%Y-%m-%d")
        days_out: List[DayPlan] = []
        poi_idx = 0

        for i in range(request.travel_days):
            cur = start_dt + timedelta(days=i)
            d_str = cur.strftime("%Y-%m-%d")
            day_attr: List[Attraction] = []
            for _ in range(3):
                if not pois_a:
                    break
                p = pois_a[poi_idx % len(pois_a)]
                poi_idx += 1
                pid = p.get("id")
                day_attr.append(
                    Attraction(
                        name=str(p.get("name") or "景点"),
                        address=str(p.get("address") or ""),
                        location=self._location_from_amap_field(p.get("location")),
                        visit_duration=90,
                        description=f"来源：高德 POI（id={pid}）",
                        category="景点",
                        ticket_price=0,
                        poi_id=str(pid or ""),
                    )
                )

            days_out.append(
                DayPlan(
                    date=d_str,
                    day_index=i,
                    description=f"第{i + 1}天：{request.city}游览（由 MCP 数据拼装）",
                    transportation=request.transportation,
                    accommodation=request.accommodation,
                    hotel=hotel_model,
                    attractions=day_attr,
                    meals=[
                        Meal(
                            type="breakfast",
                            name=f"{d_str} 早餐",
                            description="当地早餐",
                            estimated_cost=35,
                        ),
                        Meal(
                            type="lunch",
                            name=f"{d_str} 午餐",
                            description="当地午餐",
                            estimated_cost=60,
                        ),
                        Meal(
                            type="dinner",
                            name=f"{d_str} 晚餐",
                            description="当地晚餐",
                            estimated_cost=80,
                        ),
                    ],
                )
            )

        weather_info: List[WeatherInfo] = []
        for i in range(request.travel_days):
            cur = start_dt + timedelta(days=i)
            d_str = cur.strftime("%Y-%m-%d")
            fw = next((f for f in forecasts if f.get("date") == d_str), None)
            if fw is None and forecasts:
                fw = forecasts[min(i, len(forecasts) - 1)]
            if fw:
                weather_info.append(
                    WeatherInfo(
                        date=d_str,
                        day_weather=str(fw.get("day_weather") or ""),
                        night_weather=str(fw.get("night_weather") or ""),
                        day_temp=fw.get("day_temp") if fw.get("day_temp") is not None else 0,
                        night_temp=fw.get("night_temp")
                        if fw.get("night_temp") is not None
                        else 0,
                        wind_direction=str(fw.get("wind_direction") or ""),
                        wind_power=str(fw.get("wind_power") or ""),
                    )
                )
            else:
                weather_info.append(WeatherInfo(date=d_str))

        total_meals = request.travel_days * (35 + 60 + 80)
        total_hotels = (hotel_model.estimated_cost if hotel_model else 0) * max(
            1, request.travel_days
        )
        budget = Budget(
            total_attractions=0,
            total_hotels=total_hotels,
            total_meals=total_meals,
            total_transportation=80 * request.travel_days,
            total=total_hotels + total_meals + 80 * request.travel_days,
        )

        return TripPlan(
            city=request.city,
            start_date=request.start_date,
            end_date=request.end_date,
            days=days_out,
            weather_info=weather_info,
            overall_suggestions=(
                f"{request.city} {request.travel_days} 日行程由高德 MCP 数据自动拼装；"
                f"天气为预报摘要，可能与出行日不完全一致，请以当日预报为准。"
            ),
            budget=budget,
        )

    @staticmethod
    def _extract_json_object(text: str) -> Optional[dict]:
        """从 MCP 返回文本中解析第一个完整 JSON 对象。"""
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        return None
        return None

    def _compact_text_search_for_planner(
        self, raw: str, max_pois: int, section: str
    ) -> str:
        """只保留 POI 关键字段，控制 token。"""
        data = self._extract_json_object(raw)
        if not data or not isinstance(data.get("pois"), list):
            return self._truncate_for_llm(raw, 6000, section)

        slim = []
        for p in data["pois"][:max_pois]:
            if not isinstance(p, dict):
                continue
            loc = p.get("location")
            if loc is None:
                entr = p.get("entr_location") or p.get("exit_location")
                loc = entr
            slim.append(
                {
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "address": p.get("address"),
                    "location": loc,
                    "typecode": p.get("typecode"),
                }
            )
        out = json.dumps({"section": section, "pois": slim}, ensure_ascii=False)
        return self._truncate_for_llm(out, 8000, section)

    def _compact_weather_for_planner(self, raw: str, request: TripRequest) -> str:
        """按行程日期截取预报条目，去掉无关字段。"""
        data = self._extract_json_object(raw)
        if not data or not isinstance(data.get("forecasts"), list):
            return self._truncate_for_llm(raw, 4000, "天气")

        forecasts = data["forecasts"]
        start_d = request.start_date
        end_d = request.end_date

        def norm_temp(v: Any) -> Any:
            if v is None:
                return None
            if isinstance(v, (int, float)):
                return v
            s = str(v).strip()
            m = re.match(r"^-?\d+", s)
            return int(m.group(0)) if m else s

        slim: List[Dict[str, Any]] = []
        for f in forecasts:
            if not isinstance(f, dict):
                continue
            d = f.get("date")
            if d and (d < start_d or d > end_d):
                continue
            slim.append(
                {
                    "date": f.get("date"),
                    "day_weather": f.get("dayweather"),
                    "night_weather": f.get("nightweather"),
                    "day_temp": norm_temp(f.get("daytemp")),
                    "night_temp": norm_temp(
                        f.get("nighttemp") or f.get("nightemp")
                    ),
                    "wind_direction": f.get("daywind") or f.get("wind_direction"),
                    "wind_power": f.get("daypower") or f.get("wind_power"),
                }
            )

        if not slim:
            for f in forecasts[: max(7, request.travel_days + 3)]:
                if not isinstance(f, dict):
                    continue
                slim.append(
                    {
                        "date": f.get("date"),
                        "day_weather": f.get("dayweather"),
                        "night_weather": f.get("nightweather"),
                        "day_temp": norm_temp(f.get("daytemp")),
                        "night_temp": norm_temp(f.get("nighttemp") or f.get("nightemp")),
                        "wind_direction": f.get("daywind"),
                        "wind_power": f.get("daypower"),
                    }
                )

        out = json.dumps(
            {"city": data.get("city"), "forecasts": slim},
            ensure_ascii=False,
        )
        return self._truncate_for_llm(out, 4000, "天气")

    @staticmethod
    def _truncate_for_llm(text: str, max_chars: int, label: str) -> str:
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + f"\n…（{label} 内容已截断，共 {len(text)} 字符）"

    def _build_planner_query(self, request: TripRequest, attractions: str, weather: str, hotels: str = "") -> str:
        """构建行程规划查询"""
        query = f"""请根据以下信息生成{request.city}的{request.travel_days}天旅行计划:

**基本信息:**
- 城市: {request.city}
- 日期: {request.start_date} 至 {request.end_date}
- 天数: {request.travel_days}天
- 交通方式: {request.transportation}
- 住宿: {request.accommodation}
- 偏好: {', '.join(request.preferences) if request.preferences else '无'}

**景点信息:**
{attractions}

**天气信息:**
{weather}

**酒店信息:**
{hotels}

**要求:**
1. 每天安排2-3个景点
2. 每天必须包含早中晚三餐
3. 每天推荐一个具体的酒店(从酒店信息中选择)
3. 考虑景点之间的距离和交通方式
4. 返回完整的JSON格式数据
5. 景点的经纬度坐标要真实准确
"""
        if request.free_text_input:
            query += f"\n**额外要求:** {request.free_text_input}"

        return query
    
    def _create_fallback_plan(self, request: TripRequest) -> TripPlan:
        """创建备用计划(当Agent失败时)"""
        start_date = datetime.strptime(request.start_date, "%Y-%m-%d")
        
        # 创建每日行程
        days = []
        for i in range(request.travel_days):
            current_date = start_date + timedelta(days=i)
            
            day_plan = DayPlan(
                date=current_date.strftime("%Y-%m-%d"),
                day_index=i,
                description=f"第{i+1}天行程",
                transportation=request.transportation,
                accommodation=request.accommodation,
                attractions=[
                    Attraction(
                        name=f"{request.city}景点{j+1}",
                        address=f"{request.city}市",
                        location=Location(longitude=116.4 + i*0.01 + j*0.005, latitude=39.9 + i*0.01 + j*0.005),
                        visit_duration=120,
                        description=f"这是{request.city}的著名景点",
                        category="景点"
                    )
                    for j in range(2)
                ],
                meals=[
                    Meal(type="breakfast", name=f"第{i+1}天早餐", description="当地特色早餐"),
                    Meal(type="lunch", name=f"第{i+1}天午餐", description="午餐推荐"),
                    Meal(type="dinner", name=f"第{i+1}天晚餐", description="晚餐推荐")
                ]
            )
            days.append(day_plan)
        
        return TripPlan(
            city=request.city,
            start_date=request.start_date,
            end_date=request.end_date,
            days=days,
            weather_info=[],
            overall_suggestions=f"这是为您规划的{request.city}{request.travel_days}日游行程,建议提前查看各景点的开放时间。"
        )


# 全局多智能体系统实例
_multi_agent_planner = None


def get_trip_planner_agent() -> MultiAgentTripPlanner:
    """获取多智能体旅行规划系统实例(单例模式)"""
    global _multi_agent_planner

    if _multi_agent_planner is None:
        _multi_agent_planner = MultiAgentTripPlanner()

    return _multi_agent_planner

