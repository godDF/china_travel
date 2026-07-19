# -*- coding: utf-8 -*-

NEXT_POI_TYPE_INSTRUCTION = """
   你是一个旅行规划助手。
   用户需求是：{}。
   当前旅行计划是：{}。
   今天是{}，当前时间是{}，当前位置是{}，POI类型列表是{}。
   请根据用户需求和当前行程选择下一个POI类型。
   请按以下格式回答。
   Thought: [你的推理]
   Type: [POI类型列表中的类型]
    """

INTERCITY_TRANSPORT_GO_INSTRUCTION = """
   你是一个旅行规划助手。
   用户需求是：{user_requirements}。
   现在我们来规划从出发城市到目标城市的交通。
   可用的交通选项有：
   {transport_info}
   你的任务是根据用户需求和提供的交通信息，对所有可用的城际交通选项进行排序。考虑以下因素：
   1. 用户偏好（如类型、舒适度、费用、速度）。
   2. 交通选项的可用性和可靠性。

   请根据用户偏好提供所有交通选项的排序列表。

   对于火车，请包含TrainID。
   对于飞机，请包含FlightID。

   你的回答应遵循以下格式：

   Thought: [你选择交通选项的推理]
   IDList: [按偏好排序的所有ID列表，每个ID为TrainID或FlightID，格式为Python列表。列表最多包含30个元素。]
   """

INTERCITY_TRANSPORT_BACK_INSTRUCTION = """
   你是一个旅行规划助手。
   用户需求是：{user_requirements}。
   现在我们来规划从目标城市返回出发城市的返程交通。
   可用的交通选项有：
   {transport_info}

   此外，以下是去程（出发至目标）的交通信息：
   {selected_go_info}

   你的任务是根据用户需求和提供的交通信息，为返程所有可用的城际交通选项进行排序。考虑以下因素：
   1. 用户偏好（如类型、舒适度、费用、速度）。
   2. 交通选项的可用性和可靠性。
   3. 与去程交通的一致性（例如，如果可能，倾向于使用相同的交通方式）。
   4. 确保在目标城市有足够的时间观光和休闲。

   请根据用户偏好提供所有交通选项的排序列表。

   对于火车，请包含TrainID。
   对于飞机，请包含FlightID。

   你的回答应遵循以下格式：

   Thought: [你对交通选项排序的推理]
   IDList: [按偏好排序的所有ID列表，每个ID为TrainID或FlightID，格式为Python列表。列表最多包含30个元素。]
   """

HOTEL_RANKING_INSTRUCTION = """
   你是一个旅行规划助手。
   用户需求是：{user_requirements}。
   现在我们来选择目标城市的合适酒店。
   可用的酒店选项有：
   {hotel_info}

   你的任务是根据用户需求和提供的酒店信息，对所有可用酒店选项进行排序。考虑以下因素：
   1. 用户偏好（如舒适度、费用、位置）。
   2. 酒店特色。
   3. 每晚房价。
   4. 每间房的床位数（numbed=2表示双床，numbed=1表示单床）。
   5. 靠近目标城市的主要景点或兴趣点。

   此外，请记住用户的预算分配在多项开支中，包括城际交通和每日餐饮。确保酒店推荐在扣除这些费用后仍符合剩余预算。请注意，提供的酒店价格是每间房每晚的费用。如果用户有具体预算要求，请确保酒店住宿总费用（包括城际交通和每日餐饮）不超过预算。为每日餐饮和其他旅行费用预留足够的预算空间。

   请根据用户偏好提供所有酒店选项的排序列表。

   对于每家酒店，请包含名称。

   你的回答应遵循以下格式：

   Thought: [你对酒店选项排序的推理]
   HotelNameList: [按偏好排序的所有酒店名称列表，格式为Python列表]

   示例：
   Thought: 根据用户对舒适度和靠近主要景点的偏好，酒店排序如下：
   HotelNameList: ["酒店1", "酒店2", ...]

   """
