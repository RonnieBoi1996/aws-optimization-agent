from detect_stacks import DEFAULT_ALL_STACK_TRIGGERS, discover_stacks, slug, stacks_for_changes
from pr_comment import MARKER, MAX_BODY, build_plan_comment, fence_safe, step_results, truncate

STACKS = ["infra", "sandbox"]


def test_only_changed_stack_selected():
    assert stacks_for_changes(["sandbox/main.tf", "README.md"], STACKS, "modules",
                              DEFAULT_ALL_STACK_TRIGGERS) == ["sandbox"]


def test_non_terraform_change_selects_nothing():
    assert stacks_for_changes(["src/optimizer/llm.py", "infra/README.md"], STACKS, "modules",
                              DEFAULT_ALL_STACK_TRIGGERS) == []


def test_shared_module_or_pipeline_change_selects_all():
    for f in ("modules/net/main.tf", ".tflint.hcl", "ci/scope_guard.py",
              ".github/workflows/terraform-pr.yml"):
        assert stacks_for_changes([f], STACKS, "modules", DEFAULT_ALL_STACK_TRIGGERS) == STACKS


def test_apply_mode_ignores_pipeline_changes():
    assert stacks_for_changes(["ci/scope_guard.py"], STACKS, "modules", (".tflint.hcl",)) == []


def test_stack_root_discovery(tmp_path):
    (tmp_path / "stacks/app").mkdir(parents=True)
    (tmp_path / "stacks/app/main.tf").write_text("")
    (tmp_path / "stacks/empty").mkdir()
    assert discover_stacks(None, "stacks", tmp_path) == ["stacks/app"]
    assert slug("stacks/app") == "stacks-app"


def _job(name, conclusion, status="completed"):
    return {"name": name, "status": status, "conclusion": conclusion}


def test_step_results():
    jobs = [_job("fmt (sandbox)", "success"), _job("plan (sandbox)", "failure"),
            _job("tflint (sandbox)", None, status="in_progress")]
    r = step_results(jobs, "sandbox")
    assert (r["fmt"], r["plan"], r["tflint"], r["checkov"]) == ("success", "failure", "pending", "missing")


def test_truncate_keeps_start_and_summary():
    text = "Terraform will perform...\n" + "x" * 100_000 + "\nPlan: 1 to add, 0 to change, 0 to destroy."
    out = truncate(text, 5000)
    assert len(out) < 5200
    assert out.startswith("Terraform will perform") and out.endswith("0 to destroy.")
    assert "truncated" in out


def test_comment_stays_under_github_limit_with_huge_plans():
    stacks = ["infra", "sandbox"]
    plans = {s: "line\n" * 50_000 for s in stacks}
    body = build_plan_comment(stacks, [], plans, {}, "https://run", "abcdef1234")
    assert MARKER in body and len(body) <= MAX_BODY
    assert body.count("```") % 2 == 0  # every code fence is closed


def test_plan_text_cannot_break_out_of_code_fence():
    assert "```" not in fence_safe("evil ``` text")


def test_mask_replaces_account_id_and_email():
    from mask_text import mask
    text = "arn:aws:iam::123456789012:role/x  endpoint = me+alerts@example.com"
    out = mask(text, ["123456789012", "me+alerts@example.com", ""])
    assert "123456789012" not in out and "example.com" not in out and out.count("***") == 2


def test_skipped_matrix_job_with_unexpanded_name_counts_as_skipped():
    jobs = [_job("scope-guard (${{ matrix.stack.dir }})", "skipped")]
    assert step_results(jobs, "sandbox")["scope-guard"] == "skipped"
