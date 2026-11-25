"""Build the synthetic PharmaPilot SQLite dataset and matching CSV exports."""

import argparse
import calendar
import csv
import random
import sqlite3
import tempfile
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

AS_OF = date(2026, 11, 25)
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data"

SCHEMA = """
CREATE TABLE products (
    product_id INTEGER PRIMARY KEY, name TEXT NOT NULL, strength_mg REAL NOT NULL,
    dosage_form TEXT NOT NULL, shelf_life_months INTEGER NOT NULL, storage_condition TEXT NOT NULL,
    UNIQUE(name, strength_mg, dosage_form)
);
CREATE TABLE materials (
    material_id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL CHECK(type IN ('API','excipient','coating','packaging')),
    cas_no TEXT, unit TEXT NOT NULL CHECK(unit IN ('kg','g')),
    reorder_level REAL NOT NULL, min_order_qty REAL NOT NULL, std_lead_time_days INTEGER NOT NULL
);
CREATE TABLE suppliers (
    supplier_id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, country TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('manufacturer','distributor')),
    certificates TEXT, avg_lead_time_days INTEGER NOT NULL,
    on_time_rate REAL NOT NULL CHECK(on_time_rate BETWEEN 0 AND 1)
);
CREATE TABLE bom (
    product_id INTEGER REFERENCES products NOT NULL, material_id INTEGER REFERENCES materials NOT NULL,
    qty_mg_per_unit REAL NOT NULL CHECK(qty_mg_per_unit > 0), function TEXT NOT NULL,
    nominal_pct REAL NOT NULL, approved_min_pct REAL NOT NULL, approved_max_pct REAL NOT NULL,
    PRIMARY KEY(product_id, material_id),
    CHECK(0 <= approved_min_pct AND approved_min_pct <= nominal_pct
        AND nominal_pct <= approved_max_pct AND approved_max_pct <= 100)
);
CREATE TABLE material_lots (
    lot_no TEXT PRIMARY KEY NOT NULL, material_id INTEGER REFERENCES materials NOT NULL,
    supplier_id INTEGER REFERENCES suppliers NOT NULL, qty REAL NOT NULL CHECK(qty >= 0),
    unit TEXT NOT NULL CHECK(unit IN ('kg','g')), received_date TEXT NOT NULL, mfg_date TEXT NOT NULL,
    expiry_date TEXT NOT NULL, retest_date TEXT,
    status TEXT NOT NULL CHECK(status IN ('released','quarantine','rejected','expired')),
    assay_pct REAL, warehouse_location TEXT NOT NULL,
    CHECK(mfg_date <= received_date AND received_date <= expiry_date AND mfg_date < expiry_date)
);
CREATE TABLE production_batches (
    batch_no TEXT PRIMARY KEY NOT NULL, product_id INTEGER REFERENCES products NOT NULL,
    batch_size_units INTEGER NOT NULL CHECK(batch_size_units > 0), mfg_date TEXT NOT NULL,
    expiry_date TEXT NOT NULL CHECK(expiry_date > mfg_date),
    status TEXT NOT NULL CHECK(status IN ('planned','in_process','released','rejected'))
);
CREATE TABLE batch_material_usage (
    batch_no TEXT REFERENCES production_batches NOT NULL, lot_no TEXT REFERENCES material_lots NOT NULL,
    qty_used REAL NOT NULL CHECK(qty_used > 0), unit TEXT NOT NULL CHECK(unit IN ('kg','g')),
    PRIMARY KEY(batch_no,lot_no)
);
CREATE TABLE specifications (
    product_id INTEGER REFERENCES products NOT NULL, test_name TEXT NOT NULL, unit TEXT NOT NULL,
    limit_min REAL, limit_max REAL, source TEXT NOT NULL, PRIMARY KEY(product_id,test_name),
    CHECK((limit_min IS NOT NULL OR limit_max IS NOT NULL)
          AND (limit_min IS NULL OR limit_max IS NULL OR limit_min <= limit_max))
);
CREATE TABLE stability_studies (
    study_id INTEGER PRIMARY KEY, batch_no TEXT REFERENCES production_batches NOT NULL,
    condition TEXT NOT NULL, start_date TEXT NOT NULL, timepoints_planned TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','complete'))
);
CREATE TABLE hplc_runs (
    hplc_run_id INTEGER PRIMARY KEY, test_name TEXT NOT NULL, run_date TEXT NOT NULL,
    instrument_id TEXT NOT NULL, column TEXT NOT NULL, sst_rsd_pct REAL NOT NULL,
    sst_plates INTEGER NOT NULL, sst_asymmetry REAL NOT NULL, sst_pass INTEGER NOT NULL CHECK(sst_pass IN (0,1))
);
CREATE TABLE stability_results (
    study_id INTEGER REFERENCES stability_studies NOT NULL, timepoint_month INTEGER NOT NULL,
    pull_date TEXT NOT NULL, test_name TEXT NOT NULL, result_value REAL NOT NULL,
    unit TEXT NOT NULL, analyst_code TEXT NOT NULL, hplc_run_id INTEGER REFERENCES hplc_runs,
    result_status TEXT NOT NULL CHECK(result_status IN ('pass','oot','oos')),
    PRIMARY KEY(study_id,timepoint_month,test_name)
);
CREATE TABLE formulation_trials (
    trial_id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, date TEXT NOT NULL,
    material_id INTEGER NOT NULL, nominal_pct REAL NOT NULL, used_pct REAL NOT NULL,
    deviation_pct REAL NOT NULL, assay REAL NOT NULL, dissolution_20min REAL NOT NULL,
    total_impurities REAL NOT NULL, hardness_n REAL NOT NULL, disintegration_min REAL NOT NULL,
    verdict TEXT NOT NULL CHECK(verdict IN ('meets_spec','fails')), notes TEXT NOT NULL,
    FOREIGN KEY(product_id,material_id) REFERENCES bom
);
CREATE TABLE impurity_limits_reference (
    reference_id INTEGER PRIMARY KEY, material_id INTEGER REFERENCES materials,
    product_id INTEGER REFERENCES products, impurity TEXT NOT NULL, limit_pct REAL NOT NULL,
    source TEXT NOT NULL, relative_retention TEXT,
    CHECK((material_id IS NULL) <> (product_id IS NULL))
);
CREATE INDEX lot_material_expiry ON material_lots(material_id,expiry_date);
CREATE INDEX batch_product ON production_batches(product_id);
CREATE INDEX study_batch ON stability_studies(batch_no);
"""

