"""M0 匹配 oracle：确定性 40 主题合成语料 + 20 条冻结标注查询。

本模块只提供**数据与物化器**，不实现任何匹配/打分逻辑，也不参与生产代码。
标签（``QUERIES``）在 M2 实现前冻结；M2 的验收断言只读取冻结字面量，不得
边写边改。语料由固定种子确定性生成，两次物化逐字节一致。

两种物化形态（token 等价）：

- ``build_topic_docs(limit)`` → ``list[TopicDoc]``，供 M1 直接调
  ``refresh_match_index`` 计时；
- ``materialize_store(store_root, limit)`` → 用 ``tests.util.ChainBuilder``
  构造 Merkle 合法的 hop 分片 + 顶层 md，供旧基线、M2 三库等价与 perf 用例。

「增量库」口径 = 物化磁盘 store 后逐主题推进（本模块只负责从零物化）。

覆盖场景（见 plan §2 / design §6.2）：单词宽召回、双词 AND、三词收缩、
0 命中、英文 token、中文二元组、topic_id 子串（``th-jw`` → ``auth-jwt``）、
长标题 near-duplicate、71 分假阳性复现、title vs R 字段优先级、Q/A/R/focus
字段命中、中英混合、3~10 主题小库子集、Zipf 词频、2~3 高频词组合、
``&#8212;`` 转义实体、大主题 + 通用词 + 短标题负样本。
"""

from __future__ import annotations

import random

TOPIC_COUNT = 40
SMALL_LIBRARY_SIZE = 10
SEED = 20261127

# ---------------------------------------------------------------------------
# 词汇表（全部合成，不含真实个人主题文本）
# ---------------------------------------------------------------------------

#: 高频工程通用词（Zipf 头部）；rank 越靠前越常见。
_HF_WORDS = (
    "设计", "方案", "实现", "优化", "架构",
    "验证", "缓存", "成本", "流程", "数据",
)

#: 每个 rank 在 12 个确定性槽位中的激活阈值（越大越常见）。
_HF_THRESHOLD = (12, 10, 7, 9, 8, 6, 10, 7, 8, 11)

#: 每个 rank 的槽位步长（与 12 互质，保证随 index 遍历全部槽位）。
_HF_STRIDE = (5, 7, 11, 1, 5, 7, 11, 1, 5, 7)

#: 长尾专有名词；刻意排除所有标注锚点词（分桶/幂等/配额/回滚/向量/重排/
#: 熔断/穿透/topic/rag/bm25/fts5/sqlite/jwt 等）。
_LONG_TAIL = (
    "索引", "接口", "服务", "模块", "集群", "部署", "监控", "告警",
    "日志", "链路", "并发", "事务", "状态", "策略", "边界", "契约",
    "迁移", "容量", "吞吐", "延迟", "抖动", "重试", "补偿", "编排",
    "调度", "隔离", "探针", "采样", "基线", "回归", "分片", "副本",
    "选举", "租约", "心跳", "哨兵", "布隆", "倒排", "嵌入", "召回",
    "分层", "词频", "逆文档", "语料", "标注", "冻结", "校准", "阈值",
    "排名", "解释", "证据", "复核", "收敛", "复盘", "评审", "模式",
    "范式", "惯例", "陷阱", "重构", "债务", "演练", "口径",
)

#: 中英混合用英文 token（刻意排除标注锚点词）。
_EN_WORDS = (
    "api", "json", "sdk", "http", "grpc", "redis", "kafka", "mysql",
    "linux", "kernel", "thread", "lock", "queue", "metric", "trace",
    "span", "proxy", "gateway", "shard", "replica", "leader", "lease",
    "cache", "buffer", "batch", "stream", "worker", "client", "server",
)

#: filler topic_id 的 ASCII slug（保证 topic_id 唯一且不含锚点 token）。
_SLUGS = (
    "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta",
    "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron", "pi", "rho",
    "sigma", "tau", "upsilon", "phi", "chi", "psi", "omega", "orion",
    "vega", "lyra", "nova", "atlas", "hermes", "apollo", "artemis",
    "helios", "selene", "eros", "iris", "juno", "minerva", "ceres",
    "vesta",
)

#: 长标题 near-duplicate 对的公共短语（≥23 个词元）。
LONG_TITLE_COMMON = (
    "跨区域分布式缓存一致性协议在失效回退与流量切换场景下的"
    "工程实践性能权衡记录"
)

#: 旧实现 71 分假阳性复现查询（C1）。
FALSE_POSITIVE_TOPICS = ("topic 匹配", "RAG", "检索")

#: 单调收缩曲线：单词 → 双词 → 三词 → 第四词归零。
CONTRACTION_TOPICS = (
    ("设计",),
    ("设计", "方案"),
    ("设计", "方案", "实现"),
    ("设计", "方案", "穿透"),
)


# ---------------------------------------------------------------------------
# 确定性文本生成
# ---------------------------------------------------------------------------

