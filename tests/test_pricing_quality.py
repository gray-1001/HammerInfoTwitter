"""价格模块质量测试：P-01 渠道白名单、P-02 低置信、P-03 匹配守卫、
P-05 观测时间、P-06 Excel 导入、P-07 涨跌口径、P-09 币种、P-13 清理、P-14 负价、P-15 链接。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from infotwitter.db import Database  # noqa: E402
from infotwitter.pricing import (  # noqa: E402
    CHANNEL_LABEL,
    _category_family,
    _safe_url,
    _valid_price,
    _weakest,
    build_slot5_rows,
    build_summary,
    edition_signature,
    expected_price,
    import_gw_excel,
    import_manual_csv,
    normalize_observed_at,
    price_movements,
    purge_mismatched_overseas,
    save_overseas_points,
)


class Cfg:
    def __init__(self, data: dict | None = None):
        self._d = {"pricing.rates": {"GBP_CNY": 9.2, "USD_CNY": 7.2, "EUR_CNY": 7.8}}
        if data:
            self._d.update(data)

    def get(self, key, default=None):
        return self._d.get(key, default)


class PriceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = Database(self.tmp / "p.db")
        self.cfg = Cfg()
        self._add_product("39-13", "ADEPTUS CUSTODES: CUSTODIAN GUARD")

    def tearDown(self):
        self.db.close()

    def _add_product(self, pid: str, name: str):
        self.db.conn.execute(
            "INSERT INTO products (id, name_en, product_line, category, status,"
            " first_seen_at, updated_at) VALUES (?,?,?,?,'active','2026-11-17','2026-11-17')",
            (pid, name, "40k", "mini"),
        )
        self.db.conn.commit()

    def _point(self, pid, channel, platform, price, observed="2026-11-17",
               confidence="high", url="", currency="CNY"):
        self.db.conn.execute(
            "INSERT OR REPLACE INTO price_points (product_id, channel, platform, price,"
            " currency, price_cny, url, observed_at, confidence, note)"
            " VALUES (?,?,?,?,?,?,?,?,?, '')",
            (pid, channel, platform, price, currency, price, url, observed, confidence),
        )
        self.db.conn.commit()


class TestChannelWhitelist(PriceCase):
    """P-01：人工 CSV 不能写官方渠道。"""

    def _csv(self, body: str) -> Path:
        path = self.tmp / "s.csv"
        path.write_text(body, encoding="utf-8")
        return path

    def test_official_channel_rejected(self):
        p = self._csv("sku_id,sku_name,channel,vendor,price,currency,in_stock,url\n"
                      "39-13,,cn_official,FAKE,1.5,CNY,1,\n")
        stats = import_manual_csv(self.db.conn, p, self.cfg)
        self.assertEqual(stats["saved"], 0)
        self.assertEqual(stats["rejected_channel"], 1)
        n = self.db.conn.execute("SELECT COUNT(*) c FROM price_points").fetchone()["c"]
        self.assertEqual(n, 0, "伪造的官店价不能入库")

    def test_unknown_channel_rejected(self):
        p = self._csv("sku_id,sku_name,channel,vendor,price,currency,in_stock,url\n"
                      "39-13,,bogus_channel,X,500,CNY,1,\n")
        stats = import_manual_csv(self.db.conn, p, self.cfg)
        self.assertEqual(stats["saved"], 0)

    def test_reserved_vendor_rejected(self):
        p = self._csv("sku_id,sku_name,channel,vendor,price,currency,in_stock,url\n"
                      "39-13,,taobao,GW China RRP,500,CNY,1,\n")
        stats = import_manual_csv(self.db.conn, p, self.cfg)
        self.assertEqual(stats["saved"], 0, "人工数据不能占用系统平台名")

    def test_allowed_channel_saved(self):
        p = self._csv("sku_id,sku_name,channel,vendor,price,currency,in_stock,url\n"
                      "39-13,,xianyu,某卖家,320,CNY,1,https://x/1\n")
        stats = import_manual_csv(self.db.conn, p, self.cfg)
        self.assertEqual(stats["saved"], 1)
        row = self.db.conn.execute("SELECT channel, platform FROM price_points").fetchone()
        self.assertEqual(row["channel"], "xianyu")

    def test_sku_id_with_wrong_name_rejected(self):
        """货号对上但名称明显不是同一个商品 → 拒绝（否则价格会挂到错商品上）。"""
        self._add_product("01-08", "ADEPTUS CUSTODES: SISTERS OF SILENCE")
        p = self._csv("sku_id,sku_name,channel,vendor,price,currency,in_stock,url\n"
                      "01-08,ORK BOYZ MOB,xianyu,X,300,CNY,1,\n")
        stats = import_manual_csv(self.db.conn, p, self.cfg)
        self.assertEqual(stats["saved"], 0)


class TestManualCsvEncoding(PriceCase):
    """P-12：GBK 导出的 CSV 也要能读。"""

    def test_gbk_csv(self):
        path = self.tmp / "gbk.csv"
        path.write_bytes(
            "sku_id,sku_name,channel,vendor,price,currency,in_stock,url\n"
            "39-13,,taobao,某商店,320,CNY,1,\n".encode("gb18030")
        )
        stats = import_manual_csv(self.db.conn, path, self.cfg)
        self.assertEqual(stats["saved"], 1)


class TestObservedAt(PriceCase):
    """P-05：观测时间必须统一成日期。"""

    def test_normalize(self):
        for raw, want in (("2026-11-17", "2026-11-17"),
                          ("2026-11-17T09:00:00+08:00", "2026-11-17"),
                          ("2026/11/17", "2026-11-17"),
                          ("", ""), ("2026", ""), ("今天", "")):
            self.assertEqual(normalize_observed_at(raw), want, raw)

    def test_two_formats_collapse_to_one_row(self):
        c = self.db.conn
        c.execute("INSERT OR REPLACE INTO price_points (product_id, channel, platform, price,"
                  " currency, price_cny, observed_at, confidence) VALUES"
                  " ('39-13','cn_official','GW China RRP',370,'CNY',370,?,'high')",
                  (normalize_observed_at("2026-11-17"),))
        c.execute("INSERT OR REPLACE INTO price_points (product_id, channel, platform, price,"
                  " currency, price_cny, observed_at, confidence) VALUES"
                  " ('39-13','cn_official','GW China RRP',370,'CNY',370,?,'high')",
                  (normalize_observed_at("2026-11-17T09:00:00+08:00"),))
        c.commit()
        n = c.execute("SELECT COUNT(*) c FROM price_points").fetchone()["c"]
        self.assertEqual(n, 1, "同一天两种格式必须归一，否则会造出假涨跌")


class TestLowConfidence(PriceCase):
    """P-02：low 不能进最低价、不能算涨跌、不能给行置信度背书。"""

    def test_low_not_used_as_lowest(self):
        self._point("39-13", "cn_official", "GW China RRP", 370)
        self._point("39-13", "xianyu", "闲鱼", 50, confidence="low")
        rows = build_summary(self.db.conn, self.cfg, limit=5)
        self.assertIsNone(rows[0]["lowest"], "低置信的 ¥50 不该成为最低价")

    def test_low_ignored_in_movements(self):
        self._point("39-13", "gw_uk", "Element Games", 200, observed="2026-11-10", confidence="low")
        self._point("39-13", "gw_uk", "Element Games", 100, observed="2026-11-16", confidence="low")
        self.assertEqual(price_movements(self.db.conn, 7), {})

    def test_normal_movement_still_computed(self):
        self._point("39-13", "cn_official", "GW China RRP", 400, observed="2026-11-15")
        self._point("39-13", "cn_official", "GW China RRP", 370, observed="2026-11-17")
        moves = price_movements(self.db.conn, 7)
        self.assertEqual(moves[("39-13", "cn_official")], -7.5)

    def test_weakest_confidence_wins(self):
        self.assertEqual(_weakest([{"confidence": "high"}, {"confidence": "medium"}]), "medium")
        self.assertEqual(_weakest([{"confidence": "high"}, None]), "high")
        self.assertEqual(_weakest([]), "")

    def test_slot5_row_confidence_is_weakest(self):
        self._point("39-13", "cn_official", "GW China RRP", 370)
        self._point("39-13", "xianyu", "闲鱼", 300, confidence="medium")
        self._point("39-13", "taobao", "淘宝", 320, confidence="high")
        rows = build_slot5_rows(self.db.conn, self.cfg, limit=1)
        self.assertEqual(rows[0]["confidence"], "medium", "行置信度取展示值里最弱的")


class TestOverseasGuard(PriceCase):
    """P-03：匹配守卫不能绕过。"""

    def test_missing_matched_is_rejected(self):
        saved = save_overseas_points(self.db.conn, self.cfg,
                                     {"39-13": {"price": 40.0, "url": "u", "score": 0.9}})
        self.assertEqual(saved, 0, "没有匹配名就无法校验，不能写")

    def test_japanese_edition_rejected(self):
        saved = save_overseas_points(
            self.db.conn, self.cfg,
            {"39-13": {"price": 40.0, "matched": "Adeptus Custodes Custodian Guard (Japanese)",
                       "score": 0.9}},
        )
        self.assertEqual(saved, 0)

    def test_codex_not_matched_to_mini(self):
        self._add_product("27-01", "CODEX: ADEPTUS CUSTODES")
        saved = save_overseas_points(
            self.db.conn, self.cfg,
            {"39-13": {"price": 40.0, "matched": "Codex: Adeptus Custodes", "score": 0.9}},
        )
        self.assertEqual(saved, 0, "规则书不能匹配到模型商品上")

    def test_normal_match_saved_with_full_note(self):
        name = "Adeptus Custodes Custodian Guard with Adrasite Spears (very long product title here)"
        saved = save_overseas_points(self.db.conn, self.cfg,
                                     {"39-13": {"price": 40.0, "matched": name, "score": 0.9}})
        self.assertEqual(saved, 1)
        note = self.db.conn.execute("SELECT note FROM price_points").fetchone()["note"]
        self.assertIn(name, note, "note 必须存完整匹配名，purge 才能正确反查")

    def test_purge_keeps_correct_long_match(self):
        name = "Adeptus Custodes Custodian Guard with Adarisite Spears Limited Edition Chinese"
        self._point("39-13", "cn_official", "GW China RRP", 370)
        save_overseas_points(self.db.conn, self.cfg,
                             {"39-13": {"price": 40.0, "matched": name, "score": 0.9}})
        result = purge_mismatched_overseas(self.db.conn)
        self.assertEqual(result["removed"], 0, "正确的匹配不该被清理掉")

    def test_edition_signature_detects_japanese(self):
        self.assertNotEqual(edition_signature("Custodian Guard (Japanese)"),
                            edition_signature("Custodian Guard"))

    def test_category_family_separates_kinds(self):
        self.assertEqual(_category_family("Codex: Adeptus Custodes"), "codex")
        self.assertEqual(_category_family("Adeptus Custodes Custodian Guard"), "mini")
        self.assertEqual(_category_family("Battleforce: Custodes"), "boxed")
        self.assertEqual(_category_family("Citadel Layer Paint"), "paint")


class TestExpectedPrice(PriceCase):
    """P-09：币种必须跟着价格走。"""

    def test_gbp(self):
        m = expected_price(109.0, "GBP", self.cfg)
        self.assertIsNotNone(m)
        self.assertLess(m["low"], m["mid"])
        self.assertLess(m["mid"], m["high"])

    def test_usd_uses_usd_rate(self):
        gbp = expected_price(100.0, "GBP", self.cfg)
        usd = expected_price(100.0, "USD", self.cfg)
        self.assertLess(usd["mid"], gbp["mid"], "美元价不能按英镑汇率换算")

    def test_unknown_currency(self):
        self.assertIsNone(expected_price(100.0, "JPY", self.cfg))

    def test_invalid_amount(self):
        self.assertIsNone(expected_price(0, "GBP", self.cfg))
        self.assertIsNone(expected_price(-5, "GBP", self.cfg))
        self.assertIsNone(expected_price(100.0, "GBP", None))


class TestPriceValidation(unittest.TestCase):
    def test_negative_and_zero(self):
        self.assertFalse(_valid_price(-1))
        self.assertFalse(_valid_price(0))
        self.assertFalse(_valid_price(None))
        self.assertFalse(_valid_price("100"))
        self.assertTrue(_valid_price(0.5))
        self.assertTrue(_valid_price(1330))

    def test_safe_url(self):
        self.assertEqual(_safe_url("https://x/1"), "https://x/1")
        self.assertEqual(_safe_url("javascript:alert(1)"), "")
        self.assertEqual(_safe_url(""), "")
        self.assertEqual(_safe_url(None), "")


class TestExcelImport(PriceCase):
    """P-06：不再静默丢价。"""

    def _make(self, rows, header):
        from openpyxl import Workbook

        path = self.tmp / "gw.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.append(header)
        for r in rows:
            ws.append(r)
        wb.save(path)
        return path

    HEADER = ["SS Code", "Product Code", "Description", "Barcode", "Qty",
              "Current CNR", "Current CNB", "New CNR", "New CNB"]

    def test_new_empty_falls_back_to_current(self):
        path = self._make([["39-13", "99070101001", "CUSTODIAN GUARD", "123", 1, 370, 300, None, None]],
                          self.HEADER)
        stats = import_gw_excel(self.db.conn, path)
        self.assertEqual(stats["price_points"], 2, "没有新价时应按现价记录")
        price = self.db.conn.execute(
            "SELECT price FROM price_points WHERE channel='cn_official'").fetchone()["price"]
        self.assertEqual(price, 370)

    def test_no_new_columns_at_all(self):
        """8 列的调价表（只有现价）以前会导入 0 个价格点却报成功。"""
        path = self._make([["39-13", "99070101001", "CUSTODIAN GUARD", "123", 1, 370, 300]],
                          self.HEADER[:7])
        stats = import_gw_excel(self.db.conn, path)
        self.assertEqual(stats["price_points"], 2)
        self.assertNotIn("error", stats)

    def test_merged_cell_code_inherited(self):
        path = self._make(
            [["39-13", "99070101001", "CUSTODIAN GUARD", "123", 1, 370, 300, 380, 310],
             [None, "99070101002", "CUSTODIAN GUARD", "123", 1, 370, 300, 380, 310]],
            self.HEADER,
        )
        stats = import_gw_excel(self.db.conn, path)
        self.assertEqual(stats["products"], 1)
        codes = self.db.conn.execute("SELECT product_codes FROM products").fetchone()["product_codes"]
        self.assertIn("99070101002", codes, "合并单元格的第二行货号不能丢")

    def test_numeric_code_accepted(self):
        path = self._make([["9901", "99070101003", "CUSTODIAN GUARD", "123", 1, 370, 300, 380, 310]],
                          self.HEADER)
        stats = import_gw_excel(self.db.conn, path)
        self.assertEqual(stats["products"], 1)

    def test_negative_price_rejected(self):
        path = self._make([["39-13", "99070101001", "CUSTODIAN GUARD", "123", 1, 370, 300, -50, None]],
                          self.HEADER)
        stats = import_gw_excel(self.db.conn, path)
        price = self.db.conn.execute(
            "SELECT price FROM price_points WHERE channel='cn_official'").fetchone()
        self.assertTrue(price is None or price["price"] > 0, "负价不能入库")
        self.assertGreaterEqual(stats["invalid_prices"], 1)

    def test_zero_points_reports_error(self):
        path = self._make([["bad-code", None, "", None, None, None, None, None, None]], self.HEADER)
        stats = import_gw_excel(self.db.conn, path)
        self.assertEqual(stats["price_points"], 0)
        self.assertIn("error", stats, "没写入任何价格点必须报错")

    def test_observed_at_normalized(self):
        path = self._make([["39-13", "99070101001", "CUSTODIAN GUARD", "123", 1, 370, 300, 380, 310]],
                          self.HEADER)
        import_gw_excel(self.db.conn, path, observed_at="2026-11-17T08:00:00+08:00")
        row = self.db.conn.execute("SELECT DISTINCT observed_at FROM price_points").fetchone()
        self.assertEqual(row["observed_at"], "2026-11-17")


class TestMovementScope(PriceCase):
    """P-07：多卖家渠道不算涨跌。"""

    def test_multi_platform_channel_skipped(self):
        self._point("39-13", "xianyu", "卖家甲", 300, observed="2026-11-15")
        self._point("39-13", "xianyu", "卖家乙", 100, observed="2026-11-16")
        self.assertEqual(price_movements(self.db.conn, 7), {},
                         "不同卖家的报价不是时间序列")

    def test_single_platform_channel_compared(self):
        self._point("39-13", "gw_uk", "Element Games", 100, observed="2026-11-15")
        self._point("39-13", "gw_uk", "Element Games", 90, observed="2026-11-17")
        moves = price_movements(self.db.conn, 7)
        self.assertEqual(moves[("39-13", "gw_uk")], -10.0)


class TestMatchScoring(unittest.TestCase):
    """匹配打分：品牌前缀不能把正确匹配稀释掉，也不能放过兄弟商品。"""

    def test_brand_prefix_does_not_dilute(self):
        from infotwitter.pricing import _similarity

        # 去掉 warhammer/40.000 之后是同一个商品
        self.assertGreaterEqual(
            _similarity("COMBAT PATROL: BATTLEZONE",
                        "WARHAMMER 40.000: COMBAT PATROL: BATTLEZONE"), 0.7)

    def test_subset_is_not_a_match(self):
        from infotwitter.pricing import _similarity

        # 候选是目标名的超集，但多了实质型号词 → 不能算匹配
        self.assertLess(
            _similarity("WARHAMMER 40000: VOID WAR BASES",
                        "BOARDING ACTIONS: VOID WAR BASES"), 0.7)

    def test_sibling_models_rejected(self):
        from infotwitter.pricing import _similarity

        self.assertLess(
            _similarity("ADEPTUS CUSTODES: CUSTODIAN GUARD",
                        "ADEPTUS CUSTODES: CUSTODIAN WARDENS"), 0.7)

    def test_paint_by_volume(self):
        from infotwitter.pricing import classify_category

        self.assertEqual(classify_category("AIR: EVIL SUNZ SCARLET (24ML)"), "paint")
        self.assertEqual(classify_category("B: STEEL LEGION DRAB 12ML ROW X6"), "paint")
        self.assertEqual(classify_category("ORKS: DEFFKOPTAS"), "mini")


class TestAutoRefreshBaseline(PriceCase):
    """A-08：中间插入一条 Excel 导入记录不能把 CSV 的 mtime 基准冲掉。"""

    def test_excel_run_does_not_reset_baseline(self):
        from infotwitter.pricing import auto_refresh, record_price_run

        csv_path = self.tmp / "s.csv"
        csv_path.write_text(
            "sku_id,sku_name,channel,vendor,price,currency,in_stock,url\n"
            "39-13,,xianyu,甲,320,CNY,1,\n", encoding="utf-8")
        cfg = Cfg({"pricing.manual_csv": str(csv_path)})
        first = auto_refresh(self.db.conn, cfg)
        self.assertEqual(first["csv_imported"], 1)

        # 模拟用户跑了一次 Excel 导入（这条 run 没有 csv_mtime）
        record_price_run(self.db.conn, "2026-W47", {"import_excel": {"rows": 10}})

        second = auto_refresh(self.db.conn, cfg)
        self.assertEqual(second["skipped"], "unchanged", "CSV 没变就不该再导一次")


if __name__ == "__main__":
    unittest.main()
