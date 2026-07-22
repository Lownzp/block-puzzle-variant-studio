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
