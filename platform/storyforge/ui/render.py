"""HTML views; production state and actions remain in the shared services."""
from html import escape
import json
import math
import re
from urllib.parse import urlencode

from storyforge.config import SflError, configuration
from storyforge.runner import service

VIEWS = {"projects": "项目", "inbox": "待处理", "episode": "分集", "settings": "设置"}


class Html(str):
    pass


def el(name, props=None, *children):
    def content(value):
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            return "".join(content(child) for child in value)
        return str(value) if isinstance(value, Html) else escape(str(value), quote=True)
    attrs = ""
    for key, value in (props or {}).items():
        if value is None or value is False:
            continue
        attrs += " " + key + ("" if value is True else '="' + escape(str(value), quote=True) + '"')
    start = "<" + name + attrs + ">"
    return Html(start if name == "input" else start + content(children) + "</" + name + ">")


def field(label, control):
    return el("label", {"class": "field"}, el("span", {}, label), control)


def input_(name, placeholder="", value="", **props):
    return el("input", {"name": name, "placeholder": placeholder, "value": value, **props})


def area(name, placeholder="", value=""):
    return el("textarea", {"name": name, "placeholder": placeholder}, value)


def button(label, *, secondary=False, **props):
    return el("button", {"type": "submit", "class": "secondary" if secondary else "primary", **props}, label)


def panel(*children):
    return el("section", {"class": "panel"}, children)


def detail(label, *children):
    return el("details", {}, el("summary", {}, label), children)


def pre(text, **props):
    return el("pre", props, text)


def url(ctx, view=None, **extra):
    selected = view or ctx["view"]
    query = {"view": selected}
    if ctx["project"]:
        query["project"] = ctx["project"]
    if selected == "episode" and ctx.get("episode"):
        query["episode"] = ctx["episode"]
    if selected == "inbox" and ctx.get("item"):
        query["item"] = ctx["item"]
    query.update({k: v for k, v in extra.items() if v is not None})
    return "/?" + urlencode(query)


def form(ctx, action, *children, inline=False, **fields):
    project = fields.pop("project", ctx["project"])
    values = {"action": action, "project": project, "_view": ctx["view"],
              "_episode": ctx.get("episode"), "_item": ctx.get("item"), **fields}
    hidden = [input_(key, value=value, type="hidden") for key, value in values.items() if value is not None]
    return el("form", {"method": "post", "action": "/action", "class": "inline-action" if inline else None}, hidden, children)


def number(value):
    return "未报告" if value is None else f"{value:,.2f}".rstrip("0").rstrip(".")


def copy_prompt(text, identity, label="复制 Prompt"):
    return el("div", {"class": "prompt-block"}, pre(text, id=identity),
              button(label, secondary=True, type="button", **{"data-copy": identity}))


def art_direction(parts):
    children = [el("h2", {}, "美术定调与共用风格")]
    if parts.get("art_prompt"):
        children += [el("h3", {}, "美术定调生图 Prompt"), copy_prompt(parts["art_prompt"], "art-direction-prompt", "复制美术定调 Prompt")]
    elif parts.get("text"):
        children.append(el("p", {"class": "muted"}, "美术定调生图 Prompt 待更新。"))
    if parts.get("style_lock"):
        children += [el("h3", {}, "每段视频共用的美术风格"), pre(parts["style_lock"])]
    if parts.get("text"):
        children.append(detail("查看完整美术方案与角色设计依据", pre(parts["text"])))
    return children


def review_issues(review):
    errors = review.get("errors") or []
    findings = review.get("findings") or []
    required = [f for f in findings if f.get("severity") in ("major", "blocker")]
    suggestions = [f for f in findings if f.get("severity") not in ("major", "blocker")]
    if not errors and not findings:
        return None

    def issue(finding):
        return el("li", {"class": "review-item"},
            el("h4", {}, finding.get("location") or "当前稿件"),
            el("p", {}, finding["problem"]),
            detail("查看审查建议与依据",
                el("p", {}, el("strong", {}, "改法建议："), finding["suggested_fix"]) if finding.get("suggested_fix") else None,
                el("p", {"class": "muted"}, el("strong", {}, "审查引用："), finding["evidence"]) if finding.get("evidence") else None))

    return el("section", {"class": "review-summary", "aria-label": "审查问题摘要"},
        el("h3", {}, f"必须修复 · {len(errors) + len(required)} 项") if errors or required else None,
        el("p", {"class": "muted"}, "这些问题会自动带入重试，备注只需补充你的偏好。修订须遵守原文与已定方案，审查建议不能作为新增剧情的依据。"),
        el("ol", {"class": "review-list"}, [el("li", {"class": "review-item"}, el("p", {}, error)) for error in errors],
            [issue(finding) for finding in required]) if errors or required else None,
        detail(f"其他建议 · {len(suggestions)} 项（不阻止通过）", el("ol", {"class": "review-list"}, [issue(finding) for finding in suggestions])) if suggestions else None)


