# v1.5.0 模块化迁移、验证与回退

## 当前边界：渐进抽离，不是完整重写

本文描述当前接口与迁移约束。此次尚未重载运行中的插件，未做真实付费出图验收，不能宣称线上已生效；AstrBot 实机、QQ 空间发布及全部历史行为仍需受控端到端验收。交付以实际代码和当次测试结果为准。

| 模块 | 当前职责与限制 |
| --- | --- |
| [主入口](<main.py>) | AstrBot 命令与生命周期适配、业务编排、状态宿主、用户交互等仍保留较多逻辑；不是无业务逻辑的纯适配器。 |
| [默认预设](<selfie_core/defaults.py>) | 保留旧角色与世界预设以兼容旧配置，不等于所有预设已改成外部数据文件。 |
| [角色配置](<selfie_core/character.py>)、[自定义画像](<selfie_core/portraits.py>) | 单个活动角色的配置快照、参考图路径选择、自定义提示词构造。没有多角色同时切换 UI。 |
| [城市目录](<selfie_core/geography.py>) | 不依赖 AstrBot 的 WorldProfile、不可变 CityCatalog，坐标与邻接按城市隔离；不查询真实铁路班次。 |
| [世界适配](<selfie_core/world.py>) | 地理、场景推进、天气缓存等旧行为的宿主 mixin，仍依赖宿主状态和部分 AstrBot 能力，不是完全独立的纯模拟器。 |
| [天气边界](<selfie_core/weather.py>) | Open-Meteo 请求与当前天气结构校验；缓存、降级与地点归属由调用方负责。 |
| [图片服务适配](<selfie_core/image_transport.py>) | ImageTransportMixin 承载 OpenAI 兼容主站/副站的服务计划、payload 与生成实现；已删除旧硅基流动专用协议，仍是宿主 mixin。 |
| [工作台服务](<selfie_core/workspace.py>)、[平台 API](<selfie_core/workspace_api.py>)、[四页界面](<views/selfie/index.html>) | 设置迁移、草稿确认、revision 冲突、owner 隔离与后台 job；使用 AstrBot 原生页面/认证，不另开服务。 |
| [素材仓库](<selfie_core/materials.py>) | 稳定编号、单行草稿、显式确认、原子备份与文件冲突检测；构造/读取不自动迁移素材。 |
| [城市编辑](<selfie_core/world_editor.py>) | NBS 2023 历史行政名称校验、50 地点草稿验证、模拟拓扑与省级路径规则；不证明地点/坐标/车站真实性。 |
| [生活引擎](<selfie_core/living.py>) | 居家、午餐、阶段事件、实际活动记录及确定性日记；不补编离线生活。 |
| [任务与用例](<selfie_core/jobs.py>) | TaskSupervisor、PhotoRequest、PhotoUseCase；主入口通过薄 `_generate` 门面调用共享用例，底层生成器可替换。 |
| [状态存储](<selfie_core/storage.py>) | 顶层 JSON 对象兼容读取与同目录临时文件原子替换；不提供跨进程事务或数据库。 |
| [公共图片下载](<selfie_core/network.py>) | 独立无鉴权下载、公共 IP/DNS 检查、逐跳重定向校验、流式限额与图片签名筛查；不是完整图像解码器或网络隔离沙箱。 |
| [环境补充](<selfie_core/scene_adaptation.py>) | 仅追加受限 JSON 环境说明，失败保持原提示词；词法过滤不能保证模型语义绝对安全。 |
| [时钟与随机源](<selfie_core/runtime.py>) | SystemClock/RandomSource 注入边界，支持离线可重复测试。 |

## v1.5.0 工作台与配置迁移

工作台有生图测试、添加素材、世界设置、事件编辑四个页签。生成素材/城市草稿后需编辑检查并显式确认；草稿按 owner 隔离，30 分钟失效，重复确认幂等。城市确认使用生成时的 workspace revision，不能覆盖期间保存的新数据。生成与测试会调用所配置的 provider，离线单元测试使用替身，不等于真实效果验收。

[配置 schema](<_conf_schema.json>)当前保留 53 项，以下 9 项由工作台接管：`home_city`、`home_district`、`travel_cities`、`costume_file`、`closeup_prompt_file`、`interact_prompt_file`、`city_catalog_file`、`district_file`、`scene_file`。首次初始化将旧值及迁移键记录到角色数据目录的 `workspace.json`；不删除原配置或原素材。已存在工作台状态时读取保存值，不再把旧配置覆盖回来。未知版本/损坏文件拒绝覆盖；原子写入和进程内锁不是跨进程事务。

此次衣服库已有 118 条显式稳定编号；编号不能混入衣服名称/别名，新增条目不重排旧编号。互动由新的[互动素材](<interaction.txt>)读取；旧互动文件已迁移到私有备份，不随插件发布。实际素材迁移须显式执行，不由 WorkspaceService 构造自动改写。

家庭房间包括厨房、餐厅、客厅、卧室、书房、阳台；默认每日 50% 留家至 14 点，11–12 点做饭、12–13 点吃饭，菜谱含 50 道菜。大上海/夜间音乐活动迁移为可编辑阶段事件，保留 40% 概率及 18 点至次日 7 点窗口，阶段服装改为显式可编辑定义，不再承诺原随机穿搭完全一致。事件地点独立于普通活动池。

