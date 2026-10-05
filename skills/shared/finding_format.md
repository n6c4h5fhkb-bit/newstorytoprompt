# 审查发现

只报告有证据的问题；没有问题时 findings=[]。不输出作者思考过程。

severity：blocker 是不可交付的错漏；major 是明显影响理解或效果的问题；minor 是建议。
kind：error 引用目标中的原文；omission 引用源剧本、状态或分镜中被遗漏的原文。evidence 必须是输入中逐字存在的非空片段。location 精确到单元和镜头。problem 说明后果，suggested_fix 给出最低层的具体改法。所有 blocker 都报告，不能为凑数制造问题。

作者的 challenge 只是一条异议，不是新事实。重新核查原证据，成立则保留，不成立则撤回。修复需验证源头和下游都一致，不能仅凭被引用的一行文字改变就判定通过。
