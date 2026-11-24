import uuid;
import time;
from typing import TypedDict;
from langgraph.graph import StateGraph, START, END;


# 状态字段名集中放一起，省得字符串到处写错。
V_字段_追踪ID = "trace_id";            # 链路追踪ID，串联一次跨群调用。
V_字段_来源 = "from_proxy";            # 请求来源代理名。
V_字段_目标 = "to_proxy";              # 请求目标代理名。
V_字段_动作 = "action";                # 请求动作类型。
V_字段_载荷 = "payload";               # 请求业务载荷。
V_字段_结果 = "result";                # 子群处理结果。
V_字段_状态 = "status";                # 请求状态。
V_字段_日志 = "logs";                  # 执行日志列表。
V_字段_跨群请求 = "cross_requests";    # 子群执行时产生的新跨群请求。
V_字段_请求队列 = "request_queue";     # 顶层联邦总线的待处理请求队列。
V_字段_结果池 = "result_pool";         # 已完成请求的结果池，按追踪ID归集。
V_字段_当前请求 = "current_request";   # 总线当前派给代理的请求。
V_字段_能力目录 = "capability_dir";    # 全局能力目录，记着每个子群能做什么。
V_字段_黑板 = "blackboard";            # 黑板子群的共享状态。

# 请求状态取值。
V_状态_待处理 = "pending";
V_状态_处理中 = "processing";
V_状态_完成 = "done";
V_状态_失败 = "failed";
V_状态_降级 = "degraded";

# 业务动作类型。
V_动作_合同发起 = "contract_init";         # 财务发起合同审核。
V_动作_合同评审 = "contract_review";       # 法务评审合同。
V_动作_合同归档 = "contract_archive";      # 财务拿到结论后归档。
V_动作_需求排期 = "dev_schedule";          # 研发给需求排期。
V_动作_资源巡检 = "ops_inspect";           # 运维做资源巡检。
V_动作_故障注入 = "fault_inject";          # 用来演示代理兜底的故障动作。

# 子群和代理的名字。
V_代理_财务 = "Finance Proxy";
V_代理_法务 = "Legal Proxy";
V_代理_研发 = "R&D Proxy";
V_代理_运维 = "Ops Proxy";

# 合同金额超过这个数就得走法务，用来触发跨群调用。
V_阈值_需法务金额 = 50000;



class GroupState(TypedDict, total=False):
    # 这里必须用 TypedDict 把状态字段都声明出来。踩过坑：langgraph 1.x 直接传
    # 裸 dict 的话，节点没返回的字段会被悄悄裁掉，前面写的 result、result_pool
    # 说没就没。声明之后没返回的字段会保留原值，total=False 表示字段都可选，子群各取所需。
    current_request: dict;       # 总线当前派给代理的请求。
    logs: list;                  # 执行日志列表。
    result: dict;                # 子群处理结果。
    cross_requests: list;        # 子群产生的跨群请求。
    blackboard: dict;            # 黑板子群的共享状态。
    route: str;                  # 星型主管的路由决策。
    frontend_result: dict;       # 研发前端小组的结果。
    backend_result: dict;        # 研发后端小组的结果。
    request_queue: list;         # 顶层联邦总线的请求队列。
    result_pool: dict;           # 已完成请求的结果池。
    capability_dir: dict;        # 全局能力目录。


def gen_trace_id():
    # 生成链路追踪ID，截一段 uuid 就够用，返回 "T-xxxxxxxx" 这种字符串。
    return "T-" + uuid.uuid4().hex[:8];


def append_log(state, line):
    # 往 state 的日志列表追加一条 line，返回追加后的新列表。
    # langgraph 对列表字段默认是替换语义，不返回完整列表的话前面的日志就丢了。
    logs = list(state.get(V_字段_日志, []));
    logs.append(line);
    return logs;


def build_request(source, target, action, payload, parent_trace_id=None):
    # 拼一个标准化跨群请求：source 来源、target 目标、action 动作、payload 载荷，
    # parent_trace_id 不传就新生成一个；返回的请求字典状态默认待处理。
    trace_id = parent_trace_id if parent_trace_id else gen_trace_id();
    return {
        V_字段_追踪ID: trace_id,
        V_字段_来源: source,
        V_字段_目标: target,
        V_字段_动作: action,
        V_字段_载荷: payload,
        V_字段_状态: V_状态_待处理,
    };