def asset_groups(assets, *, expanded=False):
    labels = {"character": "人物", "location": "场景", "prop": "道具", "ui": "界面与文字", "layout": "色卡与布局", "voice": "声音", "music": "音乐"}
    statuses = {"needed": "待描述", "described": "已描述", "approved": "已批准"}
    groups = []
    for kind, label in labels.items():
        rows = [a for a in assets if a["type"] == kind]
        if not rows:
            continue
        cards = [el("div", {"class": "asset-card"},
            el("div", {"class": "row"}, el("strong", {}, a["name"]), el("span", {"class": "muted"}, statuses.get(a["status"], a["status"]))),
            el("p", {"class": "muted asset-reference"}, a["placeholder"]), el("p", {}, a["description"]),
            el("p", {"class": "muted"}, "变体：" + a["what_changed"]) if a["parent"] else None,
            el("details", {"open": expanded and kind == "character"},
               el("summary", {}, "查看声音描述" if kind == "voice" else "查看音乐描述" if kind == "music" else "查看生成提示词"),
               copy_prompt(a["image_prompt"], "asset-prompt-" + a["id"], "复制资产 Prompt") if a["image_prompt"] else pre("描述尚未完成。"))) for a in rows]
        groups.append(el("div", {"class": "asset-group"}, el("h3", {}, f"{label} · {len(rows)}"), el("div", {"class": "asset-grid"}, cards)))
    return groups


def running_progress(ctx, jobs):
    if not ctx["background"]["running"]:
        return None
    active = next((job for job in reversed(jobs) if job["status"] == "running"), None)
    labels = {"A1":"整理原文", "A2":"提炼章节内容", "A3":"拆解剧情", "A4":"规划分集",
        "A5":"选择开篇方向", "A6":"打磨关键片段", "A7":"撰写与审查首集", "A8":"准备首集确认",
        "A9":"撰写与审查后续分集", "A10":"保存分集定稿", "B1":"设计美术方案", "B2":"提取人物、场景与道具",
        "B3":"编写资产提示词", "B4":"准备美术确认", "B5":"设计与审查分镜", "B6":"整理参考素材",
        "B7":"编写视频提示词", "B8":"复核视频提示词", "B9":"整理交付文件"}
    title = labels.get(active["stage"], "处理当前步骤") if active else "准备下一步"
    episode = re.match(r"ep(\d+)", active["target"]) if active else None
    if episode:
        title = f"第 {int(episode[1])} 集 · {title}"
    return panel(el("span", {"class":"badge"}, "运行中"), el("h2", {}, title),
        el("p", {"class":"muted"}, "当前步骤完成后会自动继续，无需重复点击重试。需要你决定的事项会在“待处理”显示。"),
        el("a", {"class":"secondary", "href":url(ctx,"inbox")}, "查看待处理") if ctx["view"] != "inbox" else None)


