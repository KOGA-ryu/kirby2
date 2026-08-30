(function () {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const node = (tag, className, text) => {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined && text !== null) item.textContent = String(text);
    return item;
  };
  const appendPair = (root, label, value) => {
    root.append(node("dt", "", label));
    root.append(node("dd", "", value));
  };
  const status = (value) => {
    const item = node("span", "status", String(value).replaceAll("_", " "));
    item.dataset.status = value;
    return item;
  };
  const card = (title, availability) => {
    const root = node("article", "card");
    const header = node("div", "card-header");
    header.append(node("h3", "", title), status(availability));
    root.append(header);
    return root;
  };
  const addRows = (root, rows) => {
    const list = node("div", "rows");
    for (const row of rows) {
      const line = node("div", "row");
      line.append(
        node("div", "row-label", row.label),
        node("div", "row-value mono", row.display_text)
      );
      list.append(line);
    }
    root.append(list);
  };
  const addEvidence = (root, payload) => {
    const details = node("details");
    details.append(node("summary", "", "Canonical evidence"));
    details.append(node("pre", "", JSON.stringify(payload, null, 2)));
    root.append(details);
  };

  const dataNode = byId("kirby2-report-data");
  if (!dataNode) throw new Error("KIRBY2_REPORT_DATA_MISSING");
  const report = JSON.parse(dataNode.textContent);
  if (report.schema_id !== "KIRBY2_PORTABLE_REPLAY_REPORT_V1") {
    throw new Error("KIRBY2_REPORT_SCHEMA_UNSUPPORTED");
  }
  document.title = `Kirby2 Replay Microscope · ${report.report_id}`;
  const framePicker = byId("frame-picker");
  const identityGrid = byId("identity-grid");
  const eventStrip = byId("event-strip");
  const overlayGrid = byId("overlay-grid");
  const paneGrid = byId("pane-grid");

  const renderFrame = (frame, selectedIndex) => {
    const presentation = frame.presentation;
    const identity = frame.identity;
    byId("report-title").textContent = presentation.recording.display_name;
    byId("report-summary").textContent = presentation.report.summary;
    byId("cursor-label").textContent = presentation.clock.cursor_label;
    byId("footer-id").textContent = `${report.report_id} · ${frame.frame_id}`;

    const watermark = byId("truth-watermark");
    watermark.textContent = presentation.watermark.label;
    watermark.dataset.mode = identity.observation_mode;

    identityGrid.replaceChildren();
    appendPair(identityGrid, "Mode", identity.observation_mode);
    appendPair(identityGrid, "Policy", identity.policy_id);
    appendPair(identityGrid, "Instrument", presentation.instrument.display_name);
    appendPair(identityGrid, "Run", identity.source_run_id);
    appendPair(identityGrid, "Cursor", String(identity.render_cursor_time_us));
    appendPair(identityGrid, "Frame", frame.frame_id);

    for (const [index, button] of [...framePicker.children].entries()) {
      button.dataset.selected = String(index === selectedIndex);
      button.setAttribute("aria-pressed", String(index === selectedIndex));
    }

    eventStrip.replaceChildren();
    if (presentation.events.length === 0) {
      const empty = card("Current partition", "RECORDED_EMPTY");
      empty.append(node("p", "card-note", "No policy-visible timeline event occurs at this exact cursor."));
      eventStrip.append(empty);
    } else {
      for (const event of presentation.events) {
        const item = card(event.title, "AVAILABLE");
        item.append(node("p", "card-note", event.summary));
        addRows(item, [
          { label: "Event", display_text: event.event_id },
          { label: "Kind", display_text: event.event_kind },
          { label: "Evidence", display_text: event.evidence_role },
          { label: "Visible at", display_text: String(event.policy_visible_at_time_us) }
        ]);
        addEvidence(item, event.source_reference);
        eventStrip.append(item);
      }
    }

    overlayGrid.replaceChildren();
    for (const overlay of presentation.overlays) {
      const item = card(overlay.title, overlay.availability);
      if (overlay.availability === "AVAILABLE") {
        item.append(node("p", "metric-value", overlay.display_value));
        item.append(node("p", "metric-meta", `${overlay.window_label} · ${overlay.formatter_id}`));
      } else {
        item.append(node("p", "card-note", overlay.explanation));
      }
      addEvidence(item, overlay.source_references);
      overlayGrid.append(item);
    }

    paneGrid.replaceChildren();
    for (const pane of presentation.panes) {
      const item = card(pane.title, pane.availability);
      if (pane.rows.length) addRows(item, pane.rows);
      if (pane.explanation) item.append(node("p", "card-note", pane.explanation));
      addEvidence(item, pane.source_references);
      paneGrid.append(item);
    }
  };

  for (const [index, frame] of report.frames.entries()) {
    const button = node("button", "frame-button", frame.presentation.clock.cursor_label);
    button.type = "button";
    button.addEventListener("click", () => renderFrame(frame, index));
    framePicker.append(button);
  }
  renderFrame(report.frames[0], 0);

  const sectionGrid = byId("section-grid");
  for (const section of report.sections) {
    const item = card(section.title, section.availability);
    item.append(node("p", "card-note", section.summary));
    addEvidence(item, section.payload);
    sectionGrid.append(item);
  }
}());