def print_separator():
    # 输出里的分隔线统一走这个，别各处自己写。
    print("——" * 50);


def print_logs(log_list):
    # 把日志逐条打印出来，前面带行号好读；入参是 log_list，没有返回值。
    for idx, line in enumerate(log_list, 1):
        print(f"  {idx:>3}. {line}");



def build_capability_dir():
    # 全局记一笔每个子群能干啥：代理转发前查一下免得发错地方，后面排查"哪个群
    # 能处理这个事"也不至于两眼一抹黑。返回 {代理名: {动作: 说明}}。
    return {
        V_代理_财务: {
            V_动作_合同发起: "Register contract and start review; large contracts auto-forward to Legal",
            V_动作_合同归档: "Archive contract based on Legal's conclusion",
        },
        V_代理_法务: {
            V_动作_合同评审: "Multi-expert blackboard review, outputs compliance and risk conclusion",
        },
        V_代理_研发: {
            V_动作_需求排期: "Lead dispatches; frontend/backend groups collaborate (nested) to produce schedule",
        },
        V_代理_运维: {
            V_动作_资源巡检: "Resource check and deployment evaluation",
        },
    };


def query_capability(cap_dir, proxy_name, action):
    # 查 cap_dir 里 proxy_name 支不支持 action，支持返回说明，否则返回 None。
    return cap_dir.get(proxy_name, {}).get(action);



# 没接真实大模型，用本地规则冒充各领域专家，图能跑起来就行。每个 agent 都是
# 一个函数：吃状态，吐要合并进状态的更新。
def agent_finance_register(state):
    # 登记合同，生成登记号。
    req = state[V_字段_当前请求];
    payload = req.get(V_字段_载荷, {});
    contract_name = payload.get("contract_name", "unnamed contract");
    amount = payload.get("amount", 0);
    reg_id = "REG-" + gen_trace_id();
    logs = append_log(state, f"[Finance·Register] contract \"{contract_name}\" amount ￥{amount}, reg_id {reg_id}");
    return {
        V_字段_结果: {"reg_id": reg_id, "contract_name": contract_name, "amount": amount},
        V_字段_日志: logs,
    };


def agent_finance_judge(state):
    # 看金额够不够大，够大就给法务发个跨群请求。
    result = state.get(V_字段_结果, {});
    amount = result.get("amount", 0);
    logs = append_log(state, f"[Finance·Judge] amount ￥{amount}, threshold ￥{V_阈值_需法务金额}");
    cross_reqs = [];
    if amount >= V_阈值_需法务金额:
        # 复用同一个追踪ID，整条链路才串得起来。
        cross_reqs.append(build_request(
            V_代理_财务, V_代理_法务, V_动作_合同评审,
            {"reg_id": result.get("reg_id"), "contract_name": result.get("contract_name"), "amount": amount},
            parent_trace_id=state[V_字段_当前请求][V_字段_追踪ID],
        ));
        logs.append("[Finance·Judge] large contract triggered, cross-group request -> Legal Proxy");
    else:
        logs.append("[Finance·Judge] small contract, no legal review needed");
    return {V_字段_跨群请求: cross_reqs, V_字段_日志: logs};


def agent_finance_archive(state):
    # 拿法务结论归档。
    req = state[V_字段_当前请求];
    payload = req.get(V_字段_载荷, {});
    conclusion = payload.get("legal_conclusion", "no conclusion");
    logs = append_log(state, f"[Finance·Archive] archived based on conclusion \"{conclusion}\"");
    return {V_字段_结果: {"archive_status": "archived", "based_on": conclusion}, V_字段_日志: logs};



def agent_legal_compliance(state):
    # 合规专家往黑板写一条。
    blackboard = dict(state.get(V_字段_黑板, {}));
    blackboard["compliance"] = "Parties qualified, clauses comply with applicable law";
    logs = append_log(state, "[Legal·Compliance] wrote to blackboard: compliance");
    return {V_字段_黑板: blackboard, V_字段_日志: logs};