日记是实际 record 的确定性汇总，不调用 LLM 润色。30 秒后台循环在午夜后写入昨日日记；离线时段不补编。最近 7 份日记（每条最多 700 字）通过原 `selfie_memory_enabled` 门控注入聊天供查读，不代表完整逐小时历史重放。

## 旧默认与自定义角色

配置项以[配置 schema](<_conf_schema.json>)为准，不要把真实配置、API key 或个人参考图当作示例提交。

- `character_*` 全部留空时使用 legacy 预设及既有参考图优先级，旧运行数据位置保持原语义。
- 任一角色字段非空即进入 custom 模式；缺失字段不再自动继承旧角色身份或旧参考图。路径字段只是管理员配置，不是上传组件或公开上传 API。
- `character_reference_body`、`character_reference_head`、`character_reference_single` 使用绝对路径或相对插件目录的路径。请自行准备有权使用的同一角色素材；模块不会自动合成人脸/身体参考图。
- custom 运行数据写入基础数据目录下 `characters/<身份键>/`，避免旧状态、照片记忆直接混入新身份。身份键由角色身份字段和部分参考图路径决定；这不是图片内容哈希，也不表示文件换内容后能自动识别新身份。
- 活动角色在实例初始化时取快照。修改名字、描述、参考图等身份配置后，应停止/重载插件；不要期望在线修改配置立即安全切换 namespace。服装或声音的变化不一定创建新 namespace。
- 更换身份不会自动迁移旧状态。返回原身份配置可重新使用原 namespace；迁移前需人工确认归属并单独备份。
- 缺少参考图时默认不允许纯文生图。是否启用 `allow_text_only` 是明确的质量策略选择，不应通过自动回退旧角色图片掩盖配置错误。

## 家、旅行与城市目录

`home_city`/`home_district` 留空保留旧家乡默认值；修改城市时必须同时配置其区县。`travel_cities` 显式空列表表示禁用自动旅行；缺省语义与 schema 默认值保留旧旅行目标。

`city_catalog_file` 指向 UTF-8 JSON（可绝对路径，也可相对插件目录）。下面是**虚构结构示例**，坐标仅用于说明格式，不可据此发布真实天气/路线信息：

```json
{
  "示例甲城": {
    "districts": {
      "中心区": {"lat": 30.0, "lon": 110.0, "neighbors": ["东区"]},
      "东区": {"lat": 30.1, "lon": 110.1, "neighbors": ["中心区"]}
    },
    "stations": ["中心区"]
  },
  "示例乙城": {
    "districts": {
      "中心区": {"lat": 31.0, "lon": 111.0, "neighbors": []}
    },
    "stations": ["中心区"]
  }
}
```

约束：

- 顶层键为城市；每城需非空 `districts`。纬度/经度必须是有限数字，分别在 ±90/±180 内。
- `neighbors` 和 `stations` 为区名列表，引用必须存在于同城。目录邻接按输入保存；世界移动策略可能按双向邻接使用。
- `stations` 表示有车站的区域，不是实际站名。`has_route` 仅判断不同配置城市的两端是否配置车站区，不证明真实线路或班次存在。
- 工作台可按城市名生成恰好 50 个地点草稿，分布在多个真实区县，经验证并确认后保存；也可使用迁移保留的 `scene_file` 与城市目录。行政名称依据 NBS 2023 离线历史资料，不保证当前最新；生成的双向连通邻接、坐标、地点及车站都是模拟数据，非权威行政边界或实地核验信息。
- 城市最低停留天数为正整数。首次迁移/旧场景缺省家乡 10 天、非家乡 3 天；原郑州 60 小时向上取整为 3 天，不是完全等价时长。新生成城市按用户设置保存。省级最短邻接路径折算旅行小时属于游戏规则，不查询真实班次。
- 场景地点格式为 `地点|类型|名称|描述|区县|城市`，文案格式为 `文案|类型|模板`。请对照[世界解析器](<selfie_core/world.py>)和已有场景格式准备内容，保持城市、区名一致。未指定城市的旧格式具有兼容默认语义，不适合混放多个新城市。
- `district_file` 的旧格式是 `区名|纬度|经度|相邻区`；它按区名索引，不能可靠区分不同城市同名区。新多城市配置应使用 CityCatalog，不要把旧扁平区域文件当作跨城完整数据模型。

## 可替换服务接口

### 生成与任务

```python
request = PhotoRequest(
    prompt="...", model="...", style=None,
    negative_extra=("blur",), extra_images=("synthetic-reference.png",),
)
use_case = PhotoUseCase(generator, store=None)
image_bytes = await use_case.execute(request)
```

`generator` 是异步可调用对象，接收 `prompt/model/style/negative_extra/extra_images` 关键字；后两项会复制为新 list，空值传 None，以兼容旧生成函数。可选 `store(request, image_bytes)` 支持同步/异步；存储错误传播，已发生副作用不会回滚。所有需要共同串行的入口必须共享同一个 PhotoUseCase 实例；不是全局/跨进程锁。不要把调用自身的 `_generate` 包装器再次注入为底层 generator。

