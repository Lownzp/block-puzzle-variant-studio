# 常规彩色方块识别优化实验记录

## Baseline

测试日期：2026-07-22  
测试范围：常规彩色方块 8 条样本（DEV-001、DEV-002、DEV-003、DEV-004、DEV-006、DEV-008、DEV-011、DEV-012）  
默认路径：`color_block_v1`，页面同款 `analyse_video` 路径，常规彩色默认 12fps 采样。

| 指标 | 当前值 |
|---|---:|
| predicted | 156 |
| truth | 167 |
| matched | 149 |
| false positive | 7 |
| missed | 18 |
| precision | 95.51% |
| recall | 89.22% |
| slot accuracy | 92.62% |
| shape accuracy | 84.56% |
| target accuracy | 91.28% |
| clear accuracy | 87.25% |
| time MAE | 0.0977s |
| within two frames | 77.18% |

## Experiment Template

## Runner Usage

从项目根目录运行：

```powershell
python scripts/run_recognition_experiments.py benchmark_experiments/color_block_YYYYMMDD `
  --only DEV-001 DEV-002 DEV-003 DEV-004 DEV-006 DEV-008 DEV-011 DEV-012 `
  --single-flags temporal_candidate_cache stable_state_scoring fsm_event_constraints cell_temporal_voting single_frame_shadow_refine `
  --combined temporal_candidate_cache stable_state_scoring
