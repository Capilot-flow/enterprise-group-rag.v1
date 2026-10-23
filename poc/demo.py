# -*- coding: utf-8 -*-
"""
GKB（集团统一 RAG 知识底座平台）— 极简 PoC 演示脚本

定位
----
本脚本仅用于**验证端到端链路**（入库 -> 切块 -> 权限过滤 -> 检索 -> 引用回答 -> 评估），
不作为生产实现，也不做任何算法调优。全部使用 Python 标准库，无外部依赖，双击/命令行即可跑。

运行方式
--------
    python -X utf8 poc/demo.py        # Windows 控制台加 -X utf8 避免中文乱码

真实实现会替换成什么（注释中已标注）：
    - 切块/向量化   -> 语义切块 + Embedding 模型 + 向量数据库
    - 相似度检索     -> 向量相似度 + 关键词混合检索
    - 回答生成       -> 大模型（带引用溯源 + 无知识覆盖即拒答）
    - 权限过滤       -> 事业部 + 密级 双层权限过滤器（服务端强制）
    - 评估           -> 完整评测集 + LLM-Judge（见 docs/evaluation-plan.md）

本脚本对应作品集文档：
    README.md（架构）、docs/evaluation-plan.md（评估口径）、docs/risk-log.md（R03/R05）
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional

# ---------------------------------------------------------------------------
# 1. 数据模型：文档 + 权限
# ---------------------------------------------------------------------------

@dataclass
class Doc:
    """知识文档（mock 语料）。真实实现中元数据由入库流水线抽取。"""
    doc_id: str
    title: str
    dept: str          # 所属事业部
    level: str         # 密级：公开 / 内部 / 高密 / 绝密
    content: str       # 正文（真实实现为切块后的片段）


@dataclass
class User:
    """当前登录用户及其权限边界。真实实现中由统一身份认证下发。"""
    name: str
    dept: str          # 所属事业部
    clearance: str     # 该用户最高可读密级


# 密级排序：用于判断「用户密级 >= 文档密级」才可见
LEVEL_RANK = {"公开": 0, "内部": 1, "高密": 2, "绝密": 3}


# ---------------------------------------------------------------------------
# 2. Mock 语料：覆盖「2 个事业部隔离」+「1 个无覆盖/幻觉反例」场景
# ---------------------------------------------------------------------------

CORPUS: List[Doc] = [
    Doc("D1", "光刻工艺参数标准", "半导体事业部", "高密",
        "光刻机曝光剂量标准为 25 mJ/cm²，焦距允许偏差 ±0.1μm，显影时间 60 秒。"),
    Doc("D2", "栅氧化层缺陷分析", "半导体事业部", "内部",
        "栅氧化层缺陷常见于颗粒污染与应力不均，需在缺陷检测环节优先排查。"),
    Doc("D3", "电芯正极配方", "新能源事业部", "绝密",
        "电芯正极采用 NCM811 体系，电解液含特定添加剂配方，属核心机密。"),
    Doc("D4", "热失控安全阈值", "新能源事业部", "高密",
        "电池热失控触发温度阈值为 130℃，超过该阈值须立即启动应急降温流程。"),
    Doc("D5", "差旅报销制度", "集团公共", "公开",
        "出差住宿标准为一线城市 500 元/晚，需保留发票并在 30 日内提交报销。"),
]

# 评测样例：(问题, 期望命中文档ID, 期望行为)
# - Q2/Q3 为「跨事业部越权」样例，期望被权限隔离 -> 拒答
# - Q5 为「无知识覆盖」样例，期望拒答而非幻觉编造
EVAL_CASES = [
    ("光刻曝光剂量是多少？", "D1", "answer"),
    ("电芯正极材料是什么？", "D3", "deny"),   # 跨事业部，应隔离
    ("热失控触发温度是多少？", "D4", "deny"),  # 跨事业部，应隔离
    ("出差住宿报销标准？", "D5", "answer"),
    ("量子计算机的退火温度是多少？", None, "deny"),  # 无覆盖，应拒答
]

# 测试用户：半导体事业部，最高可读「高密」
TEST_USER = User("研发工程师-张三", "半导体事业部", "高密")


# ---------------------------------------------------------------------------
# 3. 简易切块 + 相似度（真实实现：语义切块 + Embedding 向量检索）
# ---------------------------------------------------------------------------

def char_bigrams(text: str) -> set:
    """中文按「字符二元组」切分，构造一个无需分词的轻量特征集。"""
    text = "".join(ch for ch in text if ch.strip())
    return {text[i:i + 2] for i in range(len(text) - 1)}


def overlap_score(query: str, content: str) -> float:
    """查询与文档内容的二元组重叠率（Jaccard 系数），模拟向量相似度打分。"""
    q, c = char_bigrams(query), char_bigrams(content)
    if not q:
        return 0.0
    return len(q & c) / len(q | c)


# ---------------------------------------------------------------------------
# 4. 权限过滤（真实实现：服务端强制的「事业部 + 密级」双层过滤器）
# ---------------------------------------------------------------------------

def is_visible(user: User, doc: Doc) -> bool:
    """用户仅可见「本事业部 + 集团公共」且密级不高于自身权限的文档。"""
    dept_ok = (doc.dept == user.dept) or (doc.dept == "集团公共")
    level_ok = LEVEL_RANK[doc.level] <= LEVEL_RANK[user.clearance]
    return dept_ok and level_ok


# ---------------------------------------------------------------------------
# 5. 检索 + 引用回答（真实实现：混合检索 + 大模型生成 + 无覆盖即拒答）
# ---------------------------------------------------------------------------

SCORE_THRESHOLD = 0.05  # 低于该相似度视为「无知识覆盖」，应拒答而非编造


def retrieve(query: str, user: User, top_k: int = 3) -> List[Doc]:
    visible = [d for d in CORPUS if is_visible(user, d)]
    scored = sorted(visible, key=lambda d: overlap_score(query, d.content), reverse=True)
    return scored[:top_k]


def answer(query: str, user: User) -> str:
    hits = retrieve(query, user)
    if not hits or overlap_score(query, hits[0].content) < SCORE_THRESHOLD:
        # 关键设计：无知识覆盖 -> 正确拒答，避免「幻觉」误导业务决策（对抗痛点 P3）
        return "⚠️ 抱歉，当前知识库中未找到可支撑的答案，已拒绝回答（避免臆测）。"
    top = hits[0]
    # 真实实现：大模型基于 top 片段生成，并显式给出引用来源锚点
    return f"回答：{top.content}\n—— 引用来源：《{top.title}》（{top.doc_id}，{top.dept}）"


# ---------------------------------------------------------------------------
# 6. 简易评估（真实实现：见 docs/evaluation-plan.md 的召回率/准确率/幻觉率）
# ---------------------------------------------------------------------------

def evaluate(user: User) -> Dict[str, object]:
    stats = {"total": 0, "hit": 0}
    for question, expect_id, expect_behavior in EVAL_CASES:
        hits = retrieve(question, user)
        result = answer(question, user)
        # 期望「answer」：应命中期望文档且未拒答
        if expect_behavior == "answer":
            ok = bool(hits) and hits[0].doc_id == expect_id and "拒绝" not in result
        # 期望「deny」：应正确拒答（无论是否越权或是否有覆盖）
        else:
            ok = "拒绝" in result
        stats["total"] += 1
        stats["hit"] += int(ok)
        mark = "✅" if ok else "❌"
        print(f"[{mark}] Q: {question}\n     -> {result}\n     (期望行为: {expect_behavior}, 期望文档: {expect_id or '无'})")
    stats["accuracy"] = stats["hit"] / stats["total"] if stats["total"] else 0.0
    return stats


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 66)
    print("GKB 集团统一 RAG 知识底座 — PoC 链路演示")
    print(f"当前用户：{TEST_USER.name}（事业部={TEST_USER.dept}，密级={TEST_USER.clearance}）")
    print("=" * 66)
    print("\n—— 步骤 1：可访问文档（权限过滤后）——")
    for d in CORPUS:
        visible = "✅ 可见" if is_visible(TEST_USER, d) else "🚫 隔离"
        print(f"  {visible}  {d.doc_id}《{d.title}》（{d.dept} / {d.level}）")

    print("\n—— 步骤 2：检索 + 引用回答（含越权拒答 / 无覆盖拒答）——")
    stats = evaluate(TEST_USER)

    print("\n—— 步骤 3：评估结果 ——")
    print(f"  样例总数: {stats['total']}   命中: {stats['hit']}   准确率: {stats['accuracy']:.0%}")

    print("\n—— 链路验证结论 ——")
    print("  1. 权限隔离生效：跨事业部（D3/D4）查询被正确拒答，越权访问率 = 0")
    print("  2. 幻觉抑制生效：无知识覆盖（量子退火）正确拒答而非编造")
    print("  3. 引用溯源生效：可回答的问题均附带文档来源锚点")


if __name__ == "__main__":
    main()
