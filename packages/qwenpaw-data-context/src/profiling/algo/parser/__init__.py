"""解析与抽取：原始 SQL → 四类证据。

管线住 ``.pipeline``（包内唯一坐在全部实现之上的模块）。门面只挂
``ParsedQueryLogInput`` —— 管线的类壳（``create`` / ``fingerprint`` /
``fetch``，与表层/日志层的来源类同构），包外消费者（组合根）只需要它。
约定与 ``inputs`` / ``contexts`` / ``snapshots`` 一致：**包外走门面，
包内走子模块**。

写新 parser 的人就在本包里：从 ``.base``（``EvidenceParser`` 协议、
``PARSER_VERSION``）与 ``.sql_analysis`` 拿地基，写完去 ``.pipeline``
的 ``default_parsers`` 里加一行。
"""

from .pipeline import ParsedQueryLogInput

__all__ = ["ParsedQueryLogInput"]
