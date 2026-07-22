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
