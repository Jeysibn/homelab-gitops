import importlib.util
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("classify", ROOT / "classify.py")
classify_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(classify_module)


def table(columns=None, constraints=None, indexes=None, comments=None):
    return {"columns": columns or {}, "constraints": constraints or {},
            "indexes": indexes or {}, "comments": comments or {}}


def col(type_name, nullable=False):
    return {"type": type_name, "nullable": nullable}


def con(kind, definition):
    return {"type": kind, "definition": definition}


def complete_inventory():
    accounts = table({
        "id": col("integer"), "type": col("character varying(20)"),
        "opening_balance": col("numeric(12,2)"), "opening_debt": col("numeric(12,2)"),
        "card_product_id": col("character varying(64)", True),
    }, {
        "accounts_check": con("c", "CHECK (type::text = 'credit_card'::text AND credit_limit IS NOT NULL AND credit_limit > 0::numeric AND opening_balance = 0::numeric OR type::text <> 'credit_card'::text AND credit_limit IS NULL AND opening_debt = 0::numeric)"),
        "accounts_card_product_id_format": con("c", "CHECK ((card_product_id IS NULL) OR ((card_product_id)::text ~ '^[a-z0-9]+(-[a-z0-9]+)*$'::text))"),
        "accounts_card_product_id_not_cash": con("c", "CHECK (((type)::text <> 'cash'::text) OR (card_product_id IS NULL))"),
    })
    categories = table({"type": col("character varying(10)"), "default_kakeibo_envelope": col("character varying(12)", True)}, {
        "categories_default_kakeibo_envelope_allowed": con("c", "CHECK ((default_kakeibo_envelope IS NULL) OR ((default_kakeibo_envelope)::text = ANY (ARRAY['needs'::character varying, 'wants'::character varying, 'culture'::character varying, 'unexpected'::character varying]::text[])))"),
        "categories_default_kakeibo_envelope_expense_only": con("c", "CHECK ((default_kakeibo_envelope IS NULL) OR ((type)::text = 'expense'::text))"),
    })
    transactions = table({"transaction_date": col("date"), "amount": col("numeric(12,2)"), "kakeibo_envelope": col("character varying(12)", True)}, {
        "transactions_kakeibo_envelope_allowed": con("c", "CHECK ((kakeibo_envelope IS NULL) OR ((kakeibo_envelope)::text = ANY (ARRAY['needs'::character varying, 'wants'::character varying, 'culture'::character varying, 'unexpected'::character varying]::text[])))"),
    }, {"idx_transactions_kakeibo_date_envelope": "CREATE INDEX idx_transactions_kakeibo_date_envelope ON public.transactions USING btree (transaction_date, kakeibo_envelope)"})
    transfers_columns = {
        "id": col("integer"), "source_account_id": col("integer"), "destination_account_id": col("integer"),
        "amount": col("numeric(12,2)"), "transfer_date": col("date"), "note": col("text", True),
        "created_at": col("timestamp with time zone"),
    }
    transfers = table(transfers_columns, {
        "account_transfers_pkey": con("p", "PRIMARY KEY (id)"),
        "account_transfers_source_account_id_fkey": con("f", "FOREIGN KEY (source_account_id) REFERENCES accounts(id)"),
        "account_transfers_destination_account_id_fkey": con("f", "FOREIGN KEY (destination_account_id) REFERENCES accounts(id)"),
        "account_transfers_amount_check": con("c", "CHECK (amount > (0)::numeric)"),
        "account_transfers_check": con("c", "CHECK (source_account_id <> destination_account_id)"),
    }, {
        "idx_account_transfers_source_date": "CREATE INDEX idx_account_transfers_source_date ON public.account_transfers USING btree (source_account_id, transfer_date DESC)",
        "idx_account_transfers_destination_date": "CREATE INDEX idx_account_transfers_destination_date ON public.account_transfers USING btree (destination_account_id, transfer_date DESC)",
    })
    month_columns = {"month": col("date"), "planned_income": col("numeric(12,2)"),
        "allocated_starting_funds": col("numeric(12,2)"), "savings_target": col("numeric(12,2)"),
        "prior_period_debt_reserve": col("numeric(12,2)"), "needs_limit": col("numeric(12,2)"),
        "wants_limit": col("numeric(12,2)"), "culture_limit": col("numeric(12,2)"),
        "unexpected_limit": col("numeric(12,2)"), "monthly_intention": col("character varying(240)", True),
        "actual_saved_declared": col("numeric(12,2)", True), "review_actuals_snapshot": col("jsonb", True),
        "created_at": col("timestamp with time zone"), "updated_at": col("timestamp with time zone")}
    month_constraint_names = ("pkey", "month_check", "planned_income_check", "allocated_starting_funds_check",
        "savings_target_check", "prior_period_debt_reserve_check", "needs_limit_check", "wants_limit_check",
        "culture_limit_check", "unexpected_limit_check", "monthly_intention_check", "actual_saved_declared_check",
        "review_available_check", "review_savings_check", "review_change_check", "review_appreciated_check",
        "review_surprise_check", "review_lesson_check", "review_actuals_snapshot_check")
    month_cons = {"kakeibo_month_plans_" + name: con("p" if name == "pkey" else "c",
        "CHECK ((month = date_trunc('month', month)::date))" if name == "month_check" else "CHECK (valid)" if name.endswith("snapshot_check") else "PRIMARY KEY (month)" if name == "pkey" else "CHECK (valid)") for name in month_constraint_names}
    month_cons["kakeibo_month_plans_review_actuals_snapshot_check"] = con("c", "CHECK (jsonb_typeof(review_actuals_snapshot) AND actual_income total_expenses unclassified envelopes [0-9])")
    plans = table(month_columns, month_cons, comments={
        "plan_presence": "saved/not_saved is explicit; NULL means a pre-006 row whose plan intent is unknown."
    })
    week_columns = {"week_start": col("date"), "surprise_note": col("text", True),
        "worthwhile_note": col("text", True), "next_week_intention": col("text", True),
        "created_at": col("timestamp with time zone"), "updated_at": col("timestamp with time zone")}
    week_names = ("pkey", "week_start_check", "surprise_note_check", "worthwhile_note_check", "next_week_intention_check")
    week_cons = {"kakeibo_weekly_reviews_" + name: con("p" if name == "pkey" else "c",
        "PRIMARY KEY (week_start)" if name == "pkey" else "CHECK (EXTRACT(ISODOW FROM week_start) = 1)" if name == "week_start_check" else "CHECK (valid)") for name in week_names}
    inventory = {"database": "monikey_dev", "server_version": "17.6", "relation_kinds": {}, "role_security": {
        "superuser": False, "create_database": False, "create_role": False, "replication": False,
        "inherit": False, "schema_create": False, "database_create": False, "database_temp": False,
        "owns_database": False, "owns_schema": False,
        "has_role_memberships": False, "owns_target_objects": False, "sequence_privileges": False,
        "table_privileges": {name: {privilege: False for privilege in
            ("select", "insert", "update", "delete", "truncate", "references", "trigger")}
            for name in ("accounts", "categories", "transactions", "credit_card_payments",
                         "account_transfers", "kakeibo_month_plans", "kakeibo_weekly_reviews")},
    }, "tables": {
        "accounts": accounts, "categories": categories, "transactions": transactions,
        "account_transfers": transfers, "kakeibo_month_plans": table(month_columns, month_cons, comments=plans["comments"]),
        "kakeibo_weekly_reviews": table(week_columns, week_cons),
    }}
    inventory["tables"]["kakeibo_month_plans"]["constraints"]["kakeibo_month_plans_plan_presence_check"] = con("c", "CHECK (plan_presence IN ('saved', 'not_saved'))")
    inventory["tables"]["kakeibo_month_plans"]["columns"]["plan_presence"] = col("character varying(12)", True)
    return inventory


