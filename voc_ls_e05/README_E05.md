# VOC-LS E0.5: PathWyse 运行时间归因实验

研究仓库：`Dragon1712/dass`。实验依赖服务器已有的 `~/code/dass/pathwyse`（或 `pathwyse-main`）源码，不调用 GitHub，也**不修改 PathWyse 源码**。

## 1. E0 已知现象与 E0.5 研究目的

2026-11-15 的 E0 数据（`e0_results/runs.csv`）显示：在 `A-n54-k7-149.sppcc` 上，原始配置 Stock 中位数约 **0.665767 s**，单线程受控配置 Controlled 中位数约 **1.366884 s**，后者耗时约为前者的 **2.053 倍**。目标值两者相同：`-12492`。这一时间差并不能直接归因于并行，因为之前还同时改变了 DSSR、自动配置、数据收集和输出冗长度。

E0.5 使用 **2×2 因子实验（双向并行 × DSSR 类型）**，另外加上自动配置与统计开销的单因素对照。目标是解释性能差异、建立强基线，避免以后 VOC-LS 只胜过一个人为变慢的基线。

## 2. 执行组别

四个主组共享设置：`PWDefault`、`autoconfig=0`、`candidate/type=node`、`use_visited=1`、`compare_unreachables=1`、`two_cycle_elimination=1`、`ng=off`、`search=bidirectional`、`algo_parallel=0`、`verbosity=1`、`data_collection/level=-1`、`output/write=0`、无限求解器内部时间限制。所有组运行相同的已编译二进制。

| profile | `algo/default/parallel` | `algo/default/dssr` | 用途 |
|---|---:|---|---|
| `p0_r` | 0 | restricted | 串行 + DSSR-R，严格比较基础组 |
| `p1_r` | 1 | restricted | 只改变方向并行 |
| `p0_s` | 0 | standard | 只改变 DSSR 策略 |
| `p1_s` | 1 | standard | 两者共同变化，估计交互影响 |

默认还有三个辅助组：

| profile | 相比哪个组 | 只额外变化的事项 | 用途 |
|---|---|---|---|
| `p1_s_auto` | `p1_s` | `autoconfig=1` | 检验自动配置影响 |
| `p0_r_collect` | `p0_r` | `data_collection/level=0` | 检验基础统计开销（其他条件相同） |
| `stock` | 原始 `pathwyse.set` | 完全照抄原始文件 | 与 E0 Stock 锚定，**不是**因果对照 |

`stock` 与 `p1_s_auto` 之间仍有 `verbosity`、`algo_names` 等差异，所以该比较只能作为剩余差异诊断，不能把差值归因于某一个因素。

**注意：** `algo_parallel` 是*多个算法*是否并行；这里的 `algo/default/parallel` 才控制 PWDefault 的正、反向搜索线程。设置 `OMP_NUM_THREADS=1` 不能关闭 C++ 的 `std::thread`。

## 3. 在离线 Linux 服务器执行

将本目录 `voc_ls_e05` 拷贝到 `~/code/dass/`，确认目录中有 `e05_run.py`、`e05_analyze.py`。

### 完整实验：7 组 × 10 轮 = 70 次正式计时（每个输入实例）

```bash
cd ~/code/dass/voc_ls_e05
set -o pipefail
python3 -u e05_run.py \
  --repo ~/code/dass/pathwyse \
  --out ~/code/dass/e05_results \
  --repeats 10 \
  --warmups 2 \
  --timeout 300 \
  --jobs 8 \
  2>&1 | tee ~/code/dass/e05_run.log

python3 e05_analyze.py --out ~/code/dass/e05_results | tee ~/code/dass/e05_analysis.log
```

本实验配置默认以 `instances/spprclib/A-n54-k7-149.sppcc` 为性能测量实例。Toy 不足以用于性能比较（E0 约 4ms，进程启动开销占主导），不过可以额外通过 `--instance instances/toy.txt` 做正确性测试。如果有更多 SPPRCLIB 数据，用多次 `--instance` 显式指定；每个实例**单独**计算归因，再跨实例汇总。

### 简短先导实验：仅四个主组 × 3 轮

```bash
python3 e05_run.py --repo ~/code/dass/pathwyse \
  --out ~/code/dass/e05_pilot \
  --repeats 3 --warmups 1 --no-extras --timeout 300
python3 e05_analyze.py --out ~/code/dass/e05_pilot
```

如已编译成功，可以加 `--skip-build`。脚本会拒绝将新测量追加到已有 `measurements.csv`，建议不同批次使用新的 `--out`。若刻意覆盖，使用 `--force`；但这会覆盖主 CSV，不建议用于论文实验。

### 可选：固定 CPU 亲和性

```bash
lscpu -e=CPU,CORE,SOCKET,NODE
```

