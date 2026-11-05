# -*- coding: utf-8 -*-
"""Unit tests for dim-to-column mapping in the context pack.

Run: python -m pytest packages/qwenpaw-data-context/tests/test_schema_prompt_dim_mapping.py -v
"""
from __future__ import annotations

import unittest

from context_manager.api.cm_models import DrillDimension
from context_manager.api.semantic_pack import (
    _build_drill_dimensions,
    render_schema_prompt,
)

class DrillDimMappingTests(unittest.TestCase):
    """raw.drill_dims 携带的物理列映射应透传到模型并渲染进 schema_prompt。"""

    def test_build_passes_column_mapping(self):
        expanded = {
            "raw": {
                "drill_dims": [
                    {
                        "key": "dim:ds-1:Sales:地区",
                        "name": "地区",
                        "dimension_type": "普通维度",
                        "column": "region_name",
                        "column_table": "fact_orders",
                        "expr": "select(region_name)",
                    },
                    {
                        "key": "dim:ds-1:Sales:日期",
                        "name": "日期",
                        "dimension_type": "普通维度",
                        "column": "",
                        "column_table": "",
                        "expr": "",
                    },
                ]
            }
        }
        dims = _build_drill_dimensions(expanded, cap=8)
        self.assertEqual(len(dims), 2)
        self.assertEqual(dims[0].column, "region_name")
        self.assertEqual(dims[0].column_table, "fact_orders")
        self.assertEqual(dims[0].expr, "select(region_name)")
        # 未绑定列的维度保持空串,向后兼容
        self.assertEqual(dims[1].column, "")

    def test_schema_prompt_renders_mapping(self):
        focus = type("F", (), {
            "metric_name": "order_count",
            "description": "订单数",
            "caliber": "",
            "aliases": [],
            "unit": "单",
            "source_columns": [],
            "drill_dimensions": [
                DrillDimension(
                    name="地区",
                    relationship="普通维度",
                    column="region_name",
                    column_table="fact_orders",
                ),
                DrillDimension(name="渠道", relationship="普通维度"),
            ],
            "common_filters": [],
            "table_joins": [],
        })()
        prompt = render_schema_prompt([focus], [])
        # 中文维度名旁带物理列名
        self.assertIn("地区(region_name)", prompt)
        self.assertIn("渠道", prompt)
        # 映射行带宿主表
        self.assertIn("地区 → fact_orders.region_name", prompt)
        # 无绑定的维度不渲染映射行
        self.assertNotIn("渠道 → ", prompt)

    def test_schema_prompt_legacy_shape_unchanged(self):
        """旧字段形态(无映射)不渲染映射块,保持向后兼容。"""
        focus = type("F", (), {
            "metric_name": "m",
            "description": "",
            "caliber": "",
            "aliases": [],
            "unit": "",
            "source_columns": [],
            "drill_dimensions": [DrillDimension(name="类别")],
            "common_filters": [],
            "table_joins": [],
        })()
        prompt = render_schema_prompt([focus], [])
        self.assertIn("可下钻维度: 类别", prompt)
        self.assertNotIn("维度列映射:", prompt)


if __name__ == '__main__':
    unittest.main()