`TaskSupervisor(on_error=None).spawn(coro)` 返回 Task；`await close()` 拒绝新任务、取消并等待已有任务。同步错误回调只接收固定脱敏信息，不接收异常私密文本；取消正常传播。应由宿主关闭 supervisor，不能由托管任务自等待关闭。

### 状态、环境、时间

- `JsonStateStore(path).load(defaults)`：深复制 defaults，缺文件返回默认值，已有 JSON 对象进行顶层浅合并，保留未知字段；损坏数据抛错。宿主禁止在加载失败后覆盖原状态。
- `save(state)`：先序列化再 flush/fsync/replace；同实例线程锁不代表跨进程锁，调用方仍需避免并行修改传入状态。磁盘断电持久性也不等于文件系统级事务保证。
- `await adapt_scene_prompt(prompt, scene, chat_callable, timeout=45)`：调用 `chat_callable(prompt=...)` 并读取 `completion_text`，仅接受三个环境字段；成功追加，原提示词完整保留，失败返回原文，取消传播。
- 宿主 `clock`/`rng` 可注入替身；默认 SystemClock 使用主机本地时区，未新增任意时区配置。RandomSource 默认保留标准库全局随机流；测试可注入 `random.Random(seed)`，不是密码学随机数。

### 生图站配置（v1.4.1）

现在只保留 OpenAI 兼容的主站与副站，早期生图站（硅基流动、Kolors、Qwen 私有参数）的配置项和代码分支已删除：

- `api_base`/`api_key`：主站，必须显式配置，不再从其他 provider 借用凭据。
- `api_base_backup`/`api_key_backup`：可选副站，主站模型链全失败后只尝试一次。
- `model`、`image_model_fallback`：主站模型及备用模型链。
- `image_size`、`image_max_refs`、`image_max_refs_backup`、`image_min_gap_seconds`：请求参数。
- `allow_text_only`：编辑模型缺参考图时是否允许退回纯文生图，默认关闭。

旧的 `api_style`、`negative_prompt`、`num_inference_steps`、`cfg`、`guidance_scale`、`watermark_off`
已从配置表单与代码中移除。它们即便还残留在实际配置文件里也不再被读取；不承诺界面保存会自动删除旧键。
这些参数只在已删除的旧协议分支里生效过，OpenAI 兼容接口不使用它们。

v1.4.2 进一步删除额度探针：`quota_probe_enabled`/`quota_probe_model` 及 `_probe_quota` 方法已移除，
生图全部失败时直接把最后一次异常上抛，不再额外发一次文字请求探测余额。错误判定 `_is_quota_error` 保留，
用于区分「余额不足/密钥失效」与普通失败。

同一版本把容易误解的标题改为中性说法：`ref_image`/`ref_image_real`/`ref_image_real_single` 显示为
「内置动漫参考图」「内置真人参考图」「内置单图参考」——它们只在保留插件自带角色（`character_*` 全空）时生效；
`district_file` 显示为「区域坐标文件」，未配置城市目录时用它查天气坐标与相邻区。

## 验证与尚未完成的部分

在插件根目录，使用现有含 aiohttp 的实例 venv，避免安装或升级线上依赖：

```powershell
& '<实例目录>\venv\Scripts\python.exe' -B -m unittest discover -s tests -p 'test_*.py' -v
```

测试数量会变化，以当次完整输出为准，不将固定数量写成验收承诺。离线测试使用合成数据/替身，不应读取个人图、真实配置或发出付费/发布请求。运行单元测试不是启动插件，也不能代替管理员批准后的实际生命周期、真实 provider、权限、发图与发布链路验证。

目前仍保留 mixin、旧状态结构、较多主入口业务编排和默认预设。尚未提供完整 Web 角色上传 UI、所有端到端场景验收、跨进程任务队列、完整存储事务或绝对语义安全保证。外部模型/天气服务变化、旧状态与新城配置冲突、参考图不可读等仍可能导致运行时失败。先离线验证，再在可回退条件下进行受控人工验收。

### 本轮刻意改变的安全默认与兼容限制

- 照片记忆默认仅原会话可见；`selfie_memory_cross_context=true` 才恢复旧全局共享。角色生活场景仍是实例共享的。
- 自定义角色公开发空间需 `custom_qzone_enabled=true`；旧默认角色策略保持。
- 编辑模型缺图/身份主图读取失败不再静默退成别的角色；`allow_text_only` 控制编辑链兜底。仅支持 OpenAI 兼容主站/副站，旧 SiliconFlow 专用生图分支已删除。
- 默认旧角色输出保持兼容；自定义角色使用通用服装策略，不继承原角色专属JK/汉堡服饰。可配置自定义睡衣、学校服、冬装。
- 工作台更改家的城市或区县时，只要已有城市/场景/旅行等生活状态便明确拒绝，要求先备份并显式迁移或重置。修改目录也须人工确认旧状态中的城市/区仍可解析；不猜测迁移或静默瞬移。
- 世界适配器已接入生活事件、每城最低停留与省级模拟路径，但不是实时交通引擎。无有效车站/落脚点的城市不应加入自动旅行列表。
- 静态时间辅助函数仍部分使用系统时间；实例场景与多数业务调用已可注入时钟，尚不提供任意时区的完整排程保证。

