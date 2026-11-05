# -*- coding: utf-8 -*-
"""Unit tests for qualified-name rewrite in the view fallthrough.

Run: python -m pytest packages/qwenpaw-data-context/tests/test_qualified_name_rewrite.py -v
"""

from __future__ import annotations

import unittest

from context_manager.view_fallthrough import rewrite_dataset_to_physical


class QualifiedNameRewriteTests(unittest.TestCase):
    """dataset→物理表兜底重写必须正确处理库名限定引用。"""

    def test_bare_name_replaced_with_schema_table(self):
        sql = "SELECT COUNT(*) FROM monthly_sales"
        out = rewrite_dataset_to_physical(
            sql, "monthly_sales", ("analytics", "dm_sales_monthly")
        )
        self.assertEqual(out, 'SELECT COUNT(*) FROM "analytics"."dm_sales_monthly"')

    def test_qualified_name_not_three_part(self):
        """db.dataset 限定引用应整段替换,不得产出 db.schema.table(语法错误回归)。"""
        sql = "SELECT COUNT(*) FROM analytics.monthly_sales WHERE status = 'paid'"
        out = rewrite_dataset_to_physical(
            sql, "monthly_sales", ("analytics", "dm_sales_monthly")
        )
        self.assertEqual(
            out,
            'SELECT COUNT(*) FROM "analytics"."dm_sales_monthly" WHERE status = \'paid\'',
        )
        self.assertNotIn("analytics.analytics", out)

    def test_backtick_qualified(self):
        sql = "SELECT * FROM `analytics`.`monthly_sales`"
        out = rewrite_dataset_to_physical(
            sql, "monthly_sales", ("analytics", "dm_sales_monthly")
        )
        self.assertEqual(out, "SELECT * FROM `analytics`.`dm_sales_monthly`")

    def test_backtick_both_sides(self):
        sql = "SELECT * FROM `analytics`.`monthly_sales`"
        out = rewrite_dataset_to_physical(
            sql, "monthly_sales", ("analytics", "dm_sales_monthly")
        )
        self.assertIn("dm_sales_monthly", out)
        self.assertNotIn("monthly_sales", out)

    def test_double_quoted_qualified(self):
        sql = 'SELECT * FROM "analytics"."monthly_sales"'
        out = rewrite_dataset_to_physical(
            sql, "monthly_sales", ("analytics", "dm_sales_monthly")
        )
        self.assertEqual(out, 'SELECT * FROM "analytics"."dm_sales_monthly"')

    def test_longer_name_not_matched(self):
        """更长名字的尾部不应被误伤(前缀重叠的另一个表)。"""
        sql = "SELECT * FROM fact_monthly_sales"
        out = rewrite_dataset_to_physical(
            sql, "monthly_sales", ("analytics", "dm_sales_monthly")
        )
        self.assertNotIn("dm_sales_monthly", out)


if __name__ == "__main__":
    unittest.main()
