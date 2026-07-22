(function () {
  const studio = {
    activeIndex: 0,
    videoTime: 0,
    playing: false,
    player: null,
    timeline: null,
    timelineItems: null,
    cropper: null,
    boardEditIndex: -1,
    boardPaint: null,
    historyJobId: null,
    undoStack: [],
    redoStack: [],
    captureSequence: 0,
    latestCapture: new Map(),
    frameNudgeHold: null,
    suppressFrameNudgeClick: null,
    videoFrameHold: null,
    suppressVideoFrameClick: null,
    scrubberDragging: false,
    referenceCandidateJobId: null,
    referenceDraftArray: null,
  };

  const HISTORY_LIMIT = 100;

  function historySnapshot(label) {
    return {
      label,
      actions: structuredClone(actionDrafts),
      referenceInteractions: structuredClone(referenceInteractionDrafts),
      referenceCandidates: structuredClone(referenceCandidateDrafts),
      activeIndex: studio.activeIndex,
    };
  }

  function recordAnnotationHistory(label) {
    studio.undoStack.push(historySnapshot(label));
    if (studio.undoStack.length > HISTORY_LIMIT) studio.undoStack.shift();
    studio.redoStack = [];
  }

  function restoreHistorySnapshot(snapshot, verb) {
    actionDrafts = structuredClone(snapshot.actions);
    referenceInteractionDrafts = structuredClone(snapshot.referenceInteractions || []);
    referenceCandidateDrafts = structuredClone(snapshot.referenceCandidates || []);
    studio.referenceDraftArray = referenceInteractionDrafts;
    studio.activeIndex = actionDrafts[snapshot.activeIndex] && !actionDrafts[snapshot.activeIndex].deleted
      ? snapshot.activeIndex
      : Math.max(0, actionDrafts.findIndex(action => !action.deleted));
    studio.boardEditIndex = -1;
    lastDeletedActionIndex = -1;
    actionsConfirmed = false;
    $("fullReplayBtn").disabled = true;
    recomputeActionBoards();
    refreshWorkbench();
    $("videoStatus").textContent = `${verb}：${snapshot.label}`;
  }

  window.undoAnnotationChange = () => {
    const target = studio.undoStack.pop();
    if (!target) return;
    studio.redoStack.push(historySnapshot(target.label));
    restoreHistorySnapshot(target, "已撤销");
  };

  window.redoAnnotationChange = () => {
    const target = studio.redoStack.pop();
    if (!target) return;
    studio.undoStack.push(historySnapshot(target.label));
    restoreHistorySnapshot(target, "已重做");
  };

  window.resetAnnotationHistory = () => {
    studio.historyJobId = rebuildJob?.jobId || null;
    studio.undoStack = [];
    studio.redoStack = [];
  };

  const rangeLabels = {
    before: "放置前",
    drag: "拖拽",
    placed: "放置后",
    clear: "消除",
  };

  function numberOrNull(value) {
    if (value === "" || value === null || value === undefined) return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? Math.round(Math.max(0, parsed) * 1000) / 1000 : null;
  }

  function timeText(value) {
    const seconds = numberOrNull(value);
    if (seconds === null) return "--:--.---";
    const minutes = Math.floor(seconds / 60);
    return `${String(minutes).padStart(2, "0")}:${(seconds % 60).toFixed(3).padStart(6, "0")}`;
  }

  function sourceFps() {
    return Math.max(1, Number(rebuildJob?.analysis?.fps || 30));
  }

  function timeToFrame(value) {
    const seconds = numberOrNull(value);
    return seconds === null ? "" : Math.max(0, Math.round(seconds * sourceFps()));
  }

  function timeFrameText(value) {
    const seconds = numberOrNull(value);
    return seconds === null ? "--:--.--- / F--" : `${timeText(seconds)} / F${timeToFrame(seconds)}`;
  }

  function localAssetUrl(path) {
    const normalized = String(path || "").replace(/\\/g, "/").replace(/^\/+/, "");
    const encodedPath = normalized.split("/").map(segment => encodeURIComponent(segment)).join("/");
    return `http://127.0.0.1:8765/${encodedPath}`;
  }

  function ensureRanges(action) {
    const evidence = action.evidenceTimes || {};
    const eventTime = numberOrNull(evidence.placed) ?? 0;
    const defaults = {
      before: { start: Math.max(0, (numberOrNull(evidence.before) ?? eventTime) - 0.12), end: numberOrNull(evidence.before) ?? eventTime },
      drag: { start: numberOrNull(evidence.action) ?? eventTime, end: eventTime },
      placed: { start: eventTime, end: eventTime + 0.12 },
      clear: { start: numberOrNull(evidence.cleared), end: numberOrNull(evidence.cleared) === null ? null : numberOrNull(evidence.cleared) + 0.4 },
    };
    action.timeRanges ||= {};
    for (const key of Object.keys(defaults)) {
      action.timeRanges[key] ||= defaults[key];
      action.timeRanges[key].start = numberOrNull(action.timeRanges[key].start);
      action.timeRanges[key].end = numberOrNull(action.timeRanges[key].end);
    }
    action.annotationNotes ||= "";
    if (action.manuallyVerified === undefined) {
      action.manuallyVerified = action.confidence === "verified" && !action.requiresConfirmation;
    }
    return action.timeRanges;
  }

  function visibleActions() {
    return actionDrafts.map((action, index) => ({ action, index })).filter(({ action }) => !action.deleted);
  }

  function rangeErrors(action) {
    const duration = Number(rebuildJob?.analysis?.duration || 0);
    const ranges = ensureRanges(action);
    const errors = [];
    for (const [key, range] of Object.entries(ranges)) {
      if (key === "clear" && range.start === null && range.end === null) {
        if (action.clearState === "on") errors.push("本步设置为启用消除，但未识别到实际消除特效区间");
        continue;
      }
      if (range.start === null || range.end === null) errors.push(`${rangeLabels[key]}区间不完整`);
      else if (range.start > range.end) errors.push(`${rangeLabels[key]}开始时间晚于结束时间`);
      else if (duration && range.end > duration + 0.01) errors.push(`${rangeLabels[key]}超出视频时长`);
    }
    if (ranges.before.end !== null && ranges.drag.end !== null && ranges.before.end > ranges.drag.end) errors.push("放置前结束时间晚于拖拽结束");
    if (ranges.drag.end !== null && ranges.placed.end !== null && ranges.drag.end > ranges.placed.end) errors.push("拖拽结束时间晚于放置后结束");
    return errors;
  }

  function actionState(action) {
    const placement = actionPlacementState(action);
    const conflict = placement.overlaps.length + placement.outOfBounds.length;
    const timeInvalid = rangeErrors(action).length > 0;
    if (conflict || !action.shape.length || timeInvalid) return { kind: "error", icon: "triangle-alert", text: "存在冲突" };
    if (+action.sourceSlot < 0 || action.clearState === "unknown" || !action.manuallyVerified) return { kind: "review", icon: "circle-help", text: "需要核对" };
    return { kind: "ok", icon: "check", text: "已核对" };
  }

  function stepValidationIssues(action) {
    const placement = actionPlacementState(action);
    const issues = [];
    if (!action.shape.length) issues.push("请确认方块形状");
    if (Number(action.sourceSlot) < 0) issues.push("请选择来源槽位（左侧 / 中间 / 右侧）");
    if (action.clearState === "unknown") issues.push("请选择消除检测状态（启用或禁用）");
    if (placement.overlaps.length) issues.push(`当前落点存在 ${placement.overlaps.length} 个重叠格`);
    if (placement.outOfBounds.length) issues.push(`当前落点有 ${placement.outOfBounds.length} 个格子越界`);
    issues.push(...rangeErrors(action));
    return issues;
  }

  function boardAddedCells(beforeBoard, afterBoard) {
    const added = new Set();
    if (!Array.isArray(beforeBoard) || !Array.isArray(afterBoard)) return added;
    for (let row = 0; row < afterBoard.length; row++) {
      for (let col = 0; col < (afterBoard[row]?.length || 0); col++) {
        const before = beforeBoard[row]?.[col];
        const after = afterBoard[row]?.[col];
        if ((before === null || before === undefined) && after !== null && after !== undefined) {
          added.add(`${row}:${col}`);
        }
      }
    }
    return added;
  }

  function bestBoardTarget(action) {
    const rows = Number(rebuildJob?.analysis?.board?.rows || 0);
    const cols = Number(rebuildJob?.analysis?.board?.cols || 0);
    const shape = action.shape || [];
    if (!rows || !cols || !shape.length) return action.target || { row: 0, col: 0 };
    const maxRow = Math.max(...shape.map(cell => Number(cell.row || 0)));
    const maxCol = Math.max(...shape.map(cell => Number(cell.col || 0)));
    const before = action.liveBeforeBoard || action.manualBeforeBoard || action.beforeBoard || [];
    const added = boardAddedCells(before, action.recognizedAfterBoard);
    const current = action.target || { row: 0, col: 0 };
    let best = null;
    for (let targetRow = 0; targetRow <= rows - maxRow - 1; targetRow++) {
      for (let targetCol = 0; targetCol <= cols - maxCol - 1; targetCol++) {
        const target = { row: targetRow, col: targetCol };
        const state = actionPlacementState({ ...action, target });
        const placed = new Set(shape.map(cell => `${targetRow + Number(cell.row || 0)}:${targetCol + Number(cell.col || 0)}`));
        let covered = 0;
        for (const key of added) if (placed.has(key)) covered++;
        const missingAdded = Math.max(0, added.size - covered);
        const extraPlaced = Math.max(0, placed.size - covered);
        const distance = Math.abs(targetRow - Number(current.row || 0)) + Math.abs(targetCol - Number(current.col || 0));
        const score = state.outOfBounds.length * 100000 + state.overlaps.length * 1000 + missingAdded * 12 + extraPlaced * 3 + distance;
        if (!best || score < best.score) best = { target, score, covered };
      }
    }
    return best?.target || current;
  }

  function normalizeActionTargets() {
    for (const action of actionDrafts) {
      if (!action || action.deleted || action.manuallyVerified || !action.shape?.length) continue;
      const before = action.liveBeforeBoard || action.manualBeforeBoard || action.beforeBoard || [];
      const currentState = actionPlacementState(action);
      const added = boardAddedCells(before, action.recognizedAfterBoard);
      if (!currentState.outOfBounds.length && !currentState.overlaps.length && !added.size) continue;
      const target = bestBoardTarget(action);
      if (Number(target.row) !== Number(action.target?.row) || Number(target.col) !== Number(action.target?.col)) {
        action.target = target;
        action.requiresConfirmation = true;
        action.manuallyVerified = false;
        action.candidateReasons ||= [];
        if (!action.candidateReasons.includes("target_auto_realigned_to_board")) {
          action.candidateReasons.push("target_auto_realigned_to_board");
        }
      }
    }
  }

  function updateStepValidationFeedback() {
    const workbench = document.querySelector(".annotation-workbench");
    const action = actionDrafts[studio.activeIndex];
    if (!workbench || !action) return;
    workbench.querySelector(".step-validation-feedback")?.remove();
    const issues = stepValidationIssues(action);
    if (!issues.length) return;
    const feedback = document.createElement("div");
    feedback.className = "step-validation-feedback";
    feedback.setAttribute("role", "alert");
    feedback.innerHTML = `<i data-lucide="circle-alert"></i><span>确认前还需：${esc(issues.join("；"))}</span>`;
    workbench.querySelector(".workbench-header")?.insertAdjacentElement("afterend", feedback);
  }

  function sourceVideoUrl() {
    const path = rebuildJob?.analysis?.sourceVideoUrl;
    return path ? localAssetUrl(path) : "";
  }

  function timelineBounds(action) {
    const ranges = ensureRanges(action);
    const start = ranges.before.start ?? ranges.drag.start ?? ranges.placed.start ?? 0;
    const end = ranges.clear.end ?? ranges.placed.end ?? ranges.drag.end ?? start + 0.1;
    return { start, end: Math.max(start + 0.04, end) };
  }

  function timelineHtml(entries) {
    return `<div class="timeline-track" id="annotationTimeline" role="application" aria-label="动作时间轴，可拖拽当前步骤的四段时间范围"></div>`;
  }

  const timelineEpoch = new Date("2020-01-01T00:00:00.000Z").getTime();
  const secondsToDate = seconds => new Date(timelineEpoch + Math.max(0, Number(seconds) || 0) * 1000);
  const dateToSeconds = date => numberOrNull((new Date(date).getTime() - timelineEpoch) / 1000) ?? 0;

  function timelineData(entries) {
    const active = actionDrafts[studio.activeIndex];
    const items = [];
    for (const { action, index } of entries) {
      const bounds = timelineBounds(action);
      const status = actionState(action);
      items.push({
        id: `action:${index}`,
        group: "actions",
        content: `步骤 ${action.stepIndex}`,
        start: secondsToDate(bounds.start),
        end: secondsToDate(bounds.end),
        className: `action-range ${status.kind === "ok" ? "" : "review"}`,
        editable: false,
      });
    }
    if (active) {
      const ranges = ensureRanges(active);
      for (const key of Object.keys(rangeLabels)) {
        const range = ranges[key];
        if (range.start === null || range.end === null) continue;
        items.push({
          id: `phase:${key}`,
          group: key,
          content: rangeLabels[key],
          start: secondsToDate(range.start),
          end: secondsToDate(Math.max(range.start + 0.001, range.end)),
          className: `phase-${key}`,
          editable: { updateTime: true, updateGroup: false, remove: false },
        });
      }
    }
    return items;
  }

  function initializeTimeline() {
    const container = $("annotationTimeline");
    if (!container || !window.vis?.Timeline || !window.vis?.DataSet) return;
    studio.timeline?.destroy();
    const entries = visibleActions();
    studio.timelineItems = new vis.DataSet(timelineData(entries));
    const groups = new vis.DataSet([
      { id: "actions", content: "全部动作", order: 0 },
      { id: "before", content: "放置前", order: 1 },
      { id: "drag", content: "拖拽", order: 2 },
      { id: "placed", content: "放置后", order: 3 },
      { id: "clear", content: "消除", order: 4 },
    ]);
    const duration = Math.max(0.1, Number(rebuildJob.analysis.duration || 0.1));
    studio.timeline = new vis.Timeline(container, studio.timelineItems, groups, {
      start: secondsToDate(0),
      end: secondsToDate(duration),
      min: secondsToDate(0),
      max: secondsToDate(duration),
      zoomMin: Math.min(600, duration * 1000),
      zoomMax: Math.max(1000, duration * 1000 * 1.2),
      stack: false,
      showCurrentTime: false,
      showMajorLabels: false,
      horizontalScroll: true,
      zoomKey: "ctrlKey",
      orientation: { axis: "bottom", item: "top" },
      margin: { item: { horizontal: 2, vertical: 4 }, axis: 4 },
      editable: { add: false, remove: false, updateGroup: false, updateTime: true },
      selectable: true,
      multiselect: false,
      snap: date => {
        const frameMs = 1000 / Math.max(1, Number(rebuildJob.analysis.fps || 30));
        const offset = date.getTime() - timelineEpoch;
        return new Date(timelineEpoch + Math.round(offset / frameMs) * frameMs);
      },
      onMove: (item, callback) => {
        if (!String(item.id).startsWith("phase:")) return callback(null);
        const key = String(item.id).split(":")[1];
        const action = actionDrafts[studio.activeIndex];
        const previous = { ...ensureRanges(action)[key] };
        action.timeRanges[key] = { start: dateToSeconds(item.start), end: dateToSeconds(item.end) };
        const errors = rangeErrors(action);
        if (errors.length) {
          action.timeRanges[key] = previous;
          $("videoStatus").textContent = `未保存${rangeLabels[key]}区间：${errors[0]}`;
          callback(null);
          return;
        }
        actionsConfirmed = false;
        action.manuallyVerified = false;
        $("fullReplayBtn").disabled = true;
        callback(item);
        refreshWorkbench({ refreshTimeline: false });
      },
    });
    studio.timeline.on("click", properties => {
      if (properties.item && String(properties.item).startsWith("action:")) {
        const index = Number(String(properties.item).split(":")[1]);
        window.selectAnnotationStep(index, true);
        return;
      }
      if (properties.time) window.annotationSeek(dateToSeconds(properties.time));
    });
  }

  function calibrationHtml() {
    const board = rebuildJob.analysis.board;
    if (!board?.calibrationFrame) return "";
    return `<section class="task-admin-section"><details class="source-calibration-details"><summary><span>识别修正</span><small>棋盘 ${board.rows} x ${board.cols} · ${esc(board.source || "自动识别")}</small></summary><div class="source-calibration"><div class="source-calibration-controls"><label>X<input id="sourceBoardX" type="number" value="${board.x}"></label><label>Y<input id="sourceBoardY" type="number" value="${board.y}"></label><label>宽<input id="sourceBoardW" type="number" value="${board.width}"></label><label>高<input id="sourceBoardH" type="number" value="${board.height}"></label><label>行<input id="sourceBoardRows" type="number" min="3" max="30" value="${board.rows}"></label><label>列<input id="sourceBoardCols" type="number" min="3" max="30" value="${board.cols}"></label><button type="button" id="recalibrateSourceBtn">按此棋盘重新识别</button></div><div class="source-frame-wrap"><img id="sourceCalibrationImage" src="${localAssetUrl(board.calibrationFrame)}" alt="拖拽四边校准棋盘范围"></div></div></details><details class="version-details"><summary><span>任务历史</span><small>恢复识别稿或人工确认版本</small></summary><div class="version-bar"><select id="actionVersionSelect" aria-label="动作脚本版本"><option value="">正在读取动作版本...</option></select><button type="button" id="restoreActionVersionBtn">恢复版本</button></div></details></section>`;
  }

  function railHtml(entries) {
    return `<aside class="step-rail"><div class="step-rail-head"><strong>动作步骤</strong><span>${entries.length} 步 · 点击切换当前标注</span></div><div class="step-list">${entries.map(({ action, index }) => {
      const state = actionState(action);
      const slot = ["左槽", "中槽", "右槽"][+action.sourceSlot] || "槽位待定";
      return `<button type="button" class="step-item ${index === studio.activeIndex ? "active" : ""}" onclick="selectAnnotationStep(${index},true)" aria-current="${index === studio.activeIndex ? "step" : "false"}"><span class="step-number">${String(action.stepIndex).padStart(2, "0")}</span><span class="step-copy"><strong>${slot} · ${action.shape.length} 格</strong><span>落点 ${action.target.row},${action.target.col} · ${state.text}</span></span><span class="step-status ${state.kind}" title="${state.text}"><i data-lucide="${state.icon}"></i></span></button>`;
    }).join("")}</div></aside>`;
  }

  function evidenceBandHtml(action) {
    const labels = { before: "放置前", action: "拖拽中", placed: "放置后", cleared: "消除后" };
    const frames = action.evidenceFrames || {};
    const times = action.evidenceTimes || {};
    return `<div class="evidence-band">${Object.entries(labels).map(([key, label]) => {
      const path = frames[key];
      const time = numberOrNull(times[key]);
      if (!path) return `<div class="evidence-frame missing"><span>${label}未捕获</span></div>`;
      return `<button type="button" class="evidence-frame" onclick="annotationSeek(${time ?? 0})" aria-label="跳到步骤 ${action.stepIndex} ${label} ${timeFrameText(time)}"><img loading="lazy" decoding="async" src="${localAssetUrl(path)}" alt="步骤 ${action.stepIndex} ${label}"><span class="evidence-caption"><span>${label}</span><span class="evidence-time">${timeFrameText(time)}</span></span></button>`;
    }).join("")}</div>`;
  }

  function rangeTimeInputHtml(actionIndex, key, edge, label, value) {
    const secondsValue = value === null ? "" : value.toFixed(3);
    const edgeLabel = edge === "start" ? "开始" : "结束";
    return `<label class="range-seconds"><span>秒</span><input type="number" step="0.001" min="0" value="${secondsValue}" aria-label="${label}${edgeLabel}秒" title="${timeFrameText(value)}" onchange="setAnnotationRange(${actionIndex},'${key}','${edge}',this.value)"></label><div class="frame-stepper"><button type="button" onclick="nudgeAnnotationRangeFrame(${actionIndex},'${key}','${edge}',-1)" aria-label="${label}${edgeLabel}向前一帧" title="向前一帧，长按连续调整"><i data-lucide="minus"></i></button><input class="range-frame" type="number" step="1" min="0" value="${timeToFrame(value)}" aria-label="${label}${edgeLabel}帧" title="${timeFrameText(value)}" onchange="setAnnotationRangeFrame(${actionIndex},'${key}','${edge}',this.value)"><button type="button" onclick="nudgeAnnotationRangeFrame(${actionIndex},'${key}','${edge}',1)" aria-label="${label}${edgeLabel}向后一帧" title="向后一帧，长按连续调整"><i data-lucide="plus"></i></button></div><button type="button" class="range-seek" onclick="seekAnnotationRangeEdge(${actionIndex},'${key}','${edge}')" aria-label="跳到${label}${edgeLabel}视频帧" title="跳到${label}${edgeLabel}视频帧"><i data-lucide="locate-fixed"></i></button>`;
  }

  function rangeEditorHtml(action, index) {
    const ranges = ensureRanges(action);
    return `<div class="range-list"><div class="range-columns" aria-hidden="true"><span></span><span>开始 · 秒 / 帧</span><span>结束 · 秒 / 帧</span><span></span></div>${Object.entries(rangeLabels).map(([key, label]) => {
      const range = ranges[key];
      return `<div class="range-row"><span>${label}</span><div class="range-value">${rangeTimeInputHtml(index, key, "start", label, range.start)}</div><div class="range-value">${rangeTimeInputHtml(index, key, "end", label, range.end)}</div></div>`;
    }).join("")}<div class="range-feedback" id="rangeJumpFeedback" role="status" aria-live="polite"></div></div>`;
  }

  function segmentHtml(className, values, selected, handler, index) {
    return `<div class="segmented ${className}">${values.map(([value, label]) => `<button type="button" data-value="${value}" class="btn btn-outline-secondary ${String(value) === String(selected) ? "active" : ""}" onclick="${handler}(${index},'${value}')">${label}</button>`).join("")}</div>`;
  }

  function annotationBoardHtml(action, index, state) {
    const editing = studio.boardEditIndex === index;
    const overlap = new Set(state.overlaps);
    const complete = new Set();
    for (const row of state.completeRows) for (let col = 0; col < state.cols; col++) complete.add(`${row}:${col}`);
    for (const col of state.completeCols) for (let row = 0; row < state.rows; row++) complete.add(`${row}:${col}`);
    let cells = "";
    for (let row = 0; row < state.rows; row++) for (let col = 0; col < state.cols; col++) {
      const key = `${row}:${col}`;
      const classes = ["placement-cell"];
      if (state.occupied.has(key)) classes.push("occupied");
      if (!editing && state.preview.has(key)) classes.push("preview");
      if (!editing && overlap.has(key)) classes.push("overlap");
      if (!editing && complete.has(key)) classes.push("complete");
      if (editing) classes.push("board-cell-editable");
      const handler = editing
        ? `data-board-edit-index="${index}" data-board-row="${row}" data-board-col="${col}" onpointerdown="beginAnnotationBoardPaint(event,${index},${row},${col})" onpointerenter="continueAnnotationBoardPaint(event,${index},${row},${col})"`
        : `onclick="setAnnotationTarget(${index},${row},${col})"`;
      const label = editing ? `将步骤 ${action.stepIndex} 放置前棋盘第 ${row} 行第 ${col} 列切换为空格或方块` : `将步骤 ${action.stepIndex} 方块吸附到第 ${row} 行第 ${col} 列`;
      cells += `<button type="button" class="${classes.join(" ")}" ${handler} aria-label="${label}"></button>`;
    }
    const conflicts = state.overlaps.length + state.outOfBounds.length;
    const result = conflicts ? `${conflicts} 个重叠或越界，必须调整形状或落点` : `位置合法${state.completeRows.length || state.completeCols.length ? `，形成行 ${state.completeRows.join(",") || "无"} / 列 ${state.completeCols.join(",") || "无"}` : ""}`;
    const corrected = Array.isArray(action.manualBeforeBoard);
    const controls = `<div class="board-heading-actions"><button type="button" class="command-button board-edit-toggle ${editing ? "active" : ""}" onclick="toggleAnnotationBoardEdit(${index})"><i data-lucide="${editing ? "check" : "pencil-line"}"></i><span>${editing ? "完成棋盘校正" : "校正原棋盘"}</span></button>${corrected ? `<button type="button" class="icon-button" onclick="resetAnnotationBeforeBoard(${index})" aria-label="恢复步骤 ${action.stepIndex} 的识别棋盘" title="恢复识别棋盘"><i data-lucide="rotate-ccw"></i></button>` : ""}</div>`;
    const hint = editing ? "按住鼠标拖过格子可连续填充或擦除；一次拖动可整体撤销。完成后会从本步开始级联重算。" : "点击希望方块覆盖的格子，将自动吸附到最近的合法位置";
    const status = editing ? `<div class="board-result placement-editing">正在校正放置前棋盘${corrected ? " · 已有人工修改" : ""}</div>` : `<div class="board-result ${conflicts ? "placement-error" : "placement-ok"}">${result}</div>`;
    return `<div class="board-heading"><div><h4>${editing ? "校正放置前棋盘" : "落点结果"}</h4><span class="board-key">${editing ? "亮灰 有方块 · 深色 空格" : "灰 原棋盘 · 绿 本次放置 · 红 冲突 · 黄 完整行列"}</span></div>${controls}</div><div class="annotation-board-wrap ${editing ? "is-board-editing" : ""}"><div class="placement-board" style="--board-cols:${state.cols}">${cells}</div></div><div class="board-placement-hint">${hint}</div>${status}`;
  }

  function candidateHtml(action, index) {
    const solutions = action.candidateSolutions || [];
    if (!solutions.length) return "";
    return `<label>合法候选<select onchange="if(this.value!=='')applyAnnotationCandidate(${index},+this.value)"><option value="">${solutions.length} 个候选，选择后应用</option>${solutions.map((solution, candidateIndex) => `<option value="${candidateIndex}">${candidateIndex + 1}. ${["左", "中", "右"][solution.sourceSlot]}槽 → (${solution.target.row},${solution.target.col}) · ${solution.shape.length}格</option>`).join("")}</select></label>`;
  }

  function workbenchHtml(action, index) {
    const state = actionPlacementState(action);
    const status = actionState(action);
    const timeIssues = rangeErrors(action);
    return `<section class="annotation-workbench"><div class="workbench-header"><div class="workbench-title"><h3>步骤 ${action.stepIndex}</h3><span>ROUND ${Math.floor((action.stepIndex - 1) / 3) + 1}</span><span class="tag ${status.kind === "review" ? "gold" : status.kind === "error" ? "" : "gray"}">${status.text}</span></div><div class="workbench-actions"><button type="button" class="command-button" onclick="addReferenceInteraction(${index})" title="按当前视频帧添加一次拿起后又放回的动作"><i data-lucide="mouse-pointer-2"></i><span>添加撤回</span></button><button type="button" class="command-button" onclick="addAnnotationStepBefore(${index})" title="在当前步骤前插入一个正常落块步骤"><i data-lucide="list-plus"></i><span>在此前添加</span></button><button type="button" class="command-button" onclick="addAnnotationStepAfter(${index})" title="在当前步骤后插入一个正常落块步骤"><i data-lucide="list-plus"></i><span>在此后添加</span></button><button type="button" class="command-button ${status.kind === "ok" ? "" : "primary"}" onclick="verifyAnnotationStep(${index})" ${status.kind === "error" ? "disabled" : ""}><i data-lucide="check-check"></i><span>${status.kind === "ok" ? "已确认本步" : "确认本步"}</span></button><button type="button" class="icon-button danger" onclick="deleteAnnotationStep(${index})" aria-label="删除步骤 ${action.stepIndex}" title="删除步骤"><i data-lucide="trash-2"></i></button></div></div><div class="workbench-body">${evidenceBandHtml(action)}<div class="workbench-grid"><div class="control-column"><section class="control-section"><div class="range-heading"><h4>时间区间</h4>${timeIssues.length ? `<span class="placement-error">${esc(timeIssues[0])}</span>` : ""}</div>${rangeEditorHtml(action, index)}</section><section class="control-section"><h4>来源槽位</h4>${segmentHtml("", [[0, "左侧"], [1, "中间"], [2, "右侧"], [-1, "待确认"]], action.sourceSlot, "setAnnotationSlot", index)}</section><section class="control-section shape-target-row"><div class="shape-editor"><h4>方块形状</h4>${shapeGrid(action, index)}</div><div><h4>棋盘落点</h4><div class="target-inputs"><label>行<input type="number" min="0" max="${state.rows - 1}" value="${action.target.row}" onchange="setAnnotationTargetAxis(${index},'row',this.value)"></label><label>列<input type="number" min="0" max="${state.cols - 1}" value="${action.target.col}" onchange="setAnnotationTargetAxis(${index},'col',this.value)"></label></div></div></section><section class="control-section"><h4>消除检测</h4>${segmentHtml("clear-mode", [["on", "启用"], ["off", "禁用"], ["unknown", "待确认"]], action.clearState, "setAnnotationClear", index)}</section>${candidateHtml(action, index)}<label class="annotation-notes">标注备注<textarea placeholder="记录遮挡、多解或特殊判断依据" onchange="setAnnotationNotes(${index},this.value)">${esc(action.annotationNotes || "")}</textarea></label></div><div class="board-column">${annotationBoardHtml(action, index, state)}</div></div></div></section>`;
  }

  function summaryState(entries) {
    let valid = 0;
    let errors = 0;
    for (const { action } of entries) {
      const state = actionState(action);
      if (state.kind === "ok") valid++;
      if (state.kind === "error") errors++;
    }
    return { valid, errors, total: entries.length };
  }

  function visibleReferenceInteractions() {
    return referenceInteractionDrafts
      .map((interaction, index) => ({ interaction, index }))
      .filter(({ interaction }) => !interaction.deleted)
      .sort((left, right) => Number(left.interaction.startTime || 0) - Number(right.interaction.startTime || 0));
  }

  function separateAutomaticReferenceCandidates() {
    const jobId = rebuildJob?.jobId || null;
    if (studio.referenceCandidateJobId !== jobId) {
      studio.referenceCandidateJobId = jobId;
      referenceCandidateDrafts = [];
      studio.referenceDraftArray = null;
    }
    if (studio.referenceDraftArray === referenceInteractionDrafts) return;
    const automatic = referenceInteractionDrafts.filter(item => !item.deleted && !item.manuallyVerified && !item.manualAdded);
    referenceCandidateDrafts = structuredClone(automatic);
    if (automatic.length) {
      referenceInteractionDrafts = referenceInteractionDrafts.filter(item => item.deleted || item.manuallyVerified || item.manualAdded);
      const status = $("videoStatus");
      if (status) status.textContent = `算法发现 ${automatic.length} 个疑似撤回片段；它们可能只是普通拖拽，未计入撤回动作`;
    }
    studio.referenceDraftArray = referenceInteractionDrafts;
  }

  function visibleReferenceCandidates() {
    return referenceCandidateDrafts
      .map((interaction, index) => ({ interaction, index }))
      .filter(({ interaction }) => !interaction.deleted);
  }

  function referenceStepPosition(interaction) {
    let afterStep = Number(interaction.afterStepIndex);
    let beforeStep = interaction.beforeStepIndex === null || interaction.beforeStepIndex === undefined
      ? null
      : Number(interaction.beforeStepIndex);
    if (!Number.isFinite(afterStep)) {
      const startTime = numberOrNull(interaction.startTime) ?? 0;
      const steps = visibleActions().map(({ action }) => ({
        stepIndex: Number(action.stepIndex),
        executeAt: numberOrNull(action.timeRanges?.placed?.start)
          ?? numberOrNull(action.evidenceTimes?.placed)
          ?? timelineBounds(action).start,
      }));
      const prior = steps.filter(step => step.executeAt <= startTime).map(step => step.stepIndex);
      const later = steps.filter(step => step.executeAt > startTime).map(step => step.stepIndex);
      afterStep = prior.length ? Math.max(...prior) : 0;
      beforeStep = later.length ? Math.min(...later) : null;
    }
    if (afterStep > 0 && beforeStep !== null) return `步骤 ${afterStep} 后 · 步骤 ${beforeStep} 前`;
    if (beforeStep !== null) return `步骤 ${beforeStep} 前`;
    if (afterStep > 0) return `步骤 ${afterStep} 后`;
    return "尚未定位到具体步骤";
  }

  function referenceIssues(interaction) {
    const issues = [];
    const rows = Number(rebuildJob?.analysis?.board?.rows || 0);
    const cols = Number(rebuildJob?.analysis?.board?.cols || 0);
    const shape = interaction.shape || [];
    const target = interaction.hoverTarget || {};
    if (![0, 1, 2].includes(Number(interaction.sourceSlot))) issues.push("请选择来源槽位");
    if (!shape.length) issues.push("请确认方块形状");
    if (!Number.isInteger(Number(target.row)) || !Number.isInteger(Number(target.col))) issues.push("请填写悬停位置");
    if (shape.some(cell => Number(target.row) + Number(cell.row) < 0 || Number(target.col) + Number(cell.col) < 0 || Number(target.row) + Number(cell.row) >= rows || Number(target.col) + Number(cell.col) >= cols)) issues.push("悬停位置超出棋盘");
    const start = numberOrNull(interaction.startTime);
    const end = numberOrNull(interaction.endTime);
    if (start === null || end === null || end <= start) issues.push("起止时间无效");
    const lastHoverEnd = Math.max(start ?? 0, ...(interaction.hoverPasses || []).map(pass => numberOrNull(pass.endTime) ?? 0));
    if (end !== null && end < lastHoverEnd) issues.push("完全归位时间不能早于最后一次悬停结束");
    for (const [passIndex, pass] of (interaction.hoverPasses || []).entries()) {
      const passStart = numberOrNull(pass.startTime);
      const passEnd = numberOrNull(pass.endTime);
      const passTarget = pass.target || {};
      if (passStart === null || passEnd === null || passEnd <= passStart) issues.push(`第 ${passIndex + 1} 次悬停时间无效`);
      if (shape.some(cell => Number(passTarget.row) + Number(cell.row) < 0 || Number(passTarget.col) + Number(cell.col) < 0 || Number(passTarget.row) + Number(cell.row) >= rows || Number(passTarget.col) + Number(cell.col) >= cols)) issues.push(`第 ${passIndex + 1} 次悬停位置超出棋盘`);
    }
    return issues;
  }

  function referenceShapeGrid(interaction, index) {
    const shape = interaction.shape || [];
    const maxRow = Math.max(4, ...shape.map(cell => Number(cell.row || 0)));
    const maxCol = Math.max(4, ...shape.map(cell => Number(cell.col || 0)));
    const active = new Set(shape.map(cell => `${cell.row}:${cell.col}`));
    let cells = "";
    for (let row = 0; row <= maxRow; row++) for (let col = 0; col <= maxCol; col++) {
      cells += `<button type="button" class="shape-cell ${active.has(`${row}:${col}`) ? "on" : ""}" onclick="toggleReferenceShapeCell(${index},${row},${col})" aria-label="撤回交互 ${index + 1} 形状 ${row},${col}"></button>`;
    }
    return `<div class="shape-grid" style="grid-template-columns:repeat(${maxCol + 1},22px)">${cells}</div>`;
  }

  function referencePassBoardHtml(interaction, interactionIndex, pass, passIndex) {
    const rows = Number(rebuildJob?.analysis?.board?.rows || 0);
    const cols = Number(rebuildJob?.analysis?.board?.cols || 0);
    if (!rows || !cols) return "";
    const beforeBoard = interaction.boardBefore || Array.from({ length: rows }, () => Array(cols).fill(null));
    const pseudoAction = {
      beforeBoard,
      shape: interaction.shape || [],
      target: pass.target || { row: 0, col: 0 },
    };
    const state = actionPlacementState(pseudoAction);
    const overlap = new Set(state.overlaps);
    const complete = new Set();
    for (const row of state.completeRows) for (let col = 0; col < cols; col++) complete.add(`${row}:${col}`);
    for (const col of state.completeCols) for (let row = 0; row < rows; row++) complete.add(`${row}:${col}`);
    const placement = new Set((interaction.shape || []).map(cell => `${Number(pass.target?.row || 0) + Number(cell.row)}:${Number(pass.target?.col || 0) + Number(cell.col)}`));
    let cells = "";
    for (let row = 0; row < rows; row++) for (let col = 0; col < cols; col++) {
      const key = `${row}:${col}`;
      const classes = ["placement-cell"];
      if (beforeBoard[row]?.[col] !== null && beforeBoard[row]?.[col] !== undefined) classes.push("occupied");
      if (placement.has(key)) classes.push("preview");
      if (overlap.has(key)) classes.push("overlap");
      if (complete.has(key)) classes.push("complete");
      cells += `<button type="button" class="${classes.join(" ")}" onclick="setReferencePassTargetFromBoard(${interactionIndex},${passIndex},${row},${col})" aria-label="将撤回 ${interactionIndex + 1} 第 ${passIndex + 1} 次悬停落点设到第 ${row} 行第 ${col} 列"></button>`;
    }
    const conflicts = state.overlaps.length + state.outOfBounds.length;
    return `<div class="reference-pass-board"><div class="placement-board" style="grid-template-columns:repeat(${cols},18px)">${cells}</div><span class="${conflicts ? "placement-error" : "placement-ok"}">${conflicts ? `${conflicts} 个冲突` : `落点 (${pass.target?.row ?? 0}, ${pass.target?.col ?? 0})`}</span></div>`;
  }

  function referencePassesHtml(interaction, interactionIndex) {
    const passes = interaction.hoverPasses || [];
    if (!passes.length) return "";
    return `<div class="reference-passes"><div class="reference-passes-head"><div><h4>悬停轨迹</h4><span>${passes.length} 次</span></div><button type="button" class="command-button compact" onclick="addReferencePass(${interactionIndex})" title="以当前视频帧新增一次悬停"><i data-lucide="plus"></i><span>添加悬停</span></button></div>${passes.map((pass, passIndex) => {
      const start = numberOrNull(pass.startTime) ?? 0;
      const end = numberOrNull(pass.endTime) ?? start;
      const target = pass.target || { row: 0, col: 0 };
      const previewRows = (pass.previewClearedRows || []).join(",") || "无";
      const previewCols = (pass.previewClearedCols || []).join(",") || "无";
      return `<div class="reference-pass"><div class="reference-pass-controls"><button type="button" class="reference-pass-jump" onclick="annotationSeek(${start + (end - start) / 2})" title="跳到第 ${passIndex + 1} 次悬停"><i data-lucide="locate-fixed"></i><span>第 ${passIndex + 1} 次</span></button><button type="button" class="icon-button danger" onclick="deleteReferencePass(${interactionIndex},${passIndex})" ${passes.length <= 1 ? "disabled" : ""} aria-label="删除第 ${passIndex + 1} 次悬停" title="删除本次悬停"><i data-lucide="trash-2"></i></button></div><div class="reference-pass-time"><label><span>悬停 · F${timeToFrame(start)}</span><span class="reference-pass-time-input"><input type="number" min="0" step="0.001" value="${start.toFixed(3)}" onchange="setReferencePassTime(${interactionIndex},${passIndex},'startTime',this.value)"><button type="button" class="icon-button" onclick="captureReferencePassTime(${interactionIndex},${passIndex},'startTime')" aria-label="将当前帧设为撤回 ${interactionIndex + 1} 第 ${passIndex + 1} 次悬停开始" title="取当前帧"><i data-lucide="crosshair"></i></button></span></label><i data-lucide="arrow-right"></i><label><span>移开 · F${timeToFrame(end)}</span><span class="reference-pass-time-input"><input type="number" min="0" step="0.001" value="${end.toFixed(3)}" onchange="setReferencePassTime(${interactionIndex},${passIndex},'endTime',this.value)"><button type="button" class="icon-button" onclick="captureReferencePassTime(${interactionIndex},${passIndex},'endTime')" aria-label="将当前帧设为撤回 ${interactionIndex + 1} 第 ${passIndex + 1} 次悬停移开" title="取当前帧"><i data-lucide="crosshair"></i></button></span></label></div><div class="reference-pass-target"><span>棋盘落点</span><label>行<input type="number" min="0" value="${target.row}" onchange="setReferencePassTarget(${interactionIndex},${passIndex},'row',this.value)"></label><label>列<input type="number" min="0" value="${target.col}" onchange="setReferencePassTarget(${interactionIndex},${passIndex},'col',this.value)"></label></div><div class="reference-pass-preview"><span>预消除</span><strong>行 ${esc(previewRows)} · 列 ${esc(previewCols)}</strong></div>${referencePassBoardHtml(interaction, interactionIndex, pass, passIndex)}</div>`;
    }).join("")}</div>`;
  }

  function referenceInteractionsHtml() {
    const entries = visibleReferenceInteractions();
    const candidates = visibleReferenceCandidates();
    if (!entries.length && !candidates.length) return "";
    const verified = entries.filter(({ interaction }) => interaction.manuallyVerified && !referenceIssues(interaction).length).length;
    const title = candidates.length ? "待判断的撤回片段" : "撤回动作";
    const copy = candidates.length ? "先确认这些片段是否真的拿起后放回；普通拖拽可直接忽略。" : "这里只显示人工添加或已经人工确认的撤回动作。";
    const statusText = candidates.length ? `${candidates.length} 待判断` : `${verified}/${entries.length} 已核对`;
    return `<section class="reference-section"><div class="reference-section-head"><div><h3>${title}</h3><p>${copy}</p></div><div class="reference-section-actions"><span>${statusText}</span><button type="button" class="command-button" onclick="addReferenceInteraction()"><i data-lucide="plus"></i><span>按当前帧新增撤回</span></button></div></div>${referenceCandidatesHtml()}${entries.length ? `<details class="reference-confirmed-details" ${candidates.length ? "" : "open"}><summary>已确认撤回 · ${entries.length}</summary><div class="reference-list">${entries.map(({ interaction, index }) => {
      const issues = referenceIssues(interaction);
      const start = numberOrNull(interaction.startTime) ?? 0;
      const end = numberOrNull(interaction.endTime) ?? start;
      const firstPass = interaction.hoverPasses?.[0];
      const hover = firstPass ? (Number(firstPass.startTime) + Number(firstPass.endTime)) / 2 : start + (end - start) / 2;
      const target = interaction.hoverTarget || { row: 0, col: 0 };
      const hoverButtons = interaction.hoverPasses?.length
        ? interaction.hoverPasses.map((pass, passIndex) => {
            const passTime = Number(pass.startTime) + (Number(pass.endTime) - Number(pass.startTime)) / 2;
            return `<button type="button" onclick="annotationSeek(${passTime})"><i data-lucide="mouse-pointer-2"></i><span>第${passIndex + 1}次悬停</span></button>`;
          }).join("")
        : `<button type="button" onclick="annotationSeek(${hover})"><i data-lucide="mouse-pointer-2"></i><span>悬停</span></button>`;
      return `<article class="reference-item ${issues.length ? "has-error" : interaction.manuallyVerified ? "verified" : "needs-review"}" data-reference-index="${index}"><div class="reference-item-head"><div><strong>撤回 ${index + 1}</strong><span>${timeText(start)} - ${timeText(end)}</span></div><div class="reference-item-actions"><button type="button" class="command-button ${interaction.manuallyVerified ? "" : "primary"}" onclick="verifyReferenceInteraction(${index})" ${issues.length ? "disabled" : ""}><i data-lucide="check-check"></i><span>${interaction.manuallyVerified ? "已确认" : "确认撤回"}</span></button><button type="button" class="icon-button danger" onclick="deleteReferenceInteraction(${index})" aria-label="删除撤回交互 ${index + 1}" title="删除撤回交互"><i data-lucide="trash-2"></i></button></div></div><div class="reference-seek"><button type="button" onclick="annotationSeek(${start})"><i data-lucide="play"></i><span>开始</span></button>${hoverButtons}<button type="button" onclick="annotationSeek(${end})"><i data-lucide="undo-2"></i><span>完全归位</span></button></div><div class="reference-fields"><div><h4>来源槽位</h4>${segmentHtml("", [[0,"左侧"],[1,"中间"],[2,"右侧"],[-1,"待确认"]], interaction.sourceSlot, "setReferenceSlot", index)}</div><div><h4>方块形状</h4>${referenceShapeGrid(interaction, index)}</div><div class="reference-target"><h4>首个悬停落点</h4><label>行<input type="number" min="0" value="${target.row}" onchange="setReferenceTarget(${index},'row',this.value)"></label><label>列<input type="number" min="0" value="${target.col}" onchange="setReferenceTarget(${index},'col',this.value)"></label></div><div class="reference-times"><h4>动作边界</h4><label class="reference-time-point"><span><b>拿起</b><small>F${timeToFrame(start)}</small></span><span class="reference-time-input"><input type="number" min="0" step="0.001" value="${start.toFixed(3)}" onchange="setReferenceTime(${index},'startTime',this.value)"><button type="button" class="icon-button" onclick="captureReferenceBoundary(${index},'startTime')" aria-label="将当前帧设为撤回 ${index + 1} 的拿起时间" title="取当前帧"><i data-lucide="crosshair"></i></button></span></label><i class="reference-time-arrow" data-lucide="arrow-right"></i><label class="reference-time-point reference-return-time"><span><b>完全归位</b><small>F${timeToFrame(end)}</small></span><span class="reference-time-input"><input type="number" min="0" step="0.001" value="${end.toFixed(3)}" onchange="setReferenceTime(${index},'endTime',this.value)"><button type="button" class="icon-button" onclick="captureReferenceBoundary(${index},'endTime')" aria-label="将当前帧设为撤回 ${index + 1} 的完全归位时间" title="取当前帧"><i data-lucide="crosshair"></i></button></span></label></div></div>${referencePassesHtml(interaction, index)}${issues.length ? `<div class="reference-warning"><i data-lucide="triangle-alert"></i><span>${esc(issues.join("；"))}</span></div>` : `<div class="reference-evidence">共 ${interaction.hoverPasses?.length || 1} 次悬停 · 最终未落下、已完全回到原槽位</div>`}</article>`;
    }).join("")}</div></details>` : ""}</section>`;
  }

  function referenceCandidatesHtml() {
    const entries = visibleReferenceCandidates();
    if (!entries.length) return "";
    return `<details class="reference-candidates"><summary><span>算法发现 ${entries.length} 个疑似片段</span><small>可能只是普通拖拽，并不代表存在撤回</small></summary><div class="reference-candidate-list">${entries.map(({ interaction, index }) => {
      const start = numberOrNull(interaction.startTime) ?? 0;
      const end = numberOrNull(interaction.endTime) ?? start;
      return `<div class="reference-candidate"><div><strong>疑似片段 ${index + 1}</strong><span>${timeText(start)} - ${timeText(end)}</span></div><div><button type="button" class="command-button compact" onclick="annotationSeek(${start})"><i data-lucide="play"></i><span>查看</span></button><button type="button" class="command-button compact" onclick="acceptReferenceCandidate(${index})"><i data-lucide="check"></i><span>确认为撤回</span></button><button type="button" class="command-button compact" onclick="dismissReferenceCandidate(${index})"><i data-lucide="x"></i><span>忽略</span></button></div></div>`;
    }).join("")}</div></details>`;
  }

  function decorateReferenceStepPositions() {
    const references = visibleReferenceInteractions();
    document.querySelectorAll(".reference-item").forEach((element, listIndex) => {
      const interaction = references[listIndex]?.interaction;
      const title = element.querySelector(".reference-item-head strong");
      if (!interaction || !title || title.nextElementSibling?.classList.contains("reference-step-position")) return;
      const position = document.createElement("span");
      position.className = "reference-step-position";
      position.textContent = referenceStepPosition(interaction);
      title.insertAdjacentElement("afterend", position);
    });
    const candidates = visibleReferenceCandidates();
    document.querySelectorAll(".reference-candidate").forEach((element, listIndex) => {
      const interaction = candidates[listIndex]?.interaction;
      const title = element.querySelector("strong");
      if (!interaction || !title || title.nextElementSibling?.classList.contains("reference-step-position")) return;
      const position = document.createElement("span");
      position.className = "reference-step-position";
      position.textContent = referenceStepPosition(interaction);
      title.insertAdjacentElement("afterend", position);
    });
  }

  function nextStepHtml(entries) {
    const candidates = visibleReferenceCandidates();
    const references = visibleReferenceInteractions();
    const problematic = entries.find(({ action }) => {
      const state = actionPlacementState(action);
      return state.overlaps.length || state.outOfBounds.length || !action.shape.length || +action.sourceSlot < 0 || action.clearState === "unknown" || !action.manuallyVerified || rangeErrors(action).length;
    });
    const badReference = references.find(({ interaction }) => !interaction.manuallyVerified || referenceIssues(interaction).length);
    let icon = "list-checks";
    let title = "继续核对动作步骤";
    let detail = "按左侧步骤顺序确认出块、落点、消除和时间范围。";
    if (candidates.length) {
      icon = "mouse-pointer-2";
      title = `先判断 ${candidates.length} 个疑似撤回片段`;
      detail = "它们可能只是普通拖拽；确认或忽略后，再保存动作真值。";
    } else if (problematic) {
      title = `核对步骤 ${problematic.action.stepIndex}`;
      detail = stepValidationIssues(problematic.action)[0] || "当前步骤仍有字段待确认。";
    } else if (badReference) {
      icon = "mouse-pointer-2";
      title = "核对撤回动作";
      detail = referenceIssues(badReference.interaction)[0] || "仍有撤回动作尚未确认。";
    } else if (actionsConfirmed) {
      icon = "video";
      title = "动作真值已保存，可以生成视频";
      detail = "下方已解锁输出配置，确认背景、方块风格和输出目录后生成。";
    } else {
      icon = "badge-check";
      title = "全部核对完成，可以保存动作真值";
      detail = "保存后才会进入生成视频阶段。";
    }
    return `<section class="next-step-banner"><i data-lucide="${icon}"></i><div><strong>${esc(title)}</strong><span>${esc(detail)}</span></div></section>`;
  }

  function coverageNoticeHtml(entries) {
    const first = entries[0]?.action;
    if (!first) return "";
    const bounds = timelineBounds(first);
    const firstFrame = Number(timeToFrame(bounds.start));
    if (!Number.isFinite(firstFrame) || firstFrame <= 1) return "";
    const lastSkippedFrame = Math.max(0, firstFrame - 1);
    return `<section class="coverage-notice"><i data-lucide="info"></i><div><strong>首个动作从 ${timeFrameText(bounds.start)} 开始</strong><span>F0-F${lastSkippedFrame} 属于开局、等待或棋盘未稳定片段，当前不会生成动作步骤；视频没有丢帧，可用左侧播放器回看。</span></div></section>`;
  }

  function footerHtml(entries) {
    const summary = summaryState(entries);
    const references = visibleReferenceInteractions();
    const referenceVerified = references.filter(({ interaction }) => interaction.manuallyVerified && !referenceIssues(interaction).length).length;
    const complete = summary.total ? Math.round(summary.valid / summary.total * 100) : 0;
    const blocked = entries.some(({ action }) => {
      const state = actionPlacementState(action);
      return state.overlaps.length || state.outOfBounds.length || !action.shape.length || +action.sourceSlot < 0 || action.clearState === "unknown" || !action.manuallyVerified || rangeErrors(action).length;
    }) || references.some(({ interaction }) => !interaction.manuallyVerified || referenceIssues(interaction).length);
    const undo = studio.undoStack.at(-1);
    const redo = studio.redoStack.at(-1);
    return `<footer class="annotation-footer"><div class="annotation-progress"><strong>${summary.valid}/${summary.total} 步已核对${references.length ? ` · 撤回 ${referenceVerified}/${references.length}` : ""}</strong><div class="progress-track" aria-label="标注完成度 ${complete}%"><div class="progress-value" style="width:${complete}%"></div></div><span class="hint">${summary.errors ? `${summary.errors} 步存在冲突` : blocked ? "仍有字段待确认" : "全部动作可提交规则校验"}</span></div><div class="annotation-footer-actions"><div class="annotation-history-controls"><button type="button" class="icon-button" onclick="undoAnnotationChange()" ${undo ? "" : "disabled"} aria-label="撤销上一步" title="${undo ? `撤销：${esc(undo.label)}` : "没有可撤销的操作"}"><i data-lucide="undo-2"></i></button><button type="button" class="icon-button" onclick="redoAnnotationChange()" ${redo ? "" : "disabled"} aria-label="重做上一步" title="${redo ? `重做：${esc(redo.label)}` : "没有可重做的操作"}"><i data-lucide="redo-2"></i></button></div><button type="button" class="primary command-button" id="confirmActionsBtn" ${blocked ? "disabled" : ""}><i data-lucide="badge-check"></i><span>确认并保存动作真值</span></button></div></footer>`;
  }

  function renderAnnotationStudio() {
    if (!rebuildJob?.analysis) return;
    window.setAnnotationStage?.("workbench");
    separateAutomaticReferenceCandidates();
    if (studio.historyJobId !== rebuildJob.jobId) window.resetAnnotationHistory();
    if (studio.player) { studio.player.dispose(); studio.player = null; }
    if (studio.timeline) { studio.timeline.destroy(); studio.timeline = null; }
    if (studio.cropper) { studio.cropper.destroy(); studio.cropper = null; }
    recomputeActionBoards();
    normalizeActionTargets();
    recomputeActionBoards();
    const entries = visibleActions();
    if (!entries.length) {
      $("analysisResult").innerHTML = `<div class="empty">没有可标注的动作步骤。请重新识别或校准棋盘。</div>`;
      return;
    }
    if (!entries.some(({ index }) => index === studio.activeIndex)) studio.activeIndex = entries[0].index;
    const active = actionDrafts[studio.activeIndex];
    for (const { action } of entries) ensureRanges(action);
    const duration = Number(rebuildJob.analysis.duration || 0);
    const summary = summaryState(entries);
    $("analysisResult").innerHTML = `<div class="annotation-studio"><div class="annotation-studio-layout"><aside class="annotation-media-pane"><section class="annotation-overview"><div class="video-stage"><div class="video-frame">${sourceVideoUrl() ? `<video id="annotationVideo" class="video-js" playsinline preload="metadata"><source src="${sourceVideoUrl()}"></video>` : `<div class="video-frame-empty">旧任务没有源视频播放地址，可使用证据帧完成校对</div>`}</div><div class="video-transport"><button type="button" class="transport-button" onclick="annotationStepFrame(-1)" aria-label="上一帧" title="上一帧"><i data-lucide="step-back"></i></button><button type="button" class="transport-button" id="annotationPlayButton" onclick="toggleAnnotationVideo()" aria-label="播放视频" title="播放"><i data-lucide="play"></i></button><button type="button" class="transport-button" onclick="annotationStepFrame(1)" aria-label="下一帧" title="下一帧"><i data-lucide="step-forward"></i></button><input id="annotationVideoScrubber" class="video-scrubber" type="range" min="0" max="${Math.max(0.001, duration)}" step="0.001" value="${Math.min(studio.videoTime, duration)}" aria-label="视频进度"><span class="video-time" id="annotationVideoTime">${timeText(studio.videoTime)} · F${timeToFrame(studio.videoTime)} / ${timeText(duration)}</span></div></div></section></aside><div class="annotation-editor-pane">${nextStepHtml(entries)}${coverageNoticeHtml(entries)}${calibrationHtml()}${referenceInteractionsHtml()}<div class="annotation-main">${railHtml(entries)}${workbenchHtml(active, studio.activeIndex)}</div>${footerHtml(entries)}</div></div></div>`;
    document.querySelector(".annotation-overview .timeline-panel")?.remove();
    decorateReferenceStepPositions();
    bindStudioElements();
    loadAnnotationVersions();
    window.renderAnnotationTaskSummary?.();
    refreshIcons();
  }

  function bindStudioElements() {
    document.querySelectorAll(".transport-button").forEach(button => {
      const direction = videoFrameDirection(button);
      if (direction) button.title = `${direction < 0 ? "上一帧" : "下一帧"}，长按连续调整`;
    });
    const video = $("annotationVideo");
    const scrubber = $("annotationVideoScrubber");
    if (scrubber) {
      const finishScrub = () => {
        if (!studio.scrubberDragging) return;
        studio.scrubberDragging = false;
        window.annotationSeek(scrubber.value);
      };
      scrubber.addEventListener("pointerdown", event => {
        studio.scrubberDragging = true;
        scrubber.setPointerCapture?.(event.pointerId);
      });
      scrubber.addEventListener("input", () => {
        window.annotationSeek(scrubber.value, { updateScrubber: false, updatePlayButton: false });
      });
      scrubber.addEventListener("pointerup", finishScrub);
      scrubber.addEventListener("pointercancel", finishScrub);
      scrubber.addEventListener("change", () => {
        studio.scrubberDragging = false;
        window.annotationSeek(scrubber.value);
      });
    }
    if (video && window.videojs) {
      studio.player = videojs(video, { controls: false, preload: "metadata", fluid: false, responsive: true });
      studio.player.ready(() => studio.player.currentTime(Math.min(studio.videoTime, Number(rebuildJob.analysis.duration || 0))));
      studio.player.on("timeupdate", () => {
        if (studio.scrubberDragging) return;
        studio.videoTime = numberOrNull(studio.player.currentTime()) ?? 0;
        const duration = Math.max(0.1, Number(rebuildJob.analysis.duration || studio.player.duration() || 0.1));
        const label = $("annotationVideoTime");
        if (label) label.textContent = `${timeText(studio.videoTime)} · F${timeToFrame(studio.videoTime)} / ${timeText(duration)}`;
        const scrubber = $("annotationVideoScrubber");
        if (scrubber && !studio.scrubberDragging) {
          scrubber.max = duration.toFixed(3);
          scrubber.value = Math.min(studio.videoTime, duration).toFixed(3);
        }
      });
      studio.player.on("play", () => updatePlayButton(true));
      studio.player.on("pause", () => updatePlayButton(false));
      studio.player.on("ended", () => updatePlayButton(false));
    }
    initializeTimeline();
    const confirm = $("confirmActionsBtn");
    if (confirm) confirm.onclick = confirmActions;
    bindCalibrationControls();
  }

  function bindCalibrationControls() {
    const image = $("sourceCalibrationImage");
    if (!image) return;
    const details = image.closest("details");
    const board = rebuildJob.analysis.board;
    const updateInputs = data => {
      $("sourceBoardX").value = Math.round(data.x);
      $("sourceBoardY").value = Math.round(data.y);
      $("sourceBoardW").value = Math.round(data.width);
      $("sourceBoardH").value = Math.round(data.height);
    };
    const createCropper = () => {
      studio.cropper?.destroy();
      studio.cropper = new Cropper(image, {
        viewMode: 1,
        dragMode: "crop",
        autoCropArea: 0.8,
        background: false,
        responsive: true,
        movable: false,
        zoomable: false,
        rotatable: false,
        scalable: false,
        ready: () => studio.cropper.setData({ x: board.x, y: board.y, width: board.width, height: board.height }),
        cropend: () => updateInputs(studio.cropper.getData(true)),
      });
    };
    details.addEventListener("toggle", () => {
      if (details.open) requestAnimationFrame(createCropper);
      else if (studio.cropper) { studio.cropper.destroy(); studio.cropper = null; }
    });
    if (details.open) createCropper();
    ["sourceBoardX", "sourceBoardY", "sourceBoardW", "sourceBoardH"].forEach(id => {
      $(id).onchange = () => studio.cropper?.setData({
        x: Number($("sourceBoardX").value),
        y: Number($("sourceBoardY").value),
        width: Number($("sourceBoardW").value),
        height: Number($("sourceBoardH").value),
      });
    });
    $("recalibrateSourceBtn").onclick = recalibrateSourceVideo;
  }

  async function loadAnnotationVersions() {
    if (!rebuildJob || !$("actionVersionSelect")) return;
    try {
      const response = await fetch(`http://127.0.0.1:8765/api/video/versions?jobId=${encodeURIComponent(rebuildJob.jobId)}`);
      const data = await response.json();
      if (!data.ok) throw new Error(data.error);
      $("actionVersionSelect").innerHTML = data.versions.length ? data.versions.map(version => `<option value="${esc(version.id)}">${esc(version.savedAt)} · ${esc(version.kind)}</option>`).join("") : `<option value="">暂无版本</option>`;
      $("restoreActionVersionBtn").disabled = !data.versions.length;
      $("restoreActionVersionBtn").onclick = restoreActionVersion;
    } catch (error) {
      $("actionVersionSelect").innerHTML = `<option value="">版本读取失败</option>`;
      $("videoStatus").textContent = `动作版本读取失败：${error.message}`;
    }
  }

  function refreshWorkbench(options = {}) {
    if (!actionsConfirmed && $("actionSaveFeedback")) $("actionSaveFeedback").hidden = true;
    if ($("rebuildOptions")) $("rebuildOptions").hidden = !actionsConfirmed;
    if ($("fullReplayBtn")) $("fullReplayBtn").disabled = !actionsConfirmed;
    const dialogBody = document.querySelector(".annotation-dialog-body");
    const editorPane = document.querySelector(".annotation-editor-pane");
    const dialogScrollTop = dialogBody?.scrollTop || 0;
    const editorScrollTop = editorPane?.scrollTop || 0;
    const referenceScrollTop = document.querySelector(".reference-list")?.scrollTop || 0;
    recomputeActionBoards();
    const active = actionDrafts[studio.activeIndex];
    const workbench = document.querySelector(".annotation-workbench");
    if (workbench && active && !active.deleted) workbench.outerHTML = workbenchHtml(active, studio.activeIndex);
    const entries = visibleActions();
    const rail = document.querySelector(".step-rail");
    if (rail) {
      const previousScrollTop = rail.querySelector(".step-list")?.scrollTop || 0;
      rail.outerHTML = railHtml(entries);
      const nextList = document.querySelector(".step-rail .step-list");
      if (nextList) nextList.scrollTop = previousScrollTop;
    }
    const footer = document.querySelector(".annotation-footer");
    if (footer) footer.outerHTML = footerHtml(entries);
    const referenceSection = document.querySelector(".reference-section");
    const referenceMarkup = referenceInteractionsHtml();
    const nextStep = document.querySelector(".next-step-banner");
    const coverageNotice = document.querySelector(".coverage-notice");
    const coverageMarkup = coverageNoticeHtml(entries);
    if (nextStep) nextStep.outerHTML = nextStepHtml(entries);
    if (coverageNotice && coverageMarkup) coverageNotice.outerHTML = coverageMarkup;
    else if (coverageNotice && !coverageMarkup) coverageNotice.remove();
    else if (!coverageNotice && coverageMarkup) document.querySelector(".task-admin-section, .reference-section, .annotation-main")?.insertAdjacentHTML("beforebegin", coverageMarkup);
    if (referenceSection && referenceMarkup) referenceSection.outerHTML = referenceMarkup;
    else if (referenceSection && !referenceMarkup) referenceSection.remove();
    else if (!referenceSection && referenceMarkup) document.querySelector(".annotation-main")?.insertAdjacentHTML("beforebegin", referenceMarkup);
    decorateReferenceStepPositions();
    const restoreAnnotationScroll = () => {
      if (dialogBody) dialogBody.scrollTop = dialogScrollTop;
      if (editorPane) editorPane.scrollTop = editorScrollTop;
      const nextReferenceList = document.querySelector(".reference-list");
      if (nextReferenceList) nextReferenceList.scrollTop = referenceScrollTop;
    };
    restoreAnnotationScroll();
    requestAnimationFrame(restoreAnnotationScroll);
    const timelinePanel = document.querySelector(".timeline-panel");
    if (timelinePanel && options.refreshTimeline !== false) {
      const summary = summaryState(entries);
      const metrics = timelinePanel.querySelectorAll(".timeline-metrics strong");
      if (metrics.length === 3) {
        metrics[0].textContent = entries.length;
        metrics[1].textContent = summary.valid;
        metrics[2].textContent = Math.max(0, entries.length - summary.valid);
      }
      if (studio.timeline && studio.timelineItems) {
        studio.timelineItems.clear();
        studio.timelineItems.add(timelineData(entries));
      } else {
        initializeTimeline();
      }
    }
    const confirm = $("confirmActionsBtn");
    if (confirm) confirm.onclick = confirmActions;
    updateStepValidationFeedback();
    refreshIcons();
  }

  function updatePlayButton(playing) {
    studio.playing = playing;
    const button = $("annotationPlayButton");
    if (!button) return;
    button.setAttribute("aria-label", playing ? "暂停视频" : "播放视频");
    button.title = playing ? "暂停" : "播放";
    button.innerHTML = `<i data-lucide="${playing ? "pause" : "play"}"></i>`;
    refreshIcons();
  }

  window.selectAnnotationStep = (index, seek) => {
    if (!actionDrafts[index] || actionDrafts[index].deleted) return;
    if (studio.boardEditIndex !== index) studio.boardEditIndex = -1;
    studio.activeIndex = index;
    if (seek) studio.videoTime = timelineBounds(actionDrafts[index]).start;
    refreshWorkbench();
    if (seek) window.annotationSeek(studio.videoTime);
  };

  window.annotationSeek = (seconds, options = {}) => {
    studio.videoTime = numberOrNull(seconds) ?? 0;
    if (studio.player) {
      studio.player.pause();
      studio.player.currentTime(Math.min(studio.videoTime, Number(rebuildJob.analysis.duration || studio.player.duration() || studio.videoTime)));
    }
    if (options.updatePlayButton !== false) updatePlayButton(false);
    const label = $("annotationVideoTime");
    const duration = Number(rebuildJob?.analysis?.duration || studio.player?.duration() || 0);
    if (label) label.textContent = `${timeText(studio.videoTime)} · F${timeToFrame(studio.videoTime)} / ${timeText(duration)}`;
    const scrubber = $("annotationVideoScrubber");
    if (scrubber && options.updateScrubber !== false) scrubber.value = Math.min(studio.videoTime, duration).toFixed(3);
  };

  window.jumpToAnnotationRange = (index, key) => {
    const action = actionDrafts[index];
    const range = action ? ensureRanges(action)[key] : null;
    const seconds = range?.start ?? range?.end;
    const feedback = $("rangeJumpFeedback");
    if (seconds === null || seconds === undefined) {
      if (feedback) feedback.textContent = `${rangeLabels[key]}尚未填写时间`;
      return;
    }
    window.annotationSeek(seconds);
    if (feedback) feedback.textContent = `已定位：${rangeLabels[key]}开始 ${timeText(seconds)}`;
  };

  window.seekAnnotationRangeEdge = (index, key, edge) => {
    const action = actionDrafts[index];
    const range = action ? ensureRanges(action)[key] : null;
    const seconds = range?.[edge];
    const feedback = $("rangeJumpFeedback");
    const edgeLabel = edge === "start" ? "开始" : "结束";
    if (seconds === null || seconds === undefined) {
      if (feedback) feedback.textContent = `${rangeLabels[key]}${edgeLabel}时间尚未填写`;
      return;
    }
    window.annotationSeek(seconds);
    if (feedback) feedback.textContent = `已定位：${rangeLabels[key]}${edgeLabel} ${timeText(seconds)} / F${timeToFrame(seconds)}`;
  };

  window.toggleAnnotationVideo = () => {
    if (!studio.player) return;
    if (studio.player.paused()) studio.player.play(); else studio.player.pause();
  };

  window.annotationStepFrame = (direction, options = {}) => {
    const fps = Math.max(1, Number(rebuildJob?.analysis?.fps || 30));
    window.annotationSeek(studio.videoTime + direction / fps, options);
  };

  function videoFrameDirection(button) {
    const source = button?.getAttribute("onclick") || "";
    const match = source.match(/annotationStepFrame\((-?\d+)\)/);
    return match ? Number(match[1]) : null;
  }

  function beginHeldVideoFrame(button) {
    const direction = videoFrameDirection(button);
    if (!direction || studio.videoFrameHold) return;
    const hold = { button, direction, active: false, delayTimer: null, repeatTimer: null };
    hold.delayTimer = window.setTimeout(() => {
      hold.active = true;
      window.annotationStepFrame(direction, { updatePlayButton: false });
      hold.repeatTimer = window.setInterval(
        () => window.annotationStepFrame(direction, { updatePlayButton: false }),
        85,
      );
    }, 420);
    studio.videoFrameHold = hold;
  }

  function stopHeldVideoFrame() {
    const hold = studio.videoFrameHold;
    if (!hold) return;
    studio.videoFrameHold = null;
    window.clearTimeout(hold.delayTimer);
    window.clearInterval(hold.repeatTimer);
    if (!hold.active) return;
    updatePlayButton(false);
    studio.suppressVideoFrameClick = hold.button;
    window.setTimeout(() => {
      if (studio.suppressVideoFrameClick === hold.button) studio.suppressVideoFrameClick = null;
    }, 0);
  }

  window.setAnnotationRange = async (index, key, bound, value) => {
    const action = actionDrafts[index];
    recordAnnotationHistory(`步骤 ${action.stepIndex} 的${rangeLabels[key]}时间`);
    const seconds = numberOrNull(value);
    ensureRanges(action)[key][bound] = seconds;
    action.manuallyVerified = false;
    actionsConfirmed = false;
    $("fullReplayBtn").disabled = true;
    refreshWorkbench();
    if (seconds !== null) await refreshAnnotationEvidence(index, key, bound, seconds);
  };

  async function refreshAnnotationEvidence(index, key, bound, seconds) {
    const action = actionDrafts[index];
    const evidenceKey = { before: "before", drag: "action", placed: "placed", clear: "cleared" }[key];
    const captureKey = `${index}:${key}:${bound}`;
    const requestId = ++studio.captureSequence;
    studio.latestCapture.set(captureKey, requestId);
    const feedback = $("rangeJumpFeedback");
    if (feedback) feedback.textContent = `正在截取 F${timeToFrame(seconds)}…`;
    try {
      const response = await fetch("/api/video/capture-frame", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ jobId: rebuildJob.jobId, stepIndex: action.stepIndex, evidenceKey, time: seconds }),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`);
      if (studio.latestCapture.get(captureKey) !== requestId) return;
      action.evidenceFrames ??= {};
      action.evidenceTimes ??= {};
      action.evidenceFrames[evidenceKey] = data.imagePath;
      action.evidenceTimes[evidenceKey] = data.time;
      ensureRanges(action)[key][bound] = data.time;
      refreshWorkbench({ refreshTimeline: false });
      const done = $("rangeJumpFeedback");
      if (done) done.textContent = `已更新：${rangeLabels[key]} F${data.frameNumber} · ${timeText(data.time)}`;
    } catch (error) {
      if (studio.latestCapture.get(captureKey) !== requestId) return;
      const failed = $("rangeJumpFeedback");
      if (failed) failed.textContent = `时间已记录，截图更新失败：${error.message}`;
    }
  }

  window.setAnnotationRangeFrame = async (index, key, bound, value) => {
    const frame = Math.max(0, Math.round(Number(value) || 0));
    await window.setAnnotationRange(index, key, bound, frame / sourceFps());
  };

  window.nudgeAnnotationRangeFrame = async (index, key, bound, delta) => {
    const currentFrame = Number(timeToFrame(ensureRanges(actionDrafts[index])[key][bound]) || 0);
    await window.setAnnotationRangeFrame(index, key, bound, Math.max(0, currentFrame + Number(delta)));
  };

  function frameNudgeArguments(button) {
    const source = button?.getAttribute("onclick") || "";
    const match = source.match(/nudgeAnnotationRangeFrame\((\d+),'([^']+)','([^']+)',(-?\d+)\)/);
    if (!match) return null;
    return {
      index: Number(match[1]),
      key: match[2],
      bound: match[3],
      delta: Number(match[4]),
    };
  }

  function applyHeldFrameNudge(hold) {
    const action = actionDrafts[hold.index];
    if (!action || action.deleted) return;
    const range = ensureRanges(action)[hold.key];
    const currentFrame = Number(timeToFrame(range[hold.bound]) || 0);
    const maxFrame = Math.max(0, Math.floor(Number(rebuildJob?.analysis?.duration || Infinity) * sourceFps()));
    const nextFrame = Math.min(maxFrame, Math.max(0, currentFrame + hold.delta));
    const seconds = numberOrNull(nextFrame / sourceFps()) ?? 0;
    range[hold.bound] = seconds;
    action.manuallyVerified = false;
    actionsConfirmed = false;
    $("fullReplayBtn").disabled = true;

    const valueHost = hold.button.closest(".range-value");
    const secondsInput = valueHost?.querySelector("input:not(.range-frame)");
    const frameInput = valueHost?.querySelector("input.range-frame");
    if (secondsInput) secondsInput.value = seconds.toFixed(3);
    if (frameInput) frameInput.value = String(nextFrame);
    window.annotationSeek(seconds);
    const feedback = $("rangeJumpFeedback");
    if (feedback) feedback.textContent = `长按调整中：${rangeLabels[hold.key]} F${nextFrame}`;
  }

  function beginHeldFrameNudge(button) {
    const args = frameNudgeArguments(button);
    if (!args || studio.frameNudgeHold) return;
    const hold = { ...args, button, active: false, delayTimer: null, repeatTimer: null };
    hold.delayTimer = window.setTimeout(() => {
      hold.active = true;
      const action = actionDrafts[hold.index];
      if (!action || action.deleted) return;
      recordAnnotationHistory(`步骤 ${action.stepIndex} 的${rangeLabels[hold.key]}时间`);
      applyHeldFrameNudge(hold);
      hold.repeatTimer = window.setInterval(() => applyHeldFrameNudge(hold), 85);
    }, 420);
    studio.frameNudgeHold = hold;
  }

  async function stopHeldFrameNudge() {
    const hold = studio.frameNudgeHold;
    if (!hold) return;
    studio.frameNudgeHold = null;
    window.clearTimeout(hold.delayTimer);
    window.clearInterval(hold.repeatTimer);
    if (!hold.active) return;
    studio.suppressFrameNudgeClick = hold.button;
    window.setTimeout(() => {
      if (studio.suppressFrameNudgeClick === hold.button) studio.suppressFrameNudgeClick = null;
    }, 0);
    const seconds = ensureRanges(actionDrafts[hold.index])[hold.key][hold.bound];
    await refreshAnnotationEvidence(hold.index, hold.key, hold.bound, seconds);
  }

  window.captureAnnotationRange = async (index, key, bound) => {
    const action = actionDrafts[index];
    const range = ensureRanges(action)[key];
    const current = numberOrNull(studio.player?.currentTime?.() ?? studio.videoTime) ?? 0;
    recordAnnotationHistory(`步骤 ${action.stepIndex} 的${rangeLabels[key]}${bound === "start" ? "开始" : "结束"}时间`);
    range[bound] = current;
    if (bound === "start" && (range.end === null || range.end < current)) range.end = current;
    if (bound === "end" && (range.start === null || range.start > current)) range.start = current;
    action.manuallyVerified = false;
    actionsConfirmed = false;
    $("fullReplayBtn").disabled = true;
    refreshWorkbench({ refreshTimeline: false });
    await refreshAnnotationEvidence(index, key, bound, current);
  };

  window.setAnnotationSlot = (index, value) => {
    recordAnnotationHistory(`步骤 ${actionDrafts[index].stepIndex} 的来源槽位`);
    actionDrafts[index].sourceSlot = Number(value);
    actionDrafts[index].manuallyVerified = false;
    actionDrafts[index].requiresConfirmation = actionDrafts[index].clearState === "unknown" || Number(value) < 0;
    actionsConfirmed = false;
    $("fullReplayBtn").disabled = true;
    refreshWorkbench();
  };

  window.setAnnotationClear = (index, value) => {
    const action = actionDrafts[index];
    recordAnnotationHistory(`步骤 ${action.stepIndex} 的消除检测`);
    action.clearState = value;
    action.manuallyVerified = false;
    action.requiresConfirmation = Number(action.sourceSlot) < 0 || value === "unknown";
    if (value === "off") action.timeRanges.clear = { start: null, end: null };
    if (value === "on" && action.timeRanges.clear.start === null) {
      const start = action.timeRanges.placed.end ?? action.timeRanges.placed.start ?? 0;
      action.timeRanges.clear = { start, end: start + 0.4 };
    }
    actionsConfirmed = false;
    $("fullReplayBtn").disabled = true;
    refreshWorkbench();
  };

  function snappedAnnotationTarget(action, clickedRow, clickedCol) {
    if (!action.shape.length) return { row: clickedRow, col: clickedCol };
    const current = action.target || { row: 0, col: 0 };
    const candidates = new Map();
    for (const cell of action.shape) {
      const target = { row: clickedRow - cell.row, col: clickedCol - cell.col };
      candidates.set(`${target.row}:${target.col}`, target);
    }
    let best = null;
    for (const target of candidates.values()) {
      const state = actionPlacementState({ ...action, target });
      const conflicts = state.overlaps.length + state.outOfBounds.length;
      const distance = Math.abs(target.row - Number(current.row || 0)) + Math.abs(target.col - Number(current.col || 0));
      const score = conflicts * 1000 + distance;
      if (!best || score < best.score) best = { target, score };
    }
    return best?.target || { row: clickedRow, col: clickedCol };
  }

  window.setAnnotationTarget = (index, row, col) => {
    const action = actionDrafts[index];
    recordAnnotationHistory(`步骤 ${action.stepIndex} 的棋盘落点`);
    action.target = snappedAnnotationTarget(action, row, col);
    actionDrafts[index].manuallyVerified = false;
    actionsConfirmed = false;
    $("fullReplayBtn").disabled = true;
    refreshWorkbench();
  };

  window.setAnnotationTargetAxis = (index, axis, value) => {
    recordAnnotationHistory(`步骤 ${actionDrafts[index].stepIndex} 的棋盘落点`);
    actionDrafts[index].target[axis] = Number(value);
    actionDrafts[index].manuallyVerified = false;
    actionsConfirmed = false;
    $("fullReplayBtn").disabled = true;
    refreshWorkbench();
  };

  function markBoardCorrectionCascade(index) {
    for (let actionIndex = index; actionIndex < actionDrafts.length; actionIndex++) {
      if (!actionDrafts[actionIndex].deleted) actionDrafts[actionIndex].manuallyVerified = false;
    }
    actionsConfirmed = false;
    $("fullReplayBtn").disabled = true;
  }

  function paintAnnotationBoardCell(index, row, col, element) {
    const paint = studio.boardPaint;
    if (!paint || paint.index !== index) return;
    const key = `${row}:${col}`;
    if (paint.visited.has(key)) return;
    paint.visited.add(key);
    paint.board[row][col] = paint.value;
    paint.changed = true;
    if (element) element.classList.toggle("occupied", paint.value !== null);
  }

  function finishAnnotationBoardPaint() {
    const paint = studio.boardPaint;
    if (!paint) return;
    studio.boardPaint = null;
    if (paint.changed) refreshWorkbench({ refreshTimeline: false });
  }

  window.beginAnnotationBoardPaint = (event, index, row, col) => {
    if (event.button !== 0 || studio.boardEditIndex !== index) return;
    event.preventDefault();
    const action = actionDrafts[index];
    recordAnnotationHistory(`步骤 ${action.stepIndex} 的原棋盘连续校正`);
    const rows = Number(rebuildJob.analysis.board.rows);
    const cols = Number(rebuildJob.analysis.board.cols);
    const source = action.manualBeforeBoard || action.liveBeforeBoard || action.beforeBoard || [];
    const board = Array.from({ length: rows }, (_, boardRow) =>
      Array.from({ length: cols }, (_, boardCol) => source[boardRow]?.[boardCol] ?? null)
    );
    const fallbackHue = source.flat().find(cell => cell !== null && cell !== undefined)
      ?? action.shape.find(cell => cell.hue !== null && cell.hue !== undefined)?.hue
      ?? 60;
    studio.boardPaint = {
      index,
      board,
      value: board[row][col] === null ? fallbackHue : null,
      visited: new Set(),
      changed: false,
    };
    action.manualBeforeBoard = board;
    action.beforeBoardManuallyCorrected = true;
    markBoardCorrectionCascade(index);
    paintAnnotationBoardCell(index, row, col, event.currentTarget);
  };

  window.continueAnnotationBoardPaint = (event, index, row, col) => {
    if (!studio.boardPaint || !(event.buttons & 1)) return;
    event.preventDefault();
    paintAnnotationBoardCell(index, row, col, event.currentTarget);
  };

  document.addEventListener("pointermove", event => {
    const paint = studio.boardPaint;
    if (!paint || !(event.buttons & 1)) return;
    const cell = document.elementFromPoint(event.clientX, event.clientY)?.closest("[data-board-edit-index]");
    if (!cell || Number(cell.dataset.boardEditIndex) !== paint.index) return;
    event.preventDefault();
    paintAnnotationBoardCell(
      paint.index,
      Number(cell.dataset.boardRow),
      Number(cell.dataset.boardCol),
      cell,
    );
  });
  document.addEventListener("pointerup", finishAnnotationBoardPaint);
  document.addEventListener("pointercancel", finishAnnotationBoardPaint);

  window.toggleAnnotationBoardEdit = index => {
    finishAnnotationBoardPaint();
    studio.boardEditIndex = studio.boardEditIndex === index ? -1 : index;
    studio.activeIndex = index;
    refreshWorkbench({ refreshTimeline: false });
  };

  window.toggleAnnotationBeforeCell = (index, row, col) => {
    const action = actionDrafts[index];
    recordAnnotationHistory(`步骤 ${action.stepIndex} 的原棋盘格`);
    const rows = Number(rebuildJob.analysis.board.rows);
    const cols = Number(rebuildJob.analysis.board.cols);
    const source = action.manualBeforeBoard || action.liveBeforeBoard || action.beforeBoard || [];
    const board = Array.from({ length: rows }, (_, boardRow) =>
      Array.from({ length: cols }, (_, boardCol) => source[boardRow]?.[boardCol] ?? null)
    );
    const fallbackHue = source.flat().find(cell => cell !== null && cell !== undefined)
      ?? action.shape.find(cell => cell.hue !== null && cell.hue !== undefined)?.hue
      ?? 60;
    board[row][col] = board[row][col] === null ? fallbackHue : null;
    action.manualBeforeBoard = board;
    action.beforeBoardManuallyCorrected = true;
    markBoardCorrectionCascade(index);
    refreshWorkbench({ refreshTimeline: false });
  };

  window.resetAnnotationBeforeBoard = index => {
    const action = actionDrafts[index];
    recordAnnotationHistory(`步骤 ${action.stepIndex} 的原棋盘校正`);
    delete action.manualBeforeBoard;
    delete action.beforeBoardManuallyCorrected;
    markBoardCorrectionCascade(index);
    refreshWorkbench({ refreshTimeline: false });
  };

  window.setAnnotationNotes = (index, value) => {
    recordAnnotationHistory(`步骤 ${actionDrafts[index].stepIndex} 的标注备注`);
    actionDrafts[index].annotationNotes = value;
    actionsConfirmed = false;
    $("fullReplayBtn").disabled = true;
    refreshWorkbench({ refreshTimeline: false });
  };

  window.applyAnnotationCandidate = (index, candidateIndex) => {
    recordAnnotationHistory(`步骤 ${actionDrafts[index].stepIndex} 的合法候选`);
    applyActionCandidate(index, candidateIndex);
    actionDrafts[index].manuallyVerified = false;
    studio.activeIndex = index;
    refreshWorkbench();
  };

  window.deleteAnnotationStep = index => {
    const action = actionDrafts[index];
    if (!action || action.deleted) return;
    recordAnnotationHistory(`删除步骤 ${action.stepIndex}`);
    action.originalStepIndex ??= action.stepIndex;
    action.deleted = true;
    lastDeletedActionIndex = index;
    activeActionEditor = -1;
    renumberVisibleActions();
    actionsConfirmed = false;
    $("fullReplayBtn").disabled = true;
    const next = visibleActions().find(entry => entry.index > index) || visibleActions().at(-1);
    if (studio.activeIndex === index) studio.activeIndex = next?.index ?? 0;
    renderAnnotationStudio();
  };

  window.verifyAnnotationStep = index => {
    const action = actionDrafts[index];
    const placement = actionPlacementState(action);
    const invalid = placement.overlaps.length || placement.outOfBounds.length || !action.shape.length || Number(action.sourceSlot) < 0 || action.clearState === "unknown" || rangeErrors(action).length;
    if (invalid) {
      refreshWorkbench({ refreshTimeline: false });
      $("videoStatus").textContent = `步骤 ${action.stepIndex}：${stepValidationIssues(action).join("；")}`;
      return;
    }
    recordAnnotationHistory(`确认步骤 ${action.stepIndex}`);
    action.manuallyVerified = true;
    action.requiresConfirmation = false;
    actionsConfirmed = false;
    $("fullReplayBtn").disabled = true;
    refreshWorkbench();
  };

  window.undoAnnotationDelete = () => {
    window.undoAnnotationChange();
  };

  toggleActionCell = (index, row, col) => {
    const action = actionDrafts[index];
    recordAnnotationHistory(`步骤 ${action.stepIndex} 的方块形状`);
    const at = action.shape.findIndex(cell => cell.row === row && cell.col === col);
    if (at >= 0) action.shape.splice(at, 1); else action.shape.push({ row, col });
    if (action.shape.length) {
      const minRow = Math.min(...action.shape.map(cell => cell.row));
      const minCol = Math.min(...action.shape.map(cell => cell.col));
      action.target.row += minRow;
      action.target.col += minCol;
      action.shape = action.shape.map(cell => ({ row: cell.row - minRow, col: cell.col - minCol }));
    }
    actionsConfirmed = false;
    action.manuallyVerified = false;
    $("fullReplayBtn").disabled = true;
    studio.activeIndex = index;
    refreshWorkbench();
  };

  function touchReference(index, label) {
    const interaction = referenceInteractionDrafts[index];
    if (!interaction || interaction.deleted) return null;
    recordAnnotationHistory(label);
    interaction.manuallyVerified = false;
    actionsConfirmed = false;
    $("fullReplayBtn").disabled = true;
    return interaction;
  }

  function resolvedBoardAfterDraft(action) {
    const rows = Number(rebuildJob?.analysis?.board?.rows || 0);
    const cols = Number(rebuildJob?.analysis?.board?.cols || 0);
    const before = structuredClone(action.liveBeforeBoard || action.manualBeforeBoard || action.beforeBoard || Array.from({ length: rows }, () => Array(cols).fill(null)));
    const state = actionPlacementState(action);
    if (state.overlaps.length || state.outOfBounds.length) return before;
    for (const cell of action.shape || []) {
      const row = Number(action.target.row) + Number(cell.row);
      const col = Number(action.target.col) + Number(cell.col);
      if (before[row]) before[row][col] = 60;
    }
    if (action.clearState === "on") {
      const fullRows = new Set(before.map((line, row) => line.every(cell => cell !== null) ? row : -1).filter(row => row >= 0));
      const fullCols = new Set();
      for (let col = 0; col < cols; col++) if (before.every(line => line[col] !== null)) fullCols.add(col);
      for (let row = 0; row < rows; row++) for (let col = 0; col < cols; col++) if (fullRows.has(row) || fullCols.has(col)) before[row][col] = null;
    }
    return before;
  }

  function firstEmptyTarget(board) {
    for (let row = 0; row < board.length; row++) for (let col = 0; col < (board[row]?.length || 0); col++) {
      if (board[row][col] === null || board[row][col] === undefined) return { row, col };
    }
    return { row: 0, col: 0 };
  }

  function addAnnotationStep(index, position) {
    const currentAction = actionDrafts[index];
    if (!currentAction || currentAction.deleted) return;
    const originalStep = currentAction.stepIndex;
    const beforeCurrent = position === "before";
    recordAnnotationHistory(`在步骤 ${originalStep} ${beforeCurrent ? "前" : "后"}添加步骤`);
    const emptyBoard = () => Array.from(
      { length: Number(rebuildJob?.analysis?.board?.rows || 0) },
      () => Array(Number(rebuildJob?.analysis?.board?.cols || 0)).fill(null),
    );
    const boardBefore = beforeCurrent
      ? structuredClone(currentAction.liveBeforeBoard || currentAction.manualBeforeBoard || currentAction.beforeBoard || emptyBoard())
      : resolvedBoardAfterDraft(currentAction);
    const target = firstEmptyTarget(boardBefore);
    const frame = 1 / sourceFps();
    const duration = Number(rebuildJob?.analysis?.duration || 0);
    const current = numberOrNull(studio.player?.currentTime?.() ?? studio.videoTime) ?? timelineBounds(currentAction).end;
    const placed = Math.min(Math.max(0, current), Math.max(0, duration - frame));
    const insertAt = beforeCurrent ? index : index + 1;
    actionDrafts.splice(insertAt, 0, {
      stepIndex: originalStep + (beforeCurrent ? 0 : 1),
      originalStepIndex: null,
      sourceEventIndex: null,
      manualAdded: true,
      resetBefore: false,
      sourceSlot: -1,
      target,
      shape: [{ row: 0, col: 0 }],
      beforeBoard: structuredClone(boardBefore),
      recognizedAfterBoard: structuredClone(boardBefore),
      clearedRows: [],
      clearedCols: [],
      clearState: "unknown",
      clearEvidence: "人工新增步骤，请确认消除状态",
      confidence: "manual_added",
      candidateReasons: [],
      candidateSolutions: [],
      requiresConfirmation: true,
      manuallyVerified: false,
      evidenceFrames: {},
      evidenceTimes: { placed },
      timeRanges: {
        before: { start: Math.max(0, placed - frame * 2), end: Math.max(0, placed - frame) },
        drag: { start: Math.max(0, placed - frame), end: placed },
        placed: { start: placed, end: Math.min(duration || placed + frame, placed + frame) },
        clear: { start: null, end: null },
      },
      annotationNotes: `人工补充在原步骤 ${originalStep} ${beforeCurrent ? "之前" : "之后"}的正常落块步骤`,
    });
    renumberVisibleActions();
    studio.activeIndex = insertAt;
    actionsConfirmed = false;
    $("fullReplayBtn").disabled = true;
    refreshWorkbench();
    $("videoStatus").textContent = `已在原步骤 ${originalStep} ${beforeCurrent ? "前" : "后"}添加正常步骤，请核对时间、出块、形状、落点和消除状态`;
  }

  window.addAnnotationStepBefore = index => addAnnotationStep(index, "before");
  window.addAnnotationStepAfter = index => addAnnotationStep(index, "after");

  window.addReferenceInteraction = sourceActionIndex => {
    const parsedIndex = Number(sourceActionIndex);
    const sourceAction = actionDrafts[Number.isInteger(parsedIndex) ? parsedIndex : studio.activeIndex];
    recordAnnotationHistory("按当前帧添加撤回动作");
    const frame = 1 / sourceFps();
    const duration = Number(rebuildJob?.analysis?.duration || 0);
    const requested = numberOrNull(studio.player?.currentTime?.() ?? studio.videoTime) ?? 0;
    const startTime = Math.min(Math.max(0, requested), Math.max(0, duration - frame * 3));
    const endTime = Math.min(duration || startTime + frame * 3, startTime + frame * 3);
    const boardBefore = sourceAction ? resolvedBoardAfterDraft(sourceAction) : Array.from(
      { length: Number(rebuildJob?.analysis?.board?.rows || 0) },
      () => Array(Number(rebuildJob?.analysis?.board?.cols || 0)).fill(null),
    );
    const target = firstEmptyTarget(boardBefore);
    const referenceIndex = referenceInteractionDrafts.length;
    referenceInteractionDrafts.push({
      type: "cancelled_drag",
      manualAdded: true,
      sourceSlot: -1,
      shape: [{ row: 0, col: 0 }],
      hoverTarget: { ...target },
      candidateTargets: [{ ...target }],
      startTime: Number(startTime.toFixed(3)),
      endTime: Number(endTime.toFixed(3)),
      returnCompleteTime: Number(endTime.toFixed(3)),
      duration: Number((endTime - startTime).toFixed(3)),
      boardBefore: structuredClone(boardBefore),
      boardMutation: false,
      clearExecuted: false,
      returnedToSource: true,
      manuallyVerified: false,
      hoverPasses: [{
        passIndex: 1,
        startTime: Number(Math.min(endTime, startTime + frame).toFixed(3)),
        endTime: Number(Math.min(endTime, startTime + frame * 2).toFixed(3)),
        target: { ...target },
        previewClearedRows: [],
        previewClearedCols: [],
        timeSource: "manual_current_frame",
      }],
    });
    actionsConfirmed = false;
    $("fullReplayBtn").disabled = true;
    refreshWorkbench({ refreshTimeline: false });
    $("videoStatus").textContent = `已在 F${timeToFrame(startTime)} 新增撤回动作，请核对来源槽位、形状、悬停轨迹和完全归位帧`;
    requestAnimationFrame(() => document.querySelector(`[data-reference-index="${referenceIndex}"]`)?.scrollIntoView({ block: "start", behavior: "smooth" }));
  };

  window.acceptReferenceCandidate = index => {
    const candidate = referenceCandidateDrafts[index];
    if (!candidate || candidate.deleted) return;
    recordAnnotationHistory(`确认疑似片段 ${index + 1} 为撤回`);
    referenceInteractionDrafts.push({
      ...structuredClone(candidate),
      autoCandidate: false,
      manualAdded: true,
      manuallyVerified: false,
    });
    candidate.deleted = true;
    actionsConfirmed = false;
    $("fullReplayBtn").disabled = true;
    refreshWorkbench({ refreshTimeline: false });
    $("videoStatus").textContent = `疑似片段 ${index + 1} 已加入撤回标注区，请核对后确认`;
  };

  window.dismissReferenceCandidate = index => {
    const candidate = referenceCandidateDrafts[index];
    if (!candidate || candidate.deleted) return;
    recordAnnotationHistory(`忽略疑似片段 ${index + 1}`);
    candidate.deleted = true;
    refreshWorkbench({ refreshTimeline: false });
    $("videoStatus").textContent = `已忽略疑似片段 ${index + 1}；它不会写入真值`;
  };

  function syncReferencePasses(interaction) {
    interaction.hoverPasses.forEach((pass, passIndex) => { pass.passIndex = passIndex + 1; });
    interaction.hoverTarget = interaction.hoverPasses[0]?.target ? { ...interaction.hoverPasses[0].target } : null;
    interaction.candidateTargets = interaction.hoverPasses.map(pass => ({ ...pass.target }));
  }

  window.addReferencePass = index => {
    const interaction = touchReference(index, `撤回交互 ${index + 1} 添加悬停`);
    if (!interaction) return;
    interaction.hoverPasses ||= [];
    const frameDuration = 1 / sourceFps();
    const interactionStart = numberOrNull(interaction.startTime) ?? 0;
    const interactionEnd = Math.max(interactionStart + frameDuration, numberOrNull(interaction.endTime) ?? interactionStart + frameDuration);
    const playerTime = numberOrNull(studio.player?.currentTime?.() ?? studio.videoTime) ?? interactionStart;
    const startTime = Math.min(Math.max(playerTime, interactionStart), Math.max(interactionStart, interactionEnd - frameDuration));
    const previous = interaction.hoverPasses.at(-1);
    interaction.hoverPasses.push({
      startTime: Number(startTime.toFixed(3)),
      endTime: Number(Math.min(interactionEnd, startTime + frameDuration).toFixed(3)),
      target: { ...(previous?.target || interaction.hoverTarget || { row: 0, col: 0 }) },
      previewClearedRows: [],
      previewClearedCols: [],
      timeSource: "manual_current_frame",
    });
    interaction.hoverPasses.sort((a, b) => Number(a.startTime) - Number(b.startTime));
    syncReferencePasses(interaction);
    $("videoStatus").textContent = `撤回交互 ${index + 1}：已在 F${timeToFrame(startTime)} 新增一次悬停，请继续标记移开帧和棋盘落点`;
    refreshWorkbench({ refreshTimeline: false });
  };

  window.deleteReferencePass = (index, passIndex) => {
    const interaction = referenceInteractionDrafts[index];
    if (!interaction || interaction.deleted || (interaction.hoverPasses?.length || 0) <= 1) return;
    touchReference(index, `撤回交互 ${index + 1} 删除第 ${passIndex + 1} 次悬停`);
    interaction.hoverPasses.splice(passIndex, 1);
    syncReferencePasses(interaction);
    refreshWorkbench({ refreshTimeline: false });
  };

  window.setReferenceSlot = (index, value) => {
    const interaction = touchReference(index, `撤回交互 ${index + 1} 的来源槽位`);
    if (!interaction) return;
    interaction.sourceSlot = Number(value);
    refreshWorkbench({ refreshTimeline: false });
  };

  window.setReferenceTarget = (index, axis, value) => {
    const interaction = touchReference(index, `撤回交互 ${index + 1} 的悬停位置`);
    if (!interaction) return;
    interaction.hoverTarget ||= { row: 0, col: 0 };
    interaction.hoverTarget[axis] = Number(value);
    refreshWorkbench({ refreshTimeline: false });
  };

  window.setReferenceTime = (index, field, value) => {
    const interaction = touchReference(index, `撤回交互 ${index + 1} 的时间`);
    if (!interaction) return;
    interaction[field] = numberOrNull(value);
    if (field === "endTime") {
      interaction.returnCompleteTime = interaction.endTime;
      interaction.returnTimeSource = "manual_input";
    }
    refreshWorkbench({ refreshTimeline: false });
  };

  window.captureReferenceBoundary = (index, field) => {
    const isStart = field === "startTime";
    const label = isStart ? "拿起" : "完全归位";
    const interaction = touchReference(index, `撤回交互 ${index + 1} 的${label}时间`);
    if (!interaction) return;
    const current = numberOrNull(studio.player?.currentTime?.() ?? studio.videoTime) ?? 0;
    interaction[field] = current;
    if (isStart) interaction.startTimeSource = "manual_current_frame";
    else {
      interaction.returnCompleteTime = current;
      interaction.returnTimeSource = "manual_current_frame";
    }
    $("videoStatus").textContent = `撤回交互 ${index + 1}：已将 F${timeToFrame(current)}（${timeText(current)}）标记为${label}`;
    refreshWorkbench({ refreshTimeline: false });
  };

  window.setReferencePassTarget = (index, passIndex, axis, value) => {
    const interaction = touchReference(index, `撤回交互 ${index + 1} 第 ${passIndex + 1} 次悬停位置`);
    const pass = interaction?.hoverPasses?.[passIndex];
    if (!pass) return;
    pass.target ||= { row: 0, col: 0 };
    pass.target[axis] = Number(value);
    if (passIndex === 0) interaction.hoverTarget = { ...pass.target };
    interaction.candidateTargets = interaction.hoverPasses.map(item => ({ ...item.target }));
    refreshWorkbench({ refreshTimeline: false });
  };

  window.setReferencePassTargetFromBoard = (index, passIndex, row, col) => {
    const interaction = touchReference(index, `撤回交互 ${index + 1} 第 ${passIndex + 1} 次悬停棋盘落点`);
    const pass = interaction?.hoverPasses?.[passIndex];
    if (!pass) return;
    const rows = Number(rebuildJob?.analysis?.board?.rows || 0);
    const cols = Number(rebuildJob?.analysis?.board?.cols || 0);
    const pseudoAction = {
      beforeBoard: interaction.boardBefore || Array.from({ length: rows }, () => Array(cols).fill(null)),
      shape: interaction.shape || [],
      target: pass.target || { row: 0, col: 0 },
    };
    pass.target = snappedAnnotationTarget(pseudoAction, row, col);
    if (passIndex === 0) interaction.hoverTarget = { ...pass.target };
    interaction.candidateTargets = interaction.hoverPasses.map(item => ({ ...item.target }));
    refreshWorkbench({ refreshTimeline: false });
  };

  window.setReferencePassTime = (index, passIndex, field, value) => {
    const interaction = touchReference(index, `撤回交互 ${index + 1} 第 ${passIndex + 1} 次悬停时间`);
    const pass = interaction?.hoverPasses?.[passIndex];
    if (!pass) return;
    pass[field] = numberOrNull(value);
    refreshWorkbench({ refreshTimeline: false });
  };

  window.captureReferencePassTime = (index, passIndex, field) => {
    const pointLabel = field === "startTime" ? "悬停开始" : "移开";
    const interaction = touchReference(index, `撤回交互 ${index + 1} 第 ${passIndex + 1} 次${pointLabel}时间`);
    const pass = interaction?.hoverPasses?.[passIndex];
    if (!pass) return;
    const current = numberOrNull(studio.player?.currentTime?.() ?? studio.videoTime) ?? 0;
    pass[field] = current;
    pass.timeSource = "manual_current_frame";
    $("videoStatus").textContent = `撤回交互 ${index + 1} 第 ${passIndex + 1} 次：已将 F${timeToFrame(current)}（${timeText(current)}）标记为${pointLabel}`;
    refreshWorkbench({ refreshTimeline: false });
  };

  window.toggleReferenceShapeCell = (index, row, col) => {
    const interaction = touchReference(index, `撤回交互 ${index + 1} 的方块形状`);
    if (!interaction) return;
    interaction.shape ||= [];
    const at = interaction.shape.findIndex(cell => Number(cell.row) === row && Number(cell.col) === col);
    if (at >= 0) interaction.shape.splice(at, 1); else interaction.shape.push({ row, col });
    if (interaction.shape.length) {
      const minRow = Math.min(...interaction.shape.map(cell => Number(cell.row)));
      const minCol = Math.min(...interaction.shape.map(cell => Number(cell.col)));
      interaction.hoverTarget ||= { row: 0, col: 0 };
      interaction.hoverTarget.row += minRow;
      interaction.hoverTarget.col += minCol;
      interaction.shape = interaction.shape.map(cell => ({ row: Number(cell.row) - minRow, col: Number(cell.col) - minCol }));
    }
    refreshWorkbench({ refreshTimeline: false });
  };

  window.verifyReferenceInteraction = index => {
    const interaction = referenceInteractionDrafts[index];
    const issues = interaction ? referenceIssues(interaction) : ["记录不存在"];
    if (issues.length) {
      $("videoStatus").textContent = `撤回交互 ${index + 1}：${issues.join("；")}`;
      refreshWorkbench({ refreshTimeline: false });
      return;
    }
    recordAnnotationHistory(`确认撤回交互 ${index + 1}`);
    interaction.manuallyVerified = true;
    interaction.returnedToSource = true;
    interaction.boardMutation = false;
    interaction.clearExecuted = false;
    actionsConfirmed = false;
    $("fullReplayBtn").disabled = true;
    refreshWorkbench({ refreshTimeline: false });
  };

  window.deleteReferenceInteraction = index => {
    const interaction = referenceInteractionDrafts[index];
    if (!interaction || interaction.deleted) return;
    recordAnnotationHistory(`删除撤回交互 ${index + 1}`);
    interaction.deleted = true;
    actionsConfirmed = false;
    $("fullReplayBtn").disabled = true;
    refreshWorkbench({ refreshTimeline: false });
  };

  renderActionReview = renderAnnotationStudio;

  const sourceInput = $("sourceVideo");
  sourceInput.onchange = () => {
    const file = sourceInput.files[0];
    $("sourceVideoName").textContent = file ? file.name : "选择需要标注的视频";
  };

  const originalOpen = $("videoRebuildBtn").onclick;
  $("videoRebuildBtn").onclick = () => {
    studio.activeIndex = 0;
    studio.videoTime = 0;
    originalOpen();
    refreshIcons();
  };

  $("closeVideoDialog").onclick = () => $("videoDialog").close();
  $("rebuildOptions")?.classList.add("rebuild-options-band");
  document.addEventListener("pointerdown", event => {
    if (event.button !== 0) return;
    const button = event.target.closest?.(".frame-stepper button");
    if (button) beginHeldFrameNudge(button);
    const videoFrameButton = event.target.closest?.(".transport-button");
    if (videoFrameButton && videoFrameDirection(videoFrameButton)) beginHeldVideoFrame(videoFrameButton);
  });
  const stopHeldControls = () => {
    stopHeldFrameNudge();
    stopHeldVideoFrame();
  };
  window.addEventListener("pointerup", stopHeldControls);
  window.addEventListener("pointercancel", stopHeldControls);
  window.addEventListener("blur", stopHeldControls);
  document.addEventListener("click", event => {
    const button = event.target.closest?.(".frame-stepper button");
    if (button && button === studio.suppressFrameNudgeClick) {
      event.preventDefault();
      event.stopImmediatePropagation();
      studio.suppressFrameNudgeClick = null;
    }
    const videoFrameButton = event.target.closest?.(".transport-button");
    if (videoFrameButton && videoFrameButton === studio.suppressVideoFrameClick) {
      event.preventDefault();
      event.stopImmediatePropagation();
      studio.suppressVideoFrameClick = null;
    }
  }, true);
  refreshIcons();
})();

