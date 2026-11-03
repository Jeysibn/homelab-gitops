import copy
import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "audit_argocd_application", ROOT / "audit_argocd_application.py"
)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
REPO_ROOT = ROOT.parents[1]


def application(resources, *, automated=None, operation=None, source=None):
    spec = {
        "source": source or {
            "repoURL": audit.EXPECTED_SOURCE["repoURL"],
            "targetRevision": "main",
            "path": audit.EXPECTED_SOURCE["path"],
        },
        "destination": {
            "server": "https://kubernetes.default.svc",
            "namespace": "monikey",
        },
        "syncPolicy": {} if automated is None else {"automated": automated},
    }
    if operation:
        spec["operation"] = {"initiatedBy": {"username": "fiction"}}
    status = {
        "sync": {"status": "OutOfSync", "revision": "fictional-revision"},
        "health": {"status": "Healthy"},
        "resources": resources,
    }
    if operation:
        status["operationState"] = {"phase": operation}
    return {
        "metadata": {"name": "monikey-inspection-access", "namespace": "argocd"},
        "spec": {"project": "default", **spec},
        "status": status,
    }


def resource(item_id, *, missing=False, status=None, prune=False):
    group, kind, namespace, name = item_id
    return {
        "group": group,
        "kind": kind,
        "namespace": namespace,
        "name": name,
        "status": status or ("OutOfSync" if missing else "Synced"),
        "health": {"status": "Missing" if missing else "Healthy"},
        "requiresPruning": prune,
    }


def exact_live_objects():
    return {
        "inspector-sa": {
            "kind": "ServiceAccount",
            "metadata": {"name": "monikey-schema-inspector", "namespace": "monikey"},
            "automountServiceAccountToken": False,
        },
        "inspector-role": {
            "kind": "Role",
            "metadata": {"name": "monikey-schema-inspector", "namespace": "monikey"},
            "rules": copy.deepcopy(audit.EXPECTED_RULES["inspector-role"]),
        },
        "inspector-binding": {
            "kind": "RoleBinding",
            "metadata": {"name": "monikey-schema-inspector", "namespace": "monikey"},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "monikey-schema-inspector"},
            "subjects": [{"kind": "ServiceAccount", "name": "monikey-schema-inspector", "namespace": "monikey"}],
        },
        "token-issuer-role": {
            "kind": "Role",
            "metadata": {"name": "monikey-inspection-token-issuer", "namespace": "monikey"},
            "rules": copy.deepcopy(audit.EXPECTED_RULES["token-issuer-role"]),
        },
        "token-issuer-binding": {
            "kind": "RoleBinding",
            "metadata": {"name": "monikey-inspection-token-issuer", "namespace": "monikey"},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "monikey-inspection-token-issuer"},
            "subjects": [{"kind": "ServiceAccount", "name": "monikey-backup", "namespace": "monikey"}],
        },
    }


