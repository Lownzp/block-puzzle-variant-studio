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
