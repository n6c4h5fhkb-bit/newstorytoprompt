# StoryForge Lite

把小说或定稿剧本转成 AI 视频提示词交付包的本地工具：

小说 → 分集方案 → 剧本与状态 → 美术定调 → 人物、场景与道具 → 分镜 → 参考映射 → 最终 Prompt → 独立文字审查 → 文本交付。

默认使用已登录的 **Codex CLI / `gpt-6-sol` / `high`**。每次 `codex exec` 使用独立文本包和 JSON Schema，不续接模型会话；配置在 [config.yaml](config.yaml)。平台只交付文本和待配图占位符，媒体生成、剪辑和发布由用户在外部工具完成。

也可以改用已登录的 Claude Code CLI 作为模型：在项目的 `project.yaml`（或 `config.yaml`）里写

```yaml
models:
  strong:   {provider: claude_cli, model: sonnet, effort: medium, timeout_seconds: 900}
  cheap:    {provider: claude_cli, model: haiku, effort: low, timeout_seconds: 600}
  reviewer: {provider: claude_cli, model: sonnet, effort: low,    timeout_seconds: 900}
```

每次调用是一次无工具、无会话、无设置的 `claude -p`（结构化输出），指令来自 `skills/`，调用记录和费用写入 `logs/calls.jsonl`。可用 `SFL_CLAUDE_COMMAND` 指定可执行文件。

完整目标尚未完成。当前真实首集已经用户确认并锁定，美术方案和资产提取已完成；资产 Prompt 部分完成，最近一次因模型服务满载暂停。正式分镜、最终视频 Prompt 和真实生成反馈仍待完成。见 [验收核对](docs/requirements-audit.md)和 [实施说明](docs/implementation.md)。

## 安装与启动