def agent_legal_risk(state):
    # 风险专家往黑板写一条。
    blackboard = dict(state.get(V_字段_黑板, {}));
    blackboard["risk"] = "Payment milestones are back-loaded; performance risk exists, suggest adding guarantee clauses";
    logs = append_log(state, "[Legal·Risk] wrote to blackboard: risk");
    return {V_字段_黑板: blackboard, V_字段_日志: logs};


def agent_legal_terms(state):
    # 条款专家往黑板写一条。
    blackboard = dict(state.get(V_字段_黑板, {}));
    blackboard["terms"] = "Clause 8 breach liability is one-sided; suggest making it mutual";
    logs = append_log(state, "[Legal·Terms] wrote to blackboard: terms");
    return {V_字段_黑板: blackboard, V_字段_日志: logs};


def agent_legal_summarize(state):
    # 主审把黑板上各位的意见拼起来，再通知财务归档。
    blackboard = state.get(V_字段_黑板, {});
    conclusion = "; ".join(f"{k}: {v}" for k, v in blackboard.items());
    logs = append_log(state, "[Legal·Lead] aggregated blackboard opinions into final review conclusion");
    cross_reqs = [build_request(
        V_代理_法务, V_代理_财务, V_动作_合同归档,
        {"legal_conclusion": conclusion},
        parent_trace_id=state[V_字段_当前请求][V_字段_追踪ID],
    )];
    logs.append("[Legal·Lead] cross-group request -> Finance Proxy (archive)");
    return {V_字段_结果: {"review_conclusion": conclusion}, V_字段_跨群请求: cross_reqs, V_字段_日志: logs};


def agent_fault_inject(state):
    # 故意崩一下，测代理的兜底。
    raise RuntimeError("simulated subgroup node crash");


# 每个构造器返回一个编译好的子图，就是一个自治子群。对外只露 invoke，里面
# 怎么跑外面管不着。
def build_finance_subgroup():
    # 财务子群走流水线：登记 -> 判定。判定节点只决定要不要喊法务，不直接调别人。
    builder = StateGraph(GroupState);
    builder.add_node("register", agent_finance_register);
    builder.add_node("judge", agent_finance_judge);
    builder.add_edge(START, "register");
    builder.add_edge("register", "judge");
    builder.add_edge("judge", END);
    return builder.compile();


def build_finance_archive_subgroup():
    # 归档是另一个动作入口，单独拆个子群，单节点搞定。
    builder = StateGraph(GroupState);
    builder.add_node("archive", agent_finance_archive);
    builder.add_edge(START, "archive");
    builder.add_edge("archive", END);
    return builder.compile();


def build_legal_subgroup():
    # 法务子群走黑板：几个专家各写各的，最后主审汇总。
    builder = StateGraph(GroupState);
    builder.add_node("compliance_expert", agent_legal_compliance);
    builder.add_node("risk_expert", agent_legal_risk);
    builder.add_node("terms_expert", agent_legal_terms);
    builder.add_node("summarize", agent_legal_summarize);
    builder.add_edge(START, "compliance_expert");
    builder.add_edge("compliance_expert", "risk_expert");
    builder.add_edge("risk_expert", "terms_expert");
    builder.add_edge("terms_expert", "summarize");
    builder.add_edge("summarize", END);
    return builder.compile();


def agent_rd_frontend_breakdown(state):
    # 前端小组拆任务。
    logs = append_log(state, "[R&D·Frontend] broke down frontend tasks: page dev + integration");
    return {V_字段_结果: {"frontend_estimate": "3 person-days"}, V_字段_日志: logs};


def agent_rd_frontend_estimate(state):
    # 前端小组给排期。
    logs = append_log(state, "[R&D·Frontend] frontend schedule output done");
    return {V_字段_日志: logs};


def build_rd_frontend_group():
    # 研发里的前端小组本身也是个小子群（流水线），拿来演示多级嵌套。
    builder = StateGraph(GroupState);
    builder.add_node("breakdown", agent_rd_frontend_breakdown);
    builder.add_node("estimate", agent_rd_frontend_estimate);
    builder.add_edge(START, "breakdown");
    builder.add_edge("breakdown", "estimate");
    builder.add_edge("estimate", END);
    return builder.compile();


