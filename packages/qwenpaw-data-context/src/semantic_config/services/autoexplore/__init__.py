"""mg-088 物理层自动探查补全（命名锁 explore_scope；spec §1.4）。

首织完成（SUCCESS/DEGRADED，Q7）触发 / 手动端点触发 → 10 步探查管线
（spec §3）→ 物理层 Table/Column 双路径直写（不走 import 全链）→
explore_scope/explore_state 留痕。
"""
