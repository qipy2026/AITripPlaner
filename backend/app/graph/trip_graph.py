"""LangGraph 行程规划工作流：采集高德数据 → LLM 合成 → MCP 摘要兜底。"""

from __future__ import annotations

from typing import Any, Dict, Optional, TypedDict

from langgraph.graph import END, StateGraph

from ..models.schemas import TripPlan, TripRequest


class TripGraphState(TypedDict, total=False):
    request: TripRequest
    attraction_response: str
    weather_response: str
    hotel_response: str
    attraction_for_llm: str
    weather_for_llm: str
    hotel_for_llm: str
    planner_response: Optional[str]
    trip_plan: Optional[TripPlan]


def run_trip_planning_graph(planner: Any, request: TripRequest) -> TripPlan:
    """执行 LangGraph 并返回 TripPlan。"""

    graph = _build_graph(planner)
    final = graph.invoke({"request": request})
    plan = final.get("trip_plan")
    if plan is None:
        raise RuntimeError("行程规划未产出有效 TripPlan")
    return plan


def _build_graph(planner: Any):
    workflow = StateGraph(TripGraphState)

    def gather_data(state: TripGraphState) -> Dict[str, Any]:
        req = state["request"]
        kw = req.preferences[0] if req.preferences else "景点"
        att = planner._call_amap("maps_text_search", {"keywords": kw, "city": req.city, "citylimit": "true"})
        w = planner._call_amap("maps_weather", {"city": req.city})
        hot = planner._call_amap(
            "maps_text_search",
            {"keywords": f"{req.accommodation}酒店", "city": req.city, "citylimit": "true"},
        )
        att_c = planner._compact_text_search_for_planner(att, max_pois=12, section="景点POI")
        w_c = planner._compact_weather_for_planner(w, req)
        hot_c = planner._compact_text_search_for_planner(hot, max_pois=10, section="酒店POI")
        return {
            "attraction_response": att,
            "weather_response": w,
            "hotel_response": hot,
            "attraction_for_llm": att_c,
            "weather_for_llm": w_c,
            "hotel_for_llm": hot_c,
        }

    def llm_plan(state: TripGraphState) -> Dict[str, Any]:
        req = state["request"]
        query = planner._build_planner_query(
            req,
            state["attraction_for_llm"],
            state["weather_for_llm"],
            state["hotel_for_llm"],
        )
        out: Dict[str, Any] = {"planner_response": None, "trip_plan": None}
        try:
            raw = planner._invoke_planner_llm(query)
            out["planner_response"] = raw
            plan = planner._try_parse_llm_trip_plan(raw, req)
            if plan is not None:
                out["trip_plan"] = plan
        except Exception as e:
            print(f"⚠️ LLM 规划节点失败: {e}")
        return out

    def fallback_plan(state: TripGraphState) -> Dict[str, Any]:
        if state.get("trip_plan") is not None:
            return {}
        req = state["request"]
        plan = planner._build_trip_plan_from_mcp_compact(
            req,
            state["attraction_for_llm"],
            state["weather_for_llm"],
            state["hotel_for_llm"],
        )
        return {"trip_plan": plan}

    def route_after_llm(state: TripGraphState) -> str:
        if state.get("trip_plan") is not None:
            return END
        return "fallback"

    workflow.add_node("gather", gather_data)
    workflow.add_node("llm_plan", llm_plan)
    workflow.add_node("fallback", fallback_plan)
    workflow.set_entry_point("gather")
    workflow.add_edge("gather", "llm_plan")
    workflow.add_conditional_edges(
        "llm_plan",
        route_after_llm,
        {"fallback": "fallback", END: END},
    )
    workflow.add_edge("fallback", END)
    return workflow.compile()
