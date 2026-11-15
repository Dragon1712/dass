# VOC-LS E0 Standard Performance Benchmark v1

适配 `Dragon1712/dass/pathwyse`（PathWyse 1.0）。**无需 Linux 服务器访问 GitHub、无需服务器 Git 元数据**。

## 0. 已验证范围与限制

- 本实验包通过 Python 语法检查、模拟求解器端到端测试、C++ 补丁锚点与恢复测试。
- **尚未在用户真实 PathWyse 1.0 源码上完成编译验证**，请先在服务器运行 `--dry-run`、重新编译及 Toy smoke。
- 官方 `toy.txt` 与仓库中唯一 `A-n54-k7-149.sppcc` 只作为 `smoke`；来自**一个原始问题**的变化不能构成独立测试集。
- `make_dataset.py` 产生的是可重复的工程合成数据（不是正式真实 benchmark），用于测试统计流水线、训练原型与跨规模诊断。论文主实验必须补充更多独立的真实 SPPRC/ESPPRC 实例。
- 任何在测试集上进行的筛选/调参都会破坏 held-out 语义；`--final-test` 必须在模型、阈值、配置锁定后才使用。

## 1. 目录与前置条件

假设：

```text
~/code/dass/
  pathwyse/                # 真实 PathWyse 项目根目录，含 pathwyse.set 和 src/
  voc_ls_e0_standard_v1/  # 本包
```

要求 Linux、Python 3.10+、GCC C++20、CMake 3.20+。无需额外 Python 包。

## 2. 安装诊断补丁（**会修改 3 个源码文件**）

**先检查，没有任何修改：**

```bash
cd ~/code/dass/voc_ls_e0_standard_v1
python3 patch_pathwyse.py --repo ~/code/dass/pathwyse --dry-run
```

真正安装：

```bash
python3 patch_pathwyse.py --repo ~/code/dass/pathwyse
cmake -S ~/code/dass/pathwyse -B ~/code/dass/pathwyse/build -DCMAKE_BUILD_TYPE=Release
cmake --build ~/code/dass/pathwyse/build -j 8
```

**验证原始 Toy：**

```bash
cd ~/code/dass/pathwyse
./bin/pathwyse instances/toy.txt
# 必须返回 Solution Status: Optimal，Obj: -10435
```

补丁在每个被修改的原始文件旁保存 `.e0_original` 备份。撤销：

```bash
python3 ~/code/dass/voc_ls_e0_standard_v1/patch_pathwyse.py \
  --repo ~/code/dass/pathwyse --restore
cmake --build ~/code/dass/pathwyse/build -j 8
```

原生源文件修改：

- `LM_default.h`：线程安全的支配检查、支配探针时间、未来推理统计接口。
- `LM_default.cpp`：`dominates()` 每调用一次计数；用 `std::atomic` 分开记前后方向；逐轮写 `PWDefault_LM.csv`；计时只在 `VOC_E0_PROFILE=1` 时启动。
- `PW_default.cpp`：每次 DSSR 轮次开始重置探针；**原本已有的** `it_ext_fw`, `it_ext_bw` 不重复添加。

### 字段的严谨定义

| 字段 | 口径 | 来源 |
|---|---|---|
| `wall_s / wall_ns` | 从创建进程到退出，包括加载实例、预处理、求解、输出 | Python `perf_counter_ns`, 阻塞式 `waitpid` + 独立超时监控 |
| `solver_s` | PathWyse `PWDefault Time` 内部计时，与总进程墙钟时间不等同 | PathWyse 日志 |
| `extensions_fw / extensions_bw` | 一次 DSSR 轮中从队列取出并扩展的标签数 | PathWyse `it_ext_fw`, `it_ext_bw` |
| `dom_checks_fw / dom_checks_bw` | `LMDefault::dominates(l1,l2)` 被调用的次数，包含失败比较和成功支配，**不是被支配标签数** | E0 探针 |
| `dom_probe_time_ns` | 逐次 `dominatesCore` 测时的累积值，包含探针时钟开销，仅供诊断，**不能视为无探针的真实成本** | E0 探针 |
| `ndominated` | PathWyse 认为被支配并移除的标签数，与 `dom_checks` 不同 | PathWyse LM CSV |
| `inference_calls / inference_time_ns` | B0 没有学习模型，必须为 **0 / 0**。E1 将在实际模型调用处显式接入 `recordE0InferenceNs` | E0 预留接口 |

