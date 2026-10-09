import json

import httpx
import pytest

from netbox_rack_design_mcp import server
from netbox_rack_design_mcp.client import NetBoxClient, NetBoxError, auth_header

TOKEN = "s3cret-token-value"


class Recorder:
    def __init__(self, routes):
        self.routes = routes  # {(method, path): json-able or callable}
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        key = (request.method, request.url.path)
        if key not in self.routes:
            return httpx.Response(404, json={"detail": "not found"})
        body = self.routes[key]
        return httpx.Response(200, json=body(request) if callable(body) else body)


def install(routes):
    rec = Recorder(routes)
    server.set_client(NetBoxClient("https://nb.example", TOKEN, transport=httpx.MockTransport(rec)))
    return rec


@pytest.fixture(autouse=True)
def _reset():
    yield
    server.set_client(None)


API = "/api/plugins/rack-design/"


def test_list_designs_passes_filters_and_trims():
    rec = install({("GET", API + "designs/"): {
        "count": 1, "results": [{"id": 3, "title": "Q3", "status": "draft", "sites": [], "extra": 1}]}})
    out = server.list_designs(site="ams1", status="draft")
    assert out["count"] == 1 and out["designs"][0]["title"] == "Q3"
    assert "extra" not in out["designs"][0]
    req = rec.calls[0]
    assert req.url.params["site"] == "ams1" and req.url.params["status"] == "draft"


def test_list_designs_omits_unset_filters():
    rec = install({("GET", API + "designs/"): {"count": 0, "results": []}})
    server.list_designs()
    assert "site" not in rec.calls[0].url.params and "status" not in rec.calls[0].url.params


def test_get_design():
    install({("GET", API + "designs/7/"): {"id": 7, "title": "X"}})
    assert server.get_design(7)["title"] == "X"


def test_get_work_order_sends_step_and_json_format():
    rec = install({("GET", API + "designs/7/work-order/"): {"steps": []}})
    assert server.get_work_order(7, step=2) == {}
    params = rec.calls[0].url.params
    assert params["step"] == "2" and params["output"] == "json"


def test_get_work_order_without_step_omits_it():
    rec = install({("GET", API + "designs/7/work-order/"): {"steps": []}})
    server.get_work_order(7)
    assert "step" not in rec.calls[0].url.params


def _plan_routes(n_steps=2):
    """A realistic Rebalance-sized plan: n steps, 2 racks, 2 PDUs x 12 banks with outlets."""
    steps = {"results": [{"index": i, "title": f"Window {i}"} for i in range(n_steps, 0, -1)]}
    placements, wo_steps, sim_steps = [], [], []
    for i in range(1, n_steps + 1):
        pid = 100 + i
        placements.append({"id": pid, "display": f"Move: srv-{i}", "step": {"index": i}, "step_order": 1})
        wo_steps.append({
            "index": i, "total_steps": n_steps,
            "power": [{"rack": "A1", "key": "r:1"}, {"rack": "A2", "key": "r:2"}],
            "actions": [{"placement_id": pid, "kind": "move", "device": {"name": f"srv-{i}", "serial": "S" * 40},
                         "from": {"rack": "A1", "position": "10", "face": "front", "site": "FRA"},
                         "to": {"rack": "A2", "position": "20", "face": "front", "site": "FRA"},
                         "children": [], "power": {"cabling": [{"outlet": "o"}] * 4}}],
        })
        racks = {}
        for key, name in (("r:1", "A1"), ("r:2", "A2")):
            pdus = {}
            for pdu in ("pdu-a", "pdu-b"):
                banks = {}
                for b in range(1, 13):
                    banks[str(b)] = {
                        "allocated_power": 100.0 * b, "planned_power": 0.0, "max_power": 1500.0,
                        "util_pct": 100.0 * b / 1500 * 100, "state": "warn" if b == 12 else "ok",
                        "outlets": {str(o): {"name": f"o{o}", "device": "x" * 30, "power": 12.5}
                                    for o in range(1, 25)},
                    }
                pdus[pdu] = {"banks": banks}
            racks[key] = {
                "power": {"draw_w": 5000.4, "capacity_w": 8000, "util_pct": 62.5, "state": "ok"},
                "distribution": {"pdus": pdus}, "distribution_status": {"state": "ok"},
                "problems": [{"code": "rack_near", "severity": "warning", "detail": f"{name}: 5000 W of 8000 W"},
                         {"code": "bank_near", "severity": "warning", "detail": "dup of hot_banks"}],
            }
        sim_steps.append({"index": i, "racks": racks})
    placements.append({"id": 14, "display": "loose", "step": None})
    routes = {
        ("GET", API + "design-steps/"): steps,
        ("GET", API + "placements/"): {"results": placements},
        ("GET", API + "designs/5/work-order/"): {"steps": wo_steps, "unscheduled": [
            {"placement_id": 14, "kind": "add", "device": {"name": "loose"}, "from": None,
             "to": {"rack": "A1", "position": "5"}, "children": []}]},
        ("POST", API + "designs/5/simulate-steps/"): {"steps": sim_steps},
    }
    return routes, sim_steps