ROOMS_PLANNING_INSTRUCTION = """
   你是一个旅行规划助手。
   用户需求是：{user_requirements}。

   你的任务是从用户需求中提取以下信息：
   1. 所需房间数。
   2. 每间房的床位数。

   如果用户需求未指定房间数或房间类型，请将这两个值默认为-1。

   房间类型及其对应床位数：
   - 单床房：1张床
   - 双床房：2张床
   - 双人房（两张单人床）：2张床
   - 大床房：1张床

   你的回答应遵循以下格式：

   Thought: [你提取信息的推理]
   RoomInfo: [房间数, 每间房床位数]
   """


BUDGETS_INSTRUCTION = """
   你是一个旅行规划助手。
   用户需求是：{user_requirements}。

   你的任务是从用户输入中提取预算信息。预算应为数值，货币单位与输入中提到的相同。

   如果用户需求未指定预算，请将值默认为-1。

   请将预算输出为单个数字。

   你的回答应遵循以下格式：

   Budget: [提取的预算数值]
   """

INNERCITY_TRANSPORTS_SELECTION_INSTRUCTION = """
   你是一个旅行规划助手。
   用户需求是：{user_requirements}。

   你的任务是从用户需求中提取偏好的市内交通方式，并根据用户偏好对以下交通选项进行排序：
   1. 地铁
   2. 出租车
   3. 步行

   用户需求可能指定了偏好的交通方式，或提供了有关偏好的提示。如果指定了特定方式，则仅在排序中包含该方式。如果未指定，则根据常识和典型用户偏好排序，默认排序为：["地铁", "出租车", "步行"]

   你的回答应遵循以下格式：

   Thought: [你对交通选项排序的推理]
   TransportRanking: [按偏好排序的交通选项列表，格式为Python列表]
   """
ATTRACTION_RANKING_INSTRUCTION = """
   你是一个旅行规划助手。
   用户需求是：{user_requirements}。
   景点信息是：
   {attraction_info}
   城际交通和酒店住宿的过往花费为：{past_cost}。

   你的任务是根据用户需求和提供的景点信息，选择并排序景点。考虑以下因素：
   1. 景点名称
   2. 景点类型
   3. 位置
   4. 建议游览时长

   此外，请记住用户的预算分配在多项开支中，包括城际交通和酒店住宿。确保景点推荐在扣除过往花费后仍符合剩余预算。

   每天至少推荐8个景点，将所有天的景点合并在一起。为了提供全面的列表，请考虑更大范围的候选景点，并优先考虑景点类型和位置的多样性。

   你的回答应遵循以下格式：

   Thought: [你对景点排序的推理]
   AttractionNameList: [按偏好排序的景点名称列表，格式为Python列表]

   示例：
   Thought: 根据用户对历史遗迹和自然景点的偏好，景点排序如下：
   AttractionNameList: ["景点1", "景点2", ...]
   """

RESTAURANT_RANKING_INSTRUCTION = """
   你是一个旅行规划助手。
   用户需求是：{user_requirements}。
   餐厅信息是：
   {restaurant_info}
   城际交通和酒店住宿的过往花费为：{past_cost}。

   你的任务是根据用户需求和提供的餐厅信息，选择并排序餐厅。考虑以下因素：
   1. 餐厅名称
   2. 菜系类型
   3. 价格范围
   4. 推荐菜品

   此外，请记住用户的预算分配在多项开支中，包括城际交通和酒店住宿。确保餐厅推荐在扣除过往花费后仍符合剩余预算。
   请注意，提供的餐厅价格范围是每人每餐的平均消费，剩余预算必须覆盖{days}天每日三餐的费用。

   每天至少推荐6家餐厅，将所有天的餐厅合并在一起。

   你的回答应遵循以下格式：

   Thought: [你对餐厅排序的推理]
   RestaurantNameList: [按偏好排序的餐厅名称列表，格式为Python列表]
   """


