-- PharmaPilot: example SQL queries over data/pharmapilot.db
-- Run one with:  python -c "import sqlite3;c=sqlite3.connect('data/pharmapilot.db');print(c.execute(open('q.sql').read()).fetchall())"
-- or open the database in "DB Browser for SQLite" and paste a query.
-- Reference date for the whole dataset ("today") = 2026-11-25.

-- Q1. The Montelukast 10 mg formula (BOM): what goes into one tablet.
SELECT m.name, b.function, b.qty_mg_per_unit, b.nominal_pct, b.approved_min_pct, b.approved_max_pct
FROM bom b JOIN materials m USING (material_id)
WHERE b.product_id = 1
ORDER BY b.qty_mg_per_unit DESC;

-- Q2. Batch readiness: kg needed for 200,000 tablets of product 1 vs released stock
--     that is still valid on the planned date (2026-12-10). Shortage when available < needed.
WITH need AS (
  SELECT b.material_id, m.name, m.unit,
         200000 * b.qty_mg_per_unit / CASE m.unit WHEN 'kg' THEN 1000000.0 ELSE 1000.0 END AS needed
  FROM bom b JOIN materials m USING (material_id)
  WHERE b.product_id = 1
), stock AS (
  SELECT material_id, SUM(qty) AS available
  FROM material_lots
  WHERE status = 'released' AND qty > 0 AND expiry_date > '2026-12-10'
  GROUP BY material_id
)
SELECT name, unit, ROUND(needed, 3) AS needed, ROUND(COALESCE(available, 0), 3) AS available,
       CASE WHEN COALESCE(available, 0) >= needed THEN 'OK' ELSE 'SHORTAGE' END AS status
FROM need LEFT JOIN stock USING (material_id);

-- Q3. FEFO order: Montelukast sodium lots usable on 2026-12-10, earliest expiry first.
SELECT lot_no, qty, unit, expiry_date, assay_pct, warehouse_location
FROM material_lots
WHERE material_id = 1 AND status = 'released' AND qty > 0 AND expiry_date > '2026-12-10'
ORDER BY expiry_date;

-- Q4. Data errors the agent must catch: lots marked 'released' but already expired.
SELECT lot_no, material_id, expiry_date, status
FROM material_lots
WHERE status = 'released' AND expiry_date < '2026-11-25';

-- Q5. Lots expiring in the next 60 days (excluding the ones already flagged expired).
SELECT lot_no, material_id, qty, unit, expiry_date, status
FROM material_lots
WHERE expiry_date BETWEEN '2026-11-25' AND date('2026-11-25', '+60 day')
ORDER BY expiry_date;

-- Q6. All OOS / OOT stability results with their batch and product.
SELECT r.result_status, s.study_id, s.batch_no, p.name, p.strength_mg, s.condition,
       r.timepoint_month, r.test_name, r.result_value
FROM stability_results r
JOIN stability_studies s USING (study_id)
JOIN production_batches pb USING (batch_no)
JOIN products p USING (product_id)
WHERE r.result_status <> 'pass'
ORDER BY r.result_status DESC, s.study_id, r.timepoint_month;

-- Q7. Assay trend of one study (study 104 has OOT then OOS): time zero vs every pull.
SELECT timepoint_month, pull_date, result_value, result_status
FROM stability_results
WHERE study_id = 104 AND test_name = 'assay'
ORDER BY timepoint_month;

-- Q8. Stability results joined to their HPLC run and system-suitability (SST) verdict.
SELECT r.study_id, r.timepoint_month, r.test_name, r.result_value,
       h.hplc_run_id, h.sst_rsd_pct, h.sst_plates, h.sst_asymmetry, h.sst_pass
FROM stability_results r
LEFT JOIN hplc_runs h USING (hplc_run_id)
WHERE r.study_id = 1 AND r.timepoint_month = 0;

-- Q9. Failed HPLC runs (MOA rules: RSD <= 2.0, asymmetry <= 2, plates >= 1300, >= 2000 for related substances).
SELECT hplc_run_id, test_name, run_date, sst_rsd_pct, sst_plates, sst_asymmetry
FROM hplc_runs
WHERE sst_pass = 0
ORDER BY run_date DESC
LIMIT 20;

-- Q10. Formulation evidence: past disintegrant trials on Montelukast 10 mg and their outcome.
SELECT t.trial_id, m.name, t.deviation_pct, t.dissolution_20min, t.disintegration_min, t.verdict
FROM formulation_trials t JOIN materials m USING (material_id)
WHERE t.product_id = 1 AND m.name = 'Croscarmellose sodium'
ORDER BY t.deviation_pct;

-- Q11. Official impurity limits for the Montelukast API (Ph. Eur.) and tablets (BP).
SELECT COALESCE('API material ' || material_id, 'Product ' || product_id) AS applies_to,
       impurity, limit_pct, relative_retention, source
FROM impurity_limits_reference
ORDER BY applies_to, impurity;

-- Q12. Traceability: which material lots went into one production batch.
SELECT u.batch_no, u.lot_no, m.name, u.qty_used, u.unit
FROM batch_material_usage u
JOIN material_lots l USING (lot_no)
JOIN materials m ON m.material_id = l.material_id
WHERE u.batch_no = 'BATCH-00001';