从当前可用的 CPU 中选择至少两个**不同的物理 CORE**，如 `--cpus 2,4`（示例，不要不经检查直接使用）。同一套 CPU 亲和性应用于所有组；串行组只运行一个搜索线程，并行组可运行两个搜索线程。不能给并行组仅分配一个 CPU，否则会人为压低其优势。不设 `--cpus` 则保持系统默认亲和性，同时在元数据中记录信息。

为减弱顺序、热机、CPU 频率等扰动，脚本：每组先预热，正式测量按轮执行，并用固定随机种子打乱每轮各配置顺序；比较时按轮做配对差。请尽量避免同一服务器上同时进行大规模 GPU/CPU 数据加载和其他并行求解任务。

## 4. 诊断统计单独运行，不计入性能表

默认每个主组额外执行 1 次 `verbosity=3`、`data_collection/level=0`、`output/write=1` 诊断。PathWyse 将每轮 DSSR 统计写入 `diagnostic_cwd/.../output/.../PWDefault.csv`，脚本汇总到 `diagnostics.json`。

诊断应区分：

- `per_iteration_records`：DSSR 每轮记录数；
- `total_extensions_across_iterations`：所有记录的 `it_ext_fw + it_ext_bw` 之和；
- `last_iteration_extensions`：最后一轮扩展数；
- `iterations`：包含单轮扩展与计时信息的 CSV 记录；
- `lm_records`：如产生 `PWDefault_LM.csv`，包含标签池统计。

这些诊断运行包含额外的日志与磁盘 I/O，因此 **`wall_s_DONT_USE_AS_BENCHMARK` 不能与正式计时混用**。`--no-diagnostics` 可跳过。

## 5. 统计方法及解释

`e05_analyze.py` 将同一个 round 内的配置测量作为配对数据；正的时间差表示**第二种配置更慢**，负值表示更快。

- **并行效应 / DSSR-R：** `T(p1_r) - T(p0_r)`
- **并行效应 / standard：** `T(p1_s) - T(p0_s)`
- **DSSR 效应 / 串行：** `T(p0_s) - T(p0_r)`
- **DSSR 效应 / 并行：** `T(p1_s) - T(p1_r)`
- **交互作用（差中差）：** `[T(p1_s)-T(p0_s)] - [T(p1_r)-T(p0_r)]`
- **自动配置效应：** `T(p1_s_auto) - T(p1_s)`
- **统计开销：** `T(p0_r_collect) - T(p0_r)`
- **Stock 残余：** `T(stock) - T(p1_s_auto)`（剩余多项因素，不能单独归因）

同时输出四组的平均、中位数、样本标准差、CV%、范围及配对差的 bootstrap 95% CI。CI 是相同实例的重复运行波动，**不能**当成跨实例推广的置信区间。最终论文应在多实例上以实例为统计单位再分析。

## 6. 通过标准与下一步决策

1. 所有主组返回 `Solution Status: Optimal`；未超时；同一实例全部组的目标值精确一致（脚本自动核验）。
2. 同一实例至少 10 次正式测量，报告每组 CV%。若大于约 10%，检查服务器 CPU 负载、亲和性、频率，再交错重跑；这只是诊断门槛，不是统计定理。
3. 归因时必须同时报告并行、DSSR、交互作用，不能仅用 Stock 与 Controlled 之比归因。
4. 若 `p1_s` 或 `p1_s_auto` 明显快于 `p0_r`，以后将保留两个 B0：固定串行的研究基线用于机制消融，以及强配置端到端基线用于最终系统比较。最终选择不应在测试集上调参。
5. 如果不同 DSSR 模式的迭代数和标签数量显著不同，E1/E2 需要在**相同 DSSR 模式**下比较学习排序，避免把松弛策略差异误判为标签选择收益。

## 7. 结果文件

```text
e05_results/
├── metadata.json          # 实例 SHA、源代码树 SHA、构建环境、CPU 亲和性
├── measurements.csv       # 交错运行的独立正式计时
├── diagnostics.json       # 诊断计数器和各轮 DSSR 记录
├── e05_analysis.json      # 归因分析，运行 e05_analyze.py 后出现
├── configs/               # 7 组 benchmark + 4 组 diagnostic 配置
├── logs/                  # 每次执行的 stdout/stderr 和 cmake 日志
├── execution_cwd/         # benchmark 空工作目录
└── diagnostic_cwd/        # 独立的诊断 CSV 工作目录
```

请优先同步 `metadata.json`、`measurements.csv`、`diagnostics.json`、`e05_analysis.json`，以及有报错的日志；如要审计全部细节再同步 `configs/` 和 `logs/`。

## 8. 状态与边界

本实验包的 Python 脚本已使用**模拟 PathWyse 程序**完成集成测试：配置生成、随机化调度、日志解析、输出正确性核对、CSV 收集及交互作用分析均通过。**尚未在你的实际 Linux 服务器执行**，所以不能预先报告 E0.5 真实并行或 DSSR 加速结果。