def agent_rd_backend_breakdown(state):
    # 后端小组拆任务。
    logs = append_log(state, "[R&D·Backend] broke down backend tasks: API + database");
    return {V_字段_结果: {"backend_estimate": "5 person-days"}, V_字段_日志: logs};


def agent_rd_backend_estimate(state):
    # 后端小组给排期。
    logs = append_log(state, "[R&D·Backend] backend schedule output done");
    return {V_字段_日志: logs};


def build_rd_backend_group():
    # 研发里的后端小组，流水线。
    builder = StateGraph(GroupState);
    builder.add_node("breakdown", agent_rd_backend_breakdown);
    builder.add_node("estimate", agent_rd_backend_estimate);
    builder.add_edge(START, "breakdown");
    builder.add_edge("breakdown", "estimate");
    builder.add_edge("estimate", END);
    return builder.compile();


def agent_rd_lead(state):
    # 星型中心，看需求类型派给前端还是后端。决策写进状态，条件边来读。
    req = state[V_字段_当前请求];
    req_type = req.get(V_字段_载荷, {}).get("req_type", "backend");
    route = "frontend" if req_type == "frontend" else "backend";
    logs = append_log(state, f"[R&D·Lead] req_type \"{req_type}\", dispatched to {route} group");
    return {"route": route, V_字段_日志: logs};


def agent_rd_summarize(state):
    # 小组都跑完了，汇总一下。
    frontend = state.get("frontend_result", {});
    backend = state.get("backend_result", {});
    logs = append_log(state, "[R&D·Summarize] aggregated group schedules into overall plan");
    return {V_字段_结果: {"frontend": frontend, "backend": backend}, V_字段_日志: logs};


def build_rd_subgroup():
    # 研发子群走星型，主管派单；前端/后端小组本身又是子图，大群套小群。
    frontend_graph = build_rd_frontend_group();
    backend_graph = build_rd_backend_group();

    def call_frontend(state):
        # 调前端小组子图，结果写回来。
        # 子图单独记日志（传空列表进去），再跟外层拼，不然日志会加重复。
        sub_input = dict(state);
        sub_input[V_字段_日志] = [];
        out = frontend_graph.invoke(sub_input);
        logs = list(state.get(V_字段_日志, [])) + list(out.get(V_字段_日志, []));
        return {"frontend_result": out.get(V_字段_结果, {}), V_字段_日志: logs};

    def call_backend(state):
        # 调后端小组子图，同上。
        sub_input = dict(state);
        sub_input[V_字段_日志] = [];
        out = backend_graph.invoke(sub_input);
        logs = list(state.get(V_字段_日志, [])) + list(out.get(V_字段_日志, []));
        return {"backend_result": out.get(V_字段_结果, {}), V_字段_日志: logs};

    def lead_route(state):
        # 条件边看 route 字段决定下一站。
        return state.get("route", "backend");

    builder = StateGraph(GroupState);
    builder.add_node("lead", agent_rd_lead);
    builder.add_node("frontend_group", call_frontend);
    builder.add_node("backend_group", call_backend);
    builder.add_node("summarize", agent_rd_summarize);
    builder.add_edge(START, "lead");
    builder.add_conditional_edges("lead", lead_route, {"frontend": "frontend_group", "backend": "backend_group"});
    builder.add_edge("frontend_group", "summarize");
    builder.add_edge("backend_group", "summarize");
    builder.add_edge("summarize", END);
    return builder.compile();


def agent_ops_inspect(state):
    # 运维巡检。
    logs = append_log(state, "[Ops·Inspect] CPU 42%, mem 61%, disk 33%, resources sufficient");
    return {V_字段_结果: {"inspect_status": "ok"}, V_字段_日志: logs};


def agent_ops_evaluate(state):
    # 运维做部署评估。
    logs = append_log(state, "[Ops·Evaluate] deployable, estimate scaling up 2 instances");
    return {V_字段_日志: logs};


def build_ops_subgroup():
    # 运维子群，流水线。
    builder = StateGraph(GroupState);
    builder.add_node("inspect", agent_ops_inspect);
    builder.add_node("evaluate", agent_ops_evaluate);
    builder.add_edge(START, "inspect");
    builder.add_edge("inspect", "evaluate");
    builder.add_edge("evaluate", END);
    return builder.compile();