def projects(ctx, state):
    result = []
    if state:
        result.append(panel(el("div", {"class": "row"}, el("div", {}, el("h2", {}, ctx["project"]), el("p", {"class": "muted"},
            "协议演示 · 固定响应，不计入真实质量指标" if state["demo"] else "本地项目 · 复用现有 Codex CLI 登录")),
            form(ctx, "run", button("运行中…" if ctx["background"]["running"] else "开始 / 继续"), inline=True),
            form(ctx, "pause", button("暂停", secondary=True), inline=True))))
        rows = []
        for name in state["episodes"]:
            progress = state["progress"][name]
            rows.append(el("div", {"class": "episode-progress"}, el("strong", {}, name.upper()),
                el("span", {"class": "done" if progress["script"] else None}, "剧本已锁定" if progress["script"] else "剧本待更新" if progress["script_stale"] else "剧本待审查 / 审批"),
                el("span", {"class": "done" if progress["storyboard"] else None}, "分镜已就绪" if progress["storyboard"] else "分镜待完成"),
                el("span", {"class": "done" if progress["delivery"] else None}, "交付已就绪" if progress["delivery"] else f"提示词 {progress['prompts']} / {progress['units'] or '—'}"),
                el("a", {"class": "secondary", "href": url(ctx, "episode", episode=name)}, "查看分集")))
        progress = running_progress(ctx, state["jobs"])
        if progress:
            result.append(progress)
        empty = "原文已开始处理，首集正文尚未完成。需要处理的事项可在“待处理”查看。" if state["jobs"] else "粘贴原文或导入一集定稿剧本，开始工作。"
        result.append(panel(el("h2", {}, "分集进度"), rows or el("p", {"class": "muted"}, empty)))
        m = state["metrics"]
        result.append(panel(el("h2", {}, "反馈与交付"),
            el("p", {}, f"首轮保留率：{number(m['first_pass_usable_rate'] * 100) + '%' if m['first_pass_usable_rate'] is not None else '未报告'} · 已报 {m['reported_units']} 段"),
            el("p", {}, f"每段累计生成次数：{number(m['generations_per_unit'])} · 已报 {m['generations_reported_units']} / {m['expected_units']} 段"),
            el("p", {}, f"单元人工用时合计：{number(m['user_minutes'])}{' 分钟' if m['user_minutes'] is not None else ''} · 已报 {m['user_minutes_reported_units']} / {m['expected_units']} 段"),
            el("p", {}, f"每集成片人工用时：{number(m['user_minutes_per_finished_episode'])}{' 分钟' if m['user_minutes_per_finished_episode'] is not None else ''} · 已报用时 {m['finished_user_minutes_reported_episodes']} / {m['finished_episodes']} 集"),
            el("p", {}, f"首集成片完成用时：{number(m['days_to_first_finished_episode'])}{' 天' if m['days_to_first_finished_episode'] is not None else ''}"),
            el("p", {}, f"首集提示词交付用时：{number(m['days_to_first_delivery'])}{' 天' if m['days_to_first_delivery'] is not None else ''} · 决策 {m['cards_created']} 项（共用 {m['cards_shared']} 项）")))
        budgets = [el("p", {"class": "muted"}, f"{name.upper()}：已使用 {b['used']} / {b['limit']} tokens" +
            ("（使用量未完整返回）" if b["usage_incomplete"] else "") + ("，已达到预算" if b["exceeded"] else "，接近预算")) for name, b in m["budgets"].items() if b["alert"]]
        if budgets:
            result.append(panel(budgets))
        result.append(panel(detail("导入一集定稿剧本", form(ctx, "import", el("div", {"class": "form-grid"},
            field("剧本（每场含“出场：”行）", area("script", "# EP01 …")), field("人物与世界设定", area("bible", "人物、地点、声音和世界设定")),
            field("起始状态", area("ledger", "第一集开始时的已知信息、持有物、关系和伏笔"))), button("导入文本")))))
    result.append(panel(el("h2", {}, "开始一个故事"), el("p", {"class": "muted"}, "小说、设定和生产记录保存在本地项目文件夹。"),
        form(ctx, "new", field("项目名称", input_("project", "例如：云清禾", required=True)), field("原文", area("novel", "可选：粘贴小说原文，也可以创建后导入剧本")),
             el("div", {"class": "actions"}, button("创建项目"), button("创建协议演示", secondary=True, name="action", value="demo")), project=None)))
    return result


