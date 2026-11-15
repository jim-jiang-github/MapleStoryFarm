# ScreenTrackerApp — 测谎仪目标追踪交付包

旋转跟踪方案的完整整合：训练权重 + 识别追踪程序 + 评估/训练工具 + 验收报告。
整个文件夹**自包含**，主程序可直接运行（不依赖项目其他目录）。

## 🚀 快速使用

双击 **`Run_ScreenTracker.bat`**：

1. 弹出全屏截图 → 拖拽框选追踪区域（Enter/空格确认，C 取消）
2. 把要追踪的内容（如测谎仪游戏窗口）放进框内
3. 鼠标自动平滑跟随识别到的目标（白团 / 隐身漩涡）

| 键 | 功能 |
|---|---|
| F12 | 全局退出（任何阶段） |
| F2 | 追踪中重新框选区域 |

运行环境：Python 3 + `ultralytics` `opencv-python` `numpy` `mss`（CUDA 可选，CPU 也可跑）。

## 📁 目录结构

```
ScreenTrackerApp/
├── best_ft3.pt              # ⭐ 最终模型权重 (YOLO11n, 3类, 5.5MB)
├── screen_tracker.py        # ⭐ 主程序: 框选区域→识别→鼠标跟随
├── track_core.py            # ⭐ YoloTracker 匀速追踪器 (门控+滑行+超前瞄准)
├── Run_ScreenTracker.bat    # ⭐ 双击启动器
│
├── eval/                    # 评估与数据工具 (在项目根目录运行, 依赖游戏/数据集)
│   ├── rot_eval_unity.py    #   正式验收: 实时闭环 1000+ 次隐身验证 ≥95%
│   ├── mouse_eval.py        #   鼠标闭环测试: N 局游戏自身判定成功率
│   ├── build_dets_cache.py  #   检测缓存构建 (新权重秒级参数扫描)
│   ├── sweep_track_cache.py #   追踪器参数网格扫描
│   ├── collect_dataset.py   #   窗口截图/数据采集工具
│   └── record_rounds.py     #   游戏遥测 TCP 客户端
│
├── training/                # Kaggle 训练 notebook (T4 GPU)
│   ├── kaggle_train_new.ipynb    # 第 1 阶段: 新画面重训 n2 (80 epochs)
│   └── kaggle_train_ft3.ipynb    # 第 2 阶段: 实时域 finetune ft3 (40 epochs)
│
└── reports/                 # 结果报告
    ├── accept_stats.png     #   正式验收统计 (25 局, hidden 97.4%)
    ├── mouse_eval_summary.png   # 鼠标闭环 200 局测试汇总
    └── 测谎仪鼠标闭环测试报告.xlsx  # 逐局明细 + 汇总 KPI (游戏判定 100%)
```

## 🧠 技术方案

- **检测**：YOLO11n，3 类（white_shape 可见白团 / vortex 隐身漩涡 / bg_shape 背景图形）。
  隐身期目标是零对比度的纸纹扭曲漩涡，靠 CNN 从带遥测真值的隐身帧学出来。
- **训练路线**（Kaggle T4）：
  1. 新画面生成数据 10138 帧（隐身帧全带真值）→ 重训 n2（vortex mAP50 0.92）
  2. 实时域采集 44739 帧（29.8fps 带逐帧遥测）→ finetune ft3（vortex R 0.963 / mAP50 0.985）
  - 域差（录制域 vs 实时域）是主要敌人，第 2 阶段是命中率 77%→98.9% 的关键
- **追踪**：`track_core.py` 匀速追踪器 — 运动门控（门外检测=误检）、滑行惯性（漏检期匀速外推）、
  超前瞄准（det+vel）、新局中心重捕、FP 闪现过滤
- **鼠标**：150Hz 后台线程 + 临界阻尼弹簧插值，光标轨迹连续平滑如人手

## 📊 验收结果（2026-11-15）

| 测试 | 规模 | 结果 |
|---|---|---|
| 正式验收（验收标准：≥1000 次隐身验证，命中率 ≥95%） | 25 局实时闭环 | **hidden 2466 次，命中率 97.4%**（中位 6.7px）✅ |
| 鼠标闭环（真实鼠标 + 游戏自身悬停判定） | 108 局 | **游戏判定 108/108 全 success (100%)**，帧级追踪 91.6% |

模型产物：`kaggle_out/ft3v6/best_ft3.pt`（即本包 `best_ft3.pt`）。
