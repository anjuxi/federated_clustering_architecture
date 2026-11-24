import os;
import sys;
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)));
import federated_clustering as fc;


V_测试_通过数 = [0];   # 用列表包一下，函数里才好改。
V_测试_失败数 = [0];


def record_result(case_name, passed, reason=""):
    # 记录一条用例结果并打印，顺便累计通过/失败数；无返回值。
    # 入参 case_name 用例名、passed 是否通过、reason 失败原因（可选）。
    if passed:
        V_测试_通过数[0] += 1;
        print(f"! [PASS] {case_name}!");
    else:
        V_测试_失败数[0] += 1;
        print(f"! [FAIL] {case_name}: {reason}!");


def run_case(case_name, assert_fn):
    # 跑一个用例，异常都接住不让整个套件崩；入参 case_name、assert_fn，无返回值。
    try:
        assert_fn();
        record_result(case_name, True);
    except AssertionError as e:
        record_result(case_name, False, str(e));
    except Exception as e:
        record_result(case_name, False, f"unexpected error: {type(e).__name__}: {e}");


def invoke_subgroup(subgroup_builder, action, payload):
    # 省事的小工具：造个请求直接 invoke 子群，测试里反复用。
    # 入参 subgroup_builder 子群构造函数、action 动作、payload 载荷；返回子群执行后的输出状态。
    req = fc.build_request("test", "test_proxy", action, payload);
    return subgroup_builder().invoke({
        fc.V_字段_当前请求: req,
        fc.V_字段_日志: [],
        fc.V_字段_黑板: {},
    });



def test_capability_dir():
    # 目录得有这几个代理，该支持的支持、不该支持的别乱支持。
    cap_dir = fc.build_capability_dir();
    assert fc.V_代理_财务 in cap_dir, "capability dir missing Finance Proxy";
    assert fc.V_代理_法务 in cap_dir, "capability dir missing Legal Proxy";
    assert fc.query_capability(cap_dir, fc.V_代理_法务, fc.V_动作_合同评审) is not None, "Legal should support contract review";
    assert fc.query_capability(cap_dir, fc.V_代理_运维, fc.V_动作_合同评审) is None, "Ops should not support contract review";


def test_request_trace_id():
    # 不传父ID就新生成，传了就沿用，链路靠这个串。
    r1 = fc.build_request("A", "B", "x", {});
    assert r1[fc.V_字段_追踪ID].startswith("T-"), "bad trace id format";
    r2 = fc.build_request("B", "C", "y", {}, parent_trace_id=r1[fc.V_字段_追踪ID]);
    assert r2[fc.V_字段_追踪ID] == r1[fc.V_字段_追踪ID], "parent and child requests should share the trace id";


def test_finance_small_no_cross():
    # 小钱自己登记完就完事，别去麻烦法务。
    out = invoke_subgroup(fc.build_finance_subgroup, fc.V_动作_合同发起, {"contract_name": "office supplies", "amount": 1000});
    assert out.get(fc.V_字段_结果, {}).get("reg_id"), "small contract should generate a reg_id";
    assert out.get(fc.V_字段_跨群请求, []) == [], "small contract should not produce cross-group requests";


def test_finance_large_cross():
    # 大钱必须走法务，跨群请求的目标和动作都得对。
    out = invoke_subgroup(fc.build_finance_subgroup, fc.V_动作_合同发起, {"contract_name": "procurement", "amount": 120000});
    cross = out.get(fc.V_字段_跨群请求, []);
    assert len(cross) == 1, "large contract should produce 1 cross-group request";
    assert cross[0][fc.V_字段_目标] == fc.V_代理_法务, "cross-group target should be Legal Proxy";
    assert cross[0][fc.V_字段_动作] == fc.V_动作_合同评审, "cross-group action should be contract review";