def inbox(ctx, store):
    if not store:
        return [panel(el("p", {}, "先创建或选择一个项目。"))]
    data = service.inbox(store)
    items = data["items"]
    progress = running_progress(ctx, store.jobs())
    if progress:
        # The Future is authoritative. Historical pause/stale entries are not
        # requests for another run while work is already in flight; human cards
        # remain visible, including cards for independent targets.
        items = [item for item in items if item["type"] == "card"]
        if not items:
            return [progress]
    if not items:
        return [panel(el("span", {"class": "badge"}, "已处理"), el("h2", {}, "这里暂时没有需要你决定的事"),
            el("p", {"class": "muted"}, "流水线正在继续。" if ctx["background"]["running"] else "可以继续运行，或到分集页复制提示词。"), form(ctx, "run", button("继续运行")))]
    ctx["item"] = min(max(ctx["item"], 0), len(items) - 1)
    item = items[ctx["item"]]
    card = item.get("card")
    question = card["question"] if card else item["question"]
    explanation = card["reason"] if card else item["target"]
    if card and card["kind"] == "stuck" and card["stage"] in ("A7", "A9") and item.get("preview", {}).get("script"):
        question = "剧本需要继续修订"
        explanation = "两轮自动修订后仍有未解决的问题。候选稿已保留，重试会带上现有审查意见；通过审查后再确认定稿。"
    if item["type"] == "paused":
        error = item.get("question") or ""
        if "Git snapshot failed" in error:
            question = "版本快照保存失败"
            explanation = "已完成的内容会保留。修复本地版本保存问题后，点击重试可从断点继续。"
        elif "Selected model is at capacity" in error:
            question = "模型服务暂时满载"
            explanation = "本次调用没有返回完整结果，任务已暂停。已收到的正文和审查结果会保留，稍后重试会继续使用当前模型。"
        elif "timed out" in error:
            question = "模型调用等待超时"
            explanation = "本次未取得完整结果，调用用量可能不完整。已完成的步骤会保留，重试会重新调用当前步骤。"
        elif "Project paused by user" in error:
            question = "项目已暂停"
            explanation = "已完成的步骤会保留，点击继续可恢复运行。"
        else:
            question = "当前步骤暂时无法继续"
            explanation = "已完成的步骤会保留。查看详情并处理原因后，可从当前步骤重试。"
    count = f"{ctx['item'] + 1} / {len(items)}" + (f" · 另有 {data['queued_cards']} 项排队" if data["queued_cards"] else "")
    children = [el("div", {"class": "row"}, el("span", {"class": "badge"}, "需要你的决定" if card else "输入有变化" if item["type"] == "stale" else "步骤暂停"), el("span", {"class": "muted"}, count)),
        el("h2", {}, question), el("p", {"class": "muted"}, explanation)]
    if item["type"] == "paused":
        children.append(detail("技术详情", pre(f"{item['stage']} · {item['target']}\n{item.get('question') or ''}")))
    preview = item.get("preview", {})
    review = card["details"] if card and (card["details"].get("errors") or card["details"].get("findings")) else preview.get("review", {})
    children.append(review_issues(review))
    if preview.get("script"):
        children += [el("div", {"class": "preview"}, el("h3", {}, preview.get("script_title", "第一集剧本")), pre(preview["script"])), detail("审查与时长提示（原始记录）", pre(json.dumps(preview["review"], ensure_ascii=False, indent=2)))]
    if preview.get("style"):
        children.append(el("div", {"class": "preview"}, art_direction(preview["art_direction"])))
    if data["demo"]:
        children.append(el("a", {"class": "primary", "href": url(ctx, "episode", episode=card["details"].get("episode", "ep01") if card else "ep01") + "#final-prompts"}, "查看完整 Prompt 演示"))
    if "assets" in preview:
        children.append(el("div", {"class": "preview"}, el("h3", {}, "已提取的资产与生成 Prompt"), asset_groups(preview["assets"])))
    if card:
        children.append(form(ctx, "answer", field("备注", area("note", "选填：补充你的偏好；现有审查问题会自动带入重试。")),
            el("div", {"class": "actions"}, [button(("按审查意见重试" if card["kind"] == "stuck" and o["key"] == "retry" and (review.get("errors") or review.get("findings")) else o["label"]) + (" · 建议" if card["recommended"] == o["key"] else ""),
                secondary=card["recommended"] != o["key"], name="option", value=o["key"]) for o in card["options"]]), card=card["id"]))
        if card["details"].get("errors") or card["details"].get("findings"):
            children.append(detail("技术详情（原始审查记录）", pre(json.dumps({k: card["details"].get(k) for k in ("errors", "findings")}, ensure_ascii=False, indent=2))))
    else:
        label = "更新交付包" if item["type"] == "stale" and item["stage"] == "B9" else "重新生成并审查" if item["type"] == "stale" else "重试 / 继续"
        children.append(form(ctx, "rerun" if item["type"] == "stale" else "retry", button(label), stage=item["stage"], target=item["target"]))
    if len(items) > 1:
        children.append(el("div", {"class": "actions"}, el("a", {"class": "secondary", "href": url(ctx, item=(ctx["item"] + 1) % len(items))}, "查看下一项")))
    return ([progress] if progress else []) + [panel(children)]


