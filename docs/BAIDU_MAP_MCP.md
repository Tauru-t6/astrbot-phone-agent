# 百度地图 MCP 对接状态

当前功能由三部分构成：`phone_location` 按需读取手机定位、`GET /map/status` 读取 AstrBot 已注册的百度 MCP 工具元信息、LLM 在当前会话有权限调用 MCP 时完成后续工具调用。状态检查不读取手机位置、不调用百度服务，也不发送坐标。插件不保存定位结果。

## 服务器连接核验

2026-11-10 已在服务器启用 `baidu-map` MCP 并重启 AstrBot。使用该 MCP 自身初始化和 `tools/list` 做连通性检查，未调用 `tools/call`。当时连接成功并发现 10 个工具，包括：

- `map_geocode`
- `map_reverse_geocode`
- `map_search_places`
- `map_place_details`
- `map_directions_matrix`
- `map_directions`
- `map_weather`
- `map_ip_location`
- `map_road_traffic`
- `map_poi_extract`

地图 WebUI 端点根据 AstrBot 注册工具 schema 显示名称、活跃状态、参数名和必需字段，不展示密钥、MCP endpoint 或任意 schema 文本。服务器成功连接不代表所用 Persona 已允许这些工具，也不代表模型会自动选择它们；AstrBot 中仍需为当前 Persona 启用对应 MCP 工具。

## 坐标限制

App 的 Android 原生定位返回 WGS84。Operit 模式保留上游明确返回的坐标系；插件不按坐标范围推断，不会伪造转换字段。百度地图中国大陆逆地理编码要求 BD09LL；百度官方 MCP 的 `map_reverse_geocode` 参数为数字型 `latitude` 与 `longitude`，这两个值必须已是 BD09LL。常见 `map_search_places` 使用的 `location` 参数也必须遵循该工具实际 schema 和坐标要求。

已连接的 MCP 工具清单有逆地理编码和搜索，但没有坐标转换工具。因此当前可以确认百度 MCP 服务连通；App 的 WGS84 位置尚不能直接用于百度国内逆地理编码。WebUI 会把逆地理编码与坐标转换能力分开显示，并阻止误导性的直接交接。要实际查询地址或周边地点，需要先接入并核实坐标转换接口，或使用明确支持 WGS84 输入的服务。不能只把 `coord_type` 字符串改成 `bd09ll`。

## 建议验证步骤

1. 在 WebUI 的“百度地图 MCP 接入”中点击“检查地图接入”。这只检查工具注册状态。
2. 需要获取用户位置时，单独点击“获取一次定位”或经用户明确请求调用 `phone_location`；高精度和地址请求仍需要确认。
3. 确认位置 `fresh=true`、`is_mock=false`、`coord_type` 及 `address_status`。未知或过期位置不用于当前地址判断。
4. 只有转换工具按其 schema 成功返回 BD09LL 后，才调用百度国内逆地理编码或地点搜索工具。

插件不在地图状态检查时预先发起坐标请求；连接检查和定位请求由用户分别触发。位置可能属于敏感数据，因此模型或用户只有在实际提问需要时才继续交给已配置的地图工具。

## App 定位返回值

`location.output` 提供 `latitude`、`longitude`、`coord_type=wgs84`、`captured_at`、基于单调时钟计算的 `age_seconds`、`is_mock`、`provider`、可选 `accuracy_m` 和 `address_status`。当前 App 不实现地址反查，`include_address=true` 时返回 `address_status=not_supported`；这不代表百度查询失败或成功，只表示手机定位提供方未做地址解析。