SELECT_POI_TIME_INSTRUCTION = """
   你是一个旅行规划助手。
   用户需求是：{user_requirements}。
   当前旅行计划是：{current_travel_plans}。
   今天是{current_date}，当前时间是{current_time}，当前访问的POI是{current_poi}，其类型为{poi_type}。
   当前POI的建议游览时间为{recommended_visit_time}分钟。

   用户有以下时间约束：
   - 午餐时间：11:00-13:00
   - 晚餐时间：17:00-20:00
   - 返回酒店时间不晚于23:00（如果不是旅行的最后一天）
   - 如果今天是旅行的最后一天，返程交通（火车/飞机）开始时间为{back_transport_time}。

   你的任务是根据用户需求、当前旅行计划和提供的信息，为当前POI选择时间。考虑以下因素：
   1. 用户偏好
   2. 当前旅行计划
   3. POI类型
   4. 当前POI的建议游览时间
   5. 午餐、晚餐和返回酒店的时间约束（如果不是最后一天）
   6. 如果是最后一天，返程交通时间

   POI游览时间的默认值为90分钟，可根据用户需求调整。

   你的回答应遵循以下格式：

   Thought: [你选择POI游览时间的推理]
   Time: [时间（分钟），仅整数值]
   """

nl2sl_prompt = """
你需要从自然语言查询中提取 start_city, target_city, days, people_number，并将自然语言查询转换为 hard_logic。
共有16个 hard_logic（变量名）：
(1) days: 必须等于用户想要旅行的天数。
"days==n" 表示用户想旅行 n 天。
(2) people_number: 必须等于旅行人数。
"people_number==n" 表示有 n 人旅行。
(3) cost: 必须小于或等于用户提供的预算。
"cost<=n" 表示旅行总费用小于或等于 n。
(4) tickets: 一个整数值，表示用户需要购买的票数。
"tickets==n" 表示用户需要购买 n 张票。
(5) rooms: 一个整数值，表示用户需要预订的房间数。
"rooms==n" 表示用户要预订 n 间房。
(6) room_type: 每间房内用户想要预订的床位数。
"room_type==n" 表示用户希望每间房有 n 张床。
(7) hotel_feature: 用户希望预订的酒店特色集合，必须在 ["Kids' Club", 'Air purifier', 'Mountain View Room', 'Private Hot Spring Room', 'Courtyard house', 'hot spring', 'Lakeside Residence', 'e-sports hotel', 'Hot spring bathing', 'Executive Lounge', 'Charging station', 'Designer hotel', 'homestay', 'Lake View Room', 'Stunning Night Views', 'Luggage Storage', 'Chinese-style courtyard', 'Billiards Room', 'Private Pool', 'Fishing', 'Charming sea view', 'Garden Architecture', 'Old Western-style house', "Children's Pool", 'Historic Residence', 'Mahjong and Card Game Room', 'Smart Room Control', "Couple's Room", 'small and beautiful', 'Tea Room', 'Family-themed room', 'Multifunction Hall', 'Laundry room', 'inn', 'Self-operated family room', 'Parking lot', 'Recommended by the Boss', 'River view room', 'Sunbathing area', 'Self-operated entertainment room', 'Kitchen', 'Air conditioning', 'Instagrammable pool', 'Villa', 'Free parking', 'Laundry service', 'Great view from the window', 'Serviced Apartment', 'Conference Hall', 'Family Room', '24-hour front desk', 'Business Center', 'Early Park Entry', 'Farm stay', 'Smart toilet', 'Gourmet Hotel', 'Spa', 'Photogenic', 'Ocean View Room', 'Swimming Pool', 'Media Room', 'Butler Service', 'Airport shuttle service', 'Sauna', 'Robot Service', "Children's Playground", 'Fitness Room', 'Washing machine', 'Self-operated Comfort Sleep Room', 'Pet-friendly', 'e-sports room', 'Excellent location', 'Suite'] 中。
"{'A'}<=hotel_feature" 表示用户希望预订的酒店具有特色 A。
(8) hotel_price: 必须小于或等于用户提供的酒店价格（每晚均价）。
"hotel_price<=n" 表示酒店价格小于或等于 n。
(9) intercity_transport: 城际交通方式集合，必须在 ['train','airplane'] 中。
"intercity_transport=={'train'}" 表示用户希望乘坐火车前往目的地。
(10) transport_type: 市内交通方式集合，必须在 ['metro','taxi','walk'] 中。
"transport_type<={'A'}" 表示用户希望在市内乘坐 A 交通方式。
(11) spot_type: 用户希望游览的景点类型集合，必须在 ['Museum/Memorial Hall', 'Art museum', 'Red tourism sites', 'natural scenery', 'Cultural Landscape', 'University campus', 'historical site', 'Amusement Park/Sports Entertainment', 'Garden', 'Other', 'Cultural Tourism Area', 'park', 'commercial district'] 中。
"{'A', 'B'}<=spot_type" 表示用户希望游览景点类型 A 和 B。
(12) attraction_names: 用户希望游览的景点名称集合。
"{'A', 'B'}<=attraction_names" 表示用户希望游览景点 A 和 B。
(13) restaurant_names: 用户希望光顾的餐厅名称集合。
"{'A', 'B'}<=restaurant_names" 表示用户希望光顾餐厅 A 和 B。
(14) hotel_names: 用户希望预订的酒店名称集合。
"{'A'}<=hotel_names" 表示用户希望预订酒店 A。
(15) food_type: 用户希望品尝的美食类型集合，必须在 ['Yunnan cuisine', 'Tibetan cuisine', 'Northeastern Chinese cuisine', 'Barbecue', 'Asian cuisine', 'Cantonese cuisine', 'Northwestern Chinese cuisine', 'Fujian cuisine', 'Hakka cuisine', 'Fast food and casual dining', 'Sichuan cuisine', 'Taiwanese cuisine', 'Other', 'Halal cuisine', 'Snacks', 'Western cuisine', 'Vegetarian cuisine', 'Japanese cuisine', 'Jiangsu-Zhejiang cuisine', 'Hubei cuisine', 'Southeast Asian cuisine', 'Hunan cuisine', 'Beijing cuisine', 'Korean cuisine', 'Seafood', 'Middle Eastern cuisine', 'fusion cuisine', 'Teahouse', 'Bar/Pub', 'Creative Cuisine', 'buffet', 'coffee shop', 'Shanghai cuisine', 'Huizhou cuisine', 'Latin American cuisine', 'Shandong Cuisine', 'Xinjiang cuisine', 'Farmhouse cuisine', 'Hainan cuisine', 'Hot pot', 'Bakery and Desserts', 'Other Chinese Cuisine'] 中。
"{'A', 'B'}<=food_type" 表示用户希望品尝美食类型 A 和 B。
(16) food_price: 必须小于或等于用户提供的餐饮价格（每餐人均）。
"food_price<=n" 表示餐饮价格小于或等于 n。
你的回答必须是合法的 json 格式。请注意 hard_logic 的格式和下面的示例。
(17) taxi_cars: 一个整数值，表示用户需要的出租车数量。可按 `(people_number+3)//4` 计算。
(18) activity_start_time: 活动开始时间。
(19) activity_end_time: 活动结束时间。
(20) activity_time: 活动持续时长。
如果旅行只有一天，应忽略 rooms 和 room_type。如果某些约束不在上述提及，你可以将它们添加到 hard_logic 中。
"""