def _active_hf(index: int) -> list[str]:
    """按 index 确定性地选取该主题激活的高频词（控制收缩曲线计数）。"""
    active: list[str] = []
    for rank, word in enumerate(_HF_WORDS):
        slot = (index * _HF_STRIDE[rank] + rank * 3 + 5) % 12
        if slot < _HF_THRESHOLD[rank]:
            active.append(word)
    return active


def _pool(active, forbid=frozenset()) -> list[str]:
    pool: list[str] = []
    for rank, word in enumerate(_HF_WORDS):
        if word in active and word not in forbid:
            pool.extend([word] * (12 - rank))
    for word in _LONG_TAIL:
        if word not in forbid:
            pool.append(word)
    for word in _EN_WORDS:
        if word not in forbid:
            pool.append(word)
    return pool


def _sentence(rng: random.Random, active, forbid, n_words: int) -> str:
    """生成一句以中文逗号分隔的确定性词序列（每个词是独立 token）。"""
    pool = _pool(active, forbid)
    words = [word for word in active if word not in forbid]
    words = words[:n_words]
    while len(words) < n_words:
        words.append(rng.choice(pool))
    rng.shuffle(words)
    return "，".join(words) + "。"


def _updated_at(index: int) -> str:
    return f"2026-11-01T{index // 60:02d}:{index % 60:02d}:00Z"


def _it(item_id, statement, basis="", status=None, a_state=None):
    return {
        "item_id": item_id,
        "track": item_id[:1],
        "statement": statement,
        "basis": basis,
        "status": status,
        "a_state": a_state,
    }


def _pad_items(items, rng, active, forbid, c_to, q_to, a_to, r_to, n_words):
    """按轨道把条目补齐到目标数量（种子条目在前，ID 连续）。"""
    counts = {"C": 0, "Q": 0, "A": 0, "R": 0}
    for item in items:
        counts[item["track"]] += 1
    targets = {"C": c_to, "Q": q_to, "A": a_to, "R": r_to}
    for track in ("C", "Q", "A", "R"):
        for number in range(counts[track] + 1, targets[track] + 1):
            item_id = f"{track}{number}"
            statement = _sentence(rng, active, forbid, n_words)
            if track == "C":
                status = "verified" if number % 2 else "inferred"
                basis = _sentence(rng, active, forbid, 6)
                items.append(_it(item_id, statement, basis, status=status))
            elif track == "R":
                basis = _sentence(rng, active, forbid, 6)
                items.append(_it(item_id, statement, basis))
            elif track == "A":
                a_state = "x" if number % 2 else "~"
                items.append(_it(item_id, statement, a_state=a_state))
            else:
                items.append(_it(item_id, statement))
    return items


def _spec(index, topic_id, title, focus, items, artifacts=()):
    return {
        "index": index,
        "topic_id": topic_id,
        "title": title,
        "focus": focus,
        "items": items,
        "artifacts": list(artifacts),
        "hop_id": f"H{index + 1:03d}",
        "updated_at": _updated_at(index),
        "summary": f"{topic_id} 合成语料快照",
    }


# ---------------------------------------------------------------------------
# 锚点主题（覆盖 design §6.2 的全部场景）
# ---------------------------------------------------------------------------

def _anchor_auth_jwt(index):
    seeds = [
        _it("C1", "网关统一签发 EdDSA 短效 jwt，密钥轮换走独立接口。",
            "压测 P99 延迟降至 1.2ms", status="verified"),
        _it("C2", "无状态鉴权在预发集群验证通过。",
            "预发窗口内的全量流量稳定", status="inferred"),
        _it("C3", "公钥轮换接口采用双密钥窗口。",
            "旧公钥保留一个轮换周期", status="verified"),
        _it("Q1", "回调签名头格式是否统一走验签中间件？"),
        _it("A1", "完成网关鉴权原型压测。", a_state="x"),
        _it("R1", "放弃全链路会话共享库。", "跨区同步延迟导致偶发 401"),
    ]
    rng = random.Random(SEED + 7919 * index)
    items = _pad_items(seeds, rng, (), frozenset(), 16, 6, 6, 5, 40)
    return _spec(index, "auth-jwt", "认证模块无状态 JWT 改造与密钥轮换",
                 "推进公钥轮换与验签中间件", items)


def _anchor_match_rag(index):
    seeds = [
        _it("C1", "倒排索引改为逐字段内容表，检索召回按词元分层。",
            "分层召回把布尔匹配与排序分离", status="verified"),
        _it("C2", "bm25 只在层内排序，布尔匹配决定召回集合。",
            "层内相对排名避免连续分数劫持", status="verified"),
        _it("C3", "topic 匹配的假阳性来自最高权重劫持。",
            "单个通用词命中标题即整体提权", status="inferred"),
        _it("Q1", "分层召回是否需要保留 any 层？"),
        _it("A1", "完成 20 条标注查询冻结。", a_state="x"),
        _it("R1", "排除连续分数阈值作为召回闸门。", "阈值在小库形同虚设"),
    ]
    rng = random.Random(SEED + 7919 * index)
    items = _pad_items(seeds, rng, (), frozenset(), 16, 6, 6, 5, 40)
    return _spec(index, "topicdock-match-rag",
                 "topic 匹配与 RAG 检索升级方案",
                 "校准分层召回与 bm25 层内排序", items)


