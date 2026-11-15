# VOC-LS E0.5 v2：高精度计时 + 自动配置消融修复

适用仓库：`Dragon1712/dass`，Linux 服务器源码路径：`~/code/dass/pathwyse`。服务器 **无需 GitHub 访问、无需 .git 目录、无需联网安装 Python 包**，Python 3.10+ 标准库即可。

## 这次具体修复了什么？

**Fix 1 — 真实的自动配置对照。** 旧 `e05_run.py` 构造 `p1_s_auto` 时，错误地将 `algo/default/autoconfig` 写死为 `0`，导致 `p1_s_auto.set` 与 `p1_s.set` 完全一致。新版以 `params.get("autoconfig", "0")` 生成该项，因此 `p1_s_auto=1`、`p1_s=0`。

**Fix 2 — 防止无效消融。** 配置生成后自动验证：四个主组之间每一对只变化一个指定因子；`p1_s_auto` 相对 `p1_s` 只变化 `algo/default/autoconfig`；`p0_r_collect` 相对 `p0_r` 只变化 `data_collection/level`。自动配置两份文件 SHA256 不同是强制条件。不满足则在开始真实实验前报错。

**Fix 3 — 外部计时。** 原版 `subprocess.run(timeout=300)` 在 POSIX 使用带超时的轮询等待，可能导致约 50 ms 的计时阶梯。新版使用 `subprocess.Popen(...).wait()` **不带 timeout 的阻塞式操作系统等待**，超时由独立 watchdog 线程控制，超时则杀死整个进程组。外部耗时来自 `time.perf_counter_ns()`，并保存 `wall_s`（9 位小数）和 `wall_ns`（整数）。

> 纳秒计数表示记录精度，不代表操作系统计时具有纳秒级准确度。外部墙钟时间仍会受 CPU 调度、负载、缓存和进程启动的影响。正式对照应同时查看 `solver_s`，必要时固定 CPU 亲和性。

**Fix 4 — 最小补跑。** 新增 `--profiles p1_s,p1_s_auto`，只运行自动配置对照。分析程序自动识别没有四个主组的补跑文件，**只报告自动配置配对差**，不会凭空算并行 × DSSR 交互效应。

## 1. 部署

先将压缩包解压到服务器的 `~/code/dass/` 下，形成以下结构：

```text
~/code/dass/
├── pathwyse/                   # 已有 PathWyse C++ 源码及 bin/pathwyse
├── e05_results/               # 旧结果，保持不动
└── voc_ls_e05_fixed/
    ├── e05_run.py
    ├── e05_analyze.py
    ├── selftest_e05.py
    └── README_E05_FIXED.md
```

## 2. 先验证本机修复

```bash
cd ~/code/dass/voc_ls_e05_fixed
python3 selftest_e05.py

python3 e05_run.py \
  --repo ~/code/dass/pathwyse \
  --out ~/code/dass/e05_fixed_preflight \
  --profiles p1_s,p1_s_auto \
  --validate-only

grep -n 'algo/default/autoconfig' \
  ~/code/dass/e05_fixed_preflight/configs/p1_s*.set
```

正确的配置核验必须看到：

```text
p1_s.set: ... autoconfig = 0
p1_s_auto.set: ... autoconfig = 1
```

并且出现 `CONFIG VALIDATION PASSED`。此步骤不会构建或运行 PathWyse。

## 3. 先补跑真正有效的自动配置对照（推荐）

```bash
cd ~/code/dass/voc_ls_e05_fixed
set -o pipefail
python3 -u e05_run.py \
  --repo ~/code/dass/pathwyse \
  --out ~/code/dass/e05_auto_fixed \
  --profiles p1_s,p1_s_auto \
  --repeats 20 \
  --warmups 2 \
  --timeout 300 \
  --skip-build \
  --no-diagnostics \
  2>&1 | tee ~/code/dass/e05_auto_fixed.log

python3 e05_analyze.py --out ~/code/dass/e05_auto_fixed \
  | tee ~/code/dass/e05_auto_fixed_analysis.log
```