nl2sl_example = "示例：\n"

nl2sl_example_1 = """
自然语言：我目前在上海。我和女朋友计划去苏州玩两天，预算1300元。我们想要一个单床房酒店，每晚价格不超过500元。请提供旅行行程。
答案：{'start_city': "Shanghai", 'target_city': "Suzhou", 'days': 2, 'people_number': 2, 'hard_logic':  ['days==2', 'people_number==2', 'cost<=1300', 'hotel_price<=500', 'tickets==2', 'rooms==1', 'room_type==1', 'taxi_cars==1']}
"""
nl2sl_example_2 = """
自然语言：我们目前在上海。我们三个人计划去北京玩两天，想在北京全聚德（前门店）用餐。我们的预算是6000元，需要两间双床房。请提供旅行行程。
答案：{'start_city': "Shanghai", 'target_city': "Beijing", 'days': 2, 'people_number': 3, 'hard_logic': ['days==2', 'people_number==3', 'cost<=6000', "{'Beijing Quanjude (Qianmen Branch)'} <= restaurant_names", 'tickets==3', 'rooms==2', 'taxi_cars==1','room_type==2']}
"""
nl2sl_example_3 = """
自然语言：我目前在重庆。我打算一个人去杭州玩两天，乘坐高铁（G），预算3000元。我喜欢自然风光，想住一个带智能客房控制的单床房酒店。我更喜欢每餐人均不超过100元，并尽可能乘坐地铁。请提供旅行行程。
答案：{'start_city': 'Chengdu', 'target_city': 'Hangzhou', 'days': 2, 'people_number': 1, 'hard_logic': ['days==2', 'people_number==1', 'cost<=3000', 'tickets==1', 'rooms==1', 'room_type==1', "intercity_transport=={'train'}", "{'natural scenery'}<=spot_type", "{'Smart Room Control'}<=hotel_feature", 'food_price<=100', "transport_type<={'metro'}" ]}
"""
nl2sl_example_4 = """
自然语言：我目前在苏州。我和朋友们计划去北京玩三天，预算8000元。我们将乘坐火车，想品尝北京菜，并参观故宫。我们更喜欢有管家服务的酒店。
答案：{'start_city': 'Suzhou', 'target_city': 'Beijing', 'days': 3, 'people_number': 2, 'hard_logic': ['days==3', 'people_number==2', 'cost<=8000', 'tickets==2', , 'taxi_cars==1', "intercity_transport=={'train'}", "{'Beijing cuisine'}<=food_type", "{'The Palace Museum'}<=attraction_names", "{'Butler Service'}<=hotel_feature"]}
"""