def episode(ctx, store, state):
    progress = running_progress(ctx, state["jobs"]) if state else None
    if not ctx["episode"]:
        if state and state["jobs"]:
            return ([progress] if progress else []) + [panel(el("p", {}, "原文已开始处理，首集正文尚未完成。审查完成后，请到“待处理”确认首集。"),
                el("a", {"class":"secondary", "href":url(ctx,"inbox")}, "查看待处理"))]
        return [panel(el("p", {}, "导入剧本后，这里会显示分镜、参考和提示词。"))]
    data = service.episode_details(store, ctx["episode"])
    sample = data["demo_preview"]
    picker = el("form", {"method": "get", "action": "/", "class": "inline-action selection-form"}, input_("project", value=ctx["project"], type="hidden"),
        input_("view", value="episode", type="hidden"), el("select", {"name": "episode", "aria-label": "当前分集"}, [el("option", {"value": name, "selected": name == ctx["episode"]}, name.upper()) for name in state["episodes"]]), button("切换", secondary=True))
    result = ([progress] if progress else []) + [panel(el("div", {"class": "row"}, el("h2", {}, "分集工作台"), picker,
        el("a", {"class": "primary", "href": "/api/delivery?" + urlencode({"project": ctx["project"], "episode": ctx["episode"]})}, "下载交付包") if data["delivery_ready"] else el("span", {"class": "muted"}, "交付未完成或需要更新")),
        pre("\n".join(data["beat_sheet"])) if data["beat_sheet"] else None)]
    notes = [f"{'待应用' if n['status'] == 'pending' else '已应用'}{'（场景审查）' if n.get('by') == 'model' else ''}：{n['note']}" for n in data["script_notes"]]
    result.append(panel(detail("剧本与剧情修改", pre(data["script"]), pre("\n".join(notes)) if notes else None,
        form(ctx, "note", field("剧本备注", area("note", "这集有哪些剧情需要调整？")), button("记录剧本修改要求"), target=ctx["episode"]))))
    if sample:
        result.append(panel(el("span", {"class": "badge"}, "完整演示预览"),
            el("p", {}, "以下完整视频 Prompt 来自固定样例，方便在视觉确认前查看最终格式。当前项目的视觉确认和正式交付进度仍按实际操作记录。"),
            el("a", {"class": "primary", "href": "#final-prompts"}, "跳到完整视频 Prompt")))
    parts = data["art_direction"] if data["art_direction"]["text"] else sample["art_direction"] if sample else None
    if parts:
        result.append(panel(art_direction(parts)))
    result.append(panel(el("h2", {}, "本集资产与生成 Prompt"),
        asset_groups(data["episode_assets"] or (sample["assets"] if sample else [])) or el("p", {"class": "muted"}, "资产提取完成后，这里会显示本集的人物、场景和道具。")))
    result.append(el("section", {"class": "panel", "id": "final-prompts"}, el("h2", {}, "完整视频 Prompt"),
        el("p", {"class": "muted"}, "固定样例 · 3段 · 30秒。可展开参考映射并复制每段完整 Prompt。" if sample else "每段包含总时长、风格、表演、空间、参考职责及逐镜头动作与声音。")))
    if sample:
        for unit in sample["units"]:
            result.append(panel(el("div", {"class": "row"}, el("h2", {}, unit["label"]), el("span", {"class": "badge"}, f"固定样例 · {unit['seconds']} 秒")),
                detail("参考映射", pre("\n".join(r["placeholder"] + "：" + r["name"] + "；" + r["position"] for r in unit["references"]))),
                copy_prompt(unit["prompt"], "demo-prompt-" + unit["id"], "复制完整视频 Prompt")))
    for unit in data["units"]:
        uid = unit["id"]
        rows = [el("tr", {}, el("td", {}, ref["placeholder"]), el("td", {}, ref["description"]), el("td", {}, ref["position"]),
            el("td", {}, form(ctx, "refs", button("移除", secondary=True), inline=True, unit=uid, operation="remove", placeholder=ref["placeholder"]))) for ref in unit["references"]]
        available = [a["placeholder"] for a in data["assets"]] + ["@上段尾帧"]
        available = [a for a in available if a not in {r["placeholder"] for r in unit["references"]}]
        references = detail("参考映射", el("div", {"class": "table-wrap"}, el("table", {},
            el("thead", {}, el("tr", {}, [el("th", {}, t) for t in ("占位符", "描述", "位置 / 用途", "")])), el("tbody", {}, rows))),
            form(ctx, "refs", el("div", {"class": "actions"}, el("select", {"name": "placeholder", "aria-label": "添加参考 " + uid}, [el("option", {"value": p}, p) for p in available]),
                button("添加参考", secondary=True, disabled=not available)), unit=uid, operation="add"))
        result.append(panel(el("div", {"class": "row"}, el("h2", {}, unit["label"]), el("span", {"class": "badge warning" if unit["stale"] else "badge"}, "待更新" if unit["stale"] else f"{unit['seconds']} 秒 · 16:9")), references,
            pre(unit["prompt"] or "提示词尚未完成。", id="prompt-" + uid), button("复制提示词", secondary=True, type="button", **{"data-copy": "prompt-" + uid, "disabled": not unit["prompt"]}),
            detail("生成后的反馈与连续性", form(ctx, "feedback", field("反馈原因", input_("note", "可选：记录原因")),
                field("累计生成次数（可选）", input_("generations", "选填", type="number", min=1, step=1, **{"data-number": True})),
                field("累计人工用时（分钟，可选）", input_("user_minutes", "选填", type="number", min=0, step="any", **{"data-number": True})),
                el("p", {"class": "muted"}, "按本单元累计填写；公共准备工作计入第一段。留空保留已有报告。"),
                el("div", {"class": "actions"}, button("首轮保留", name="result", value="ok"), button("需要重做", secondary=True, name="result", value="redo")), unit=uid),
                form(ctx, "note", field("采用的连续性变化（仅更新后续单元）", area("note", "例如：保留的片段里，她已坐在椅子上，腕镣留在地上。")), button("采用变化并记录", secondary=True), target=uid))))
    if any(u["delivered"] for u in data["units"]):
        m = state["metrics"]["per_episode"].get(ctx["episode"], {})
        result.append(panel(detail("整集完成反馈", el("p", {"class": "muted"}, "已记录成片完成：" + m["finished_at"] if m.get("finished_at") else "生成和剪辑完成后，记录本集已完成。"),
            form(ctx, "finish", field("整集累计人工用时（分钟，可选）", input_("user_minutes", "选填", type="number", min=0, step="any", **{"data-number": True})),
                el("p", {"class": "muted"}, "包括准备、生成和剪辑用时；整集报告优先于单元合计，留空保留已有报告。"), button("更新完成反馈" if m.get("finished_at") else "本集已完成"), episode=ctx["episode"]))))
    return result