def build_fault_subgroup():
    # 一个会抛异常的子群，专门用来测兜底。
    builder = StateGraph(GroupState);
    builder.add_node("fault_node", agent_fault_inject);
    builder.add_edge(START, "fault_node");
    builder.add_edge("fault_node", END);
    return builder.compile();


def make_proxy(proxy_name, subgroup_builders, cap_dir, fallback_result=None):
    # 造一个代理节点函数。代理是子群对外的唯一口子（网关）：校验入参、转格式、
    # 调子群、汇总结果，子群崩了兜底，不掺和子群内部怎么跑。
    # 入参 proxy_name 代理名、subgroup_builders {动作: 子群构造函数}、cap_dir 能力目录，
    # fallback_result 是子群崩了的兜底返回（可空）；返回一个吃状态吐状态更新的节点函数。
    if fallback_result is None:
        fallback_result = {"hint": "subgroup error, proxy degraded, please retry later"};

    def proxy(state):
        start = time.time();
        req = state.get(V_字段_当前请求);
        logs = list(state.get(V_字段_日志, []));
        logs.append(f"[{proxy_name}] received request, action={req.get(V_字段_动作)}, trace={req.get(V_字段_追踪ID)}");

        # 先校验动作支不支持，不支持直接降级，别往下走了。
        action = req.get(V_字段_动作);
        if action not in subgroup_builders:
            logs.append(f"[{proxy_name}] validation failed: unsupported action \"{action}\", degraded");
            return _proxy_return(state, req, {"hint": f"unsupported action: {action}"}, V_状态_降级, logs, start);

        # 能力目录没登记也记一笔，但还是试着跑，容错。
        cap_desc = query_capability(cap_dir, proxy_name, action);
        if cap_desc is None:
            logs.append(f"[{proxy_name}] action \"{action}\" not in capability dir, still attempting (fault tolerance)");

        # 顶层请求转成子群认得的入参。
        sub_input = {
            V_字段_当前请求: req,
            V_字段_日志: logs,
            V_字段_黑板: {},
        };

        # 调子群，代理不插手里面的流程。
        try:
            subgroup = subgroup_builders[action]();
            sub_out = subgroup.invoke(sub_input);
            elapsed = round(time.time() - start, 4);
            logs = list(sub_out.get(V_字段_日志, logs));
            logs.append(f"[{proxy_name}] subgroup done in {elapsed}s");

            result = sub_out.get(V_字段_结果, {});
            cross_reqs = list(sub_out.get(V_字段_跨群请求, []));
            if cross_reqs:
                logs.append(f"[{proxy_name}] subgroup produced {len(cross_reqs)} cross-group request(s), forwarded to federation bus");
            return _proxy_return(state, req, result, V_状态_完成, logs, start, cross_reqs);
        except Exception as e:
            # 子群崩了不能拖垮整条链路，降级返回。
            elapsed = round(time.time() - start, 4);
            logs.append(f"[{proxy_name}] subgroup error: {type(e).__name__}: {e}, falling back to degraded");
            return _proxy_return(state, req, fallback_result, V_状态_降级, logs, start);

    return proxy;


def _proxy_return(state, req, result, status, logs, start, cross_reqs=None):
    # 代理统一的返回收口：结果塞结果池、跨群请求入队、日志记耗时。
    # 入参 state/req/result/status/logs/start，cross_reqs 可空；返回要合并进状态的字段字典。
    result_pool = dict(state.get(V_字段_结果池, {}));
    result_pool[req[V_字段_追踪ID]] = {"result": result, V_字段_状态: status, V_字段_来源: req[V_字段_来源], V_字段_目标: req[V_字段_目标], V_字段_动作: req[V_字段_动作]};

    new_queue = list(state.get(V_字段_请求队列, []));
    if cross_reqs:
        new_queue.extend(cross_reqs);
    elapsed = round(time.time() - start, 4);
    logs.append(f"[{req[V_字段_目标]}] request finished, status={status}, total {elapsed}s");
    return {
        V_字段_结果池: result_pool,
        V_字段_请求队列: new_queue,
        V_字段_当前请求: None,
        V_字段_日志: logs,
    };


