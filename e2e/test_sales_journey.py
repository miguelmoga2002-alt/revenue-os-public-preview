"""Chromium E2E: login → CSV UI → Pipeline → Account 360 → Focus → follow-up.

All test identities and records are synthetic, provisioned exclusively in the
isolated Docker Compose runner. This test uses actual HTTP and browser JS:
no API mocking, no localStorage fixtures, no production datasets.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect

BASE = os.environ.get("REVENUE_OS_E2E_URL", "")
PASSWORD = "E2E_only_synthetic_2026_not_a_real_secret!"
ARTIFACTS = Path("artifacts/browser")


def api(page, method, endpoint, body=None, token=None):
    authorization = token or page.evaluate("() => token")
    response = page.request.fetch(
        BASE + endpoint, method=method,
        headers={"Authorization": "Bearer " + authorization,
                 **({"Content-Type": "application/json"} if body is not None else {})},
        data=body,
        timeout=20_000,
    )
    return response


def require(response, expected=200):
    assert response.status == expected, (
        f"{response.request.method} {response.url} returned "
        f"{response.status}: {response.text()[:1500]}"
    )
    return response.json()


def crm_login(page, username):
    page.goto(BASE + "/crm")
    page.locator("#username").fill(username)
    page.locator("#password").fill(PASSWORD)
    page.locator("#signin").click()
    expect(page.locator("#workspace")).to_be_visible()
    assert page.locator("html").get_attribute("lang") == "es"


def migration_login(page):
    page.goto(BASE + "/")
    page.locator("#username").fill("e2e-admin-a")
    page.locator("#password").fill(PASSWORD)
    page.locator("#loginBtn").click()
    expect(page.locator("#step1")).to_be_visible()
    expect(page.locator("#login")).to_be_hidden()


def import_csv(page, kind, filename, csv, mapping, return_to_start=True):
    page.locator("#kind").select_option(kind)
    page.locator("#file").set_input_files({
        "name": filename, "mimeType": "text/csv", "buffer": csv.encode("utf-8")
    })
    expect(page.locator("#fileStatus")).to_contain_text("Seleccionado:")
    expect(page.locator("#fileStatus")).to_contain_text(filename)
    page.locator("#analyze").click()
    expect(page.locator("#step2")).to_be_visible()
    expect(page.locator("#total")).not_to_have_text("0")
    for field, header in mapping.items():
        page.locator(f'#mapping select[data-field="{field}"]').select_option(header)
    page.locator("#continue").click()
    expect(page.locator("#step3")).to_be_visible()
    page.locator("#commit").click()
    expect(page.locator("#step4")).to_be_visible()
    expect(page.locator("#msg3")).to_contain_text("Importación confirmada")
    expect(page.locator("#importResult")).to_be_visible()
    expect(page.locator("#importResult")).to_contain_text("Guardados en PostgreSQL")
    if return_to_start:
        page.locator("#newMigration").click()
        expect(page.locator("#step1")).to_be_visible()




def audit_accessibility(page, label):
    """Axe-core automated check (not a complete manual WCAG audit)."""
    axe_path = Path("e2e/node_modules/axe-core/axe.min.js")
    assert axe_path.is_file(), "CI must install pinned axe-core"
    page.add_script_tag(path=str(axe_path))
    report = page.evaluate("""async () => {
        const result = await axe.run(document, {
            runOnly: { type: 'tag',
                values: ['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa'] }
        });
        return { violations: result.violations.map(v => ({
            id: v.id, impact: v.impact, description: v.description,
            nodes: v.nodes.map(n => ({target: n.target, summary: n.failureSummary}))
        })) };
    }""")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / f"axe_{label}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    serious = [v for v in report["violations"]
               if v["impact"] in ("serious", "critical")]
    assert not serious, f"Serious accessibility violations in {label}: {serious}"

def test_complete_sales_journey_in_real_chromium():
    if not BASE or os.environ.get("GITHUB_ACTIONS") != "true":
        pytest.skip("Browser journey only runs in disposable GitHub Actions")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="es-ES", timezone_id="Europe/Madrid",
            # Test-only context: axe-core injection requires CSP bypass. The
            # app still serves strict CSP; tests/test_http_hardening.py verifies
            # real HTML hashes and the restrictive header for every screen.
            bypass_csp=True,
        )
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        migration = context.new_page()
        migration.on("pageerror", lambda error: errors.append(str(error)))
        try:
            migration_login(migration)
            migration.locator('#openHistory').click()
            expect(migration.locator('#step4')).to_be_visible()
            migration.locator('#newMigration').click()
            expect(migration.locator('#step1')).to_be_visible()
            base = {"external_id": "ID", "name": "Name"}
            import_csv(migration, "companies", "companies.csv",
                       "ID,Name\nO1,Synthetic Company QA\n", base)
            # UI-only rollback of a disposable import. This must work on
            # PostgreSQL without shell access or database deletion.
            import_csv(migration, "companies", "rollback_demo.csv",
                       "ID,Name\nO-ROLL,Discard this test company\n", base,
                       return_to_start=False)
            expect(migration.locator("#rollbackSelected")).to_be_disabled()
            migration.locator("#history .batch-check").first.check()
            expect(migration.locator("#rollbackSelected")).to_be_enabled()
            migration.once("dialog", lambda dialog: dialog.accept())
            migration.locator("#rollbackSelected").click()
            expect(migration.locator("#rollbackStatus")).to_contain_text("1 de 1 lotes revertidos")
            # Check durable absence, not just a success notification.
            after_rollback = require(api(migration, "GET", "/api/v1/records?kind=companies"))
            assert "O-ROLL" not in {row["external_id"] for row in after_rollback}
            expect(migration.locator("#metrics")).to_contain_text("companies")
            migration.locator("#newMigration").click()
            import_csv(migration, "contacts", "contacts.csv",
                       "ID,Name,Company ID,Email\nP1,Synthetic Lead QA,O1,qa@example.invalid\nP2,Hidden Lead QA,O1,hidden@example.invalid\n",
                       {**base, "company_external_id": "Company ID", "email": "Email"})
            import_csv(migration, "deals", "deals.csv",
                       "ID,Name,Stage,Company ID,Contact ID\nD1,Synthetic Opportunity QA,Qualified,O1,P1\n",
                       {**base, "stage": "Stage",
                        "company_external_id": "Company ID",
                        "contact_external_id": "Contact ID"}, return_to_start=False)
            # Exercise the public admin UI, not an auxiliary API call.
            migration.locator("#reconcileSource").fill("invalid source")
            migration.locator("#previewRelations").click()
            expect(migration.locator("#relationsMessage")).to_contain_text("Proveedor inválido")
            expect(migration.locator("#applyRelations")).to_be_disabled()
            migration.locator("#reconcileSource").fill("pipedrive")
            migration.locator("#previewRelations").click()
            expect(migration.locator("#relationSummary")).to_contain_text("Referencias resolubles")
            expect(migration.locator("#relationMissing")).to_contain_text("Ninguna referencia pendiente")
            expect(migration.locator("#applyRelations")).to_be_enabled()
            migration.locator("#applyRelations").click()
            expect(migration.locator("#relationsMessage")).to_contain_text("Reconciliación registrada")
            assert "Creados: 4" in migration.locator("#relationsMessage").inner_text()
            assert migration.locator("#applyRelations").is_disabled()
            audit_accessibility(migration, "migration")
            admin_token = migration.evaluate("() => token")
            summary = require(api(migration, "GET", "/api/v1/migrations/summary", token=admin_token))
            assert summary["companies"] == 1
            assert summary["contacts"] == 2
            assert summary["deals"] == 1
            # The browser already confirmed reconciliation; direct SQL/API only
            # observes its resulting links, never performs this action.

            deals = require(api(migration, "GET", "/api/v1/records?kind=deals", token=admin_token))
            deal_id = next(row["id"] for row in deals if row["external_id"] == "D1")
            contacts = require(api(migration, "GET", "/api/v1/records?kind=contacts", token=admin_token))
            lead_id = next(row["id"] for row in contacts if row["external_id"] == "P1")
            hidden_id = next(row["id"] for row in contacts if row["external_id"] == "P2")
            persisted_links = require(api(migration, "GET",
                                          f"/api/v1/records/{deal_id}/links",
                                          token=admin_token))
            assert len(persisted_links) == 2

            manager = context.new_page()
            manager.on("pageerror", lambda error: errors.append(str(error)))
            crm_login(manager, "e2e-admin-a")
            # Admin provisions a synthetic rep entirely from the UI.
            expect(manager.locator("#manageTeam")).to_be_visible()
            manager.locator("#manageTeam").click()
            expect(manager.locator("#teamDialog")).to_be_visible()
            manager.locator("#repUsername").fill("e2e-created-rep")
            manager.locator("#repPassword").fill(PASSWORD)
            manager.locator("#saveRep").click()
            expect(manager.locator("#teamMessage")).to_contain_text("Comercial creado")
            expect(manager.locator("#teamList")).to_contain_text("e2e-created-rep")
            audit_accessibility(manager, "team_dialog")
            manager.locator("#closeTeam").click()
            expect(manager.locator("#teamDialog")).to_be_hidden()
            new_rep = context.new_page()
            new_rep.goto(BASE + "/focus")
            new_rep.locator("#username").fill("e2e-created-rep")
            new_rep.locator("#password").fill(PASSWORD)
            new_rep.locator("#signin").click()
            expect(new_rep.locator("#app")).to_be_visible()
            new_rep.close()

            expect(manager.locator("#pipedriveSync")).to_be_visible()
            expect(manager.locator("#multiCRM")).to_be_visible()
            expect(manager.locator("#multiPreview")).to_be_disabled()
            expect(manager.locator("#multiStatus")).to_contain_text("Sin conexión activa")

            expect(manager.locator("#pipedrivePreview")).to_be_disabled()
            expect(manager.locator("#pipedriveStatus")).to_contain_text("No configurado")

            expect(manager.locator("#board")).to_contain_text("Synthetic Opportunity QA")
            expect(manager.locator("#board")).to_contain_text("Ref. CRM: pipedrive / D1")
            # Prompt is a real browser dialog, not a direct API fixture.
            manager.once("dialog", lambda dialog: dialog.accept("Qualified, Proposal, Won"))
            manager.locator("#configure").click()
            expect(manager.locator("#board .lane")).to_have_count(3)
            qualified = manager.locator(".lane").filter(has=manager.locator("h2", has_text="Qualified"))
            proposal = manager.locator(".lane").filter(has=manager.locator("h2", has_text="Proposal"))
            expect(qualified).to_contain_text("Synthetic Opportunity QA")
            audit_accessibility(manager, "pipeline")

            with manager.expect_response(
                lambda response: "/pipeline/deals/" in response.url
                and response.request.method == "PATCH"
            ) as moved:
                qualified.locator(".deal").drag_to(proposal)
            assert moved.value.status == 200
            expect(proposal).to_contain_text("Synthetic Opportunity QA")
            require(api(manager, "GET", f"/api/v1/crm/{deal_id}/360"))
            assert require(api(manager, "GET", f"/api/v1/crm/{deal_id}/360"))["record"]["stage"] == "Proposal"

            # Reload eliminates the in-memory token, so re-login must reload
            # the persisted board state from PostgreSQL, not from browser storage.
            crm_login(manager, "e2e-admin-a")
            expect(manager.locator(".lane").filter(
                has=manager.locator("h2", has_text="Proposal")
            )).to_contain_text("Synthetic Opportunity QA")
            manager.locator(".deal").filter(
                has_text="Synthetic Opportunity QA"
            ).get_by_role("button", name="Abrir Account 360").click()
            expect(manager.locator("#drawer")).to_have_class(re.compile(".*fullscreen.*"))
            expect(manager).to_have_url(BASE + "/deals/" + deal_id)
            expect(manager.locator("#details .deal-ref")).to_contain_text(deal_id)
            expect(manager.locator("#details .deal-ref")).to_contain_text("Referencia CRM: pipedrive / D1")
            expect(manager.locator("#copyDealUrl")).to_be_visible()
            expect(manager.locator("#close")).to_be_focused()
            expect(manager.locator("#details h1")).to_have_text("Synthetic Opportunity QA")
            # Canonical URL must survive a fresh browser tab and login.
            fresh = context.new_page()
            fresh.goto(BASE + "/deals/" + deal_id)
            expect(fresh.locator("#login")).to_be_visible()
            fresh.locator("#username").fill("e2e-admin-a")
            fresh.locator("#password").fill(PASSWORD)
            fresh.locator("#signin").click()
            expect(fresh.locator("#details h1")).to_have_text("Synthetic Opportunity QA")
            expect(fresh).to_have_url(BASE + "/deals/" + deal_id)
            fresh.close()
            # A same-tenant rep without a grant MUST NOT load the copied link.
            unauthorized = context.new_page()
            unauthorized.goto(BASE + "/deals/" + deal_id)
            unauthorized.locator("#username").fill("e2e-created-rep")
            unauthorized.locator("#password").fill(PASSWORD)
            unauthorized.locator("#signin").click()
            expect(unauthorized.locator("#message")).to_contain_text("Registro no accesible")
            expect(unauthorized.locator("#drawer")).not_to_have_class(re.compile(".*open.*"))
            assert require(api(unauthorized, "GET", f"/api/v1/crm/{deal_id}/360"), 404)
            unauthorized.close()
            expect(manager.locator("#dealTimelineList")).to_contain_text("Etapa modificada")
            manager.locator("#dealNoteText").fill("CI: nota inicial del deal, sin comunicación externa")
            with manager.expect_response(
                lambda response: "/deals/" in response.url
                and response.url.endswith("/notes") and response.request.method == "POST"
            ) as note_created:
                manager.locator("#dealSaveNote").click()
            assert note_created.value.status == 200
            expect(manager.locator("#dealTimelineList")).to_contain_text("CI: nota inicial del deal")
            expect(manager.locator("#dealNoteMessage")).to_contain_text("Nota guardada")
            expect(manager.locator("#accountOther")).to_contain_text("Synthetic Lead QA")
            audit_accessibility(manager, "account360")
            # Account 360 stage selector gives a keyboard-accessible alternative to drag-drop.
            assert manager.locator('#details select[aria-label="Nueva etapa"]').count() == 1
            # After typing a note, keyboard focus correctly stays in the editing context.
            manager.keyboard.press("Escape")
            expect(manager.locator("#drawer")).not_to_have_class(re.compile(".*open.*"))
            expect(manager).to_have_url(BASE + "/crm")
            # Keyboard-only activation, focus trapping and Escape restoration.
            card = manager.locator(".deal").filter(has_text="Synthetic Opportunity QA")
            card.focus()
            manager.keyboard.press("Enter")
            expect(manager.locator("#drawer")).to_have_class(re.compile(".*open.*"))
            expect(manager.locator("#close")).to_be_focused()
            manager.keyboard.press("Tab")
            assert manager.evaluate(
                "() => document.getElementById('drawer').contains(document.activeElement)"
            )
            manager.keyboard.press("Escape")
            expect(manager.locator("#drawer")).not_to_have_class(re.compile(".*open.*"))
            expect(card).to_be_focused()

            rep_id = next(row["id"] for row in require(api(
                manager, "GET", "/api/v1/admin/reps"
            )) if row["username"] == "e2e-rep-a")
            # Assignment is inside the opened deal, with record binding
            # determined by the drawer; no global CRM-record dropdown.
            expect(manager.locator("#adminTasks")).to_be_hidden()
            card.get_by_role("button", name="Abrir Account 360").click()
            expect(manager.locator("#adminTasks")).to_be_visible()
            assert manager.locator("#drawer #adminTasks").count() == 1
            expect(manager.locator("#taskRecordName")).to_have_text("Synthetic Opportunity QA")
            assert manager.locator("#taskRecord").count() == 0
            manager.locator("#assignTask").click()
            assert manager.locator("#taskOwner").evaluate("el => el.validity.valueMissing")
            manager.locator("#taskOwner").select_option(rep_id)
            manager.locator("#taskTitle").fill("E2E: Llamar al lead QA")
            manager.locator("#taskChannel").select_option("call")
            due_local = (datetime.now(timezone.utc) -
                         timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M")
            manager.locator("#taskDue").fill(due_local)
            with manager.expect_response(
                lambda response: response.url.endswith("/api/v1/work/tasks")
                and response.request.method == "POST"
            ) as created_task:
                manager.locator("#assignTask").click()
            assert created_task.value.status == 200
            task = created_task.value.json()
            assert not task["already_recorded"]
            # Exact same payload/key retried through a second HTTP request must
            # return original task (not a new task and not a new audit event).
            original_payload = created_task.value.request.post_data_json
            assert original_payload["idem"]
            assert original_payload["record_id"] == deal_id
            replay = require(api(manager, "POST", "/api/v1/work/tasks", original_payload))
            assert replay["id"] == task["id"] and replay["already_recorded"]
            bad_reuse = {**original_payload, "title": "Different action"}
            assert require(api(manager, "POST", "/api/v1/work/tasks", bad_reuse), 409)
            expect(manager.locator("#taskMessage")).to_contain_text("Tarea programada correctamente")
            expect(manager.locator("#dealTimelineList")).to_contain_text("Tarea asignada:")
            expect(manager.locator("#dealTimelineList .timeline-task .timeline-kind")).to_contain_text("TAREA PROGRAMADA")
            expect(manager.locator("#assignTask")).to_be_enabled()
            assert require(api(manager, "GET", f"/api/v1/crm/{deal_id}/360"))[
                "next_commitment"]["title"] == "E2E: Llamar al lead QA"
            # A later scheduled manager task must be visible in Focus now,
            # but must NOT displace the due next-best action or be claimable.
            future_payload = {**original_payload,
                "idem": "e2e-manager-future-123456",
                "title": "E2E: Preparar reunión futura",
                "due_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()}
            future_task = require(api(manager, "POST", "/api/v1/work/tasks", future_payload))
            assert future_task["already_recorded"] is False
            waiting_payload = {**original_payload,
                "idem": "e2e-manager-waiting-123456",
                "title": "E2E: Tarea secundaria ya vencida",
                "due_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}
            waiting_task = require(api(manager, "POST", "/api/v1/work/tasks", waiting_payload))
            assert waiting_task["already_recorded"] is False

            rep = context.new_page()
            rep.on("pageerror", lambda error: errors.append(str(error)))
            rep.goto(BASE + "/focus")
            rep.locator("#username").fill("e2e-rep-a")
            rep.locator("#password").fill(PASSWORD)
            rep.locator("#signin").click()
            expect(rep.locator("#task")).to_contain_text("E2E: Llamar al lead QA")
            expect(rep.locator("#upcoming")).to_be_visible()
            expect(rep.locator("#upcomingList")).to_contain_text("E2E: Preparar reunión futura")
            expect(rep.locator("#queued")).to_be_visible()
            expect(rep.locator("#queuedList")).to_contain_text("E2E: Tarea secundaria ya vencida")
            assert any(x["id"] == waiting_task["id"]
                       for x in require(api(rep, "GET", "/api/v1/work/queue"))["queued"])
            assert require(api(rep, "POST", "/api/v1/work/claim/" + future_task["id"]), 409)
            expect(rep.locator("#actionEvidence")).to_contain_text("Datos de la recomendación")
            expect(rep.locator("#actionEvidence")).to_contain_text("work_items.due_at")
            expect(rep.locator("#actionEvidence")).to_contain_text("no se han verificado señales externas")
            initial_nba = require(api(rep, "GET", "/api/v1/work/queue"))["ready"][0]
            assert initial_nba["recommendation"]["automatic_execution"] is False
            assert initial_nba["score"] == sum(initial_nba["score_breakdown"].values())
            assert all(fact["origin"] == "work_items" for fact in initial_nba["evidence"])
            assert require(api(rep, "GET", f"/api/v1/crm/{hidden_id}/360"), 404)
            rep.get_by_role("button", name="Ver Account 360").click()
            expect(rep).to_have_url(BASE + "/focus?record=" + deal_id)
            expect(rep.locator("#permanentDealUrl")).to_have_attribute("href", "/deals/" + deal_id)
            expect(rep.locator("#copyDealUrlFocus")).to_be_visible()
            expect(rep.locator(".deal-ref")).to_contain_text("Referencia interna: " + deal_id)
            expect(rep.locator("#context")).to_be_visible()
            # Focus also re-opens its deep link after a fresh sign-in.
            focus_deep = context.new_page()
            focus_deep.goto(BASE + "/focus?record=" + deal_id)
            focus_deep.locator("#username").fill("e2e-rep-a")
            focus_deep.locator("#password").fill(PASSWORD)
            focus_deep.locator("#signin").click()
            expect(focus_deep.locator("#context")).to_be_visible()
            expect(focus_deep.locator("#context")).to_contain_text("Synthetic Opportunity QA")
            focus_deep.close()
            expect(rep.locator("#context")).to_contain_text("Synthetic Opportunity QA")
            expect(rep.locator("#context")).to_have_class(re.compile(".*workspace-detail.*"))
            expect(rep.locator("#focusTimelineList")).to_contain_text("CI: nota inicial del deal")
            rep.locator("#focusNoteText").fill("CI: nota comercial guardada desde Focus")
            rep.locator("#focusSaveNote").click()
            expect(rep.locator("#focusNoteStatus")).to_contain_text("Nota guardada")
            expect(rep.locator("#focusTimelineList")).to_contain_text("CI: nota comercial guardada desde Focus")
            # A rep creates and replans only their own action from the same deal.
            rep.locator("#ownTaskTitle").fill("E2E: Preparar propuesta propia")
            rep.locator("#ownTaskChannel").select_option("email")
            own_due = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
            rep.locator("#ownTaskDue").fill(own_due)
            rep.locator("#ownTaskSubmit").click()
            expect(rep.locator("#ownTaskMessage")).to_contain_text("Tarea propia programada")
            own_tasks = require(api(rep, "GET", f"/api/v1/deals/{deal_id}/my-tasks"))["items"]
            own = next(x for x in own_tasks if x["title"] == "E2E: Preparar propuesta propia")
            rep.locator("#ownReplanSelect").select_option(own["id"])
            revised_due = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
            rep.locator("#ownReplanDue").fill(revised_due)
            rep.locator("#ownReplanSubmit").click()
            expect(rep.locator("#ownReplanMessage")).to_contain_text("reprogramada y auditada")
            updated = require(api(rep, "GET", f"/api/v1/deals/{deal_id}/my-tasks"))["items"]
            assert next(x for x in updated if x["id"] == own["id"])["due_at"] != own["due_at"]
            expect(rep.locator("#focusTimelineList")).to_contain_text("E2E: Preparar propuesta propia")
            expect(rep.locator("#focusTimelineList .event-reschedule .timeline-label")).to_contain_text("REPROGRAMACIÓN")
            expect(rep.locator("#context")).to_contain_text("Señales comerciales registradas")
            rep.locator("#signalKind").select_option("proposal_sent")
            rep.locator("#signalDetail").fill("E2E: propuesta declarada, no conector")
            rep.locator("#signalSave").click()
            expect(rep.locator("#signalMessage")).to_contain_text("Señal registrada")
            recorded = require(api(rep, "GET", f"/api/v1/signals/records/{deal_id}"))["items"]
            assert len(recorded) == 1
            assert recorded[0]["source_type"] == "manual_report"
            assert recorded[0]["verification"] == "user_attested"
            rep.locator('[data-correct]').click()
            rep.locator("#signalKind").select_option("meeting_held")
            rep.locator("#signalDetail").fill("E2E: corrección manual")
            rep.locator("#signalSave").click()
            expect(rep.locator("#signalMessage")).to_contain_text("Señal registrada")
            corrected = require(api(rep, "GET", f"/api/v1/signals/records/{deal_id}"))["items"]
            assert len(corrected) == 2
            assert sum(x["status"] == "current" for x in corrected) == 1
            assert any(x["supersedes_id"] == recorded[0]["id"] for x in corrected)
            rep.locator("#contextBack").click()
            expect(rep.locator("#context")).to_be_hidden()
            expect(rep).to_have_url(BASE + "/focus")
            rep.locator("#claim").click()
            expect(rep.locator("#outcome")).to_be_visible()
            audit_accessibility(rep, "focus")
            rep.locator("#result").select_option("contacted")
            rep.locator("#note").fill("CI: lead atendido desde Chromium")
            # Missing next action must reject the attempt and preserve active task.
            rep.locator("#finish").click()
            expect(rep.locator("#finishmessage")).to_contain_text("programa el siguiente paso")
            expect(rep.locator("#outcome")).to_be_visible()
            rep.locator("#next").fill("Enviar propuesta QA")
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
            rep.locator("#due").fill(tomorrow)
            rep.locator("#finish").click()
            expect(rep.locator("#message")).to_contain_text("Resultado guardado")
            signal_history = require(api(rep, "GET", f"/api/v1/signals/records/{deal_id}"))["items"]
            assert any(x["source_type"] == "work_result" and x["verification"] == "recorded_action" for x in signal_history)
            result = require(api(rep, "GET", f"/api/v1/crm/{deal_id}/360"))
            assert result["recent_results"][0]["note"] == "CI: lead atendido desde Chromium"
            # The secondary overdue task remains the earliest commitment.
            # Completing the primary action must still create the separate follow-up.
            assert result["next_commitment"]["title"] == "E2E: Tarea secundaria ya vencida"
            followups = require(api(rep, "GET", f"/api/v1/deals/{deal_id}/my-tasks"))["items"]
            assert any(row["title"] == "Enviar propuesta QA" for row in followups)

            # Guided reps cannot manipulate Kanban and the second tenant
            # cannot inspect the first tenant's deal or its Account 360.
            rep_pipeline = context.new_page()
            rep_pipeline.on("pageerror", lambda error: errors.append(str(error)))
            crm_login(rep_pipeline, "e2e-rep-a")
            expect(rep_pipeline.locator("#pipedriveSync")).to_be_hidden()
            expect(rep_pipeline.locator("#multiCRM")).to_be_hidden()


            expect(rep_pipeline.locator("#message")).to_contain_text("Focus es obligatorio")
            expect(rep_pipeline.locator(".deal")).to_have_count(0)
            expect(rep_pipeline.locator("#adminTasks")).to_be_hidden()
            expect(rep_pipeline.locator("#manageTeam")).to_be_hidden()
            assert require(api(rep_pipeline, "POST", "/api/v1/work/tasks", {
                "idem": "forbidden-user-task-123", "owner_id": rep_id, "record_id": lead_id, "title": "Forbidden",
                "channel": "call", "due_at": datetime.now(timezone.utc).isoformat(),
            }), 403)
            assert require(api(rep_pipeline, "GET", "/api/v1/pipeline/board"), 403)
            other = context.new_page()
            other.on("pageerror", lambda error: errors.append(str(error)))
            crm_login(other, "e2e-admin-b")
            assert require(api(other, "GET", f"/api/v1/crm/{deal_id}/360"), 404)
            assert require(api(other, "GET", "/api/v1/pipeline/board"))["lanes"] == []
            assert not errors, "Browser JavaScript errors: " + " | ".join(errors)
        finally:
            if not migration.is_closed():
                migration.screenshot(path=str(ARTIFACTS / "migration_end.png"), full_page=True)
            context.tracing.stop(path=str(ARTIFACTS / "sales_journey_trace.zip"))
            browser.close()


def test_migration_login_validation_and_real_network_recovery():
    """Covers invalid login, missing username and real browser offline recovery."""
    if not BASE or os.environ.get("GITHUB_ACTIONS") != "true":
        pytest.skip("Real-network browser QA runs only on disposable CI")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            # axe-core must be injected in this isolated context. CSP is tested
            # separately in test_real_browser_csp_all_three_logins.
            context = browser.new_context(viewport={"width": 390, "height": 844},
                                          locale="es-ES", bypass_csp=True)
            page = context.new_page()
            page.goto(BASE + "/")
            requests = []
            page.on("request", lambda request: requests.append(request)
                    if request.url.endswith("/api/v1/auth/login") else None)
            # Placeholder is not an actual value. Empty username must never
            # reach Pydantic as a raw JSON validation error.
            expect(page.locator("#username")).to_have_value("")
            page.locator("#password").fill(PASSWORD)
            page.locator("#loginBtn").click()
            expect(page.locator("#loginMsg")).to_contain_text("Introduce tu usuario")
            assert not requests, "Empty username must be rejected in the UI"
            assert "string_too_short" not in page.locator("#loginMsg").inner_text()

            page.locator("#username").fill("e2e-admin-a")
            page.locator("#password").fill("intentionally-invalid-e2e-password")
            page.locator("#loginBtn").click()
            expect(page.locator("#loginMsg")).to_contain_text("Credenciales")
            assert "string_too_short" not in page.locator("#loginMsg").inner_text()

            # Native form keyboard submission, no mouse required.
            page.locator("#password").fill(PASSWORD)
            page.locator("#password").press("Enter")
            expect(page.locator("#step1")).to_be_visible()
            expect(page.locator("#login")).to_be_hidden()
            audit_accessibility(page, "migration_mobile")

            context.set_offline(True)
            page.locator("#openHistory").click()
            expect(page.locator("#msg1")).to_contain_text("Sin conexión con el servidor")
            context.set_offline(False)
            page.locator("#openHistory").click()
            expect(page.locator("#step4")).to_be_visible()
        finally:
            browser.close()


@pytest.mark.parametrize("path,username,login_button,logout_button,private_selector", [
    ("/", "e2e-admin-a", "#loginBtn", "#logout", "#app"),
    ("/crm", "e2e-admin-a", "#signin", "#logout", "#workspace"),
    ("/focus", "e2e-rep-a", "#signin", "#exit", "#app"),
])
def test_browser_logout_requires_server_ack_and_revokes_old_bearer(
        path, username, login_button, logout_button, private_selector):
    """Real HTTP revocation in all three UIs; network failures must not be silent."""
    if not BASE or os.environ.get("GITHUB_ACTIONS") != "true":
        pytest.skip("Run only against synthetic isolated browser CI")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                viewport={"width": 1280, "height": 800}, locale="es-ES")
            page = context.new_page()
            page.goto(BASE + path)
            page.locator("#username").fill(username)
            page.locator("#password").fill(PASSWORD)
            page.locator(login_button).click()
            expect(page.locator(private_selector)).to_be_visible()
            expect(page.locator("#login")).to_be_hidden()
            original_token = page.evaluate("() => token")
            assert isinstance(original_token, str) and len(original_token) == 64
            require(api(page, "GET", "/api/v1/me", token=original_token))

            # The browser must NOT report a successful server-side logout
            # after a failed network request, and must preserve retry ability.
            context.set_offline(True)
            page.locator(logout_button).click()
            expect(page.locator("#logoutError")).to_be_visible()
            expect(page.locator("#logoutError")).to_contain_text(
                "No se pudo confirmar el cierre de sesión")
            expect(page.locator(private_selector)).to_be_visible()
            expect(page.locator("#login")).to_be_hidden()
            context.set_offline(False)
            require(api(page, "GET", "/api/v1/me", token=original_token))

            # A successful logout invalidates the *original* bearer on the
            # server, not only the in-memory UI variable.
            page.locator(logout_button).click()
            expect(page.locator("#login")).to_be_visible()
            expect(page.locator(private_selector)).to_be_hidden()
            require(api(page, "GET", "/api/v1/me", token=original_token), 401)
            require(api(page, "GET", "/api/v1/auth/sessions",
                        token=original_token), 401)
        finally:
            browser.close()


@pytest.mark.parametrize("path", ["/", "/crm", "/focus"])
@pytest.mark.parametrize("viewport_width", [390, 640])
def test_narrow_viewports_no_horizontal_document_overflow(path, viewport_width):
    """Automated reflow smoke, not a substitute for manual browser zoom review."""
    if not BASE or os.environ.get("GITHUB_ACTIONS") != "true":
        pytest.skip("Responsive browser QA runs only on disposable CI")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width":viewport_width,"height":844},
                                    locale="es-ES", bypass_csp=False)
            response = page.goto(BASE + path)
            assert response and response.status == 200
            # No page-level horizontal scrolling at mobile and narrowed desktop.
            dimensions = page.evaluate("""() => ({
                actual: document.documentElement.scrollWidth,
                expected: document.documentElement.clientWidth
            })""")
            assert dimensions["actual"] <= dimensions["expected"] + 2, (
                f"{path} at {viewport_width}px horizontal overflow: {dimensions}")
            for field in ("username", "password"):
                expect(page.locator("#"+field)).to_be_visible()
                assert page.locator("#"+field).bounding_box()["x"] >= 0
        finally:
            browser.close()


@pytest.mark.parametrize("path,button,visible",[
    ("/", "#loginBtn", "#step1"),
    ("/crm", "#signin", "#workspace"),
    ("/focus", "#signin", "#app"),
])
def test_real_browser_csp_all_three_logins(path, button, visible):
    """No bypass_csp: reproduce browser-side script execution on each login."""
    if not BASE or os.environ.get("GITHUB_ACTIONS") != "true":
        pytest.skip("CSP browser validation only runs on disposable CI")
    script_errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(locale="es-ES", bypass_csp=False)
            page = context.new_page()
            page.on("pageerror", lambda error: script_errors.append(str(error)))
            page.on("console", lambda event: script_errors.append(event.text)
                    if event.type == "error" else None)
            response = page.goto(BASE + path)
            assert response and response.status == 200
            csp = response.headers.get("content-security-policy","")
            assert "'sha256-" in csp and "unsafe-eval" not in csp
            page.locator("#username").fill("e2e-admin-a")
            page.locator("#password").fill(PASSWORD)
            page.locator(button).click()
            expect(page.locator(visible)).to_be_visible()
            assert not any("violates Content Security Policy" in e
                    or "Refused to execute" in e for e in script_errors), script_errors
        finally:
            browser.close()
