(() => {
  "use strict";

  const byId = id => document.getElementById(id);
  const api = () => window.PostprocessWorkspace;
  const explicitAssignmentModes = new Map();
  const assignmentModeKey = (resultId, workflowId) => `${resultId || ""}::${workflowId || ""}`;

  function addOption(select, value, label, workflowId = "") {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    if (workflowId) option.dataset.workflowId = workflowId;
    select.appendChild(option);
    return option;
  }

  function hierarchySource(workflow, geojsonFiles = []) {
    const path = workflow.outputs?.regularized?.path || "";
    if (!path) return "";
    const catalogEntry = geojsonFiles.find(file => file.path === path);
    if (catalogEntry) return catalogEntry.stage === "regularized" ? path : "";
    return path.split("/").pop() === "regularized.geojson" ? path : "";
  }

  function refresh(context = api()?.getContext()) {
    if (!context) return;
    const select = byId("ppHierarchySource");
    const previous = select.value;
    const workflows = context.workflows.filter(workflow =>
      workflow.workflow_kind !== "anomaly" && hierarchySource(workflow, context.geojsonFiles)
    );
    select.replaceChildren();
    addOption(select, "", "Select a regularized output…");
    workflows.forEach((workflow, index) => {
      const created = workflow.created_at
        ? new Date(workflow.created_at).toLocaleString()
        : workflow.id;
      const latest = index === 0 ? " · Latest" : "";
      addOption(
        select,
        hierarchySource(workflow, context.geojsonFiles),
        `Regularized · ${created}${latest}`,
        workflow.id,
      );
    });
    if (previous && [...select.options].some(option => option.value === previous)) select.value = previous;
    else if (select.options.length > 1) select.selectedIndex = 1;
    select.disabled = workflows.length === 0;
    const selectedWorkflowId = select.selectedOptions[0]?.dataset.workflowId;
    const selectedWorkflow = workflows.find(workflow => workflow.id === selectedWorkflowId);
    const hierarchyUnavailable = !select.value
      || ["queued", "running"].includes(selectedWorkflow?.status);
    byId("ppBuildHierarchy").disabled = hierarchyUnavailable;
    byId("ppSkipHierarchy").disabled = hierarchyUnavailable;
    const assignmentSelect = byId("ppAssignmentSource");
    const assignmentWorkflows = context.workflows.filter(workflow =>
      workflow.workflow_kind !== "anomaly" && hierarchySource(workflow, context.geojsonFiles)
    );
    assignmentSelect.replaceChildren();
    addOption(assignmentSelect, "", "Select how to assign IDs…");
    assignmentWorkflows.forEach((workflow, index) => {
      const created = workflow.created_at
        ? new Date(workflow.created_at).toLocaleString()
        : workflow.id;
      const latest = index === 0 ? " · Latest" : "";
      if (workflow.outputs?.solar_rows?.path) {
        const rowsOption = addOption(
          assignmentSelect,
          `${workflow.id}:rows`,
          `Use Rows and Regularized panels · ${created}${latest}`,
          workflow.id,
        );
        rowsOption.dataset.useRows = "true";
      }
      const noRowsOption = addOption(
        assignmentSelect,
        `${workflow.id}:no-rows`,
        `Skip Rows and use Regularized panels only · ${created}${latest}`,
        workflow.id,
      );
      noRowsOption.dataset.useRows = "false";
    });
    if (assignmentSelect.options.length > 1) {
      const latestWorkflow = assignmentWorkflows.find(workflow => workflow.id === context.workflowId)
        || assignmentWorkflows[0];
      const explicitMode = explicitAssignmentModes.get(
        assignmentModeKey(context.resultId, latestWorkflow?.id),
      );
      const preferredUseRows = explicitMode == null
        ? Boolean(latestWorkflow?.outputs?.solar_rows?.path)
        : explicitMode;
      const preferred = [...assignmentSelect.options].find(option =>
        option.dataset.workflowId === latestWorkflow?.id
        && option.dataset.useRows === String(preferredUseRows)
      );
      assignmentSelect.value = preferred?.value || assignmentSelect.options[1].value;
    }
    assignmentSelect.disabled = assignmentWorkflows.length === 0;
    const assignmentWorkflowId = assignmentSelect.selectedOptions[0]?.dataset.workflowId;
    const assignmentWorkflow = assignmentWorkflows.find(workflow => workflow.id === assignmentWorkflowId);
    byId("ppAssignIds").disabled = !assignmentSelect.value
      || ["queued", "running"].includes(assignmentWorkflow?.status);
  }

  async function buildHierarchy() {
    const workspace = api();
    const context = workspace.getContext();
    const select = byId("ppHierarchySource");
    const option = select.selectedOptions[0];
    const workflowId = option?.dataset.workflowId;
    if (!context.resultId || !workflowId || !select.value) return;
    let workflow = context.workflows.find(item => item.id === workflowId);
    workspace.setMessage("Checking the existing Rows GeoJSON before replacement…");
    try {
      workflow = await workspace.requestJson(
        `/api/results/${encodeURIComponent(context.resultId)}/postprocess/${encodeURIComponent(workflowId)}`,
        { cache: "no-store" },
      );
    } catch (error) {
      workspace.setMessage(`Could not check the existing Rows output: ${error.message}`, "err");
      return;
    }
    const hasExistingHierarchy = Boolean(workflow?.outputs?.solar_rows);
    if (hasExistingHierarchy) {
      const confirmed = await workspace.confirmReplacement(
        "Replace rows?",
        "The existing Rows GeoJSON and its manual edits will be replaced. Existing row and panel IDs will be cleared until Step 4 is run again.",
      );
      if (!confirmed) return;
    }
    const inputPath = select.value;
    explicitAssignmentModes.delete(assignmentModeKey(context.resultId, workflowId));
    byId("ppBuildHierarchy").disabled = true;
    workspace.setMessage("Starting row generation…");
    try {
      workspace.selectWorkflow(workflowId);
      const payload = await workspace.requestJson(
        `/api/results/${encodeURIComponent(context.resultId)}/postprocess/${encodeURIComponent(workflowId)}/hierarchy`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            input_path: inputPath,
            max_orientation_difference_deg: Number(byId("ppRowAngle").value),
            max_lateral_distance_factor: Number(byId("ppRowLateral").value),
            max_along_gap_factor: Number(byId("ppRowGap").value),
            max_inner_row_gap_factor: Number(byId("ppInnerRowGap").value),
            min_row_overlap_percent: Number(byId("ppRowOverlap").value),
          }),
        },
      );
      await workspace.runWorkflow(payload);
    } catch (error) {
      workspace.setMessage(error.message, "err");
      byId("ppBuildHierarchy").disabled = false;
    }
  }

  async function assignIds() {
    const workspace = api();
    const context = workspace.getContext();
    const select = byId("ppAssignmentSource");
    const selectedOption = select.selectedOptions[0];
    const workflowId = selectedOption?.dataset.workflowId;
    const useRows = selectedOption?.dataset.useRows === "true";
    if (!context.resultId || !workflowId || !select.value) return;
    const workflow = context.workflows.find(item => item.id === workflowId);
    if (workflow?.assignment_stats) {
      const confirmed = await workspace.confirmReplacement(
        "Replace panel IDs?",
        "Existing row and panel IDs will be replaced. If anomalies were already assigned to these panels, that final anomaly output will be removed and must be assigned again.",
      );
      if (!confirmed) return;
    }
    byId("ppAssignIds").disabled = true;
    workspace.setMessage(useRows
      ? "Assigning IDs from the edited Rows layer…"
      : "Assigning panel IDs with row ID 0000…");
    try {
      workspace.selectWorkflow(workflowId);
      const payload = await workspace.requestJson(
        `/api/results/${encodeURIComponent(context.resultId)}/postprocess/${encodeURIComponent(workflowId)}/assign-ids?use_rows=${useRows}`,
        { method: "POST" },
      );
      await workspace.runWorkflow(payload);
    } catch (error) {
      workspace.setMessage(error.message, "err");
      byId("ppAssignIds").disabled = false;
    }
  }

  function setStepExpanded(stepId, expanded) {
    const step = byId(stepId);
    const collapsed = step?.classList.contains("collapsed");
    if (step && collapsed === expanded) step.querySelector(".postprocessStepCollapse")?.click();
  }

  function skipHierarchy() {
    const workspace = api();
    const sourceOption = byId("ppHierarchySource")?.selectedOptions[0];
    const workflowId = sourceOption?.dataset.workflowId;
    if (!workflowId) return;
    const assignmentSelect = byId("ppAssignmentSource");
    const noRowsOption = [...assignmentSelect.options].find(option =>
      option.dataset.workflowId === workflowId && option.dataset.useRows === "false"
    );
    if (!noRowsOption) return;
    explicitAssignmentModes.set(assignmentModeKey(workspace.getContext().resultId, workflowId), false);
    assignmentSelect.value = noRowsOption.value;
    assignmentSelect.disabled = false;
    byId("ppAssignIds").disabled = false;
    workspace.selectWorkflow(workflowId);
    workspace.setMessage("Row generation skipped. Assign standalone panel IDs using row ID 0000.");
    window.requestAnimationFrame(() => {
      setStepExpanded("ppHierarchyStep", false);
      setStepExpanded("ppAssignIdsStep", true);
      byId("ppAssignIdsStep")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
  }

  function init() {
    const panelIdsInfoModal = byId("ppPanelIdsInfoModal");
    const closePanelIdsInfo = () => {
      panelIdsInfoModal?.classList.remove("show");
      panelIdsInfoModal?.classList.add("hidden");
    };
    byId("ppPanelIdsInfo")?.addEventListener("click", () => {
      panelIdsInfoModal?.classList.remove("hidden");
      panelIdsInfoModal?.classList.add("show");
      byId("ppPanelIdsInfoClose")?.focus();
    });
    byId("ppPanelIdsInfoClose")?.addEventListener("click", closePanelIdsInfo);
    byId("ppPanelIdsInfoCloseFooter")?.addEventListener("click", closePanelIdsInfo);
    panelIdsInfoModal?.addEventListener("click", event => {
      if (event.target === panelIdsInfoModal) closePanelIdsInfo();
    });
    byId("ppBuildHierarchy")?.addEventListener("click", buildHierarchy);
    byId("ppSkipHierarchy")?.addEventListener("click", skipHierarchy);
    byId("ppAssignIds")?.addEventListener("click", assignIds);
    byId("ppHierarchySource")?.addEventListener("change", event => {
      byId("ppBuildHierarchy").disabled = !event.target.value;
      const workflowId = event.target.selectedOptions[0]?.dataset.workflowId;
      if (workflowId) api()?.selectWorkflow(workflowId);
    });
    byId("ppAssignmentSource")?.addEventListener("change", event => {
      byId("ppAssignIds").disabled = !event.target.value;
      const selectedOption = event.target.selectedOptions[0];
      const workflowId = selectedOption?.dataset.workflowId;
      if (workflowId && selectedOption.dataset.useRows) {
        explicitAssignmentModes.set(
          assignmentModeKey(api()?.getContext()?.resultId, workflowId),
          selectedOption.dataset.useRows === "true",
        );
      }
      if (workflowId) api()?.selectWorkflow(workflowId);
    });
    document.addEventListener("postprocess:data", event => refresh(event.detail));
    document.addEventListener("postprocess:workflow", event => refresh(event.detail.context));
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