PRODUCTS = (
    ("Montelukast", 10, "film-coated tablet"), ("Montelukast", 5, "chewable tablet"),
    ("Montelukast", 4, "chewable tablet"), ("Paracetamol", 500, "tablet"),
    ("Metformin", 500, "tablet"), ("Metformin", 850, "tablet"),
    ("Amlodipine", 5, "tablet"), ("Amlodipine", 10, "tablet"),
    ("Atorvastatin", 20, "tablet"), ("Atorvastatin", 40, "tablet"),
    ("Omeprazole", 20, "capsule"), ("Ibuprofen", 200, "tablet"),
    ("Ibuprofen", 400, "tablet"), ("Cetirizine", 10, "tablet"),
    ("Loratadine", 10, "tablet"), ("Fexofenadine", 120, "tablet"),
    ("Pantoprazole", 40, "tablet"), ("Esomeprazole", 20, "capsule"),
    ("Losartan", 50, "tablet"), ("Valsartan", 80, "tablet"),
    ("Candesartan", 8, "tablet"), ("Bisoprolol", 5, "tablet"),
    ("Rosuvastatin", 10, "tablet"), ("Famotidine", 20, "tablet"),
    ("Melatonin", 3, "tablet"),
)

COMMON = (
    ("Microcrystalline cellulose", "excipient", "9004-34-6", "filler", 45),
    ("Lactose monohydrate", "excipient", "5989-81-1", "filler", 35),
    ("Croscarmellose sodium", "excipient", "74811-65-7", "disintegrant", 5),
    ("Hydroxypropyl cellulose", "excipient", "9004-64-2", "binder", 4),
    ("Magnesium stearate", "excipient", "557-04-0", "lubricant", 1),
    ("Hypromellose (HPMC)", "coating", "9004-65-3", "coating", 3),
    ("Titanium dioxide", "coating", "13463-67-7", "coating", 1),
    ("Iron oxides", "coating", None, "coating", 0.2),
    ("Colloidal silicon dioxide", "excipient", None, "glidant", 0.7),
    ("Mannitol", "excipient", "69-65-8", "filler", 30),
    ("Chewable flavor blend (synthetic)", "excipient", None, "flavor", 1),
    ("Capsule shell (synthetic)", "excipient", None, "capsule shell", 18),
    ("Sodium starch glycolate", "excipient", None, "disintegrant", 4),
    ("Povidone", "excipient", "9003-39-8", "binder", 3),
    ("Talc", "excipient", "14807-96-6", "glidant", 1),
    ("Starch", "excipient", "9005-25-8", "filler", 18),
)
BP = "BP Montelukast Tablets (supplied reference)"
PH_EUR = "Ph. Eur. 2583 Montelukast Sodium (supplied reference)"
IN_HOUSE = "Synthetic in-house demo"
TABLET_LIMITS = (
    ("impurity_b", 0.3, "1.55"), ("impurity_c", 1.7, "0.45"),
    ("impurity_g", 0.5, "0.92"), ("impurity_d_plus_e", 0.15, "1.16 / 1.18"),
    ("impurity_f", 0.15, "1.04"), ("impurity_other", 0.2, None),
    ("total_impurities", 2.7, None),
)
API_LIMITS = (
    ("impurity_a", 0.2, "0.8 (chiral)"), ("impurity_b", 0.3, "1.9"),
    ("impurity_c", 0.2, "0.37 / 0.43"), ("impurity_d_plus_e", 0.15, "0.9"),
    ("impurity_f", 0.15, "1.2"), ("impurity_g", 0.15, "0.8"),
    ("impurity_other", 0.10, None), ("total_impurities", 0.6, None),
)
LONG_TERM = (0, 3, 6, 9, 12, 18, 24, 36)
ACCELERATED = (0, 3, 6)