def _anchor_false_positive(index):
    seeds = [
        _it("C1", "把 topic 匹配与 rag 召回当作通用工程术语复用。",
            "术语在评审模板中反复出现", status="verified"),
        _it("C2", "评审流程强调证据链与复盘记录。",
            "收敛结论需要可复核依据", status="verified"),
        _it("Q1", "模式复用是否需要额外约束？"),
        _it("A1", "完成评审收敛复盘。", a_state="x"),
        _it("R1", "排除以单一分数判断质量。", "分数无法区分变好与变得不一样"),
    ]
    rng = random.Random(SEED + 7919 * index)
    items = _pad_items(seeds, rng, (), frozenset(), 16, 6, 6, 5, 40)
    return _spec(index, "rd-coder-pattern-and-review-convergence",
                 "代码模式与评审收敛的工程复盘",
                 "沉淀代码模式与评审收敛的复盘口径", items)


def _anchor_long_title(index, title_suffix, topic_suffix, seed_bias):
    rng = random.Random(SEED + 7919 * index)
    seeds = [
        _it("C1", "一致性协议在跨区域部署下需要权衡失效回退与流量切换。",
            f"区域间延迟与切换窗口共同决定策略 {seed_bias}", status="verified"),
        _it("C2", "缓存副本的失效回退顺序必须可解释。",
            "回退路径与流量切换顺序一致", status="inferred"),
        _it("Q1", "跨区域失效回退是否需要双写窗口？"),
        _it("A1", "完成跨区域一致性压测。", a_state="x"),
    ]
    items = _pad_items(seeds, rng, ("缓存",), frozenset(), 16, 6, 6, 5, 40)
    return _spec(index, f"long-title-cache-{topic_suffix}",
                 LONG_TITLE_COMMON + title_suffix,
                 "记录一致性协议在跨区域部署下的失效回退与流量切换权衡", items)


def _anchor_large_negative(index):
    seeds = [
        _it("C1", "数据面吞吐瓶颈来自缓存命中率抖动。",
            "缓存淘汰策略影响尾延迟", status="verified"),
        _it("C2", "穿透型流量会击穿热点缓存。",
            "穿透请求需要独立限流", status="verified"),
        _it("C3", "成本优化优先处理低命中率缓存。",
            "单位吞吐成本随命中率下降", status="inferred"),
        _it("Q1", "缓存穿透与雪崩是否需要统一预案？"),
        _it("A1", "完成数据面调优压测。", a_state="x"),
        _it("R1", "排除单纯扩容缓解抖动。", "扩容无法修复命中率下降"),
    ]
    rng = random.Random(SEED + 7919 * index)
    items = _pad_items(
        seeds, rng, ("缓存", "优化", "成本", "数据"), frozenset(),
        18, 6, 6, 6, 36,
    )
    return _spec(index, "perf-dataplane-tuning", "高吞吐数据面调优",
                 "数据面吞吐与延迟的工程调优记录", items)


def _anchor_escape_entity(index):
    seeds = [
        _it("Q1", "转义实体 — 依据(verified): 需要在往返后保持等价。"),
        _it("C1", "账本条目正文压平为单行。",
            "换行替换为空格", status="verified"),
        _it("A1", "完成转义往返校验。", a_state="x"),
    ]
    rng = random.Random(SEED + 7919 * index)
    items = _pad_items(seeds, rng, (), frozenset(), 14, 6, 6, 5, 36)
    return _spec(index, "codec-escape-entity", "账本转义实体往返校验",
                 "校验转义实体在解析后恢复原字符", items)


def _anchor_title_weight(index):
    # 「分桶」只出现在 title；C/Q/A/R/focus/topic_id 均不含（Q12 字段守卫）。
    seeds = [
        _it("C1", "批次按百分比逐步放量。",
            "每批观察核心指标", status="verified"),
        _it("Q1", "回退的判定阈值如何设定？"),
        _it("A1", "完成发布复盘。", a_state="x"),
    ]
    rng = random.Random(SEED + 7919 * index)
    items = _pad_items(seeds, rng, (), frozenset(), 14, 5, 5, 4, 36)
    return _spec(index, "release-canary-retro", "分桶发布策略复盘",
                 "复盘发布策略", items)


def _anchor_r_track(index):
    # 「分桶」只出现在 R1 statement；title/focus/C/Q/A 均不含（Q12 字段守卫）。
    seeds = [
        _it("R1", "分桶批次回退时触发告警。", "监控指标异常触发自动回退"),
        _it("C1", "事故回溯需要覆盖指标与日志两条证据链。",
            "证据链按时间线对齐", status="verified"),
        _it("Q1", "事故复盘是否需要独立跟踪项？"),
        _it("A1", "完成事故回溯记录。", a_state="x"),
    ]
    rng = random.Random(SEED + 7919 * index)
    items = _pad_items(seeds, rng, (), frozenset(), 14, 5, 5, 4, 36)
    return _spec(index, "incident-postmortem-record", "线上事故回溯记录",
                 "按时间线复盘事故与回退动作", items)


