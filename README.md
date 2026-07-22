# Block Puzzle Variant Studio

本项目是一个本地运行的方块消除/拼图类素材变体工作台，覆盖样本视频识别、动作标注、确定性回放、素材替换和批量变体视频生成。

## Directory Layout

- `variant_bridge.py` - 本地 HTTP bridge 服务入口。
- `timeline_analyzer.py` - 视频棋盘、方块和动作时序识别核心。
- `index.html`, `annotation-studio.*` - 本地浏览器 UI。
- `models/` - 轻量识别模型和参数文件。
- `vendor/` - 离线前端依赖，包含 Bootstrap、Video.js、vis-timeline、Cropper.js、Lucide。
- `scripts/` - 训练、评测、回放和诊断脚本。
- `tests/` - 单元测试与视觉识别回归测试。
- `docs/` - 项目文档、验收标准、识别优化计划和调研记录。
- `vendor_packages/` - 前端依赖下载/锁定用的包清单。

运行本地服务：

```powershell
.\启动变体生成器.cmd
```

基础验证：

```powershell
python -m py_compile variant_bridge.py timeline_analyzer.py recording_finalizer.py scripts/reanalyze_truth_set.py
python -m unittest discover -s tests -q
```
