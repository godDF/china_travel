---
name: chinatravel-agent-env
description: 当需要借助本地 agent_env CLI 解决或查询 ChinaTravel 基准测试题目时使用，包括加载测试查询、检索景点/餐厅/酒店/交通信息、生成行程 JSON 并对已解题进行评估。
---

# ChinaTravel Agent 环境

当任务要求你使用本地 `agent_env` 命令行界面解决或检查 ChinaTravel 基准测试查询时，请使用本技能。

## 核心原则

优先使用结构化 CLI 工具。仅当结构化工具目录无法覆盖所需查询时，才直接使用 `world` 命令。

在仓库根目录下执行命令：

```bash
python -m agent_env.cli tools
python -m agent_env.cli splits
python -m agent_env.cli call <tool_name> '<json_arguments>'
python -m agent_env.cli world "<WorldEnv command>"
CLI 返回 JSON 格式结果。在依赖 data 字段之前，请先检查 success 是否为 true。

常见查询操作
列出可用的查询分片（split）：

bash
python -m agent_env.cli call china_travel_list_splits
加载查询元数据：

bash
python -m agent_env.cli call china_travel_load_query '{"split":"easy"}'
python -m agent_env.cli call china_travel_load_query '{"split":"easy","uid":"<uid>"}'
查看可用资源列（字段）：

bash
python -m agent_env.cli call attractions_keys '{"city":"上海"}'
python -m agent_env.cli call restaurants_keys '{"city":"上海"}'
python -m agent_env.cli call accommodations_keys '{"city":"上海"}'
筛选资源：

bash
python -m agent_env.cli call attractions_select '{"city":"上海","key":"name","op":"contains","value":"博物馆"}'
python -m agent_env.cli call restaurants_select '{"city":"上海","key":"cuisine","op":"eq","value":"本帮江浙菜"}'
python -m agent_env.cli call accommodations_select '{"city":"上海","key":"price","op":"le","value":500}'
查找附近资源：

bash
python -m agent_env.cli call attractions_nearby '{"city":"上海","point":"上海迪士尼度假区","topk":5,"dist":5}'
python -m agent_env.cli call restaurants_nearby '{"city":"上海","point":"上海迪士尼度假区","topk":5,"dist":2}'
python -m agent_env.cli call accommodations_nearby '{"city":"上海","point":"上海迪士尼度假区","topk":5,"dist":5}'
查询交通信息：

bash
python -m agent_env.cli call intercity_transport_select '{"start_city":"北京","end_city":"上海","intercity_type":"train","earliest_leave_time":"07:00"}'
python -m agent_env.cli call goto '{"city":"上海","start":"上海站","end":"上海迪士尼度假区","start_time":"09:00","transport_type":"metro"}'
务必使用工具返回的确切值（价格、ID、名称、时间、距离、交通段等）。不要自行编造 POI 名称或交通细节。

输出约定
仅返回符合 chinatravel/evaluation/output_schema.json 格式的 JSON 行程单。顶层对象必须包含：

people_number

start_city

target_city

itinerary

每个活动（activity）必须包含：

type

start_time

end_time

price

cost

transports

城际活动还需包含 start、end、tickets 以及 TrainID 或 FlightID。景点活动需包含 position 和 tickets。住宿活动需包含 position、room_type 和 rooms。

分片自动化
使用捆绑的测试套件加载指定分片，以非交互方式调用测试套件，将生成的方案保存在 results/<method>/<uid>.json 下，并进行评估：

bash
python agent_env/scripts/solve_script_with_harness.py --split easy
有用选项：

bash
python agent_env/scripts/solve_script_with_harness.py --split easy --uid <uid>
python agent_env/scripts/solve_script_with_harness.py --split easy --harness opencode --model dashscope/qwen3.6-27b
python agent_env/scripts/solve_script_with_harness.py --split easy --harness codex --model gpt-5.5
python agent_env/scripts/solve_script_with_harness.py --split easy --timeout 900
python agent_env/scripts/solve_script_with_harness.py --split easy --limit 1
python agent_env/scripts/solve_script_with_harness.py --split easy --no-run-harness
该测试套件会对所选模型隐藏验证器（oracle verifier）字段，但内部保留以进行硬约束评估。除非指定 method 覆盖，否则结果目录默认为 <model>-<split>-<harness>。