def test_get_execution_plan_posts_the_saved_order_and_is_compact():
    routes, _ = _plan_routes(2)
    rec = install(routes)
    out = server.get_execution_plan(5)
    post = [c for c in rec.calls if c.method == "POST"][0]
    assert json.loads(post.content) == {"steps": [[101], [102]]}
    assert [s["index"] for s in out["steps"]] == [1, 2] and out["total_steps"] == 2
    step = out["steps"][0]
    assert step["title"] == "Window 1"
    assert step["actions"] == [{
        "placement_id": 101, "kind": "move", "device": "srv-1",
        "from": {"rack": "A1", "face": "front", "u": "10"},
        "to": {"rack": "A2", "face": "front", "u": "20"}}]
    a1 = step["racks"][0]
    assert (a1["rack"], a1["draw_w"], a1["capacity_w"], a1["util_pct"], a1["state"]) == ("A1", 5000, 8000, 62, "ok")
    assert len(a1["hot_banks"]) == 2 and a1["hot_banks"][0]["bank"] == "12"
    assert set(a1["hot_banks"][0]) == {"pdu", "bank", "state", "load_w", "limit_w", "pct"}
    assert step["problems"][0] == {"rack": "A1", "code": "rack_near", "severity": "warning",
                                   "detail": "A1: 5000 W of 8000 W"}
    # every problem is kept, bank_near included; hot_banks is extra detail
    assert [p["code"] for p in step["problems"]] == ["rack_near", "bank_near", "rack_near", "bank_near"]
    assert [u["placement_id"] for u in out["unscheduled"]] == [14]
    assert "distribution" not in json.dumps(out) and "outlets" not in json.dumps(out)


def test_get_execution_plan_detail_returns_the_full_simulation():
    routes, sim = _plan_routes(2)
    install(routes)
    out = server.get_execution_plan(5, detail=True)
    assert out["simulation"] == sim
    assert [a["placement_id"] for a in out["steps"][0]["actions"]] == [101]
    assert [u["placement_id"] for u in out["unscheduled"]] == [14]


def test_compact_plan_of_a_rebalance_sized_design_stays_small():
    routes, _ = _plan_routes(7)
    install(routes)
    compact = len(json.dumps(server.get_execution_plan(5)))
    full = len(json.dumps(server.get_execution_plan(5, detail=True)))
    assert compact < 9000, compact
    assert full > 10 * compact


def test_simulate_order_is_compact_by_default_and_detail_gives_raw():
    routes, sim = _plan_routes(7)
    rec = install(routes)
    out = server.simulate_order(5, [[101], [102]])
    assert json.loads([c for c in rec.calls if c.method == "POST"][0].content) == {"steps": [[101], [102]]}
    assert len(json.dumps(out)) < 9000
    assert out["steps"][0]["racks"][1]["rack"] == "A2"
    assert out["steps"][0]["actions"][0]["device"] == "srv-1"
    assert server.simulate_order(5, [[101]], detail=True) == {"steps": sim}


def test_http_error_is_reported_without_the_token():
    install({})
    with pytest.raises(NetBoxError) as exc:
        server.get_design(1)
    assert "404" in str(exc.value)
    assert TOKEN not in str(exc.value)


def test_token_only_in_authorization_header():
    rec = install({("GET", API + "designs/1/"): {}})
    server.get_design(1)
    req = rec.calls[0]
    assert req.headers["authorization"] == f"Token {TOKEN}"
    assert TOKEN not in str(req.url)


def test_v2_token_uses_bearer():
    assert auth_header("nbt_abc.def") == "Bearer nbt_abc.def"
    assert auth_header("plain") == "Token plain"


def test_missing_env_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("NETBOX_URL", raising=False)
    monkeypatch.delenv("NETBOX_TOKEN", raising=False)
    with pytest.raises(NetBoxError, match="NETBOX_URL"):
        NetBoxClient.from_env()


def test_tools_are_registered_and_work_order_description_has_the_default_format():
    import asyncio

    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert set(tools) == {
        "list_designs", "get_design", "get_execution_plan", "get_work_order", "simulate_order"}
    desc = tools["get_work_order"].description
    assert "ALWAYS win" in desc
    for word in ("Header", "Safety notes", "photo", "rollback", "report back"):
        assert word.lower() in desc.lower()
    assert "changes nothing" in tools["simulate_order"].description
    assert "changes nothing" in tools["get_execution_plan"].description
    for name in ("get_execution_plan", "simulate_order"):
        assert "COMPACT" in tools[name].description and "detail=true" in tools[name].description
    assert "total_steps" in desc


def test_prompt_uses_user_instructions_over_default():
    install({("GET", API + "designs/5/work-order/"): {"steps": [{"index": 1}]}})
    text = server.smart_hands_ticket(5, 1, instructions="Write it in Jira wiki markup.")
    assert "Jira wiki markup" in text and "DEFAULT SMART-HANDS" not in text
    assert '"index": 1' in text


def test_prompt_falls_back_to_default_format():
    install({("GET", API + "designs/5/work-order/"): {"steps": []}})
    assert "DEFAULT SMART-HANDS" in server.smart_hands_ticket(5, 1)


def test_work_order_drops_empty_values_but_keeps_false_and_zero():
    install({("GET", API + "designs/7/work-order/"): {
        "total_steps": 3, "steps": [{"index": 1, "total_steps": 3, "problems": [], "actions": [
            {"placement_id": 1, "stale": False, "bay": None, "children": [], "to": {"u": 0, "face": ""}}]}]}})
    out = server.get_work_order(7)
    assert out["total_steps"] == 3 and out["steps"][0]["total_steps"] == 3
    act = out["steps"][0]["actions"][0]
    assert act == {"placement_id": 1, "stale": False, "to": {"u": 0}}
    assert "problems" not in out["steps"][0]
