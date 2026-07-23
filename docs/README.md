# Documentation

## Project Guides

- [视频动作识别优化实施计划](视频动作识别优化实施计划.md) - 识别瓶颈、优化方向和阶段性实施计划。
- [识别架构改造方案](识别架构改造方案.md) - 平台期全局复盘：托盘优先重构、测量修正与目标函数调整的方向与执行顺序。
- [验收标准](验收标准.md) - 视频变体重建的验收规则和质量门槛。
- [常规彩色方块识别优化实验记录](recognition/optimization-experiments.md) - 常规彩色路径的 baseline、单项 A/B 和组合实验记录。

## Suggested Documentation Structure

- `architecture/` - 系统架构、识别链路、前后端接口说明。
- `recognition/` - 棋盘识别、时序状态、误识别分析、benchmark 结论。
- `operations/` - 本地启动、GitHub 推送、数据目录维护、常见问题。
- `experiments/` - 临时实验记录和对比结论。实验输出本身不要提交到 Git。

新增文档优先放入上述目录；如果只是阶段性结论，先放 `experiments/`，稳定后再整理进正式文档。