## 回退：先停止，再恢复，默认保留最新运行数据

扩展前的完整私有备份保存在本机私有备份目录（不在插件仓库内），包含 `plugin/`、配置快照与状态快照。本说明不记录本机绝对路径，也未读取或披露其配置值。备份与本机路径**不属于公开发布内容**，对外发布前应移除本机维护信息，绝不附带私有快照。文件备份不等于应用级一致性快照。

1. 停止 AstrBot 实例，确认没有旧进程、定时任务或插件后台任务继续写入。
2. 在受控私有位置另备份当前 plugin、实际配置、最新 runtime，记录本次回退时间，避免把升级后的新数据永久覆盖。
3. 从扩展前备份恢复 **plugin 与对应实际 config**（使用备份中的插件目录与私有配置快照）。先核对源/目标绝对路径，再按已确认清单替换；不要把旧版本不认识的新代码残留当作成功回退。工作台保存后旧 config 不会自动同步回写，需将当前工作台数据另外留存；不要把新设置丢失误认成恢复失败。
4. **默认保留最新 runtime**，防止丢失备份之后的新照片、计数、状态和其他活动。不要为了回退代码就顺手恢复整份旧运行数据。
5. 若旧版确实无法读取新 runtime，先保留最新副本，再单独评估并恢复必要运行数据。**恢复完整旧 runtime 会失去备份之后的活动与新照片记录**；这是数据回退，不是无损操作。custom namespace 在旧版中可能不可见，保留它们等待迁移，不要删除。
6. 核查代码、配置与运行目录对应关系后启动，先检查日志/状态读取，不要立即触发付费生图或空间发布。发现不兼容则停止并从刚保存的当前副本恢复，另行处理迁移。

## v1.5.20 动作覆写优先级修正

- `main._photo_action()` 判断顺序改为：**事件当前动作 > 睡觉 / 吃饭（平级）> 默认自拍**。事件进行中不再被睡觉/吃饭盖掉；睡觉与吃饭场景互斥，保持平级即可。
- `test_main_boundaries.test_photo_action_priority_event_over_sleep_meal_then_default` 同步覆盖四种来源与顺序；总计 419 项通过。

## v1.5.20 动作覆写优先级、阶段名称与配文润色

- `main._photo_action()` 判断顺序改为：**事件当前动作 > 睡觉 / 吃饭（平级）> 默认自拍**。事件进行中不再被睡觉/吃饭盖掉；睡觉与吃饭场景互斥，保持平级即可。
- 事件阶段新增可选 `phase_name`（与事件 `name` 区分，避免合并 dict 时撞名）：`event_materials.normalize_phase_name()` 清洗单行/限长 24 字，`phase_label()` 读取；`living.validate_event()` 校验类型和长度；`workspace.save_events()` 保存前清洗；`generate_event_fragment` 新增 `phase_name` 字段（6–12 字短名，AI 总结用）。
- 优先级取值链：阶段名称 → 阶段场景短名（`scene_label()`：素材 name 不可读时从场景描述前半句提取）→ 动作/事件短名。日程计划项、观察记录、自拍配文共用这条链；`record()` 额外保存 `phase_index`/`phase_name`/`place`。
- `world._scene_caption_tail()` 增加配文润色：有阶段名称用「在流水线组装」，有场景短名用「在电子厂公司食堂」，事件标题兜底时不加「在」；`main._look_caption()` 把穿着写成「穿防尘工装」。
- 测试：阶段名称校验/清洗/AI 摘要、`scene_label`/`event_label` 去修饰、配文润色、优先级；总计 424 项通过。

## v1.5.19 统一动作覆写与事件 active 结构修复

- `event_materials.active_event(state)`：统一读取当前事件计划（优先 `state['living']['active']`，兼容旧版顶层 `state['active']`）。此前 `main._event_photo_scene()` 与 `world._scene_reusable()` 都直接读顶层 `state['active']`，运行时永远拿不到 active，导致事件动作不生效、配文沿用旧缓存里的长提示词。
- `main._photo_action(scene, expression, gesture, night_kind)`：唯一的「谁顶替默认自拍动作」判断，优先级 **睡觉 > 吃饭/做饭 > 事件当前动作 > 默认自拍**，返回 `{source, intro, framing, pose, face_line, light_line, tail, custom_text}`；`_selfie_prompt()` 只消费这份结果，睡觉/吃饭/事件不再各写一套分支。
- `selfie_core/sleep.py`：新增三种入睡拍法（普通床 / 青旅上下铺 / 网吧包厢）的构图、动作、关灯要求与 `sleep_cue()`；`defaults.SELFIE_SHOT_INTRO` 统一自拍开头语。
- `main._event_photo_scene()`：事件阶段没有动作随机池时，退回当前阶段主动作正文，活动名仍只用短名，保证「事件动作一定顶替自拍姿势」。
- `world._scene_caption_tail()`：超长 `activity`（>24 字）压成短概括，配文永不携带整段生图提示词。
- `workspace._test()` / `views/selfie/index.html`：生图测试动作来源新增 `sleep` + `sleep_kind`（bed/bunk/cyber），「动作 / 特写」只列三种过夜拍法；提交、确认弹窗、结果信息都显示所选拍法。
- 测试：`tests/test_sleep.py` 新增睡觉模板/测试 cue 用例；`test_main_boundaries` 增加统一覆写优先级、事件 active 嵌套结构、没有动作池的事件、睡觉/吃饭提示词替换；`test_world_integration` 增加配文兜底；总计 419 项通过。