def add_months(day: date, months: int) -> date:
    """Shift calendar months, clamping days at the end of a shorter month."""
    year, month = divmod(day.year * 12 + day.month - 1 + months, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def insert(db: sqlite3.Connection, table: str, rows: list[tuple]) -> None:
    """Insert rows into one of the generator's fixed tables."""
    markers = ",".join("?" for _ in rows[0])
    db.executemany(f"INSERT INTO {table} VALUES ({markers})", rows)


def make_catalog(db: sqlite3.Connection, rng: random.Random) -> tuple:
    """Create the demo catalog and return lookup data for dependent tables."""
    api_names = list(dict.fromkeys(name for name, _, _ in PRODUCTS))
    api_ids = {name: i for i, name in enumerate(api_names, 1)}
    catalog = [
        ("Montelukast sodium" if name == "Montelukast" else name, "API",
         {"Montelukast": "151767-02-1", "Paracetamol": "103-90-2",
          "Ibuprofen": "15687-27-1"}.get(name))
        for name in api_names
    ]
    common_start = len(catalog) + 1
    catalog.extend((name, kind, cas) for name, kind, cas, _, _ in COMMON)
    for i in range(len(catalog) + 1, 121):
        kind = ("excipient", "coating", "packaging")[i % 3]
        catalog.append((f"{kind.title()} grade {i:03} (synthetic)", kind, None))

    materials = [(i, name, kind, cas, "g" if i % 7 == 0 else "kg",
                  rng.choice((10, 25, 50)), rng.choice((5, 10, 25)), rng.randint(7, 70))
                 for i, (name, kind, cas) in enumerate(catalog, 1)]
    products = [(i, name, strength, form, 36 if i <= 3 else 24,
                 "Protect from light (demo)" if i <= 3 else "Controlled room temperature (demo)")
                for i, (name, strength, form) in enumerate(PRODUCTS, 1)]
    suppliers = [(i, f"Supplier-{i:03} (synthetic)", rng.choice(("Jordan", "India", "Germany", "Türkiye")),
                  rng.choice(("manufacturer", "distributor")), rng.choice((None, "GMP", "GMP; CEP", "DMF")),
                  rng.randint(7, 65), round(rng.uniform(0.72, 0.99), 3))
                 for i in range(1, 51)]
    insert(db, "products", products)
    insert(db, "materials", materials)
    insert(db, "suppliers", suppliers)

    formulas = {}
    spec_by_product = {}
    bom_rows = []
    spec_rows = []
    for product, name, strength, form, *_ in products:
        if product == 1:
            selected = list(range(9))
        elif form == "chewable tablet":
            selected = [0, 2, 3, 4, 8, 9, 10, 13, 15]
        elif form == "capsule":
            selected = [0, 1, 2, 3, 4, 8, 11, 13, 15]
        else:
            selected = [0, 1, 2, 3, 4, 8, 12, 13, 15]
        components = [(api_ids[name], "API", round(strength * 1.04, 4) if product <= 3 else strength)]
        components += [(common_start + j, COMMON[j][3], COMMON[j][4]) for j in selected]
        total_mass = sum(mass for _, _, mass in components)
        formulas[product] = []
        for material, function, mass in components:
            pct = round(100 * mass / total_mass, 6)
            tolerance = 0 if function == "API" else rng.choice((0.05, 0.075, 0.10))
            row = (product, material, mass, function, pct,
                   round(pct * (1 - tolerance), 6), round(pct * (1 + tolerance), 6))
            formulas[product].append(row)
            bom_rows.append(row)

        source = BP if product == 1 else IN_HOUSE
        specs = [
            ("assay", "%", 94 if product == 1 else 92, 105 if product == 1 else 108, source),
            ("dissolution_20min", "%", 80, None, source),
            ("content_uniformity_av", "AV", None, 15,
             "Pharmacopoeial general AV threshold (demo application)"),
            ("water", "%", None, 5, IN_HOUSE),
            ("hardness", "N", 30, 150, IN_HOUSE),
        ]
        specs.extend((test, "%", None, limit, source) for test, limit, _ in TABLET_LIMITS)
        spec_by_product[product] = {test: (unit, low, high) for test, unit, low, high, _ in specs}
        spec_rows.extend((product, *row) for row in specs)
    insert(db, "bom", bom_rows)
    insert(db, "specifications", spec_rows)

    references = [(1, None, test, limit, PH_EUR, retention) for test, limit, retention in API_LIMITS]
    references.extend((None, 1, test, limit, BP, retention) for test, limit, retention in TABLET_LIMITS)
    references.extend((None, 1, test, 0.15, BP, retention)
                      for test, retention in (("impurity_d", "1.16"), ("impurity_e", "1.18")))
    insert(db, "impurity_limits_reference", [(i, *row) for i, row in enumerate(references, 1)])
    return products, materials, formulas, spec_by_product


def make_inventory(db: sqlite3.Connection, rng: random.Random, materials: list[tuple]) -> dict:
    """Create historical lots plus deliberately mixed current inventory."""
    rows = []
    historical = {}
    current_slots = [index for index in range(120 * 50) if index % 50 >= 8]
    flagged = rng.sample(current_slots, 300 + 180 + 120 + 40 + 60)
    near = set(flagged[:300])
    quarantine = set(flagged[300:480])
    expired = set(flagged[480:600])
    stale = set(flagged[600:640])
    rejected = set(flagged[640:])
    for material, _, kind, _, unit, *_ in materials:
        historical[material] = []
        for slot in range(50):
            index = (material - 1) * 50 + slot
            status = "released"
            if slot < 8:
                # Eight overlapping closed inventory periods cover the historical production dates.
                received = AS_OF - timedelta(days=1500 - 180 * slot)
                expiry = received + timedelta(days=360)
                if expiry < AS_OF:
                    status = "expired"
                historical[material].append((f"LOT-{index + 1:05}", received, expiry, unit))
            else:
                received = AS_OF - timedelta(days=rng.randint(120, 420))
                expiry = AS_OF + timedelta(days=rng.randint(61, 900))

            # Keep the intentional current exceptions away from historical trace lots.
            if slot >= 8:
                if index in near:
                    expiry = AS_OF + timedelta(days=rng.randint(0, 60))
                if index in quarantine:
                    status = "quarantine"
                if index in rejected:
                    status = "rejected"
                if index in expired or index in stale:
                    expiry = AS_OF - timedelta(days=rng.randint(1, 100))
                    status = "expired" if index in expired else "released"
            made = received - timedelta(days=rng.randint(5, 45))
            retest = max(made + timedelta(days=1), expiry - timedelta(days=90)) if kind == "API" else None
            qty = 0.0 if slot < 8 else round(rng.uniform(0.1, 30 if material == 1 else 150), 3)
            if unit == "g":
                qty *= 1000
            rows.append((f"LOT-{index + 1:05}", material, rng.randint(1, 50), round(qty, 3), unit,
                         received.isoformat(), made.isoformat(), expiry.isoformat(),
                         retest.isoformat() if retest else None, status,
                         round(rng.uniform(98.0, 102.0), 2) if kind == "API" else None,
                         f"WH-{rng.randint(1, 4)}-{rng.randint(1, 12):02}"))
    insert(db, "material_lots", rows)
    return historical


def make_batches(db: sqlite3.Connection, rng: random.Random, products: list[tuple],
                 formulas: dict, historical: dict) -> list[tuple]:
    """Create batches and complete material traces for a historical subset."""
    batches = []
    usage = []
    study_candidates = []
    for product, _, _, _, shelf_life, _ in products:
        for slot in range(120):
            index = len(batches) + 1
            batch = f"BATCH-{index:05}"
            units = rng.choice((25_000, 50_000, 100_000, 200_000))
            if slot < 20:
                made = AS_OF - timedelta(days=rng.randint(1110, 1460))
                status = "released"
                study_candidates.append((batch, product, made))
            elif slot >= 116:
                made = AS_OF + timedelta(days=slot - 115)
                status = "planned"
            else:
                made = AS_OF - timedelta(days=rng.randint(0, 1090))
                status = "in_process" if slot == 115 else ("rejected" if slot % 37 == 0 else "released")
                if status == "in_process":
                    made = AS_OF
            expiry = add_months(made, shelf_life)
            batches.append((batch, product, units, made.isoformat(), expiry.isoformat(), status))
            if slot < 60:
                for _, material, mass, *_ in formulas[product]:
                    lot, _, _, unit = min(
                        (row for row in historical[material] if row[1] <= made <= row[2]),
                        key=lambda row: row[2],
                    )
                    qty = round(units * mass / (1_000_000 if unit == "kg" else 1_000), 6)
                    usage.append((batch, lot, qty, unit))
    insert(db, "production_batches", batches)
    insert(db, "batch_material_usage", usage)
    return study_candidates


def within_limits(value: float, low: float | None, high: float | None) -> bool:
    """Apply inclusive limits with support for one-sided specifications."""
    return (low is None or value >= low) and (high is None or value <= high)


def hplc_run(run_id: int, test: str, day: date, rng: random.Random, failing: bool = False) -> tuple:
    """Generate SST measurements and calculate the demo pass flag from them."""
    min_plates = 2000 if test == "related_substances" else 1300
    rsd = round(rng.uniform(0.3, 1.9), 2)
    plates = min_plates + rng.randint(0, 1300)
    asymmetry = round(rng.uniform(0.85, 1.95), 2)
    if failing:
        failure = run_id % 3
        if failure == 0:
            rsd = 2.15
        elif failure == 1:
            plates = min_plates - 100
        else:
            asymmetry = 2.12
    column_length = 150 if test == "related_substances" else 250
    return (run_id, test, day.isoformat(), f"LC-{rng.randint(1, 8):02}",
            f"Inertsil ODS-3 5um 4.6x{column_length}", rsd, plates, asymmetry,
            int(rsd <= 2 and plates >= min_plates and asymmetry <= 2))


def make_stability(db: sqlite3.Connection, rng: random.Random, candidates: list[tuple],
                   specs: dict) -> None:
    """Simulate time-zero baselines, pull results and shared chromatography runs."""
    studies = []
    results = []
    runs = []
    selected = rng.sample(candidates, 400)
    outliers = rng.sample(range(1, 281), 16)
    oot_studies, oos_studies = set(outliers[:12]), set(outliers[12:])
    for study, (batch, product, made) in enumerate(selected, 1):
        accelerated = study > 280
        planned = ACCELERATED if accelerated else LONG_TERM
        start = made + timedelta(days=5)
        # Stagger ten accelerated studies to illustrate active studies without future pulls.
        if study > 390:
            start = AS_OF - timedelta(days=110)
        pulls = [(m, add_months(start, m)) for m in planned if add_months(start, m) <= AS_OF]
        studies.append((study, batch, "40C/75%RH" if accelerated else "30C/65%RH",
                        start.isoformat(), ",".join(map(str, planned)),
                        "complete" if len(pulls) == len(planned) else "active"))

        baseline_assay = round(rng.uniform(100.0, 101.0), 3)
        if study in oot_studies:
            rate = rng.uniform(0.123, 0.145)
        elif study in oos_studies:
            rate = 0.29
        else:
            rate = rng.uniform(0.100, 0.105) * (2.5 if accelerated else 1)
        for month, day in pulls:
            run_ids = {}
            for test in ("assay", "dissolution_20min", "related_substances"):
                run_ids[test] = len(runs) + 1
                runs.append(hplc_run(run_ids[test], test, day, rng))
            growth = month * (2.5 if accelerated else 1)
            montelukast = product <= 3
            impurity_values = {
                "impurity_b": 0.035 + 0.001 * growth,
                "impurity_c": 0.08 + (0.015 if montelukast else 0.003) * growth,
                "impurity_g": 0.035 + (0.008 if montelukast else 0.002) * growth,
                "impurity_d_plus_e": 0.025 + 0.001 * growth,
                "impurity_f": 0.022 + 0.001 * growth,
                "impurity_other": 0.045 + 0.001 * growth,
            }
            impurity_values = {test: round(value * rng.uniform(0.91, 1.09), 3)
                               for test, value in impurity_values.items()}
            values = {
                "assay": baseline_assay - month * rate,
                "dissolution_20min": rng.uniform(92, 98) - 0.16 * growth,
                "content_uniformity_av": rng.uniform(4, 9) + 0.04 * growth,
                "water": rng.uniform(0.8, 1.8) + 0.045 * growth,
                "hardness": rng.uniform(60, 95) - 0.2 * growth,
                **impurity_values, "total_impurities": sum(impurity_values.values()),
            }
            for test, raw in values.items():
                value = round(raw, 3)
                unit, low, high = specs[product][test]
                status = "pass"
                if not within_limits(value, low, high):
                    status = "oos"
                elif test == "assay" and round(baseline_assay - value, 3) >= 4:
                    status = "oot"
                method = "related_substances" if "impurit" in test else test
                results.append((study, month, day.isoformat(), test, value, unit,
                                f"AN-{rng.randint(1, 18):02}", run_ids.get(method), status))

    methods = ("assay", "dissolution_20min", "related_substances")
    for run_id in range(len(runs) + 1, 8001):
        day = AS_OF - timedelta(days=rng.randint(0, 1460))
        # Rotate failure mode independently of the method selected.
        runs.append(hplc_run(run_id, rng.choice(methods), day, rng, failing=run_id > 7840))
    insert(db, "stability_studies", studies)
    insert(db, "hplc_runs", runs)
    insert(db, "stability_results", results)


def make_trials(db: sqlite3.Connection, rng: random.Random, formulas: dict, specs: dict) -> None:
    """Generate historical demo observations, without inferring safe future ranges."""
    rows = []
    for product, formula in formulas.items():
        choices = [row for row in formula if row[3] in ("filler", "disintegrant", "lubricant")]
        for _ in range(60):
            _, material, _, function, nominal, *_ = rng.choice(choices)
            used = round(nominal * rng.uniform(0.65, 1.35), 6)
            deviation = round(100 * (used / nominal - 1), 3)
            slower = 0.0
            if function == "disintegrant":
                slower = max(0.0, -deviation)
            elif function == "lubricant":
                slower = max(0.0, deviation)
            added_impurities = 0.045 * max(0.0, deviation) if function == "filler" else 0
            outcomes = {
                "assay": round(rng.uniform(97, 103), 3),
                "dissolution_20min": round(rng.uniform(90, 97) - 0.55 * slower, 3),
                "total_impurities": round(rng.uniform(0.3, 1.25) + added_impurities, 3),
                "hardness": round(rng.uniform(50, 95), 3),
            }
            disintegration = round(rng.uniform(4, 8) + 0.33 * slower, 3)
            passed = all(within_limits(value, *specs[product][test][1:])
                         for test, value in outcomes.items()) and disintegration <= 15
            trial_date = AS_OF - timedelta(days=rng.randint(1, 1000))
            rows.append((len(rows) + 1, product, trial_date.isoformat(), material, nominal, used,
                         deviation, *outcomes.values(), disintegration,
                         "meets_spec" if passed else "fails",
                         f"Synthetic one-factor {function} trial"))
    insert(db, "formulation_trials", rows)


def generate(output: Path, seed: int) -> None:
    """Publish a fresh database and CSVs after generating them in a staging directory."""
    rng = random.Random(seed)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as temporary:
        staging = Path(temporary)
        database = staging / "pharmapilot.db"
        exports = staging / "csv"
        exports.mkdir()
        counts = {}
        with closing(sqlite3.connect(database)) as db:
            db.execute("PRAGMA foreign_keys = ON")
            db.executescript(SCHEMA)
            with db:
                products, materials, formulas, specs = make_catalog(db, rng)
                lots = make_inventory(db, rng, materials)
                batches = make_batches(db, rng, products, formulas, lots)
                make_stability(db, rng, batches, specs)
                make_trials(db, rng, formulas, specs)
            tables = db.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")
            for (table,) in tables:
                rows = db.execute(f"SELECT * FROM {table} ORDER BY rowid")
                with (exports / f"{table}.csv").open("w", encoding="utf-8", newline="") as file:
                    writer = csv.writer(file)
                    writer.writerow([column[0] for column in rows.description])
                    counts[table] = 0
                    for row in rows:
                        writer.writerow(row)
                        counts[table] += 1
        (output / "csv").mkdir(exist_ok=True)
        for file in sorted(exports.iterdir()):
            file.replace(output / "csv" / file.name)
        database.replace(output / database.name)
    for table, count in counts.items():
        print(f"{table}: {count}")


def main() -> None:
    """Parse arguments and build the demo data."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    generate(args.output_dir, args.seed)


if __name__ == "__main__":
    main()