def settings(ctx):
    values = service.settings()
    children = [el("h2", {}, "模型与运行设置"), el("p", {"class": "muted"}, "复用你现有的 Codex CLI 登录。设置写入 config.yaml；项目中的独立设置优先。")]
    labels = {"strong": "创作角色", "cheap": "提取角色", "reviewer": "审查角色"}
    for tier, profile in values["models"].items():
        children.append(el("div", {"class": "form-grid"}, input_(f"values.models.{tier}.provider", value="codex_cli", type="hidden"),
            field(labels.get(tier, tier) + " · Codex CLI", input_(f"values.models.{tier}.model", "模型", profile["model"], required=True)),
            field("思考强度", el("select", {"name": f"values.models.{tier}.effort"}, [el("option", {"value": effort, "selected": effort == profile.get("effort")}, effort) for effort in ("low", "medium", "high", "xhigh", "max", "ultra")]))))
    fields = [("每集 token 预算", "token_budget.per_episode", "不设上限", values["token_budget"]["per_episode"], 1),
        ("语速（字 / 秒）", "speech_rate_chars_per_sec", "4.5", values["speech_rate_chars_per_sec"], "any"),
        ("并发调用数", "concurrency.llm", "4", values["concurrency"]["llm"], 1),
        ("每阶段最多可见卡片（1–3）", "card.max_open_per_phase", "3", values["card"]["max_open_per_phase"], 1),
        ("评分接近时的分差", "card.score_margin", "0.1", values["card"]["score_margin"], "any"),
        ("提示词长度警告（字）", "warnings.prompt_chars", "3500", values["warnings"]["prompt_chars"], 1),
        ("原文对白重合警告（比例）", "warnings.source_overlap", "0.15", values["warnings"]["source_overlap"], "any")]
    children.append(el("div", {"class": "form-grid"}, [field(label, input_("values." + key, placeholder, "" if value is None else value, type="number", step=step,
        **{"data-number": True, "data-empty-null": key == "token_budget.per_episode", "required": key != "token_budget.per_episode"})) for label, key, placeholder, value, step in fields]))
    children.append(button("保存设置"))
    return [panel(form(ctx, "settings", children)), panel(el("p", {"class": "muted"}, "项目的时长、参考模型和音乐选择保存在 project.yaml，可在文件中覆盖全局默认。"))]