# 顶层只管代理之间怎么调，不碰子群内部的图。
def bus_node(state):
    # 联邦总线节点：从队列取一个请求派给对应代理，队列空了就收工。
    # 入参 state，返回要合并的状态字段（队列、当前请求、日志）。
    queue = list(state.get(V_字段_请求队列, []));
    logs = list(state.get(V_字段_日志, []));
    if not queue:
        logs.append("[Federation Bus] request queue empty, federation chain ended");
        return {V_字段_日志: logs, V_字段_当前请求: None};
    current = queue[0];
    remaining = queue[1:];
    logs.append(f"[Federation Bus] dispatch -> {current[V_字段_目标]} (action: {current[V_字段_动作]}, trace: {current[V_字段_追踪ID]})");
    return {V_字段_请求队列: remaining, V_字段_当前请求: current, V_字段_日志: logs};


def bus_route(state):
    # 总线的条件路由：看当前请求的目标代理决定下一站，队列空了返回 "done"；入参是 state。
    queue = state.get(V_字段_请求队列, []);
    current = state.get(V_字段_当前请求);
    # 总线派单后会设 current_request；为空说明队列空了，结束。
    if current is None or not current:
        return "done";
    return current.get(V_字段_目标, "done");


def build_federation_bus(proxy_funcs, cap_dir):
    # 拼顶层联邦图：总线加各个代理，代理跑完都回总线，形成对等协作的循环。
    # 入参 proxy_funcs {代理名: 代理节点函数}、cap_dir 能力目录；返回编译好的顶层图。
    builder = StateGraph(GroupState);
    builder.add_node("federation_bus", bus_node);
    for proxy_name, proxy_func in proxy_funcs.items():
        builder.add_node(proxy_name, proxy_func);
    builder.add_edge(START, "federation_bus");
    edge_map = {proxy_name: proxy_name for proxy_name in proxy_funcs};
    edge_map["done"] = END;
    builder.add_conditional_edges("federation_bus", bus_route, edge_map);
    for proxy_name in proxy_funcs:
        builder.add_edge(proxy_name, "federation_bus");
    return builder.compile();


def assemble_midoffice():
    # 组装中台：研发、财务、法务、运维四个子群各配一个代理，都挂到联邦总线上。
    # 无入参，返回 (编译好的联邦图, 能力目录)。
    cap_dir = build_capability_dir();
    proxy_funcs = {
        V_代理_财务: make_proxy(V_代理_财务, {
            V_动作_合同发起: build_finance_subgroup,
            V_动作_合同归档: build_finance_archive_subgroup,
            V_动作_故障注入: build_fault_subgroup,   # 演示兜底用
        }, cap_dir),
        V_代理_法务: make_proxy(V_代理_法务, {
            V_动作_合同评审: build_legal_subgroup,
        }, cap_dir),
        V_代理_研发: make_proxy(V_代理_研发, {
            V_动作_需求排期: build_rd_subgroup,
        }, cap_dir),
        V_代理_运维: make_proxy(V_代理_运维, {
            V_动作_资源巡检: build_ops_subgroup,
        }, cap_dir),
    };
    fed_graph = build_federation_bus(proxy_funcs, cap_dir);
    return fed_graph, cap_dir;


def run_federation_chain(fed_graph, cap_dir, initial_reqs, scenario):
    # 跑一条联邦链路，把执行轨迹和结果池打印出来，返回链路跑完后的最终状态。
    # 入参：fed_graph 联邦图、cap_dir 能力目录、initial_reqs 初始请求列表、scenario 场景名。
    print_separator();
    print(f"! Scenario: {scenario}!");
    print_separator();
    initial_state = {
        V_字段_请求队列: initial_reqs,
        V_字段_结果池: {},
        V_字段_当前请求: None,
        V_字段_日志: [],
        V_字段_能力目录: cap_dir,
    };
    final_state = fed_graph.invoke(initial_state);
    print("! Execution logs: !");
    print_logs(final_state.get(V_字段_日志, []));
    print_separator();
    print("! Result pool: !");
    result_pool = final_state.get(V_字段_结果池, {});
    if not result_pool:
        print("  (no result)");
    for trace_id, entry in result_pool.items():
        print(f"  trace {trace_id}: action={entry.get(V_字段_动作)}, status={entry.get(V_字段_状态)}");
        print(f"    result: {entry.get('result')}");
    print_separator();
    return final_state;