class NL2SL_INSTRUCTION:
    def __init__(self):
        pass

    @classmethod
    def format(cls, nature_language):
        return (
            nl2sl_prompt
            + nl2sl_example
            + nl2sl_example_1
            + nl2sl_example_2
            + nl2sl_example_3
            + nl2sl_example_4
            + "\n示例结束。"
            + "\nnature_language: "
            + nature_language
            + "\nlogical_constraints: "
            + nature_language
            + "\n"
        )


nl2sl_prompt_v2 = """
你需要从自然语言查询中提取 start_city, target_city, people_number, days，并将自然语言查询转换为 hard_logic。
你需要从自然语言查询中提取 hard_logic，并将其格式化为 Python 代码。每个 hard_logic 应为一个 Python 代码块，最终结果应为布尔值。
我们将提供一些原子变量和函数来帮助你转换。你可以将它们组合成 hard_logic，只要它们是合法的 Python 表达式。

!!! 你必须将最终结果存储在变量 `result` 中，以便我们可以从变量 `result` 获取最终结果。!!!
!!! 注意，对于某些 hard_logic，你必须根据活动类型选择活动。!!!

变量：
(1) plan: 包含具体计划信息的字典。

函数：
(1) day_count(plan)
文档：获取计划中的天数。
返回：int
(2) people_count(plan)
文档：获取计划中的人数。
返回：int
(3) target_city(plan)
文档：获取计划的目标城市。
返回：str
(4) allactivities(plan)
文档：获取计划中的所有活动。
返回：活动列表
(5) activity_cost(activity)
文档：获取特定活动的费用（不含交通费）。
返回：float
(6) activity_position(activity)
文档：获取特定活动的地点名称。
返回：str
(7) activity_type(activity)
文档：获取特定活动的类型。可选值：['breakfast', 'lunch', 'dinner', 'attraction', 'accommodation', 'train', 'airplane']
返回：str
(8) activity_tickets(activity)
文档：获取特定活动所需的票数。适用于：['attraction', 'train', 'airplane']
返回：int
(9) activity_transports(activity)
文档：获取特定活动的交通信息。
返回：字典列表
(10) activity_start_time(activity)
文档：获取特定活动的开始时间。
返回：str
(11) activity_end_time(activity)
文档：获取特定活动的结束时间。
返回：str
(12) innercity_transport_cost(transports)
文档：获取市内交通的总费用。
返回：float
(13) metro_tickets(transports)
文档：如果交通类型为地铁，获取地铁票数。
返回：int
(14) taxi_cars(transports)
文档：如果交通类型为出租车，获取出租车数量。我们假设出租车数量为 `(people_count(plan) + 3) // 4`。
返回：int
(15) room_count(activity)
文档：获取住宿活动的房间数。
返回：int
(16) room_type(activity)
文档：获取住宿活动的房间类型。1：大床房，2：双床房
返回：int
(17) restaurant_type(activity, target_city)
文档：获取目标城市中餐厅的菜系类型。我们仅支持 ['Yunnan cuisine', 'Tibetan cuisine', 'Northeastern Chinese cuisine', 'Barbecue', 'Asian cuisine', 'Cantonese cuisine', 'Northwestern Chinese cuisine', 'Fujian cuisine', 'Hakka cuisine', 'Fast food and casual dining', 'Sichuan cuisine', 'Taiwanese cuisine', 'Other', 'Halal cuisine', 'Snacks', 'Western cuisine', 'Vegetarian cuisine', 'Japanese cuisine', 'Jiangsu-Zhejiang cuisine', 'Hubei cuisine', 'Southeast Asian cuisine', 'Hunan cuisine', 'Beijing cuisine', 'Korean cuisine', 'Seafood', 'Middle Eastern cuisine', 'fusion cuisine', 'Teahouse', 'Bar/Pub', 'Creative Cuisine', 'buffet', 'coffee shop', 'Shanghai cuisine', 'Huizhou cuisine', 'Latin American cuisine', 'Shandong Cuisine', 'Xinjiang cuisine', 'Farmhouse cuisine', 'Hainan cuisine', 'Hot pot', 'Bakery and Desserts', 'Other Chinese Cuisine']。
返回：str
(18) attraction_type(activity, target_city)
文档：获取目标城市中景点的类型。我们仅支持 ['Museum/Memorial Hall', 'Art museum', 'Red tourism sites', 'natural scenery', 'Cultural Landscape', 'University campus', 'historical site', 'Amusement Park/Sports Entertainment', 'Garden', 'Other', 'Cultural Tourism Area', 'park', 'commercial district']。
返回：str
(19) accommodation_type(activity, target_city)
文档：获取目标城市中住宿的特色。我们仅支持 ["Kids' Club", 'Air purifier', 'Mountain View Room', 'Private Hot Spring Room', 'Courtyard house', 'hot spring', 'Lakeside Residence', 'e-sports hotel', 'Hot spring bathing', 'Executive Lounge', 'Charging station', 'Designer hotel', 'homestay', 'Lake View Room', 'Stunning Night Views', 'Luggage Storage', 'Chinese-style courtyard', 'Billiards Room', 'Private Pool', 'Fishing', 'Charming sea view', 'Garden Architecture', 'Old Western-style house', "Children's Pool", 'Historic Residence', 'Mahjong and Card Game Room', 'Smart Room Control', "Couple's Room", 'small and beautiful', 'Tea Room', 'Family-themed room', 'Multifunction Hall', 'Laundry room', 'inn', 'Self-operated family room', 'Parking lot', 'Recommended by the Boss', 'River view room', 'Sunbathing area', 'Self-operated entertainment room', 'Kitchen', 'Air conditioning', 'Instagrammable pool', 'Villa', 'Free parking', 'Laundry service', 'Great view from the window', 'Serviced Apartment', 'Conference Hall', 'Family Room', '24-hour front desk', 'Business Center', 'Early Park Entry', 'Farm stay', 'Smart toilet', 'Gourmet Hotel', 'Spa', 'Photogenic', 'Ocean View Room', 'Swimming Pool', 'Media Room', 'Butler Service', 'Airport shuttle service', 'Sauna', 'Robot Service', "Children's Playground", 'Fitness Room', 'Washing machine', 'Self-operated Comfort Sleep Room', 'Pet-friendly', 'e-sports room', 'Excellent location', 'Suite']。
返回：str
(20) innercity_transport_type(transports)
文档：获取市内交通的类型。我们仅支持 ['metro', 'taxi', 'walk']。
返回：str
(21) innercity_transport_tickets(activity)
文档：获取市内交通的票数。
返回：int

回答的 json 格式如下：
"""