def render(app, query):
    names = [p.name for p in sorted(app.projects.glob("*")) if (p / "project.yaml").exists()]
    name = query.get("project") or (names[0] if names else "")
    view = query.get("view", "projects")
    if view not in VIEWS or name and name not in names:
        raise SflError("Unknown view or project")
    store = app.store(name) if name else None
    state = service.status(store) if store else None
    episodes = state["episodes"] if state else []
    selected = query.get("episode")
    ctx = {"project": name, "view": view, "episode": selected if selected in episodes else (episodes[0] if episodes else None),
           "item": int(query.get("item", 0)), "background": app.background(name) if name else {"running": False, "report": None}}
    content = {"projects": lambda: projects(ctx, state), "inbox": lambda: inbox(ctx, store), "episode": lambda: episode(ctx, store, state), "settings": lambda: settings(ctx)}[view]()
    ctx["title"] = VIEWS[view]
    ctx["content"] = "".join(content)
    ctx["navigation"] = "".join(el("a", {"href": url(ctx, key), "data-view": key, "class": "active" if key == view else None}, title, el("span", {}, f"{index:02d}")) for index, (key, title) in enumerate(VIEWS.items(), 1))
    ctx["project_options"] = "".join(el("option", {"value": p, "selected": p == name}, p + (" · 演示" if configuration(app.projects / p).get("demo") else "")) for p in names) or str(el("option", {"value": ""}, "创建你的第一个项目"))
    return ctx


def document(template, ctx):
    values = {"CONTENT": ctx["content"], "NAVIGATION": ctx["navigation"], "PROJECT_OPTIONS": ctx["project_options"],
        "TITLE": escape(ctx["title"]), "PROJECT": escape(ctx["project"], quote=True), "VIEW": escape(ctx["view"], quote=True),
        "EPISODE": escape(ctx["episode"] or "", quote=True), "ITEM": str(ctx["item"]), "RUNNING": "true" if ctx["background"]["running"] else "false"}
    # Replace only original template slots. User text resembling a slot remains text.
    return re.sub(r"<!--SFL:([A-Z_]+)-->", lambda match: values[match[1]], template)


def form_command(fields):
    body = {}
    for name, values in fields.items():
        if name.startswith("_"):
            continue
        value = values[-1]
        if name in ("generations", "user_minutes") or name.startswith("values.") and name not in {
            "values.models." + tier + "." + field for tier in ("strong", "cheap", "reviewer") for field in ("model", "effort", "provider")}:
            if value == "":
                if name == "values.token_budget.per_episode":
                    value = None
                else:
                    continue
            else:
                value = float(value)
                if not math.isfinite(value):
                    raise SflError("Numeric form values must be finite")
                if value.is_integer():
                    value = int(value)
        cursor = body
        parts = name.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    if body.get("action") == "demo":
        body.pop("novel", None)
    if body.get("action") in ("new", "demo"):
        body["project"] = body.get("project", "").strip()
    return body