def test_legal_blackboard():
    # 三个专家都得写黑板，主审结论里三类意见一个都不能少，最后还得喊财务归档。
    out = invoke_subgroup(fc.build_legal_subgroup, fc.V_动作_合同评审, {"contract_name": "X", "amount": 100});
    conclusion = out.get(fc.V_字段_结果, {}).get("review_conclusion", "");
    for keyword in ["compliance", "risk", "terms"]:
        assert keyword in conclusion, f"review conclusion should include {keyword}";
    cross = out.get(fc.V_字段_跨群请求, []);
    assert len(cross) == 1 and cross[0][fc.V_字段_目标] == fc.V_代理_财务, "legal review should trigger finance archive";


def test_rd_star_frontend():
    # 要前端就派前端，前端有结果、后端空着。
    out = invoke_subgroup(fc.build_rd_subgroup, fc.V_动作_需求排期, {"req_type": "frontend"});
    result = out.get(fc.V_字段_结果, {});
    assert result.get("frontend"), "frontend result should not be empty";
    assert result.get("backend") == {}, "backend result should be empty";


def test_rd_star_backend():
    # 要后端就派后端，反过来。
    out = invoke_subgroup(fc.build_rd_subgroup, fc.V_动作_需求排期, {"req_type": "backend"});
    result = out.get(fc.V_字段_结果, {});
    assert result.get("backend"), "backend result should not be empty";
    assert result.get("frontend") == {}, "frontend result should be empty";


def test_rd_logs_no_dup():
    # 回归用例：之前嵌套子图拼日志会把外层日志加重复，现在派单和主管各只能出现一次。
    fed_graph, cap_dir = fc.assemble_midoffice();
    req = fc.build_request("user", fc.V_代理_研发, fc.V_动作_需求排期, {"req_type": "frontend"});
    final = fed_graph.invoke({
        fc.V_字段_请求队列: [req], fc.V_字段_结果池: {},
        fc.V_字段_当前请求: None, fc.V_字段_日志: [], fc.V_字段_能力目录: cap_dir,
    });
    logs = final.get(fc.V_字段_日志, []);
    dispatch_count = sum(1 for l in logs if "dispatch -> R&D Proxy" in l);
    lead_count = sum(1 for l in logs if "R&D·Lead" in l);
    assert dispatch_count == 1, f"r&d dispatch should appear once, got {dispatch_count} (duplicated logs)";
    assert lead_count == 1, f"r&d lead should appear once, got {lead_count} (duplicated logs)";


def test_ops_standalone():
    # 运维自己巡检评估，不跨群。
    out = invoke_subgroup(fc.build_ops_subgroup, fc.V_动作_资源巡检, {"target": "cluster"});
    assert out.get(fc.V_字段_结果, {}).get("inspect_status") == "ok", "inspect status should be ok";
    assert out.get(fc.V_字段_跨群请求, []) == [], "ops should not produce cross-group requests";


def test_proxy_unsupported_action():
    # 代理收到不会的动作，降级就行，别崩。
    cap_dir = fc.build_capability_dir();
    proxy = fc.make_proxy(fc.V_代理_财务, {fc.V_动作_合同发起: fc.build_finance_subgroup}, cap_dir);
    req = fc.build_request("user", fc.V_代理_财务, "nonexistent_action", {});
    out = proxy({fc.V_字段_当前请求: req, fc.V_字段_日志: [], fc.V_字段_请求队列: [], fc.V_字段_结果池: {}});
    pool = out.get(fc.V_字段_结果池, {});
    entry = list(pool.values())[0];
    assert entry[fc.V_字段_状态] == fc.V_状态_降级, "unsupported action should degrade";


def test_proxy_fault_fallback():
    # 子群内部炸了，代理得兜住，返回降级结果。
    cap_dir = fc.build_capability_dir();
    proxy = fc.make_proxy(fc.V_代理_财务, {fc.V_动作_故障注入: fc.build_fault_subgroup}, cap_dir);
    req = fc.build_request("user", fc.V_代理_财务, fc.V_动作_故障注入, {});
    out = proxy({fc.V_字段_当前请求: req, fc.V_字段_日志: [], fc.V_字段_请求队列: [], fc.V_字段_结果池: {}});
    entry = list(out.get(fc.V_字段_结果池, {}).values())[0];
    assert entry[fc.V_字段_状态] == fc.V_状态_降级, "proxy should degrade on subgroup fault";
    assert "degraded" in str(entry.get("result")), "degraded result should carry a hint";