**特别注意**：代码用 `VOC_E0_PROFILE=0` 运行性能计时、用 `VOC_E0_PROFILE=1` 运行独立诊断。正式墙钟耗时不能与带探针的 `dom_probe_time_ns` 相加。每轮扩展数与支配次数可以逐轮求和，不能把最后一轮值当成全程值。

## 3. 划分数据集

```bash
cd ~/code/dass/voc_ls_e0_standard_v1
python3 make_dataset.py \
  --repo ~/code/dass/pathwyse \
  --out ~/code/dass/e0_standard_data/manifest.json \
  --synthetic-count 60 \
  --ood-count 12 \
  --seed 20261115
```

输出：60 个独立随机图（按图规模分层 60/20/20 → 36 train / 12 val / 12 test），加 12 个未见规模 n=24 的 OOD 图，另加官方 Toy 和 SPPRCLIB smoke。划分以**独立图 ID**为单位，无随机拆分同源查询。图的节点数主要在 10/14/18，OOD 为 24。

### 补充更多真实实例（论文必需）

把真实 `.sppcc` 文件通过 Windows 同步到 Linux 后：

```bash
python3 make_dataset.py \
  --repo ~/code/dass/pathwyse \
  --out ~/code/dass/e0_standard_data/manifest.json \
  --synthetic-count 60 --ood-count 12 \
  --real-dir ~/code/dass/datasets/spprclib_real
```

如果一个路网存在多个不同查询，建议用 `--real-groups path/to/groups.json` 指定相同源图的统一 `group` ID（JSON 对象，键为绝对路径或文件名，值为 graph_id），防止泄漏。如果独立真实图组不足 5 个，所有这些真实实例都只进入 `smoke`，不构成正式 train/val/test。

`manifest.json` 中每个实例含 `id`、`split`、`group`、`source`、`path`、`sha256`；测试集实际数据必须冻结。

## 4. 首次标准实验（冒烟 + 仪器验证）

```bash
cd ~/code/dass/voc_ls_e0_standard_v1
python3 selftest.py

python3 -u e0_standard_runner.py \
  --repo ~/code/dass/pathwyse \
  --manifest ~/code/dass/e0_standard_data/manifest.json \
  --out ~/code/dass/e0_standard_smoke \
  --splits smoke --profiles B0_R,B0_Strong \
  --phase both --timing-repeats 5 --diagnostic-repeats 1 \
  --warmups 1 --timeout 300 \
  2>&1 | tee ~/code/dass/e0_standard_smoke.log

python3 e0_aggregate.py --out ~/code/dass/e0_standard_smoke
```

`--phase both` 先无探针正式计时，再独立开探针收集诊断指标。输出配置对应：

- `B0_R`：`PWDefault + bidirectional + parallel=0 + DSSR restricted + autoconfig=0`。
- `B0_Strong`：`PWDefault + bidirectional + parallel=1 + DSSR standard + autoconfig=0`。
- 二者仅并行与 DSSR 两项不同（代码自动验证）。

第一次请确认 Toy 为 `Optimal/-10435`，SPPRCLIB 为 `Optimal/-12492`，诊断中 `dom_checks_total > 0`，并且 `inference_calls = 0`。

## 5. 工程合成数据实验（非论文真实泛化结果）

先执行 train + val（例：先限制 12 例审查运行成本）：