class ClassificationTests(unittest.TestCase):
    def test_all_reviewed_signatures_are_applied(self):
        inventory = complete_inventory()
        original = classify_module.EXPECTED_SIGNATURES.copy()
        try:
            for number in ("002", "004", "005", "006"):
                classify_module.EXPECTED_SIGNATURES[number] = classify_module.signature_for(number, inventory["tables"])
            results = classify_module.classify(inventory)
        finally:
            classify_module.EXPECTED_SIGNATURES.update(original)
        self.assertEqual({key: value["status"] for key, value in results.items()},
                         {key: "APPLIED" for key in ("002", "003", "004", "005", "006")})

    def test_baseline_before_002_reports_missing_not_unknown(self):
        inventory = complete_inventory()
        inventory["tables"].pop("account_transfers")
        self.assertEqual(classify_module.classify(inventory)["002"]["status"], "MISSING")

    def test_schema_after_002_and_003(self):
        inventory = complete_inventory()
        tables = inventory["tables"]
        tables["accounts"]["columns"].pop("card_product_id")
        tables["accounts"]["constraints"].pop("accounts_card_product_id_format")
        tables["accounts"]["constraints"].pop("accounts_card_product_id_not_cash")
        tables["categories"]["columns"].pop("default_kakeibo_envelope")
        tables["categories"]["constraints"].clear()
        tables["transactions"]["columns"].pop("kakeibo_envelope")
        tables["transactions"]["constraints"].clear()
        tables["transactions"]["indexes"].clear()
        tables.pop("kakeibo_month_plans")
        tables.pop("kakeibo_weekly_reviews")
        original = classify_module.EXPECTED_SIGNATURES.copy()
        classify_module.EXPECTED_SIGNATURES["002"] = classify_module.signature_for("002", tables)
        try:
            statuses = {k: v["status"] for k, v in classify_module.classify(inventory).items()}
        finally:
            classify_module.EXPECTED_SIGNATURES.update(original)
        self.assertEqual(statuses["002"], "APPLIED")
        self.assertEqual(statuses["003"], "APPLIED")
        self.assertEqual(statuses["004"], "MISSING")

    def test_003_present_legacy_constraints_are_missing(self):
        inventory = complete_inventory()
        accounts = inventory["tables"]["accounts"]
        accounts["constraints"].update({
            "accounts_opening_balance_check": con("c", "CHECK (opening_balance >= 0::numeric)"),
            "accounts_opening_debt_check": con("c", "CHECK (opening_debt >= 0::numeric)"),
        })
        self.assertEqual(classify_module.classify(inventory)["003"]["status"], "MISSING")

    def test_003_one_legacy_constraint_is_partial(self):
        inventory = complete_inventory()
        inventory["tables"]["accounts"]["constraints"]["accounts_opening_balance_check"] = con(
            "c", "CHECK (opening_balance >= 0::numeric)"
        )
        self.assertEqual(classify_module.classify(inventory)["003"]["status"], "PARTIAL")

    def test_schema_after_004_has_005_missing(self):
        inventory = complete_inventory()
        tables = inventory["tables"]
        tables["categories"]["columns"].pop("default_kakeibo_envelope")
        tables["categories"]["constraints"].clear()
        tables["transactions"]["columns"].pop("kakeibo_envelope")
        tables["transactions"]["constraints"].clear()
        tables["transactions"]["indexes"].clear()
        tables.pop("kakeibo_month_plans")
        tables.pop("kakeibo_weekly_reviews")
        original = classify_module.EXPECTED_SIGNATURES.copy()
        classify_module.EXPECTED_SIGNATURES["004"] = classify_module.signature_for("004", tables)
        try:
            statuses = {k: v["status"] for k, v in classify_module.classify(inventory).items()}
        finally:
            classify_module.EXPECTED_SIGNATURES.update(original)
        self.assertEqual(statuses["004"], "APPLIED")
        self.assertEqual(statuses["005"], "MISSING")
        self.assertEqual(statuses["006"], "UNKNOWN")

    def test_schema_after_005_has_006_missing(self):
        inventory = complete_inventory()
        plans = inventory["tables"]["kakeibo_month_plans"]
        plans["columns"].pop("plan_presence")
        plans["constraints"].pop("kakeibo_month_plans_plan_presence_check")
        plans["comments"].pop("plan_presence")
        original = classify_module.EXPECTED_SIGNATURES.copy()
        try:
            for number in ("002", "004", "005"):
                classify_module.EXPECTED_SIGNATURES[number] = classify_module.signature_for(number, inventory["tables"])
            self.assertEqual(classify_module.classify(inventory)["005"]["status"], "APPLIED")
            self.assertEqual(classify_module.classify(inventory)["006"]["status"], "MISSING")
        finally:
            classify_module.EXPECTED_SIGNATURES.update(original)

    def test_partial_002_and_missing_index_are_partial(self):
        inventory = complete_inventory()
        inventory["tables"]["account_transfers"]["indexes"].pop("idx_account_transfers_source_date")
        self.assertEqual(classify_module.classify(inventory)["002"]["status"], "PARTIAL")

    def test_wrong_relation_kind_is_not_missing(self):
        inventory = complete_inventory()
        inventory["tables"].pop("account_transfers")
        inventory["relation_kinds"]["account_transfers"] = "v"
        self.assertEqual(classify_module.classify(inventory)["002"]["status"], "PARTIAL")

    def test_wrong_constraint_is_not_applied(self):
        inventory = complete_inventory()
        inventory["tables"]["accounts"]["constraints"]["accounts_card_product_id_format"]["definition"] = "CHECK (true)"
        self.assertEqual(classify_module.classify(inventory)["004"]["status"], "PARTIAL")

    def test_006_missing_comment_is_partial(self):
        inventory = complete_inventory()
        original = classify_module.EXPECTED_SIGNATURES.copy()
        for number in ("002", "004", "005", "006"):
            classify_module.EXPECTED_SIGNATURES[number] = classify_module.signature_for(number, inventory["tables"])
        inventory["tables"]["kakeibo_month_plans"]["comments"].clear()
        try:
            self.assertEqual(classify_module.classify(inventory)["006"]["status"], "PARTIAL")
        finally:
            classify_module.EXPECTED_SIGNATURES.update(original)

    def test_insufficient_catalog_visibility_is_unknown(self):
        inventory = {"database": "monikey_dev", "server_version": "17.6", "role_security": {}, "tables": {}}
        self.assertEqual(classify_module.classify(inventory)["002"]["status"], "UNKNOWN")
        self.assertEqual(classify_module.classify(inventory)["005"]["status"], "UNKNOWN")

    def test_target_database_mismatch_fails_closed(self):
        inventory = complete_inventory()
        inventory["database"] = "unexpected"
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "inventory.json"
            target = Path(temporary) / "report.json"
            source.write_text(json.dumps(inventory))
            result = subprocess.run(["python3", str(ROOT / "classify.py"), str(source),
                                     "--output", str(target), "--expected-database", "monikey_dev"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(target.exists())

    def test_non_17_database_version_is_unknown(self):
        inventory = complete_inventory()
        inventory["server_version"] = "16.9"
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "inventory.json"
            target = Path(temporary) / "report.json"
            source.write_text(json.dumps(inventory))
            result = subprocess.run(["python3", str(ROOT / "classify.py"), str(source),
                                     "--output", str(target)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            report = json.loads(target.read_text())
            self.assertTrue(all(item["status"] == "UNKNOWN" for item in report["migrations"].values()))

    def test_signature_mismatch_is_not_applied(self):
        inventory = complete_inventory()
        inventory["tables"]["account_transfers"]["constraints"]["account_transfers_check"]["definition"] += " OR true"
        self.assertEqual(classify_module.classify(inventory)["002"]["status"], "PARTIAL")

    def test_inspection_query_contains_no_mutation_statement(self):
        sql = (ROOT / "inspect.sql").read_text().lower()
        self.assertIsNone(re.search(r"(?im)^\s*(insert|update|delete|truncate|create|alter|drop|grant|revoke)\s", sql))
        self.assertIn("transaction read only", sql)
        self.assertIn("statement_timeout", sql)
        self.assertIn("has_table_privilege", sql)
        self.assertNotRegex(sql, r"\b(select|from)\s+(accounts|transactions|credit_card_payments|account_transfers)\b")

    def test_workflow_is_manual_fixed_target_and_separate_from_backup(self):
        workflow = (ROOT.parents[1] / ".github/workflows/monikey-schema-inspection.yml").read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotRegex(workflow, r"(?m)^\s*(push|pull_request|schedule|workflow_run):")
        self.assertIn("github.ref == 'refs/heads/main' && inputs.confirm_schema_inspection", workflow)
        self.assertIn("name: monikey-production-inspection", workflow)
        self.assertIn("deployment-branch-policies", workflow)
        self.assertIn('branch_names != ["main"]', workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("actions: read", workflow)
        for unsafe in ("pods/exec", "MONIKEY_BACKUP_KUBECONFIG_B64", "monikey_app", "argocd app sync"):
            self.assertNotIn(unsafe, workflow)
        self.assertIn("monikey_schema_inspector", workflow)
        self.assertIn("-d monikey_dev", workflow)

    def test_pgpass_writer_escapes_password_delimiters(self):
        writer = ROOT / "write_pgpass.py"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pgpass"
            subprocess.run(["python3", str(writer), str(path)], input="fiction:with\\delimiters\n",
                           text=True, check=True)
            self.assertEqual(path.read_text(), "127.0.0.1:15432:monikey_dev:monikey_schema_inspector:fiction\\:with\\\\delimiters\n")


if __name__ == "__main__":
    unittest.main()