V_架构对比表 = [
    ("Linear pipeline", "Serial, fixed, one-way flow", "Standardized SOP, easy to debug, fits sequential steps"),
    ("Round-table negotiation", "Round-robin, peer speaking", "Multi-view debate, ordered turns, moderator keeps order"),
    ("Star dispatch", "Central manager assigns on demand", "Flexible routing, workers don't talk to each other"),
    ("Hierarchical tree", "Pyramid, decompose upward", "Domain split, scales to large clusters, info aggregates layer by layer"),
    ("Blackboard sharing", "Read/write shared space, state-triggered", "Experts decoupled, contribute autonomously, good for evidence fusion"),
    ("Mesh decentralization", "Peer-to-peer direct, consensus", "No center, no single point of failure, robust but harder to govern"),
    ("Federated subgrouping", "Subgroup autonomy + proxy cross-group, supports nesting", "Groups iterate independently, scales to very large clusters, cross-domain has boundaries"),
];


def print_arch_comparison():
    # 把七种架构的对比表打出来。
    print_separator();
    print("! Multi-agent architecture comparison!");
    print_separator();
    print(f"  {'Architecture':<24}{'Style':<57}{'Key trait'}");
    for arch, style, trait in V_架构对比表:
        print(f"  {arch:<24}{style:<57}{trait}");
    print_separator();


def print_arch_mermaid():
    # 联邦分群的架构图。
    print_separator();
    print("! Federated subgrouping diagram (Mermaid)!");
    print_separator();
    print("  flowchart TD");
    print("    G1[Subgroup A, internal: star] --> A1[Federated Proxy A]");
    print("    G2[Subgroup B, internal: blackboard] --> A2[Federated Proxy B]");
    print("    G3[Subgroup C, internal: pipeline] --> A3[Federated Proxy C]");
    print("    A1 <--> A2");
    print("    A1 <--> A3");
    print("    A2 <--> A3");
    print_separator();


def print_capability_dir(cap_dir):
    # 打一下能力目录，看看各子群都暴露了啥。
    print_separator();
    print("! Global capability directory!");
    print_separator();
    for proxy_name, caps in cap_dir.items():
        print(f"  [{proxy_name}]");
        for action, desc in caps.items():
            print(f"    - {action}: {desc}");
    print_separator();


def main():
    # 程序入口：先打架构图和对比表，再依次跑四个演示场景；无入参无返回值。
    print_separator();
    print("! Federated Clustering Architecture (autonomous nested groups) demo started!");
    print_separator();

    fed_graph, cap_dir = assemble_midoffice();

    # 先把架构图、对比表、能力目录过一遍。
    print_arch_mermaid();
    print_arch_comparison();
    print_capability_dir(cap_dir);

    # 大额合同：财务 -> 法务 -> 财务，跨群往返。
    contract_req = build_request("user", V_代理_财务, V_动作_合同发起, {
        "contract_name": "Core system procurement",
        "amount": 120000,
    });
    run_federation_chain(fed_graph, cap_dir, [contract_req], "Large contract review (cross-group roundtrip: Finance -> Legal -> Finance)");

    # 研发排期：星型派单 + 前后端小组嵌套。
    rd_req = build_request("user", V_代理_研发, V_动作_需求排期, {"req_type": "frontend"});
    run_federation_chain(fed_graph, cap_dir, [rd_req], "R&D requirement scheduling (star dispatch + multi-level nesting)");

    # 运维巡检：子群自己搞定，不跨群。
    ops_req = build_request("user", V_代理_运维, V_动作_资源巡检, {"target": "production cluster"});
    run_federation_chain(fed_graph, cap_dir, [ops_req], "Ops resource inspection (subgroup standalone)");

    # 故意弄个子群故障，看代理兜不兜得住。
    fault_req = build_request("user", V_代理_财务, V_动作_故障注入, {});
    run_federation_chain(fed_graph, cap_dir, [fault_req], "Proxy circuit-breaker fallback demo (subgroup fault fallback)");

    print_separator();
    print("✅ All scenarios finished, federated clustering architecture runs fine!");
    print_separator();


if __name__ == "__main__":
    main();