需要 Python 3.11+、Git 和已安装登录的原生 Codex CLI。Python 依赖为 PyYAML。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe cli.py doctor
.\.venv\Scripts\python.exe cli.py serve --port 8765
```

打开[本地界面](http://127.0.0.1:8765)。若当前 Python 已安装依赖，可直接使用 `python cli.py doctor` 和 `python cli.py serve`。

`doctor` 在本机检查 CLI 路径、版本、凭证目录、登录状态和模型配置；其输出可能包含本机信息，不应加入 Git。调用继承现有 `CODEX_HOME`，未设置时使用当前用户的 `.codex`。Windows 会解析 npm CLI 对应的原生执行文件，也可用 `SFL_CODEX_COMMAND` 指定路径。

## 本地界面

- **项目**：创建故事、导入文本、开始或暂停。
- **待处理**：处理首集确认、美术确认、预算与重试；候选稿上方直接显示必须修复的问题，可展开看改法和依据。
- **分集**：查看美术定调、资产 Prompt 和最终逐镜 Prompt，编辑参考，下载交付，记录生成反馈。
- **设置**：模型、思考强度、预算、并发和阈值。

重试卡的备注选填，已有审查意见自动带入。点击“按审查意见重试”保存决定，再点击“继续运行”启动作业。普通网页继续保留已选分集，并在下一项人工确认处等待；故障重试同时保留停止阶段和分集范围。

演示采用固定响应，明确标注，不计入真实调用或质量指标。演示中的完整 Prompt 可以在视觉确认前阅读，预览不会替用户批准、锁定或记录交付。

## 从小说开始

准备 UTF-8 文本：

```powershell
python cli.py new my_novel --novel novel.txt
python cli.py run my_novel --episodes 1 --until A8
python cli.py inbox my_novel
```

原文按章段编号，摘要和规划保留引用。关键时刻生成三版并独立选优，普通放大时刻一版。首集确认后锁定，再进行导演流程；后续集按上一集结束状态、末场和风格示例顺序续写。

使用实际卡片 ID 回答：

```powershell
python cli.py --project my_novel answer CARD_ID approve
python cli.py run my_novel --episodes 1 --until B9
```

若需修改，回答 `reject --note '具体修改要求'`；技术修订卡使用 `retry` 或 `stop`。卡片不会超时批准。CLI 每次 `run` 使用该次参数；未指定范围时运行完整流程，并保存范围供网页续跑。

## 导入定稿剧本

需要三份文本，固定协议样例在 [测试剧本](tests/fixtures/golden_project/ep01.md)、[设定](tests/fixtures/golden_project/bible.md)和 [起始状态](tests/fixtures/golden_project/ep00.md)：

- 剧本：每场包含“出场：”行。
- 人物与世界设定：名称、地点、外观、声音和既定事实。
- 起始状态：第一集开始时的知情、持有物、人物关系和伏笔。

```powershell
python cli.py new my_drama
python cli.py import my_drama --episode ep01.md --bible bible.md --ledger-in ep00.md
python cli.py run my_drama
python cli.py inbox my_drama
```

导入表示已采用该剧本，不重复小说项目的首集审批；仍须确认美术。已有资产描述可附加 `--assets assets.csv`。名称和列格式须符合 [资产列定义](platform/storyforge/store/__init__.py)。

## 资产与最终 Prompt

人物母图描述脸型、五官、发型、体型、衣装材质和稳定辨识点；可使用头部特写与正侧背三视图。场景可用空场正反打或多视角，道具可用结构四宫格。布局按用途调整，不把拼版构图带入视频。

人物与可拆卸道具优先分开。佩戴、持有、开合和落点用镜头状态描述，同一物件不因多张参考增加数量；显著换装、年龄或伤势仍可做子图。美术补充等待视觉确认，不作为新剧情事实。

每段最终视频 Prompt 包含总时长、共用美术、表演重点、环境与光线、空间布局、参考职责和逐镜动作、对白时间、声音。完整规则见 [提示词设计](docs/prompt-design.md)。

通过审查后，交付位于 `projects/my_drama/delivery/ep01/`：

- `uNN.md`：最终 Prompt 与参考映射。
- `assets.md`：美术定调、共用风格、色卡及母子资产 Prompt，按制作依赖排序。
- `overlays.md`：后期文字与时间。
- `manifest.json`：时长、引用、审查、警告和版本指纹。

下载前检查文件完整性和版本，交付包仅包含清单文件。参考或使用中的资产变更会使相关交付失效；代码不会为满足参考上限而静默删除场内道具。

## 修改、恢复与反馈

```powershell
python cli.py pause my_drama
python cli.py --project my_drama note ep01 '强化结尾悬念，保留既定事实'
python cli.py --project my_drama note ep01_u01 '她已坐下，同一件道具仍留在桌角'
python cli.py --project my_drama rerun B7 ep01_u02 --note '镜头2改成慢推近景，其余保持'
python cli.py --project my_drama rerun B5 ep01 --note '第一段拆成两个镜头'
python cli.py --project my_drama adopt ep01_u02          # 采纳你手改过的 prompts/ep01/u02.md（先过硬检查）
python cli.py --project my_drama asset @云清禾_母图 --identity-notes '成年女子，青绿眼睛，灰白破裙'
python cli.py history my_drama ep01                      # 列出可回滚的快照，再用 revert
python cli.py export my_drama ep01
python cli.py --project my_drama feedback ep01_u01 ok
python cli.py --project my_drama feedback ep01_u02 redo --note '具体问题' --generations 2 --user-minutes 8
python cli.py --project my_drama finish ep01 --user-minutes 35
```

`config.yaml` 的 `fix_policy.open_major` 决定自动修复两轮后仍未解决的 major 意见怎么办：`card`（默认，出卡等你决定）或 `accept`（把意见记录下来继续走：分镜与剧本写入产物记录，文字重构审查写进交付的“审查提示”，blocker 和硬检查错误仍然出卡）。真实模型下审查几乎每一轮都会挖出新的次要点，建议在 `project.yaml` 里设 `accept`，见 [真实模型验证记录](docs/real-run-claude-cli.md)。场景审查给出的误判剧本意见可用 `dismiss <note_id>` 驳回（`notes/script_notes.json` 里找 id，网页分集页有“驳回这条意见”）。

手改 Prompt：直接编辑 `prompts/epNN/uNN.md` 后运行 `adopt`（或在网页分集页“直接编辑这一段 Prompt”保存）。通过硬检查才会采纳；之后文字重构审查只记录意见，不再覆盖你的写法，需要时 `rerun --note` 会以你的版本为基础修改。`asset` 可单独改某个资产的描述、身份要点或生成 Prompt（网页分集页每个资产也有“修改这个资产”），用到它的 Prompt 会标为待更新，改动本身视为你对该资产的批准。

`rerun` 不带备注时会重新请求模型而不是取回缓存；B7（单元 Prompt，目标 `epNN_uNN`）和 B5（分镜，目标 `epNN`）可加 `--note`，模型在上一版基础上只改备注涉及的部分，网页分集页每段也有“按备注重写”。

剧本问题交回编剧修改和审查；导演不自行改源剧本。连续性修改只影响后续单元，已有片段保持当前版本。变旧的已交付内容须明确选择重新生成。

缓存和收据支持恢复；正常保存的成功结果可以复用。可识别且尚无模型输出的连接故障最多尝试三次，CLI 内部重试关闭。认证、配置、超时、部分输出及工具越界直接暂停；模型容量错误保留结果，等待重试，不自动换模型。未知费用和用量记为 `null`。

并发上限在 `concurrency.llm`。摘要、分集观众与故事审查、各场提示词、各单元盲重构可以并行；剧本与分镜分别保持集序。每集 token 预算可配置，并发中的在途调用可能使最终用量超过上限。

反馈的生成次数和人工用时按累计值记录。首次反馈用于首轮保留率，后续成功不改写最初结果；未报告数据保持未知，演示排除。真实模板比较见 [M0 验证表](docs/m0-validation.md)。

## 离线测试

```powershell
python -X utf8 -m unittest discover -s tests -v
```

测试使用固定样例和显式失败注入，不调用云端模型，不证明真实创作质量。原生 CLI 重试集成测试默认跳过；设置 `SFL_TEST_NATIVE_CLI=1` 后使用隔离的虚构凭证和本地 HTTP 服务。

Seedance 能力限制保存在 `model_cards/`，默认参数需要按实际账号验证。配置不完整时停止，不冒充已经校准。

## 仓库边界

根 Git 仓库只管理源码、角色方法、模型配置、固定测试样例、设计文档和整理后的说明。默认配置没有密钥，可选 API 示例只引用环境变量名。

真实项目、小说原文、生成内容、登录信息、缓存、数据库、日志、调用收据、截图和原始文档备份留在本地并被忽略。原始历史备份位于 `docs/local/`，不进入 Git；测试中的短文本为固定协议样例。每个项目已有的独立 Git 快照仍保留。

初始提交使用通用作者标识。远程同步只包含受版本控制的源码、固定样例和公开文档，真实项目数据保留在本地。