def _anchor_q_track(index):
    seeds = [
        _it("Q1", "重试接口是否需要幂等键？"),
        _it("C1", "重试语义需要明确成功与失败的边界。",
            "边界不清会导致重复提交", status="verified"),
        _it("A1", "完成重试语义讨论。", a_state="x"),
    ]
    rng = random.Random(SEED + 7919 * index)
    items = _pad_items(seeds, rng, (), frozenset(), 14, 6, 5, 4, 36)
    return _spec(index, "retry-semantics", "接口重试语义讨论",
                 "明确重试接口的失败边界", items)


def _anchor_a_track(index):
    # 「配额」只出现在 A1 statement；C/Q/focus/basis 均不含（Q14 字段守卫）。
    seeds = [
        _it("A1", "完成配额申请与容量评估。", a_state="x"),
        _it("C1", "容量治理需要先对齐容量口径。",
            "口径决定扩容节奏", status="verified"),
        _it("Q1", "容量是否需要按团队拆分？"),
    ]
    rng = random.Random(SEED + 7919 * index)
    items = _pad_items(seeds, rng, (), frozenset(), 14, 5, 6, 4, 36)
    return _spec(index, "capacity-governance", "容量治理行动项",
                 "推进容量评估与扩容节奏", items)


def _anchor_r_rollback(index):
    # 「回滚」只出现在 R1 statement；C/Q/focus/basis 均不含（Q15 字段守卫）。
    seeds = [
        _it("R1", "回滚到上一个稳定版本。", "新版本指标抖动"),
        _it("C1", "升级失败处理需要保留恢复窗口。",
            "恢复窗口覆盖观察期", status="verified"),
        _it("Q1", "恢复窗口是否需要自动延长？"),
        _it("A1", "完成升级失败复盘。", a_state="x"),
    ]
    rng = random.Random(SEED + 7919 * index)
    items = _pad_items(seeds, rng, (), frozenset(), 14, 5, 5, 5, 36)
    return _spec(index, "upgrade-failure-retro", "升级失败处理复盘",
                 "复盘升级失败与恢复窗口", items)


def _anchor_focus(index):
    seeds = [
        _it("C1", "检索链路需要同时覆盖关键词与语义召回。",
            "混合召回提升长尾覆盖", status="verified"),
        _it("Q1", "混合召回是否需要统一排序？"),
        _it("A1", "完成检索链路演进记录。", a_state="x"),
    ]
    rng = random.Random(SEED + 7919 * index)
    items = _pad_items(seeds, rng, (), frozenset(), 14, 5, 5, 4, 36)
    return _spec(index, "retrieval-evolution", "检索链路演进记录",
                 "下一阶段推进向量重排与混合召回", items)


def _anchor_sqlite_fts(index):
    seeds = [
        _it("C1", "SQLite FTS5 逐字段索引保住字段权重。",
            "detail=full 让 bm25 可用", status="verified"),
        _it("C2", "FTS5 索引是派生缓存，可冷重建。",
            "索引不进入事实源", status="verified"),
        _it("Q1", "FTS5 缺失时如何优雅降级？"),
        _it("A1", "完成索引选型记录。", a_state="x"),
        _it("R1", "排除 contentless 形态。", "rowid 与删除顺序耦合"),
    ]
    rng = random.Random(SEED + 7919 * index)
    items = _pad_items(seeds, rng, (), frozenset(), 14, 5, 5, 5, 36)
    return _spec(index, "sqlite-fts5-index", "SQLite FTS5 索引选型记录",
                 "对比 FTS5 逐字段索引与倒排表", items)


def _anchor_ranking_eval(index):
    seeds = [
        _it("C1", "检索排序评估方案的设计取舍。",
            "分层召回与排序分开评估", status="verified"),
        _it("C2", "评估需要固定标注集与冻结基线。",
            "基线只作证据不作预期", status="verified"),
        _it("Q1", "排序稳定性如何度量？"),
        _it("A1", "完成检索排序评估方案。", a_state="x"),
    ]
    rng = random.Random(SEED + 7919 * index)
    items = _pad_items(seeds, rng, (), frozenset(), 14, 5, 5, 4, 36)
    return _spec(index, "search-ranking-eval", "检索排序评估方案",
                 "评估分层召回与排序稳定性", items)


def _anchor_breaker(index):
    seeds = [
        _it("C1", "熔断阈值按错误率与延迟分档。",
            "分档避免单一阈值误触发", status="verified"),
        _it("C2", "限流与熔断需要协同。",
            "限流先于熔断生效", status="inferred"),
        _it("Q1", "熔断恢复是否需要半开探测？"),
        _it("A1", "完成限流熔断预案。", a_state="x"),
    ]
    rng = random.Random(SEED + 7919 * index)
    items = _pad_items(seeds, rng, (), frozenset(), 14, 5, 5, 4, 36)
    return _spec(index, "rate-limit-breaker", "限流熔断预案",
                 "梳理限流与熔断的触发阈值", items)