```bash
python3 -u e0_standard_runner.py \
  --repo ~/code/dass/pathwyse \
  --manifest ~/code/dass/e0_standard_data/manifest.json \
  --out ~/code/dass/e0_standard_dev \
  --splits train,val --limit 12 \
  --phase both --timing-repeats 3 --diagnostic-repeats 1 \
  --warmups 1 --timeout 300
```

若全部正确，再移除 `--limit`、启用更多重复次数，使用全新的 `--out` 路径，或者在同一配置中使用 `--resume`。固定 CPU 可加 `--cpus 2,3`，**必须先用** `taskset -pc $$` **确认这些核心在允许集合中**；并行模式建议两个不同物理核心。

正式独立测试仅在冻结设置后执行：

```bash
python3 -u e0_standard_runner.py \
  --repo ~/code/dass/pathwyse \
  --manifest ~/code/dass/e0_standard_data/manifest.json \
  --out ~/code/dass/e0_standard_heldout \
  --splits test,ood --final-test \
  --phase both --timing-repeats 5 --diagnostic-repeats 1 \
  --warmups 1 --timeout 300
```

## 6. 标准输出

```text
e0_standard_smoke/
  metadata.json                 # 源码、配置、二进制和 manifest SHA256
  manifest_snapshot.json        # 冻结的实例/图组划分清单
  configs/                      # 固定的 timing/diagnostic 配置
  timing.csv                    # 每次无探针进程墙钟时间、内部 solver 时间、最优值
  diagnostics_iterations.csv    # 每个 DSSR 轮：扩展、支配次数、推理字段、插入/支配标签
  diagnostics_totals.csv        # 逐轮求和（不是只取末轮）
  summary_per_instance.csv      # 按实例汇总 5 次重复的均值/中位数/p95
  aggregate_by_split.json       # 按独立图组而非查询/运行次数计算性能分布/Bootstrap CI
  logs/
  diagnostic_work/              # 独立 cwd 保留 PathWyse 原生 PWDefault*.csv
```

## 7. 正式论文实验的验收与注意事项

1. **精确性**：所有实验求解器返回 `Optimal`、超时 0、可行路径与资源约束一致；Toy 必须匹配独立已知解，其他实例的 Optimal 标识需在论文实验中进一步与可复核真值对照。
2. **可比性**：相同源码树、二进制、算法参数、实例 SHA256、预热规则、CPU 配置、timeout、运行机器；E1 的 B0/E1 必须固定 DSSR/并行模式，仅改变候选评分。
3. **开销分离**：绝对性能用 `timing.csv`；搜索机制用 `diagnostics_*.csv`。诊断时间会受到计数器插入、计时器调用和磁盘写入干扰。
4. **样本独立性**：Bootstrapping 应以独立图组为单位；同一图的 5 次重复不能算 5 个统计样本。同源图多个查询在 `e0_aggregate.py` 中先按 `group` 合并，再进行 CI。
5. **E1 推理占位**：B0 没有模型，所以 `inference_calls=0` 与 `inference_time_ns=0` 是语义正确的输出，不代表测得“模型零开销”。接入 E1 时在模型真正执行的位置测量并调用 `LMDefault::recordE0InferenceNs(...)`；如果要求无探针正式运行也分解推理耗时，需给 E1 额外设置轻量级求解级计时器并在查询结束时输出。
6. **风险**：未实机 C++ 编译验证；如果编译或数据解析失败，请将 `cmake --build` 的完整报错或对应实例 `.log` 提供用于对齐；不要把模拟 selftest 当作真实求解验证。

### 文件变更与恢复

源码补丁修改 `LM_default.h/.cpp`、`PW_default.cpp` 共 3 个文件。补丁支持重复调用与 `--restore`，会在服务器原文件旁生成 `.e0_original` 备份。建议在 Windows 本地先保留已有版本，再同步修订后的三个文件回本地 Git 管理。不要把编译目录和逐次长日志直接加入论文代码仓库。