example_nl2sl_v2 = """
示例：

nature_language:
我目前在上海。我一个人计划去杭州坐火车玩一天，预算1500元。请提供旅行行程。
answer:
{
"start_city": "Shanghai",
"target_city": "Hangzhou",
"days": 1,
"people_number": 1,
"hard_logic_py": ["result=(day_count(plan)==1)","result=(people_count(plan)==1)","total_cost=0 \nfor activity in allactivities(plan): total_cost+=activity_cost(activity)+innercity_transport_cost(activity_transports(activity))\nresult=(total_cost<=1500)","result=True\nfor activity in allactivities(plan):\n  if activity_type(activity) in ['attraction', 'airplane', 'train'] and activity_tickets(activity)!=1: result=False\n  if innercity_transport_type(activity_transports(activity))=='metro'and metro_tickets(activity_transports(activity))!=1: result=False","result=True\nfor activity in allactivities(plan):\n  if innercity_transport_type(activity_transports(activity))=='taxi'and taxi_cars(activity_transports(activity))!=1: result=False","intercity_transport_set=set()\nfor activity in allactivities(plan):\n  if activity_type(activity) in ['train', 'airplane']: intercity_transport_set.add(intercity_transport_type(activity))\nresult=(intercity_transport_set=={'train'})"],

}

nature_language:
我目前在广州。我和两个朋友计划去成都玩三天。我们只乘坐地铁，并入住成都明月酒店。请提供旅行行程。
answer:
{
"start_city": "Guangzhou",
"target_city": "Chengdu",
"days": 3,
"people_number": 3,
"hard_logic_py": [
"result=(day_count(plan)==3)","result=(people_count(plan)==3)","result=True\nfor activity in allactivities(plan):\n  if activity_type(activity) in ['attraction', 'airplane', 'train'] and activity_tickets(activity)!=3: result=False\n  if innercity_transport_type(activity_transports(activity))=='metro'and metro_tickets(activity_transports(activity))!=3: result=False","result=True\nfor activity in allactivities(plan):\n  if innercity_transport_type(activity_transports(activity))=='taxi'and taxi_cars(activity_transports(activity))!=1: result=False","accommodation_name_set=set()\nfor activity in allactivities(plan):\n  if activity_type(activity)=='accommodation': accommodation_name_set.add(activity_position(activity))\nresult=({'Minya Hotel'}<=accommodation_name_set)","innercity_transport_set=set()\nfor activity in allactivities(plan):\n  if activity_transports(activity)!=[]: innercity_transport_set.add(innercity_transport_type(activity_transports(activity)))\nresult=(innercity_transport_set<={'metro'})"],
}

nature_language:
我目前在上海。我和朋友计划去北京玩三天，预算6000元。我们在市内只乘坐地铁，偏好单床房。请提供旅行行程。
answer:
{
"start_city": "Shanghai",
"target_city": "Beijing",
"days": 3,
"people_number": 2,
"hard_logic_py": ["result=(day_count(plan)==3)","result=(people_count(plan)==2)","total_cost=0 \nfor activity in allactivities(plan): total_cost+=activity_cost(activity)+innercity_transport_cost(activity_transports(activity))\nresult=(total_cost<=6000)","result=True\nfor activity in allactivities(plan):\n  if activity_type(activity) in ['attraction', 'airplane', 'train'] and activity_tickets(activity)!=2: result=False\n  if innercity_transport_type(activity_transports(activity))=='metro'and metro_tickets(activity_transports(activity))!=2: result=False","result=True\nfor activity in allactivities(plan):\n  if innercity_transport_type(activity_transports(activity))=='taxi'and taxi_cars(activity_transports(activity))!=1: result=False","result=True\nfor activity in allactivities(plan):\n  if activity_type(activity)=='accommodation' and room_count(activity)!=1: result=False\n  if activity_type(activity)=='accommodation' and room_type(activity)!=1: result=False","innercity_transport_set=set()\nfor activity in allactivities(plan):\n  if activity_transports(activity)!=[]: innercity_transport_set.add(innercity_transport_type(activity_transports(activity)))\nresult=(innercity_transport_set<={'metro'})"],
}

nature_language:
"""


class NL2SL_INSTRUCTION_V2:
    def __init__(self):
        pass

    @classmethod
    def format(cls, nature_language):
        nature_language = nature_language.strip().replace("\n", "")
        return nl2sl_prompt_v2 + example_nl2sl_v2 + nature_language + "\nanwser:"