_ANCHOR_BUILDERS = {
    10: _anchor_auth_jwt,
    11: _anchor_match_rag,
    12: _anchor_false_positive,
    13: lambda i: _anchor_long_title(i, "甲", "a", "甲区"),
    14: lambda i: _anchor_long_title(i, "乙", "b", "乙区"),
    15: _anchor_large_negative,
    16: _anchor_escape_entity,
    17: _anchor_title_weight,
    18: _anchor_r_track,
    19: _anchor_q_track,
    20: _anchor_a_track,
    21: _anchor_r_rollback,
    22: _anchor_focus,
    23: _anchor_sqlite_fts,
    24: _anchor_ranking_eval,
    25: _anchor_breaker,
}


def _filler_spec(index: int):
    rng = random.Random(SEED + 104729 * index)
    active = _active_hf(index)
    t1, t2, t3 = rng.sample(_LONG_TAIL, 3)
    title = f"{t1}、{t2}、{t3}工程记录"
    focus = _sentence(rng, active, frozenset(), 6)
    items: list[dict] = []
    _pad_items(items, rng, active, frozenset(), 16, 6, 6, 5, 40)
    return _spec(index, f"case-{index:02d}-{_SLUGS[index % len(_SLUGS)]}",
                 title, focus, items)


def _specs(count: int = TOPIC_COUNT):
    specs = []
    for index in range(count):
        builder = _ANCHOR_BUILDERS.get(index) if index < TOPIC_COUNT else None
        specs.append(builder(index) if builder else _filler_spec(index))
    return specs


# ---------------------------------------------------------------------------
# 双形态物化器
# ---------------------------------------------------------------------------

def build_topic_docs(limit: int | None = None):
    """形态 (a)：返回 ``list[TopicDoc]``（供 M1 直接调 refresh_match_index）。"""
    import topicdock

    docs = []
    for spec in _specs(TOPIC_COUNT if limit is None else limit):
        doc = topicdock.TopicDoc(topic_id=spec["topic_id"], title=spec["title"])
        doc.updated_at = spec["updated_at"]
        doc.head_hop = spec["hop_id"]
        doc.last_actor = "fixture"
        doc.latest = topicdock.LatestHop(
            hop_id=spec["hop_id"],
            updated_at=spec["updated_at"],
            actor="fixture",
            parents=[],
            sha12="0" * 12,
            summary=spec["summary"],
            next_focus=spec["focus"],
        )
        for item in spec["items"]:
            doc.items[item["track"]].append(
                topicdock.TopicItem(
                    item_id=item["item_id"],
                    track=item["track"],
                    statement=item["statement"],
                    a_state=item["a_state"],
                    evidence_status=item["status"],
                    basis=item["basis"] or None,
                )
            )
        doc.artifacts = list(spec["artifacts"])
        docs.append(doc)
    return docs


def materialize_store(store_root, limit: int | None = None):
    """形态 (b)：用 ChainBuilder 写 Merkle 合法的 hop 分片 + 顶层 md。

    调用前 ``store_root`` 必须是已 ``td init`` 的 store（含 ``topics/`` 与
    ``hops/``）。DB 由调用方随后用 ``td doctor --rebuild`` 或逐主题 save 填充。
    """
    import topicdock
    from tests.util import ChainBuilder

    builders = []
    for spec in _specs(TOPIC_COUNT if limit is None else limit):
        builder = ChainBuilder(store_root, spec["topic_id"], spec["title"])
        blocks = []
        for item in spec["items"]:
            fields = {"结论正文": item["statement"]}
            basis = item["basis"]
            if basis:
                if item["track"] == "C":
                    fields["一句话依据"] = basis
                elif item["track"] == "R":
                    fields["一句话原因"] = basis
                elif item["track"] == "A":
                    fields["断点"] = basis
            blocks.append(
                topicdock.HopBlock(
                    item_id=item["item_id"],
                    op="create",
                    status=item["status"],
                    a_state=item["a_state"],
                    fields=fields,
                )
            )
        builder.add_hop(
            spec["hop_id"], [], blocks,
            created_at=spec["updated_at"],
            summary=spec["summary"],
            next_focus=spec["focus"],
        )
        builder.write_md()
        builders.append(builder)
    return builders


# ---------------------------------------------------------------------------
# similar_topics 场景（M2/M3 复用；不进入 20 条 match 标签）
# ---------------------------------------------------------------------------

#: 长标题 near-duplicate 正样本：两个标题都完整包含公共长短语。
SIMILAR_POSITIVE = {
    "title": LONG_TITLE_COMMON,
    "expect_top3": ["long-title-cache-a", "long-title-cache-b"],
}

#: 大主题 + 通用词 + 短标题负样本：正文大量出现「缓存」，标题不含。
SIMILAR_NEGATIVE = {
    "title": "缓存",
    "forbidden": ["perf-dataplane-tuning"],
}


# ---------------------------------------------------------------------------
# M2 常量校准（M0 临时 SQLite FTS5 探针实测，详见 M0 台账）
# ---------------------------------------------------------------------------