共 **2 个配置 × 20 次正式测量 = 40 次**，另有 4 次预热。自动分析中的 `auto_under_parallel_standard` 是配对差：

`T(p1_s_auto) - T(p1_s)`，正值表示自动配置更慢，负值表示自动配置更快。

注意：在本次循环图 + DSSR standard 设置下，PathWyse 自动配置可能选择与显式配置相同的 `node` 候选策略。因此效果接近零也完全可能；必须在配置确实不同的前提下才有解释价值。

## 4. 建议重测四组高精度主实验（独立运行，不覆盖旧结果）

```bash
python3 -u e05_run.py \
  --repo ~/code/dass/pathwyse \
  --out ~/code/dass/e05_timing_fixed \
  --profiles p0_r,p1_r,p0_s,p1_s \
  --no-extras \
  --repeats 10 \
  --warmups 2 \
  --timeout 300 \
  --skip-build \
  --no-diagnostics \
  2>&1 | tee ~/code/dass/e05_timing_fixed.log

python3 e05_analyze.py --out ~/code/dass/e05_timing_fixed \
  | tee ~/code/dass/e05_timing_fixed_analysis.log
```

共 **4 个配置 × 10 次正式测量 = 40 次**，外加预热。此步骤确认旧版并行节省约 0.451 s 的结论在新外部计时器下能否复现。**旧版与新版计时方法不同，不要把两轮 CSV 直接拼接当作同一实验。**

## 5. 运行环境建议

仅当你了解 CPU 拓扑时才使用 `--cpus`：

```bash
lscpu -e=CPU,CORE,SOCKET,NODE
```

从当前进程允许使用的 CPU 中，选出两个不同物理 CORE 的逻辑 CPU，如 `--cpus 2,4`（仅为示例）。选择后对所有配置使用相同列表。**不要给并行组只分配一个逻辑 CPU**。如果服务器负载变化明显，建议暂停其他重 CPU 任务，检查频率与是否出现热降频。

## 6. 输出文件与验收标准

新版产生：

```text
<out>/
├── metadata.json        # 源码树/二进制/配置 SHA256、计时方法、配置对照校验
├── configs/*.set        # 自动生成的参数文件
├── measurements.csv     # 每次正式运行，含 wall_s/wall_ns/solver_s
├── e05_analysis.json    # 均值、中位数、CV、配对差和 bootstrap CI
├── logs/*.log
└── execution_cwd/
```

自动配置补跑验收标准：

1. `metadata.json` 的 `config_validation.all_valid = true`。
2. `configs/p1_s.set` 的 `autoconfig = 0`，`configs/p1_s_auto.set` 的 `autoconfig = 1`；两者 SHA256 必须不同。
3. 40/40 次测量状态均为 `Optimal`、退出码 0、无超时；目标值均为 `-12492`（这是之前的 PathWyse 对照结果，而不是独立理论证书）。
4. `e05_analysis.json` 存在 `auto_under_parallel_standard`，并记录配对均值差、置信区间。
5. `wall_s` 不再因 Python 带超时等待而呈现固定约 50ms 递增阶梯；不要期望完全没有操作系统噪声。

如果默认 PathWyse 二进制不存在，请去掉 `--skip-build`，新版会使用本机 `cmake` 编译。**不要使用旧目录或旧输出 `e05_results`**，以免新旧计时方法混淆。

## 7. 本地已完成的验证与边界

- 通过 Python 语法检查。
- 通过自动配置配置差异验证，包括**故意让两个配置相同应拒绝运行**的负面测试。
- 通过无超时的阻塞等待和 watchdog 超时终止测试。
- 通过模拟求解器双组补跑及自动分析测试。
- 通过模拟求解器四组因子统计测试。

上述测试没有在你的真实 Linux PathWyse 环境运行，**不产生新的真实算法结果**。真正的 E0.5 修正结果需要你在服务器上运行并回传。