```

输出目录会包含：

- `baseline/`：默认路径预测结果
- `<flag>/`：单项优化预测结果
- `combined/`：组合优化预测结果
- `summary.json`：机器可读汇总
- `summary.md`：人工阅读汇总

优化项进入默认路径前，必须把 `summary.md` 的关键结论追加到本文件对应实验小节。

### EXP-YYYYMMDD-NN: 优化项名称

- Commit:
- 测试集:
- 启用开关:
- 对照基线:
- 是否进入默认路径:

| 指标 | baseline | experiment | delta |
|---|---:|---:|---:|
| predicted |  |  |  |
| truth |  |  |  |
| matched |  |  |  |
| false positive |  |  |  |
| missed |  |  |  |
| precision |  |  |  |
| recall |  |  |  |
| slot accuracy |  |  |  |
| shape accuracy |  |  |  |
| target accuracy |  |  |  |
| clear accuracy |  |  |  |
| time MAE |  |  |  |
| within two frames |  |  |  |

变好样本：

- 

变差样本：

- 

结论：

- 

### EXP-20260722-01: 多帧候选缓存与单格时间投票

- Commit: 本提交
- 测试集: DEV-004、DEV-008
- 启用开关: `temporal_candidate_cache`、`cell_temporal_voting`
- 对照基线: `benchmark_experiments/color_probe_20260722/summary.md`
- 是否进入默认路径: 否。`temporal_candidate_cache` 仅保留为诊断数据；`cell_temporal_voting` 为负向优化。

| 指标 | baseline | temporal_candidate_cache | cell_temporal_voting | combined |
|---|---:|---:|---:|---:|
| predicted | 45 | 45 | 51 | 51 |
| truth | 52 | 52 | 52 | 52 |
| matched | 44 | 44 | 43 | 43 |
| false positive | 1 | 1 | 8 | 8 |
| missed | 8 | 8 | 9 | 9 |
| precision | 97.78% | 97.78% | 84.31% | 84.31% |
| recall | 84.62% | 84.62% | 82.69% | 82.69% |
| slot accuracy | 93.18% | 93.18% | 93.02% | 93.02% |
| shape accuracy | 81.82% | 81.82% | 79.07% | 79.07% |
| target accuracy | 90.91% | 90.91% | 86.05% | 86.05% |
| clear accuracy | 81.82% | 81.82% | 81.40% | 81.40% |
| time MAE | 0.1261s | 0.1261s | 0.1856s | 0.1856s |
| within two frames | 86.36% | 86.36% | 76.75% | 76.75% |

变好样本：

- 无整体变好样本。DEV-008 召回从 63.64% 小幅升到 68.18%，但 precision、target、time 都明显变差，不能接受。

变差样本：

- DEV-004: `cell_temporal_voting` 从 30/30 全匹配变为 28/30，新增 3 个 false positive。
- DEV-008: `cell_temporal_voting` false positive 从 1 增加到 5，target accuracy 从 71.43% 降到 60.00%。

结论：

- 单格时间投票会把拖动中短暂出现的半贴合/阴影/残影补成“稳定占用”，增加假动作，不进入默认路径。
- 候选缓存不改变结果，可继续作为调试数据基础，用于后续稳定态质量评分和误识别定位。

### EXP-20260722-02: FSM 来源槽位硬约束

- Commit: 本提交
- 测试集: DEV-004、DEV-008
- 启用开关: `fsm_event_constraints`，以及 `cell_temporal_voting + fsm_event_constraints`
- 对照基线: `benchmark_experiments/color_probe_fsm_20260722/summary.md`
- 是否进入默认路径: 否。这版 FSM 条件过强，只作为失败实验保留。

| 指标 | baseline | fsm_event_constraints | combined |
|---|---:|---:|---:|
| predicted | 45 | 33 | 34 |
| truth | 52 | 52 | 52 |
| matched | 44 | 32 | 32 |
| false positive | 1 | 1 | 2 |
| missed | 8 | 20 | 20 |
| precision | 97.78% | 96.97% | 94.12% |
| recall | 84.62% | 61.54% | 61.54% |
| slot accuracy | 93.18% | 96.88% | 96.87% |
| shape accuracy | 81.82% | 87.50% | 81.25% |
| target accuracy | 90.91% | 93.75% | 90.62% |
| clear accuracy | 81.82% | 84.37% | 84.37% |
| time MAE | 0.1261s | 0.0516s | 0.0885s |
| within two frames | 86.36% | 93.75% | 84.38% |

变好样本：

- DEV-004 的 precision 保持 100%，但 recall 从 100% 降到 90%，不能视为有效优化。

变差样本：

- DEV-008: `fsm_event_constraints` 预测动作从 15 个降到 6 个，recall 从 63.64% 降到 22.73%。

结论：

- 当前真实动作里仍有较多 `sourceSlot = -1` 或来源槽位证据不足的情况，硬性过滤会把真实动作一起删掉。
- 下一步不能先靠事件级硬过滤兜底，应优先加强“放置后棋盘状态”和“动作候选证据”的质量评分，再用 FSM 做软评分或冲突复核。

### EXP-20260722-03: 稳定态评分与单帧阴影 refine

- Commit: 本提交
- 测试集: DEV-004、DEV-008
- 启用开关: `stable_state_scoring`、`single_frame_shadow_refine`，以及二者组合
- 对照基线: `benchmark_experiments/color_probe_remaining_20260722/summary.md`
- 是否进入默认路径: 否。`stable_state_scoring` 为负向优化；`single_frame_shadow_refine` 在本测试集无变化。

| 指标 | baseline | stable_state_scoring | single_frame_shadow_refine | combined |
|---|---:|---:|---:|---:|
| predicted | 45 | 36 | 45 | 36 |
| truth | 52 | 52 | 52 | 52 |
| matched | 44 | 34 | 44 | 34 |
| false positive | 1 | 2 | 1 | 2 |
| missed | 8 | 18 | 8 | 18 |
| precision | 97.78% | 94.44% | 97.78% | 94.44% |
| recall | 84.62% | 65.38% | 84.62% | 65.38% |
| slot accuracy | 93.18% | 94.12% | 93.18% | 94.12% |
| shape accuracy | 81.82% | 79.41% | 81.82% | 79.41% |
| target accuracy | 90.91% | 88.24% | 90.91% | 88.24% |
| clear accuracy | 81.82% | 82.36% | 81.82% | 82.36% |
| time MAE | 0.1261s | 0.2103s | 0.1261s | 0.2103s |
| within two frames | 86.36% | 70.59% | 86.36% | 70.59% |

变好样本：

- 无整体变好样本。`single_frame_shadow_refine` 与 baseline 完全一致。

变差样本：

- DEV-004: `stable_state_scoring` 从 30/30 全匹配降到 24/30，漏检 6 个。
- DEV-008: `stable_state_scoring` 从 matched 14 降到 10，missed 从 8 增加到 12。

结论：

- 当前稳定态评分阈值会把真实短动作一起过滤掉，不能进入默认路径。
- 单帧阴影 refine 在 DEV-004/DEV-008 上没有触发有效差异，说明这两个样本里的主问题不是单帧 `_cell_shadow_like` 阈值，而是动作候选和放置后状态解释。
- 后续优化方向应从“稳定态硬过滤”改为“候选保留 + 质量打分 + 人工界面展示分歧”，避免召回率继续下降。

### EXP-20260723-01: clear 序列修复最小闭环

- Commit: 本提交
- 测试集: DEV-004、DEV-008
- 启用开关: `sequence_repair_clear_v1`
- 对照基线: `benchmark_experiments/color_probe_repair_clear_guarded_20260723/summary.md`
- 是否进入默认路径: 否。当前保护版不改变结果；未保护版会误修 DEV-004 的 clear。

| 指标 | baseline | sequence_repair_clear_v1 | delta |
|---|---:|---:|---:|
| predicted | 45 | 45 | 0 |
| truth | 52 | 52 | 0 |
| matched | 45 | 45 | 0 |
| false positive | 0 | 0 | 0 |
| missed | 7 | 7 | 0 |
| precision | 100.00% | 100.00% | +0.00pp |
| recall | 86.54% | 86.54% | +0.00pp |
| semanticActionAccuracy | 77.78% | 77.78% | +0.00pp |
| stateEquivalentRate | 86.67% | 86.67% | +0.00pp |
| shapeAccuracy | 86.67% | 86.67% | +0.00pp |
| targetAccuracy | 95.56% | 95.56% | +0.00pp |

变好样本：

- 无。保护版在 DEV-004、DEV-008 上没有触发自动修复。

变差样本：

- 保护版无变差。
- 未保护试跑中，DEV-008 第 3 步 clear 可被修对，但 DEV-004 第 19、25 步会被误修为 clear on，导致 semantic 从 77.78% 降到 73.34%。因此不能只凭“后续棋盘更像”接受 clear 修复。

结论：

- clear 修复必须依赖更强的原始证据，不能只依赖 replay 距离；否则后续动作、候选分段或识别状态偏差会把真实 `off` 误改成 `on`。
- 当前版本保留为实验开关和基础设施，不进入默认路径。
- 下一步应优先做 shape/target 候选修复，clear 仅作为候选维度参与全局评分，而不是单独自动改。

### EXP-20260723-02: shape/target 单步候选修复

- Commit: 本提交
- 测试集: DEV-004、DEV-008
- 启用开关: `sequence_repair_shape_target_v1`
- 对照基线: `benchmark_experiments/color_probe_repair_shape_target_20260723/summary.md`
- 是否进入默认路径: 否。当前版本没有触发有效修复，指标不变。

| 指标 | baseline | sequence_repair_shape_target_v1 | delta |
|---|---:|---:|---:|
| predicted | 45 | 45 | 0 |
| truth | 52 | 52 | 0 |
| matched | 45 | 45 | 0 |
| false positive | 0 | 0 | 0 |
| missed | 7 | 7 | 0 |
| precision | 100.00% | 100.00% | +0.00pp |
| recall | 86.54% | 86.54% | +0.00pp |
| semanticActionAccuracy | 77.78% | 77.78% | +0.00pp |
| stateEquivalentRate | 86.67% | 86.67% | +0.00pp |
| shapeAccuracy | 86.67% | 86.67% | +0.00pp |
| targetAccuracy | 95.56% | 95.56% | +0.00pp |

变好样本：

- 无。

变差样本：

- 无。

结论：

- 仅依赖单步 `repairHints` 中的棋盘差分 shape/target 候选，不能覆盖 DEV-008 的主要错误。
- 对齐检查显示，DEV-008 多个 shape 错误实际上与动作分段/漏动作/后续配对偏移相关；当前动作的棋盘差分经常是局部状态解释，不等于真实来源槽位 shape。
- 下一步应转向“候选序列补漏与重评分”：在相邻动作窗口内允许插入/合并候选动作，再用全局 replay 评分，而不是只修单步 shape。

### EXP-20260723-03: 候选序列补漏实验

- Commit: 本提交
- 测试集: DEV-004、DEV-008
- 启用开关: `sequence_candidate_gap_fill_v1`
- 对照基线: `benchmark_experiments/color_probe_gap_fill_20260723/summary.md`
- 是否进入默认路径: 否。当前策略会增加 false positive，不能自动启用。

| 指标 | baseline | sequence_candidate_gap_fill_v1 | delta |
|---|---:|---:|---:|
| precision | 100.00% | 95.74% | -4.26pp |
| recall | 86.54% | 86.54% | +0.00pp |
| semanticActionAccuracy | 77.78% | 77.78% | +0.00pp |
| stateEquivalentRate | 86.67% | 86.67% | +0.00pp |
| shapeAccuracy | 86.67% | 86.67% | +0.00pp |
| targetAccuracy | 95.56% | 95.56% | +0.00pp |
| false positive | 0 | 2 | +2 |
| missed | 7 | 7 | 0 |

变好样本：
- 无。DEV-004 无插入候选；DEV-008 插入 2 个候选，但未匹配真值。

变差样本：
- DEV-008: 额外插入 2 个 `autoInserted` 候选，分别来自未覆盖的纯新增稳定态转换。两者 `sourceSlot = -1`，槽位置信度为 0.2，评测判定为 false positive。

结论：
- 只看“稳定态之间出现纯新增格子”不足以判断真实动作，容易把局部中间态或非动作状态当成补漏动作。
- 序列补漏不能先插入动作再期待评测修正；必须先具备来源槽位证据、手部/拖拽轨迹证据或与前后动作一致的全局 replay 改善。
- 该实验保留为诊断开关，后续方向应改为“候选重评分”: 对低置信动作先生成 slot/shape/target/clear 的局部候选，然后用前后稳定态 replay、槽位消耗一致性和时间证据联合排序；只有高置信候选才自动替换，其余继续进入人工复核。
