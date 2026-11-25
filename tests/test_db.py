"""Verify the generated SQLite database and CSV exports using only stdlib."""

import csv
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
AS_OF = "2026-11-25"
COUNTS = {
    "products": (23, 27), "materials": (115, 125), "suppliers": (45, 55),
    "bom": (225, 275), "material_lots": (5900, 6100),
    "production_batches": (2900, 3100), "batch_material_usage": (14000, 16000),
    "specifications": (275, 325), "stability_studies": (380, 420),
    "stability_results": (25000, 40000), "hplc_runs": (7800, 8200),
    "formulation_trials": (1450, 1550), "impurity_limits_reference": (16, 25),
}


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        path = DATA / "pharmapilot.db"
        self.assertTrue(path.is_file(), "Generate data/pharmapilot.db first")
        self.db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        self.addCleanup(self.db.close)

    def assert_no_rows(self, query, params=()):
        self.assertIsNone(self.db.execute(query, params).fetchone(), query)

    def test_counts_and_csv_contents(self):
        self.assertEqual({p.stem for p in (DATA / "csv").glob("*.csv")}, set(COUNTS))
        for table, (minimum, maximum) in COUNTS.items():
            with self.subTest(table=table):
                rows = self.db.execute(f"SELECT * FROM {table} ORDER BY rowid")
                header = [item[0] for item in rows.description]
                expected = [[str(value) if value is not None else "" for value in row]
                            for row in rows]
                self.assertLessEqual(minimum, len(expected))
                self.assertLessEqual(len(expected), maximum)
                with (DATA / "csv" / f"{table}.csv").open(encoding="utf-8", newline="") as file:
                    self.assertEqual(list(csv.reader(file)), [header, *expected])

    def test_integrity_and_temporal_rules(self):
        self.assertEqual(self.db.execute("PRAGMA integrity_check").fetchone(), ("ok",))
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assert_no_rows("""SELECT 1 FROM material_lots
            WHERE expiry_date <= mfg_date OR received_date < mfg_date
            OR received_date > ? OR (status = 'expired' AND expiry_date >= ?)
            OR (retest_date IS NOT NULL AND retest_date <= mfg_date)""", (AS_OF, AS_OF))
        self.assert_no_rows("SELECT 1 FROM production_batches WHERE expiry_date <= mfg_date")
        self.assert_no_rows("""SELECT 1 FROM batch_material_usage u
            JOIN production_batches b USING (batch_no) JOIN material_lots l USING (lot_no)
            LEFT JOIN bom ON bom.product_id=b.product_id AND bom.material_id=l.material_id
            WHERE bom.material_id IS NULL OR b.status='planned' OR u.unit<>l.unit
            OR u.qty_used<=0 OR b.mfg_date<l.received_date OR b.mfg_date>l.expiry_date""")
        self.assert_no_rows("""SELECT 1 FROM stability_results r
            JOIN stability_studies s USING (study_id)
            JOIN production_batches b USING (batch_no)
            WHERE r.pull_date < s.start_date OR r.pull_date > ?
            OR s.start_date < b.mfg_date OR b.status <> 'released'""", (AS_OF,))

    def test_inventory_edge_cases(self):
        for predicate, bounds in [
            ("expiry_date BETWEEN '2026-11-25' AND '2027-01-24'", (270, 330)),
            ("status='quarantine'", (160, 200)),
            ("status='released' AND expiry_date<'2026-11-25'", (1, 100)),
        ]:
            count, = self.db.execute(f"SELECT COUNT(*) FROM material_lots WHERE {predicate}").fetchone()
            self.assertLessEqual(bounds[0], count)
            self.assertLessEqual(count, bounds[1])

    def test_spec_and_trend_status(self):
        self.assert_no_rows("""SELECT 1 FROM stability_results r
            JOIN stability_studies s USING (study_id)
            JOIN production_batches b USING (batch_no)
            LEFT JOIN specifications p ON p.product_id=b.product_id AND p.test_name=r.test_name
            LEFT JOIN stability_results zero ON zero.study_id=r.study_id
                AND zero.test_name=r.test_name AND zero.timepoint_month=0
            WHERE p.test_name IS NULL OR zero.test_name IS NULL OR r.unit<>p.unit
            OR r.result_status <> CASE
              WHEN r.result_value<p.limit_min OR r.result_value>p.limit_max THEN 'oos'
              WHEN r.test_name='assay' AND ROUND(zero.result_value-r.result_value, 3)>=4 THEN 'oot'
              ELSE 'pass' END""")
        self.assertEqual(
            {row[0] for row in self.db.execute("SELECT DISTINCT result_status FROM stability_results")},
            {"pass", "oot", "oos"},
        )

    def test_sst_and_run_links(self):
        self.assert_no_rows("""SELECT 1 FROM hplc_runs WHERE sst_pass <>
            (sst_rsd_pct<=2 AND sst_asymmetry<=2
             AND sst_plates>=CASE WHEN test_name='related_substances' THEN 2000 ELSE 1300 END)""")
        failures, = self.db.execute("SELECT COUNT(*) FROM hplc_runs WHERE sst_pass=0").fetchone()
        self.assertTrue(140 <= failures <= 180)
        self.assert_no_rows("""SELECT 1 FROM stability_results r JOIN hplc_runs h USING (hplc_run_id)
            WHERE h.sst_pass<>1 OR h.run_date<>r.pull_date OR h.test_name<>
                CASE WHEN r.test_name LIKE 'impurity_%' OR r.test_name='total_impurities'
                     THEN 'related_substances' ELSE r.test_name END""")

    def test_reproducible_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated = Path(tmp)
            subprocess.run(
                [sys.executable, str(ROOT / "generator" / "generate_db.py"),
                 "--seed", "42", "--output-dir", str(generated)],
                check=True, capture_output=True,
            )
            for file in (DATA / "csv").glob("*.csv"):
                self.assertEqual(file.read_bytes(), (generated / "csv" / file.name).read_bytes())
            with closing(sqlite3.connect(generated / "pharmapilot.db")) as regenerated:
                self.assertEqual(list(self.db.iterdump()), list(regenerated.iterdump()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