## v1.5.18 事件动作单套化、日程概括与菜品补种

- `event_materials.py`：新增 `summarize_action()`、`usable_action_name()`、`usable_action_text()`、`action_name()`、`action_name_for_text()`、`event_label()`。动作素材的 `text` 是长篇生图提示词，`name` 才是「逛商场」这种短名字；从旧数据迁移来的条目 name 是提示词前 24 字，会被识别为不可用并退回事件名当概括。日程、照片配文只用短名字——**这条规则对所有事件、所有阶段通用**：短名字 → 手写短动作 → 事件名（截 15 字）→ 概括，任一环节拿不到人话就退到「活动现场 / 外出活动」，绝不把提示词原样带出去。
- `living.py`：`_event_scene()` 的 `activity` 改放短名字，完整动作正文放进 `event_action`（仅 `phase == active` 时）；`schedule()` 的计划项用动作短名，观察记录用 `_observed_activity()` 在读取时把旧的长提示词压成概括；`photo_variation()` 返回值增加动作短名（第三项）。
- `main.py`：`_event_photo_scene()` 把抽到的动作写进 `variant['event_action']`、`activity` 只留短名；准备阶段 / 结束场景不再套用阶段动作池。`_do_closeup()` 不再用事件动作覆盖特写 cue（事件内特写/互动只发点单的特写提示词）。`_selfie_prompt()` 有 `event_action` 时用它顶替默认「伸手自拍」姿势与构图，去掉原来追加的「她此刻正在做的事：<整段提示词>」，并同时承担 `event_action` 时的 9:16 照片开头语。
- `meal.py` / `workspace.py`：`meal_cue(..., setup=N)` 支持指定 5 套吃饭机位之一；生图测试 payload 新增 `meal_setup`（0–4，越界按随机）。
- `materials.py` / `main.py`：新增 `SEED_VERSIONS` 与 `merge_seed()`：升级时按名称把随包新增的默认条目补进数据目录文件（菜品 9 → 53 道），同一版本只补一次，用户改过 / 自建 / 删除的条目不受影响；`main.__init__` 在 `ensure_files()` 之后调用。
- `views/selfie/index.html`：动作来源选「吃饭」时，「动作 / 特写」下拉改为 5 套吃饭机位（`#t-closeup` 复用），并加提示文字；测试提交带 `meal_setup`，结果信息显示所选机位。
- `meals.txt`：默认从 9 道扩到 53 道（与 `living.MEALS` 一致，每道约 100 字，包含菜、主食、餐具与桌面）。
- 测试：新增事件短名 / 日程概括 / 默认素材补种 / 吃饭机位 / 事件自拍提示词（AST 隔离执行 `_selfie_prompt`）用例；总计 407 项通过。

## v1.5.17 菜品素材、数据目录与日志器合规

- `materials.py`：新增第四类 `meal`（`meals.txt`，格式 `菜名 | 描述`）；新增 `ensure_files()` 在首次加载时把插件包里的默认素材复制到数据目录；`MaterialStore` 支持 `fallback_root`：数据目录缺文件时只读回退插件包默认值，写入始终落在数据目录。
- `main.py`：`_material_dir()` / `_resolve_material_path()` 统一素材路径；`_meal_library()` 按 mtime 热加载菜品；吃饭/做饭提示词按菜名引用菜品描述。
- `workspace.py`：`_store_for()` 统一素材库根目录为数据目录；生图测试 `action_source` 支持 `meal` + `meal_id`，用 `meal.meal_cue()` 生成吃饭提示词；新增 `_MEAL_INSTRUCTION`（约 100 字、写菜/主食/餐具）。
- `workspace_host.py`：去掉 `import logging` 兜底，日志器只依赖 `astrbot.api.logger`（审核要求）。
- `world.py`：`_scene_reusable()` 在 00:30 分界处不再复用同小时旧场景，保证「关灯」生效。
- `views/selfie/index.html`：素材类型增加「菜品」，生图测试动作来源增加「吃饭（菜品库）」及菜品下拉；菜品草稿提示、列表、编辑、删除复用现有流程（支持文字 / 参考图 / 两者）。
- 测试：新增 `tests/test_meal_material.py`、`tests/test_bed_boundary.py`；总计 398 项通过。

## v1.5.16 吃饭 / 做饭提示词（selfie_core/meal.py）