#: 层内 bm25 噪声下限：高频词（df>N/2）的 FTS5 IDF 被钳到 ~1e-6，实测
#: 单/双/三高频词 all 层 spread ∈ [3.3e-9, 9.8e-7]；非退化查询 spread ≥ 6.7e-4。
#: design 暂定 1e-6 与实测噪声上限过近，M0 建议收紧到 1e-5（10× 余量）。
SCORE_MIN_SPREAD = 1e-5

#: 退化最优阈值：高频词查询 best bm25 ∈ [-6.6e-6, -1.5e-6]（均 > -1e-4），
#: 有区分度查询 best ≤ -0.21（均 < -1e-4）。design 暂定 -1e-4 实测成立。
SCORE_DEGENERATE_BEST = -1e-4


# ---------------------------------------------------------------------------
# 20 条冻结标注查询（由 M0 一致性脚本机械推导后冻结）
# ---------------------------------------------------------------------------

QUERIES: list[dict] = [
    {
        "id": "Q01",
        "topics": [
            "设计"
        ],
        "expect_all": [
            "case-00-alpha",
            "case-01-beta",
            "case-02-gamma",
            "case-03-delta",
            "case-04-epsilon",
            "case-05-zeta",
            "case-06-eta",
            "case-07-theta",
            "case-08-iota",
            "case-09-kappa",
            "case-26-lyra",
            "case-27-nova",
            "case-28-atlas",
            "case-29-hermes",
            "case-30-apollo",
            "case-31-artemis",
            "case-32-helios",
            "case-33-selene",
            "case-34-eros",
            "case-35-iris",
            "case-36-juno",
            "case-37-minerva",
            "case-38-ceres",
            "case-39-vesta",
            "search-ranking-eval"
        ],
        "expect_partial": [],
        "expect_any": [],
        "expect_top1": "case-39-vesta",
        "forbidden_all": [],
        "reason": "高频通用词「设计」宽召回；bm25 退化（spread<1e-6）后按 updated_at 取最新。",
        "top1_scored": True
    },
    {
        "id": "Q02",
        "topics": [
            "设计",
            "方案"
        ],
        "expect_all": [
            "case-00-alpha",
            "case-01-beta",
            "case-03-delta",
            "case-04-epsilon",
            "case-05-zeta",
            "case-06-eta",
            "case-07-theta",
            "case-08-iota",
            "case-27-nova",
            "case-28-atlas",
            "case-29-hermes",
            "case-30-apollo",
            "case-31-artemis",
            "case-32-helios",
            "case-34-eros",
            "case-35-iris",
            "case-36-juno",
            "case-37-minerva",
            "case-39-vesta",
            "search-ranking-eval"
        ],
        "expect_partial": [
            "case-02-gamma",
            "case-09-kappa",
            "case-26-lyra",
            "case-33-selene",
            "case-38-ceres",
            "topicdock-match-rag"
        ],
        "expect_any": [],
        "expect_top1": "case-39-vesta",
        "forbidden_all": [],
        "reason": "双词 AND 收缩；同为高频词组合，bm25 退化按 updated_at。",
        "top1_scored": True
    },
    {
        "id": "Q03",
        "topics": [
            "设计",
            "方案",
            "实现"
        ],
        "expect_all": [
            "case-05-zeta",
            "case-06-eta",
            "case-07-theta",
            "case-08-iota",
            "case-29-hermes",
            "case-30-apollo",
            "case-31-artemis",
            "case-32-helios",
            "case-34-eros",
            "case-35-iris"
        ],
        "expect_partial": [
            "case-00-alpha",
            "case-01-beta",
            "case-03-delta",
            "case-04-epsilon",
            "case-09-kappa",
            "case-27-nova",
            "case-28-atlas",
            "case-33-selene",
            "case-36-juno",
            "case-37-minerva",
            "case-39-vesta",
            "search-ranking-eval"
        ],
        "expect_any": [
            "case-02-gamma",
            "case-26-lyra",
            "case-38-ceres",
            "topicdock-match-rag"
        ],
        "expect_top1": "case-31-artemis",
        "forbidden_all": [],
        "reason": "三词收缩；「实现」df 较低使 bm25 非退化，按 bm25 取最优。",
        "top1_scored": True
    },
    {
        "id": "Q04",
        "topics": [
            "设计",
            "方案",
            "穿透"
        ],
        "expect_all": [],
        "expect_partial": [
            "case-00-alpha",
            "case-01-beta",
            "case-03-delta",
            "case-04-epsilon",
            "case-05-zeta",
            "case-06-eta",
            "case-07-theta",
            "case-08-iota",
            "case-27-nova",
            "case-28-atlas",
            "case-29-hermes",
            "case-30-apollo",
            "case-31-artemis",
            "case-32-helios",
            "case-34-eros",
            "case-35-iris",
            "case-36-juno",
            "case-37-minerva",
            "case-39-vesta",
            "search-ranking-eval"
        ],
        "expect_any": [
            "case-02-gamma",
            "case-09-kappa",
            "case-26-lyra",
            "case-33-selene",
            "case-38-ceres",
            "perf-dataplane-tuning",
            "topicdock-match-rag"
        ],
        "expect_top1": None,
        "forbidden_all": [],
        "reason": "加入「穿透」后 all 层归零（穿透只在不含设计/方案的大主题）；0 命中不参与 top-1。",
        "top1_scored": False
    },
    {
        "id": "Q05",
        "topics": [
            "bm25"
        ],
        "expect_all": [
            "topicdock-match-rag"
        ],
        "expect_partial": [],
        "expect_any": [],
        "expect_top1": "topicdock-match-rag",
        "forbidden_all": [],
        "reason": "英文 token bm25 唯一命中 topicdock-match-rag。",
        "top1_scored": True
    },
    {
        "id": "Q06",
        "topics": [
            "fts5"
        ],
        "expect_all": [
            "sqlite-fts5-index"
        ],
        "expect_partial": [],
        "expect_any": [],
        "expect_top1": "sqlite-fts5-index",
        "forbidden_all": [],
        "reason": "英文 token fts5 唯一命中 sqlite-fts5-index。",
        "top1_scored": True
    },
    {
        "id": "Q07",
        "topics": [
            "缓存"
        ],
        "expect_all": [
            "case-02-gamma",
            "case-03-delta",
            "case-04-epsilon",
            "case-05-zeta",
            "case-06-eta",
            "case-07-theta",
            "case-08-iota",
            "case-09-kappa",
            "case-26-lyra",
            "case-27-nova",
            "case-28-atlas",
            "case-29-hermes",
            "case-30-apollo",
            "case-31-artemis",
            "case-32-helios",
            "case-33-selene",
            "case-34-eros",
            "case-35-iris",
            "case-38-ceres",
            "case-39-vesta",
            "long-title-cache-a",
            "long-title-cache-b",
            "perf-dataplane-tuning",
            "sqlite-fts5-index"
        ],
        "expect_partial": [],
        "expect_any": [],
        "expect_top1": "case-39-vesta",
        "forbidden_all": [],
        "reason": "中文二元组「缓存」宽召回；bm25 退化按 updated_at。",
        "top1_scored": True
    },
    {
        "id": "Q08",
        "topics": [
            "熔断"
        ],
        "expect_all": [
            "rate-limit-breaker"
        ],
        "expect_partial": [],
        "expect_any": [],
        "expect_top1": "rate-limit-breaker",
        "forbidden_all": [],
        "reason": "中文二元组「熔断」唯一命中 rate-limit-breaker。",
        "top1_scored": True
    },
    {
        "id": "Q09",
        "topics": [
            "th-jw"
        ],
        "expect_all": [
            "auth-jwt"
        ],
        "expect_partial": [],
        "expect_any": [],
        "expect_top1": "auth-jwt",
        "forbidden_all": [],
        "reason": "topic_id 子串 th-jw ⊂ auth-jwt；两词元按子串归属全中进 all。",
        "top1_scored": True
    },
    {
        "id": "Q10",
        "topics": [
            "跨区域分布式缓存一致性协议在失效回退与流量切换场景下的工程实践性能权衡记录"
        ],
        "expect_all": [
            "long-title-cache-a",
            "long-title-cache-b"
        ],
        "expect_partial": [],
        "expect_any": [
            "case-00-alpha",
            "case-01-beta",
            "case-02-gamma",
            "case-03-delta",
            "case-04-epsilon",
            "case-05-zeta",
            "case-06-eta",
            "case-07-theta",
            "case-08-iota",
            "case-09-kappa",
            "case-26-lyra",
            "case-27-nova",
            "case-28-atlas",
            "case-29-hermes",
            "case-30-apollo",
            "case-31-artemis",
            "case-32-helios",
            "case-33-selene",
            "case-34-eros",
            "case-35-iris",
            "case-36-juno",
            "case-37-minerva",
            "case-38-ceres",
            "case-39-vesta",
            "incident-postmortem-record",
            "perf-dataplane-tuning",
            "rd-coder-pattern-and-review-convergence",
            "release-canary-retro",
            "retrieval-evolution",
            "sqlite-fts5-index"
        ],
        "expect_top1": "long-title-cache-b",
        "forbidden_all": [],
        "reason": "长标题 near-duplicate：公共长短语使两个标题全中进 all；bm25 取更优者。",
        "top1_scored": True
    },
    {
        "id": "Q11",
        "topics": [
            "topic 匹配",
            "RAG",
            "检索"
        ],
        "expect_all": [
            "topicdock-match-rag"
        ],
        "expect_partial": [
            "rd-coder-pattern-and-review-convergence"
        ],
        "expect_any": [
            "retrieval-evolution",
            "search-ranking-eval"
        ],
        "expect_top1": "topicdock-match-rag",
        "forbidden_all": [
            "rd-coder-pattern-and-review-convergence"
        ],
        "reason": "71 分假阳性复现：合法主题全中进 all，无关主题仅 3/4 进 partial 并列入 forbidden_all。",
        "top1_scored": True
    },
    {
        "id": "Q12",
        "topics": [
            "分桶"
        ],
        "expect_all": [
            "incident-postmortem-record",
            "release-canary-retro"
        ],
        "expect_partial": [],
        "expect_any": [],
        "expect_top1": "release-canary-retro",
        "forbidden_all": [],
        "reason": "字段优先级：分桶仅在 release-canary-retro 标题(3.0)、仅在 incident-postmortem-record R 轨(1.2)，title 胜出；权重互换时 top-1 翻转。",
        "top1_scored": True
    },
    {
        "id": "Q13",
        "topics": [
            "幂等"
        ],
        "expect_all": [
            "retry-semantics"
        ],
        "expect_partial": [],
        "expect_any": [],
        "expect_top1": "retry-semantics",
        "forbidden_all": [],
        "reason": "Q 轨命中：幂等仅在 retry-semantics 的 Q 条目（全库唯一出现点）。",
        "top1_scored": True
    },
    {
        "id": "Q14",
        "topics": [
            "配额"
        ],
        "expect_all": [
            "capacity-governance"
        ],
        "expect_partial": [],
        "expect_any": [],
        "expect_top1": "capacity-governance",
        "forbidden_all": [],
        "reason": "A 轨命中：配额仅在 capacity-governance 的 A 条目（全库唯一出现点）。",
        "top1_scored": True
    },
    {
        "id": "Q15",
        "topics": [
            "回滚"
        ],
        "expect_all": [
            "upgrade-failure-retro"
        ],
        "expect_partial": [],
        "expect_any": [],
        "expect_top1": "upgrade-failure-retro",
        "forbidden_all": [],
        "reason": "R 轨命中：回滚仅在 upgrade-failure-retro 的 R 条目（全库唯一出现点）。",
        "top1_scored": True
    },
    {
        "id": "Q16",
        "topics": [
            "向量重排"
        ],
        "expect_all": [
            "retrieval-evolution"
        ],
        "expect_partial": [],
        "expect_any": [],
        "expect_top1": "retrieval-evolution",
        "forbidden_all": [],
        "reason": "focus 命中：向量/量重/重排 仅在 retrieval-evolution 的 next_focus（全库唯一出现点）。",
        "top1_scored": True
    },
    {
        "id": "Q17",
        "topics": [
            "SQLite FTS5"
        ],
        "expect_all": [
            "sqlite-fts5-index"
        ],
        "expect_partial": [],
        "expect_any": [],
        "expect_top1": "sqlite-fts5-index",
        "forbidden_all": [],
        "reason": "中英混合 SQLite+FTS5 唯一命中 sqlite-fts5-index。",
        "top1_scored": True
    },
    {
        "id": "Q18",
        "topics": [
            "RAG 检索"
        ],
        "expect_all": [
            "topicdock-match-rag"
        ],
        "expect_partial": [
            "rd-coder-pattern-and-review-convergence",
            "retrieval-evolution",
            "search-ranking-eval"
        ],
        "expect_any": [],
        "expect_top1": "topicdock-match-rag",
        "forbidden_all": [],
        "reason": "中英混合 RAG+检索 唯一命中 topicdock-match-rag。",
        "top1_scored": True
    },
    {
        "id": "Q19",
        "topics": [
            "缓存",
            "优化"
        ],
        "expect_all": [
            "case-02-gamma",
            "case-03-delta",
            "case-04-epsilon",
            "case-05-zeta",
            "case-06-eta",
            "case-26-lyra",
            "case-27-nova",
            "case-28-atlas",
            "case-29-hermes",
            "case-30-apollo",
            "case-34-eros",
            "case-35-iris",
            "case-38-ceres",
            "case-39-vesta",
            "perf-dataplane-tuning"
        ],
        "expect_partial": [
            "case-00-alpha",
            "case-01-beta",
            "case-07-theta",
            "case-08-iota",
            "case-09-kappa",
            "case-31-artemis",
            "case-32-helios",
            "case-33-selene",
            "case-36-juno",
            "case-37-minerva",
            "long-title-cache-a",
            "long-title-cache-b",
            "sqlite-fts5-index"
        ],
        "expect_any": [],
        "expect_top1": "case-38-ceres",
        "forbidden_all": [],
        "reason": "2 个高频词组合：缓存+优化 收缩；bm25 非退化，按 bm25 取最优。",
        "top1_scored": True
    },
    {
        "id": "Q20",
        "topics": [
            "设计",
            "实现",
            "验证"
        ],
        "expect_all": [
            "case-06-eta",
            "case-08-iota"
        ],
        "expect_partial": [
            "case-01-beta",
            "case-03-delta",
            "case-04-epsilon",
            "case-05-zeta",
            "case-07-theta",
            "case-09-kappa"
        ],
        "expect_any": [
            "case-00-alpha",
            "case-02-gamma"
        ],
        "expect_top1": "case-08-iota",
        "forbidden_all": [],
        "reason": "小库（前 10 主题）三高频词组合；bm25 退化按 updated_at。",
        "top1_scored": True,
        "library_size": 10
    }
]
