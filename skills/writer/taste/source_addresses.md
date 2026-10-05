# 源文定位与结构字段

段落地址为 ch001:p0001。摘要与节拍只能引用输入中实际存在的地址；章节分块不会重编号段落。角色返回 JSON，平台另写人可读的 Markdown。

分集的 end_hook.type 为 danger_peak（危险峰值）、pending_reveal（待揭示）、discovery_on_the_brink（即将发现）、reversal_setup（反转准备）、escalation（威胁升级）、forced_decision（被迫选择）或 turning_line（转向台词）。文字说明放在 description。估算时长是警告信号，不为满足数字填充剧情。

decisions 对每个 beat 恰好列一条 keep、cut 或 merge。merge_into 指向 keep 的目标 beat，合并的源文地址也进入该目标所在的分集。episodes.beats 只列保留后的目标 beat。每个 keep 恰好归一集，cut 和 merge 不再单列分集归属。

hooks 给 3–5 个候选、0–1 分数、expensive（改变后返工是否昂贵）、taste（是否属于用户偏好）以及选中 key 和一句理由。不要为避免卡片而虚报标志。moments 列开篇和每个重大转折为 key=true；关键时刻必放大三版，其他 amplify=true 时刻一版。