- 新增纯函数模块 `selfie_core/meal.py`：`meal_photo(scene, rng)` 在 `living_phase` 为 `cook` / `lunch` / `lunch_out` 时返回机位、动作和「饭菜必须入画」的补充要求；`meal_photo_prompt()` 供自定义角色路径使用整段文字版。
- 5 套吃饭机位 + 5 套做饭机位，全部与菜品解耦，换菜单不用改提示词；菜名取 `scene['meal']`，缺失时从活动文字里解析。
- 主食搭配 `STAPLE_RULES`：炒菜 / 炖菜配米饭和筷子；面 / 粉 / 馄饨本身是主食可干吃；饺子配醋、粥配小菜、三明治配饮料、汉堡炸鸡配可乐薯条。
- `living.py`：午餐场景从「餐厅」改为「厨房」，`meal_table=True` 时在 `_emit` 里把「厨房靠窗的小餐桌 + 摆好的饭菜」补进 `desc`；`MEALS` 从 50 道扩到 53 道（新增汉堡套餐 / 炸鸡配可乐 / 披萨）。
- `main._selfie_prompt()`：命中吃饭 / 做饭时替换默认「伸长手臂自拍」姿势，改用 meal 的构图与动作，并在场景描述后追加 `meal['tail']` 的硬性要求；自定义角色走 `meal_text()` 追加。
- 测试：新增 `tests/test_meal.py` 13 项（分类、搭配、5 套模板、每道菜格式化、外食 / 做饭 / 非吃饭阶段），`test_living` 更新菜单数与午餐房间断言；总计 390 项通过。

## v1.5.15 旧世界导入的「|区|城市」后缀修复与清理

- 解析修复：[legacy_world.py](<selfie_core/legacy_world.py>) 的 `read_places()` 不再把第 5、6 列拼进描述：≥6 列时 `place = "|".join(parts[3:-2])`、`district/city = parts[-2:]`；5 列时最后一个是区、城市用 `default_city`；4 列时没有区/城市。描述里真的含 `|` 的行会保留在描述中。旧实现 `"|".join(parts[3:])` 会把「…步道|未央区|西安」整体写进 `place`，进而进生图提示词。
- 存量清理：`WorkspaceService._clean_legacy_place_suffixes()` 在 `initialize()` 的「已有 workspace.json」分支执行（`_import_legacy_world()` 之后）：只处理 `cities[*].metadata.source == "legacy_files"` 的城市，逐条比对 `place` 是否以 `f"|{district}|{city}"` 结尾（区存在但城市为空时比对 `|{district}`），只删这个后缀并 `rstrip()`，正文为空则保留原值；真的改动才 `revision += 1` 并落盘，同时写 `migration.legacy_place_suffix_cleaned(_at)`。幂等：清理过的数据再次加载不再命中，不会反复提升 revision。
- 边界：手工生成/编辑的城市（`metadata.source` 不是 `legacy_files`）一律不碰；只改 `place` 字段，不动 `name/district/city/kind`，地点条数与 `events` 原样保留。
- 验证：新增 [tests/test_legacy_world.py](<tests/test_legacy_world.py>) 覆盖 4/5/6/7 列解析、描述中的 `|`、表头/短行忽略，以及清理的只改后缀、非 legacy 城市不动、revision 只涨一次、重复加载幂等；用线上数据副本实测清理 119 条，地点条数与事件 JSON 不变。

## v1.5.14 生图测试事件动作枚举与名称显示

- 前端 `eventActionsForTest(event)` 改为枚举「动作素材库全部条目 + 各阶段 `action_pool` / `action_ref` 引用的动作（含 `source: global` 的全局特写）」，按提示词去重；旧数据只有内联动作文本时仍作为兜底列出。之前只收 `action_ref`（事件主动作 + 每阶段主动作），因此 7 个动作的事件在测试页只出现 3 个。
- 选项从纯提示词字符串改为对象 `{key, name, text, label}`：`label` 形如「动作名称：提示词开头」；名称缺失、名称等于提示词、或名称本身是一段提示词片段（前缀命中或长共同前缀）时只显示提示词，避免同一段话出现两遍。`#t-closeup` 的 change 处理器与 `renderTestMaterialSources()` 同步按对象取值，选中后仍只把 `text` 写回 `#t-action`。
- 新增 `testEventActionName(text)`：确认生成弹窗与测试结果信息显示「动作：事件动作「名称」 N 字」。
- 服务端契约不变：测试的 `event_action` 仍是动作文本，`_event_actions()` 的兜底语义不动，本次纯前端。

## v1.5.12 事件随机池（场景 / 动作 / 服装）

