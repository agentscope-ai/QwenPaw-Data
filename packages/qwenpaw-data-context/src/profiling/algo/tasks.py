"""任务词汇表。

**只有任务定义**——依赖关系由各 context 的 ``SUPPORTED_TASKS`` 声明，
不在这里再维护一张 ``REQUIRES``（两处会漂移）。
"""

from __future__ import annotations

from enum import Enum


class ProfileTask(str, Enum):
    """算法任务。四个 —— 判据是「有独立的证据需求」。

    parse 不在其列：它没有独立证据需求、没有 LLM、没有用户审核阶段，
    因此是 ``QueryLogContext`` 的 fetch，不是任务。
    """

    DOMAIN = "domain"  # JOIN 聚类 + LLM 归域
    DATASET = "dataset"  # 物理表 → 逻辑视图，继承列元数据（可插拔）
    CALIBER = "columns_caliber"  # 列角色判定（维度/指标/日期一次定完）
    DICT = "columns_dict"  # 跨表聚合 + 名称归一 + 描述补全


TASK_ORDER: tuple[ProfileTask, ...] = (
    ProfileTask.DOMAIN,
    ProfileTask.DATASET,
    ProfileTask.CALIBER,
    ProfileTask.DICT,
)


#: 每个任务的产出存回哪个 context。profiler 用它决定 ⑤ 步存哪里。
#: **一律是 staged 侧** —— handler 的产出按定义就是待人核验的草稿；
#: confirmed 是后端 promote 之后的结果，算法永远不往那边写。
OUTPUT_CONTEXT: dict[ProfileTask, str] = {
    ProfileTask.DOMAIN: "domain_staged",
    ProfileTask.DATASET: "dataset_staged",
    ProfileTask.CALIBER: "columns_caliber_staged",
    ProfileTask.DICT: "columns_dict_staged",
}


__all__ = ["OUTPUT_CONTEXT", "TASK_ORDER", "ProfileTask"]
