# VOC-LS / E0 (Dragon1712/dass)

固定源仓库：`https://github.com/Dragon1712/dass`；当前审核基线提交：`d5a786dbbe2351d65564bf1245b54c72edfe2164`；**PathWyse 工程根目录在 `dass/pathwyse-main/`**。

## 目的 / 为什么这样做

1. **正确性先于速度**：首先用作者的 toy 数据（预期 Obj=-10435、Tour=0 2 1 4、Consumption=9）检查精确求解，另用独立枚举器对 toy 核对；SPPRCLIB 仅做求解器交叉一致性检查，不是独立最优性证明。
2. **区分源码自带配置与科研受控基线**：`stock` 使用现有 `pathwyse.set` 的原始设置，做原始兼容性/正确性验收；`controlled` 固定 PWDefault、双向、无并行、autoconfig=0、node、DSSR restricted、NG off，作为后续 B0 同配置基线。两组运行配置不同，**绝不直接把 stock vs controlled 的耗时差写成算法加速**。
3. **统计成本与性能成本分开**：`controlled` 的低输出版本用于时延统计，`controlled_diagnostic` 使用 `verbosity=3` 每实例执行 1 次获取算法统计。详细日志时间不与主计时混用。
4. **冻结源码和输入**：保存 git SHA、每个参数文件和实例的 SHA256、编译器、平台、每次运行退出码和日志，确保将来可以复现。
5. **不用 GPU**：PathWyse 是 C++ CPU 求解器；用单线程减小并发噪声，避免将 GPU 资源配置误认为有收益。

## 推荐用法：服务器可访问 GitHub

```bash
sudo apt-get update
sudo apt-get install -y git build-essential cmake libomp-dev python3
# 将本实验包放到 Linux 服务器上，进入本目录
bash e0_bootstrap.sh
# 调整重复次数或求解器限时（默认 3 次、每次 120 秒）
E0_REPEATS=5 E0_TIMEOUT=300 bash e0_bootstrap.sh
```

脚本会克隆 `Dragon1712/dass`，自动选择内部 `pathwyse-main` 工程根目录编译并执行。

## 仓库已经 clone 的情况（避免重复下载）

```bash
git clone https://github.com/Dragon1712/dass.git
cd dass
# 以下命令在代码仓库根目录执行；请替换脚本所在绝对路径
python3 /path/to/e0_run.py \
  --repo "$PWD" \
  --out "$PWD/e0_results" \
  --profiles both --repeats 3 --timeout 300
```

也可以 `--repo "$PWD/pathwyse-main"`，脚本可自动辨认这两种路径。

**如果你已更新仓库代码导致 SHA 变化**：先核对 `git rev-parse HEAD`，确认新版本确实用于实验，再加 `--expected-commit ''` 允许版本变化；结果仍将记录实际 SHA。

## 不依赖自动脚本的最小手工复现

```bash
git clone https://github.com/Dragon1712/dass.git
cd dass/pathwyse-main
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j 8
./bin/pathwyse instances/toy.txt
# 预期 Solution Status=Optimal, Obj=-10435, Tour="0 2 1 4", Consumption=9
./bin/pathwyse instances/spprclib/A-n54-k7-149.sppcc
```

注意：从 `dass` 而不是 `dass/pathwyse-main` 运行 `cmake -S .` 会找不到正确的根级 CMakeLists。配置文件按 `-param` 显式指定，避免执行目录不同导致默认参数被误用。

## 配置与对照

| 设置 | stock | controlled | 为什么 |
|---|---|---|---|
| `algo/default/autoconfig` | 仓库原始值 1 | 0 | 关闭自动覆盖候选选择 |
| `algo/default/parallel` | 原始值 1 | 0 | 消除调度噪声 |
| `algo/default/candidate/type` | 原始值 node（可能被 auto 覆盖） | node | 固定 B0 传统策略 |
| `algo/default/dssr` | 原始值 standard | restricted | 准备后续论文对照的 DSSR-R 场景；不作为变量改进效果证据 |
| `algo/default/ng` | off | off | 不引入另一层松弛机制 |
| `data_collection/level` | 原始值 -1 | 0 | 记录轻量统计， -1 才是关 |
| `verbosity` | 原始值 2 | 1 (诊断=3) | 减少主计时的输出噪声 |

其余参数继承仓库 `pathwyse.set`，特别是 `use_visited=1` 与 `compare_unreachables=1`，不改变精确性约束。`algo/default/timelimit=0` 为求解器内部不主动提前结束，但实验驱动仍有超时保护，超时不视为最优。

## 数据与验收

当前仓库仅内置 1 个 SPPRCLIB (`A-n54-k7-149.sppcc`)；toy 只检验通路。可添加更多 `.sppcc` 后使用 `--all-spprclib`；也可以多次使用 `--instance /abs/path/xxx` 指定实例。

- 各 selected 实例返回 `Solution Status: Optimal`、进程返回码 0、没有 timeout。
- toy 的最优目标为 `-10435`；额外查看路径和消耗是否与官方示例一致。
- 重复运行同配置目标值一致；不同配置目标值一致（绝对容差 `1e-6`）。
- 输出 `runs.csv`（每次运行）、`details.json`（额外计数器）、`metadata.json`（环境、哈希、聚合与判定）、`logs/`（所有程序输出和编译日志）。

注意：`ndominated` 代表**被支配标签数量**，并非 dominance *comparisons*，不能拿它充当支配比较次数。要做后者，需要后续在真实支配比较调用位置添加无副作用计数器。小实例的进程启动开销可能占比较大，不要用 toy 的毫秒级 wall time 评估学习策略加速。

## 运行结果状态

本实验包已做语法及模拟输出测试，但由于此会话计算容器无法解析 `github.com`，目前**没有完成真实源码构建、toy 程序求解或 SPPRCLIB 测试**，必须在可访问仓库的 Linux 服务器上执行上述命令后才能宣布 E0 通过。