- 数据模型：事件素材本身不变，新增可选字段 `event.outfit_pool`（事件服装随机池引用数组）与阶段的 `scene_pool`（场景 id 数组）、`action_pool`（作用域引用数组）、`outfit_pool`（作用域引用数组）、`outfit_mode`（`fixed` / `event` / `inherit`）、`outfit_inherit`（沿用第几个阶段，必须小于当前阶段）。`scene_ref` / `action_ref` / `outfit_ref` 继续作为主素材保留，因此旧事件零迁移、旧版本插件可读（忽略额外字段），清空勾选即回到固定玩法。
- 解析边界：[event_materials.py](<selfie_core/event_materials.py>) 的 `resolve_event()` 在深拷贝上把池引用解析成 `scene_pool_items` / `action_pool_items` / `outfit_pool_items` **只读副本**，原始引用数组保持原样，WebUI 仍按稳定 id 编辑；坏引用（池里引用已删除素材）整体报 `ValueError`，`workspace.events()` 会把该事件标成 `material_error` 并停用，不进入半坏运行态。
- 运行期：[living.py](<selfie_core/living.py>) 的 `validate_event()` 校验池形状与「沿用只能指向更早阶段」；`photo_variation(event, phase_index, rng)` 纯函数负责每张照片从阶段场景池/动作池各抽一条（准备阶段用第一段、结束场景用最后一段）；`LivingEngine._phase_outfit()` 负责服装解析与缓存：`event` 模式整场只摇一次（缓存在 `active['rolled_outfit']`），`inherit` 递归取更早阶段实际穿的那套，`fixed` 走原来的 `outfit_ref`；子事件内部没有服装随机池。
- 生成侧：`main._event_photo_scene()` 在拍照时给场景副本换背景并返回抽到的动作；自拍把动作写进提示词（「她此刻正在做的事」），特写把动作作为本次 cue 覆盖特写池要求，QQ 空间配图同样走随机池。日程/日记/聊天背景仍用第一个勾选的主场景，不因每次拍照随机而变化。事件结束后的「在附近休息」（`living_phase == "end"`）是独立一幕，不套用阶段池。
- WebUI：事件素材显示「场景 N / 内部编号」；阶段卡片改用可搜索引用框（`buildRefCombobox` / `buildRefPool`）逐条添加，输入关键字即时过滤、点条目选中、右侧「添加」再长一行，服装模式用单选切换并即时重渲染子控件；「事件服装随机池」放在素材库下方；每段有「预览这一抽」按钮（纯前端随机，不生成图片、不写数据）。前端校验覆盖池非空、场景 id 存在、沿用阶段只指向更早阶段。旧的 `buildPhaseModeRow` / `phaseMode` / `hasOutfitValue` 与 `__actionMode` / `__outfitMode` 内部状态已删除。

## v1.5.11 生图测试简化、相册大图与 LivingMemory 作用域修复

- 前端删除 `t-mode` 单选与 `state.testMode`，场景类型改由地点选择推导：`testSceneMode()` 按 `state.testSelection.kind` 返回 `home` / `overnight` / `event` / `daily`，`handleTest()` 直接把它作为 payload 的 `mode`。服务端 `_test()` 的 `mode` 契约不变，旧调用方式仍可用。
- 「服装来源」「动作来源」保留为两个独立下拉，`costume_source` / `action_source` 逻辑不变；「事件素材来源」下拉只在某个来源为 `event` 时显示，地点本身是事件场景时锁定为同一事件（服务端 `_pick_event_costume` 仍只接受该事件的服装）。
- 场景输入框下方新增 `#t-scene-hint` 动态说明（当前是日常地点 / 家 / 过夜 / 事件场景），替代原来独立的模式提示；删除 `updateTestModeHint`、`syncTestModeUI`、`setTestMode`、`applyEventTextsToForm`，统一为 `syncTestPanel()`。
- 相册大图改为「图片元素按内容尺寸居中 + overlay 黑底」：单击图片不再立即关闭（延迟 260ms，双击会取消），只有点到图片外的黑色区域才关闭；双击以点击处为锚点放大到 2.6 倍、再双击还原；滚轮改为按光标位置连续缩放（上放大 / 下缩小，范围 1–6 倍，`no-anim` 关闭过渡避免滚轮发飘），放大后拖动带边界约束。
- LivingMemory 日记作用域修复：user 模式的 scope 是 `livingmemory:user:<平台类型名>:<发送者ID>`，旧实现从 `umo` 推断时把平台配置 ID 当成了平台名，写出 `livingmemory:user:unknown:小汉堡:friendmessage:...` 的孤立作用域，导致日记在 LivingMemory 里看不到（只在 2D 拓扑图里是个孤立点、记忆管理列表里没有）。现在 `_remember_livingmemory_target()` 记录 `platform` / `sender_id`，`_livingmemory_platform_name()` 用 `context.platform_manager`（实例 meta 或 `platforms_config` 的 id→type）把配置 ID 映射回类型名，`_livingmemory_scope(..., platform, sender_id)` 优先用记录值再调 LivingMemory 自己的 `resolve_memory_scope`。
- 兼容旧账本：`_livingmemory_target_meta()` 同时接受 float 时间戳与 dict；同步时把同一天、不在当前 scope 集合里的账本条目（旧版本写错的 `unknown` 作用域）通过 `delete_memory` 删掉再按正确作用域重写，删除失败则留到下轮重试，避免 LivingMemory 里堆积看不见的重复记忆（`delete_memory` 会同时清理索引、documents、graph 与 atoms）。
- 生活循环恢复整点推进：新增 `world._scene_reusable()` 整点闸门，同一钟点内复用 `state['scene']`，只有到站（`pending_target.arrive_at` 到点）、事件开始/阶段切换/结束这些真实切换才提前重算，避免后台每 30 秒轮询重掷地点并写入观察记录（日程「每分钟一个观察点」、角色 30 秒换一个地方）。
- 生活循环空转不落盘：`_living_loop` 只在真的生成了新日记（`engine.diary()` 写入 diaries 缓存）时 `_save_state()`；场景切换由 `_get_scene`/`_workspace_commit_scene` 自己保存。30 秒心跳不再每轮重写约 150KB 的 state.json。
- 新增 `workspace_host._compact_observation_records()`：加载时一次性清理旧版轮询产物——某小时记录 ≥4 条才判定为轮询重复，同一小时只保留第一条；正常整点记录不受影响。`_living_loop` 每个进程只执行一次。
- 素材文件确认：`costumes.txt` / `closeup_prompts.txt` / `interaction.txt` 仍由 `MaterialStore` 与聊天学习流程读写，是运行时素材库；`districts.txt` / `scenes.txt` 只在旧实例首次加载时导入 `workspace.json`，之后不再读取，发布包也不包含。