def test_e2e_contract_chain():
    # 大额合同端到端：财务发起 -> 法务评审 -> 财务归档，一个追踪ID串三跳。
    fed_graph, cap_dir = fc.assemble_midoffice();
    req = fc.build_request("user", fc.V_代理_财务, fc.V_动作_合同发起, {"contract_name": "core procurement", "amount": 200000});
    final = fed_graph.invoke({
        fc.V_字段_请求队列: [req], fc.V_字段_结果池: {},
        fc.V_字段_当前请求: None, fc.V_字段_日志: [], fc.V_字段_能力目录: cap_dir,
    });
    logs = final.get(fc.V_字段_日志, []);
    text = "\n".join(logs);
    assert "dispatch -> Finance Proxy" in text, "chain should include Finance Proxy";
    assert "dispatch -> Legal Proxy" in text, "chain should include Legal Proxy";
    trace_id = req[fc.V_字段_追踪ID];
    traced = [l for l in logs if "dispatch" in l and trace_id in l];
    assert len(traced) == 3, f"the same trace id should dispatch 3 times (finance/legal/finance), got {len(traced)}";
    entry = final.get(fc.V_字段_结果池, {}).get(trace_id, {});
    assert entry.get(fc.V_字段_状态) == fc.V_状态_完成, "final status should be done";
    assert entry.get("result", {}).get("archive_status") == "archived", "final should be archived";


def test_result_pool_aggregation():
    # 两个请求各归各的，别互相覆盖。
    fed_graph, cap_dir = fc.assemble_midoffice();
    r1 = fc.build_request("user", fc.V_代理_运维, fc.V_动作_资源巡检, {"target": "A"});
    r2 = fc.build_request("user", fc.V_代理_运维, fc.V_动作_资源巡检, {"target": "B"});
    final = fed_graph.invoke({
        fc.V_字段_请求队列: [r1, r2], fc.V_字段_结果池: {},
        fc.V_字段_当前请求: None, fc.V_字段_日志: [], fc.V_字段_能力目录: cap_dir,
    });
    pool = final.get(fc.V_字段_结果池, {});
    assert r1[fc.V_字段_追踪ID] in pool, "result pool should contain request 1";
    assert r2[fc.V_字段_追踪ID] in pool, "result pool should contain request 2";
    assert len(pool) == 2, "two requests should be aggregated separately, not overwritten";



def main():
    # 测试入口：注册全部用例逐个跑，最后打印汇总；无入参无返回值。
    fc.print_separator();
    print("! Federated clustering test suite running!");
    fc.print_separator();

    cases = [
        ("Capability dir build & query", test_capability_dir),
        ("Request carries trace ID", test_request_trace_id),
        ("Finance small contract no cross-group", test_finance_small_no_cross),
        ("Finance large contract triggers cross-group", test_finance_large_cross),
        ("Legal blackboard summarize", test_legal_blackboard),
        ("R&D star dispatch - frontend", test_rd_star_frontend),
        ("R&D star dispatch - backend", test_rd_star_backend),
        ("R&D logs not duplicated (regression)", test_rd_logs_no_dup),
        ("Ops subgroup standalone", test_ops_standalone),
        ("Proxy rejects unsupported action", test_proxy_unsupported_action),
        ("Proxy fault fallback", test_proxy_fault_fallback),
        ("E2E contract review cross-group chain", test_e2e_contract_chain),
        ("Result pool aggregation by trace ID", test_result_pool_aggregation),
    ];

    fc.print_separator();
    for case_name, case_fn in cases:
        run_case(case_name, case_fn);
    fc.print_separator();

    print(f"! Summary: passed {V_测试_通过数[0]}, failed {V_测试_失败数[0]}!");
    if V_测试_失败数[0] == 0:
        print("✅ All tests passed, federated clustering works fine!");
    else:
        print("❌ Some tests failed, check FAIL logs above!");
        sys.exit(1);
    fc.print_separator();


if __name__ == "__main__":
    main();