class ArgoApplicationAuditTests(unittest.TestCase):
    def test_exactly_five_expected_additions_are_safe_to_sync(self):
        statuses = [resource(item_id, missing=True) for item_id in audit.EXPECTED]
        result = audit.summarize(application(statuses), 1, {})
        self.assertTrue(result["safeToSync"])
        self.assertEqual(result["desiredResourceCount"], 5)
        self.assertEqual(result["liveResourceCount"], 0)
        self.assertEqual(len(result["pendingAdditions"]), 5)
        self.assertEqual(result["pendingUpdates"], [])
        self.assertEqual(result["pendingDeletions"], [])
        self.assertIsNone(result["liveRbacValid"])

    def test_synced_live_resources_and_exact_rbac_are_safe(self):
        statuses = [resource(item_id) for item_id in audit.EXPECTED]
        result = audit.summarize(application(statuses), 0, exact_live_objects())
        self.assertTrue(result["safeToSync"])
        self.assertEqual(result["liveResourceCount"], 5)
        self.assertEqual(result["liveRbacStatus"], "VERIFIED")

    def test_broadened_live_permissions_make_application_unsafe(self):
        statuses = [resource(item_id) for item_id in audit.EXPECTED]
        live = exact_live_objects()
        live["inspector-role"]["rules"].append({
            "apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]
        })
        result = audit.summarize(application(statuses), 0, live)
        self.assertFalse(result["liveRbacValid"])
        self.assertFalse(result["safeToSync"])

    def test_updates_deletions_automation_and_running_operation_block_sync(self):
        statuses = [resource(item_id) for item_id in audit.EXPECTED]
        statuses[0]["status"] = "OutOfSync"
        result = audit.summarize(application(statuses), 1, exact_live_objects())
        self.assertEqual(len(result["pendingUpdates"]), 1)
        self.assertFalse(result["safeToSync"])

        statuses = [resource(item_id) for item_id in audit.EXPECTED]
        statuses.append({
            "group": "", "kind": "ConfigMap", "namespace": "monikey", "name": "old-config",
            "status": "OutOfSync", "health": {"status": "Healthy"}, "requiresPruning": True,
        })
        result = audit.summarize(application(statuses), 1, exact_live_objects())
        self.assertEqual(result["pendingDeletions"], ["ConfigMap/monikey/old-config"])
        self.assertFalse(result["safeToSync"])

        result = audit.summarize(application([resource(i) for i in audit.EXPECTED], automated={}), 0,
                                 exact_live_objects())
        self.assertEqual(result["automatedSync"], "ENABLED")
        self.assertFalse(result["safeToSync"])
        result = audit.summarize(application([resource(i) for i in audit.EXPECTED], operation="Running"), 0,
                                 exact_live_objects())
        self.assertTrue(result["operationRunning"])
        self.assertFalse(result["safeToSync"])

    def test_unexpected_resources_including_secrets_block_without_printing_data(self):
        statuses = [resource(item_id) for item_id in audit.EXPECTED]
        statuses.append({
            "group": "", "kind": "Secret", "namespace": "monikey", "name": "unexpected",
            "status": "Synced", "health": {"status": "Healthy"},
        })
        result = audit.summarize(application(statuses), 0, exact_live_objects())
        self.assertEqual(result["unexpectedResources"], ["Secret/monikey/unexpected"])
        self.assertNotIn("data", json.dumps(result))
        self.assertFalse(result["safeToSync"])

    def test_missing_or_mismatched_comparison_status_is_unknown(self):
        statuses = [resource(item_id) for item_id in list(audit.EXPECTED)[:-1]]
        result = audit.summarize(application(statuses), 1, exact_live_objects())
        self.assertTrue(result["incompleteResourceStatus"])
        self.assertFalse(result["safeToSync"])
        result = audit.summarize(application([resource(i) for i in audit.EXPECTED],
                                         source={"repoURL": "unexpected"}), 0, exact_live_objects())
        self.assertFalse(result["sourceRepositoryMatches"])
        self.assertFalse(result["safeToSync"])

    def test_plan_only_requests_named_expected_live_resources(self):
        statuses = [resource(item_id, missing=(item_id[3] == "monikey-inspection-token-issuer"))
                    for item_id in audit.EXPECTED]
        with tempfile.TemporaryDirectory() as temporary:
            app_file = Path(temporary) / "app.json"
            plan_file = Path(temporary) / "plan.txt"
            app_file.write_text(json.dumps(application(statuses)))
            audit.write_plan(json.loads(app_file.read_text()), plan_file)
            self.assertEqual(set(plan_file.read_text().splitlines()), {
                "inspector-sa", "inspector-role", "inspector-binding"
            })

    def test_gitops_application_and_manifest_contain_only_five_resources(self):
        app_text = (REPO_ROOT / "kubernetes/applications/monikey-inspection-access.yaml").read_text()
        access_text = (REPO_ROOT / "kubernetes/platform/monikey/inspection-access/manifests/access.yaml").read_text()
        self.assertIn("repoURL: https://github.com/jeysibn/homelab-gitops.git", app_text)
        self.assertIn("targetRevision: main", app_text)
        self.assertIn("path: kubernetes/platform/monikey/inspection-access/manifests", app_text)
        self.assertIn("namespace: monikey", app_text)
        self.assertNotIn("automated:", app_text)
        self.assertNotIn("kubernetes/platform/monikey/manifests", app_text)
        docs = [part.strip() for part in access_text.split("---") if part.strip()]
        identities = set()
        for document in docs:
            kind = re.search(r"(?m)^kind: (.+)$", document)
            name = re.search(r"(?m)^  name: (.+)$", document)
            namespace = re.search(r"(?m)^  namespace: (.+)$", document)
            self.assertTrue(kind and name and namespace)
            identities.add((kind.group(1), name.group(1), namespace.group(1)))
        self.assertEqual(identities, {
            ("ServiceAccount", "monikey-schema-inspector", "monikey"),
            ("Role", "monikey-schema-inspector", "monikey"),
            ("RoleBinding", "monikey-schema-inspector", "monikey"),
            ("Role", "monikey-inspection-token-issuer", "monikey"),
            ("RoleBinding", "monikey-inspection-token-issuer", "monikey"),
        })
        self.assertNotRegex(access_text, r"(?i)kind: (Secret|ConfigMap|Deployment|StatefulSet|Job)\b")
        self.assertNotIn("argocd.argoproj.io/hook", access_text)

    def test_workflow_is_manual_protected_read_only_and_never_syncs(self):
        workflow = (REPO_ROOT / ".github/workflows/monikey-inspection-access-audit.yml").read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("default: false", workflow)
        self.assertIn("github.ref == 'refs/heads/main' && inputs.confirm_read_only_audit", workflow)
        self.assertIn("name: monikey-inspection-provisioning", workflow)
        self.assertIn("ARGOCD_READONLY_AUTH_TOKEN", workflow)
        self.assertIn("tailscale/github-action@v2", workflow)
        self.assertIn("argocd app get monikey-inspection-access", workflow)
        self.assertIn("--app-namespace argocd", workflow)
        self.assertNotIn("--namespace monikey", workflow)
        self.assertIn("argocd app diff monikey-inspection-access", workflow)
        self.assertNotIn("argocd app sync", workflow)
        self.assertNotIn("argocd app wait", workflow)
        self.assertNotIn("--hard-refresh", workflow)
        self.assertNotIn("--insecure", workflow)
        self.assertNotIn("ARGOCD_AUTH_TOKEN: ${{ secrets.ARGOCD_AUTH_TOKEN }}", workflow)
        self.assertNotIn("upload-artifact", workflow)
        self.assertIn("trap ", workflow)


if __name__ == "__main__":
    unittest.main()