## v1.5.10 日记五段式整理

- 新增 `selfie_core/diary.py`（纯函数）：`section_for()` 按本地时辰归入上午(5-11)/中午(11-14)/下午(14-18)/晚上，`build_prompt()` 生成只允许使用实际记录的整理提示词，`clean_diary()` 校验必须包含五个固定开头，`fallback_diary()` 在模型失败时用确定性规则重排（午饭评价按日期哈希挑选，避免每天一样）。
- host 侧：`_compose_diary()` 调 `_workspace_llm`，任何异常/不合格输出都回落到 `fallback_diary()`；`_reformat_diaries()` 把旧格式文件（`formatted != True`）整理后写回 `{text, raw, formatted, format_fallback}`，最近 7 天、600 秒节流；整理过的日子用 `_sync_diary_to_livingmemory(force=True)` 先删旧记忆再写新格式。
- 日记文件结构追加 `raw` / `formatted` / `format_fallback`；`text` 仍是对外唯一正文，工作台与 LivingMemory 同步都读它。

## v1.5.9 工作台后台任务与跨插件日记同步

- 任务不再依赖页面存活：bootstrap 之外的前端把 job_id 记在 localStorage，关页/刷新后重新接管轮询；服务端任务本就运行在 asyncio 后台，只受 `_ACTIVE` 单用户并发与 15 分钟 TTL 限制。相册列表返回 `{items, total}`，前端每页 24 张按 offset 拉取。
- 生图测试把「场景模式」与「素材来源」拆成两个维度：payload 新增 `costume_source` / `action_source` / `event_costume` / `event_action`，服务端 `_test()` 先解析场景（日常/事件/家/过夜），再分别解析服装与动作；事件素材只能取该事件已解析的服装（`_pick_event_costume` 校验），日常素材走素材库编号。
- 日记同步复用 LivingMemory 的公开写入接口：`_livingmemory_plugin()` 通过 `context.get_registered_star` 拿实例并检查 `initializer.is_initialized`、`memory_engine.index_writer`；scope 优先调用其 `memory_scope.resolve_memory_scope`，拿不到模块时按 `memory_scope_mode` 自行推导；写入用 `add_memory(preserve_create_time=True)` 并把 `create_time` 设为当天 23:50；幂等账本在 `state.json` 的 `livingmemory_diary_synced`。任何异常只 warning，绝不打断生活循环或自拍主流程。

## v1.5.8 世界数据单一来源

插件不再随包发布任何城市、地点、事件或区域坐标：defaults.py 只保留通用模板与家居场景，districts.txt / scenes.txt 仅作为旧实例的导入源。workspace.json 是城市、区县拓扑、地点与事件的唯一运行时来源；legacy_world.import_legacy() 只在首次加载时把旧文件按邻接连通分量拆成多座城市并写入，已存在的城市不覆盖。新实例首次设定家时由 initialize_world 现场生成 50 个地点，校验通过才一次性提交；失败不写入。素材库的衣服/特写/互动支持 update / delete（带备份、编号稳定、被事件引用的条目拒绝删除），特写编号按文件编号取值而不是行号。

## 安全发布检查

[忽略规则](<.gitignore>)只减少误提交，不会移除已经被 Git 跟踪的文件，也不会阻止手工压缩带入隐私内容。发布采用源代码/文档/schema/运行素材的明确白名单，并检查暂存区、历史与最终包：

- 排除真实 API key、账号/群号、实际 config/state、照片记忆、用户生成图片、个人 assets、日志、缓存与全部私有备份。
- 不公开备份 manifest 明细；不要将真实运行目录作为发布包根目录整体打包。
- 角色参考图需用户自行提供或经过授权单独分发；不要把本机个人素材当作示例。
- 不使用 `*.json` 全局忽略；[配置 schema](<_conf_schema.json>)和经过脱敏的源示例 JSON 应继续可发布。
- 若秘密曾进入 Git 历史，添加 ignore 不足以修复；应立即撤销/轮换凭据并按团队流程清理历史。
