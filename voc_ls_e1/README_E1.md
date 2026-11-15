# VOC-LS E1 — B0 PathWyse Node Scheduler vs B1 Always Learned (offline source patch)

**研究仓库**：`https://github.com/Dragon1712/dass`（2026-11-15 已核查的 `pathwyse/src/core/algorithms/dynamic_programming/PW_default/`）。

**核心规则**：B0 和 B1 使用**同一编译二进制、同一输入实例、同一配置文件**，唯一的在线策略差异是 `E1_POLICY=baseline` 或 `E1_POLICY=learned`；B1 通过 `E1_MODEL` 加载真实训练得到的线性排序器，不使用预知的最优答案。B0 不进行预测、不打开 trace。所有性能实验都禁止日志式训练记录。

本包**无 GitHub 网络依赖、无需服务器 Git 元数据、无 Python 第三方依赖**。服务器的 CMake、G++、PathWyse 源码需要就绪。

## 0. 文件清单

| 文件 | 用途 |
|---|---|
| `e1_apply.py` | 基于 C++ 原始源码锚点生成可逆补丁；备份 4 个 C++ 文件，失败即停止 |
| `e1_split.py` | 根据实例文件内容 SHA256 生成训练/测试不重叠划分 |
| `e1_run.py` | B0 训练轨迹采集；相同配置 B0/B1 严格配对运行 |
| `e1_train.py` | 训练 7 维特征轻量 **线性回归排名器**，输出 C++ 可加载的纯文本权重 |
| `e1_analyze.py` | 每实例、每配置、每轮配对耗时差、标签扩展差、额外评分耗时及置信区间 |
| `selftest_e1.py` | 无真实 PathWyse 的全流程模拟测试，含 SHA 泄漏负面测试 |

### 为什么只改 4 个 C++ 文件？

- `LM_default.h/.cpp`：新增 `getCandidateLearned` 与模型加载，候选仍保存在原始 `forward_candidates/backward_candidates` 列表，不建独立的长期优先队列，从而避免标签指针失效、重复扩展和失去同步的问题。
- `PW_default.h/.cpp`：为 B0 轨迹采集记录扩展尝试和即时成功插入数；正常计时关闭轨迹记录，原有 `dominates`、`insert`、`join`、DSSR 判定不改变。
- **不修改 CMake**：新增方法直接放在原来已经编译的 C++ 文件中。

### B1 是怎样的 Always Learned？

B0 完全使用原先的 `getCandidateNode()`。B1 使用**相同的 node 选择顺序**，但在当前节点的每个允许搜索方向取前 `K=8` 个合法 open label，以模型预测分数挑选下一个扩展标签。B1 **每次取候选都调用模型**，无动态 VOC 门控；其他候选仍保留在原池中，不改变支配、剪枝和最优判定。模型为 7 维线性评分（客观值、关键资源消耗、unreachable 数、visited 数、节点度、相对节点编号、方向），训练标签为 B0 历史扩展的 `accepted/(attempted+1)`（预测即时成功插入率的代理目标）。**这只是可验证的 Always Learned 原型，不是原论文中的学习型 dominant-state 模型，也不是 VOC 模型**。

这是一个最小机制检验：如果这种廉价模型在少数实例上有正净收益、或者不同轮次收益有明显差别，再考虑更强模型与动态 VOC；否则重新设计预测目标，而非直接宣称 VOC 不可行。

## 1. Linux 服务器安装和编译

把 `voc_ls_e1` 解压到 `~/code/dass/`，确认已有源码位于 `~/code/dass/pathwyse`（如果是 `pathwyse-main`，相应修改命令）。

```bash
cd ~/code/dass/voc_ls_e1
python3 selftest_e1.py
python3 e1_apply.py --repo ~/code/dass/pathwyse --check
python3 e1_apply.py --repo ~/code/dass/pathwyse --apply

cd ~/code/dass/pathwyse
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j 8

E1_POLICY=baseline ./bin/pathwyse instances/spprclib/A-n54-k7-149.sppcc
```

**输出日志必须出现** `E1_STATS policy=baseline candidate_calls=...`；若未出现，说明未编译新代码。

`e1_apply.py` 在 `pathwyse/.voc_ls_e1_backup/` 自动创建原始四文件备份及 SHA256 清单；`--revert` 仅在新文件 SHA 与补丁清单完全一致时还原，避免误覆盖你之后手工修改的代码。原有源码如果与核查版不符，会先在 `--check` 报错，而不是盲目打补丁。

## 2. 科研正式数据：先划分多实例训练和测试集

**当前用户仓库仅内置一个 SPPRCLIB `.sppcc` 文件，不能凭一个实例宣称跨实例泛化。** 请通过你现有 Windows 本机同步方式添加更多合法 SPPRCLIB 实例到服务器，然后执行：

```bash
cd ~/code/dass/voc_ls_e1
python3 e1_split.py \
  --repo ~/code/dass/pathwyse \
  --glob 'instances/spprclib/*.sppcc' \
  --out ~/code/dass/e1_split \
  --train-fraction 0.7 --seed 20261115
```

输出 `train.txt`、`test.txt`、`split_manifest.json`。按实例文件 SHA256 去重，训练/测试交叉由评测程序再次核对。

## 3. 只从 B0 的训练实例采集监督数据（训练时不计时比较）

```bash
cd ~/code/dass/voc_ls_e1
python3 -u e1_run.py collect \
  --repo ~/code/dass/pathwyse \
  --instances-file ~/code/dass/e1_split/train.txt \
  --out ~/code/dass/e1_train_traces \
  --profiles p0_r,p1_s \
  --skip-build --timeout 300 \
  2>&1 | tee ~/code/dass/e1_collect.log
```

此处 B0 轨迹采集依赖 `E1_TRACE_FILE`，记录每次扩展前特征及扩展后即时插入结果。`p0_r` 和 `p1_s` 均采集 B0 轨迹，使模型在 restricted 和 standard 上都有训练覆盖。不同 DSSR 的轨迹来自同一实例，在训练时按实例汇总权重，不把它们当作独立训练实例。

输出 `e1_train_traces/train_manifest.json` 和 `traces/*.csv`。模型训练**只读取这个 manifest**，不会读取测试实例的 trace。

## 4. 离线训练 B1 模型

```bash
python3 e1_train.py \
  --manifest ~/code/dass/e1_train_traces/train_manifest.json \
  --model ~/code/dass/e1_model/rank_linear.v1 \
  --ridge 0.01 \
  --max-rows-per-instance 50000
```

输出：
- `e1_model/rank_linear.v1`：7 维权重、归一化均值与标准差、截距，C++ 运行时直接读取。
- `e1_model/rank_linear.v1.json`：训练实例 SHA256、采样数、代理标签定义和权重等元数据。

模型只使用扩展之前即可获得的字段。模型预测结果是“候选扩展的即时有效插入率”代理，不是反事实长期计算价值；这使 E1 的性能试验与后续 VOC 模型研究区分开。

## 5. B0/B1 严格配对：Research + Strong 两套配置

```bash
python3 -u e1_run.py eval \
  --repo ~/code/dass/pathwyse \
  --instances-file ~/code/dass/e1_split/test.txt \
  --out ~/code/dass/e1_results \
  --model ~/code/dass/e1_model/rank_linear.v1 \
  --profiles p0_r,p1_s \
  --repeats 10 --warmups 2 \
  --top-k 8 --timeout 300 \
  --skip-build \
  2>&1 | tee ~/code/dass/e1_run.log

python3 e1_analyze.py --out ~/code/dass/e1_results \
  | tee ~/code/dass/e1_analyze.log
```

- 同一实例、同一 profile、同一轮次，执行 B0 与 B1 两种 policy，并在该轮随机化运行顺序。
- `p0_r` 是**串行 DSSR restricted**，用于机制归因。`p1_s` 是**并行 DSSR standard**，用于评估强基线性能；不能将两个不同 profile 直接相减声称学习加速。
- 正式测量没有 `E1_TRACE_FILE`，模型加载、特征、排序开销都计入整个进程 `wall_s`；另外保存 `PWDefault Time` 和 `E1_STATS selector_ns`。
- 每个配对都验证 `Solution Status=Optimal` 与最优目标值一致，所有测试实例的 SHA256 与训练实例不重叠，错误立即停止。
- `E1_STATS reordered_choices` 表示 B1 相对原 node policy 实际改变了多少次候选选择。如果该值始终为 0，说明学习模型没有产生真正不同的排序决策，即使运行时间有小变化也不能归因于搜索顺序改善。

主要输出：

```text
e1_results/
├── metadata.json
├── configs/p0_r.set
├── configs/p1_s.set
├── measurements.csv           # 每实例 × 每 profile × 每轮 × B0/B1
├── e1_analysis.json           # 配对结果、跨实例等权统计
└── logs/                     # 失败与完整诊断时使用
```

## 6. 目前只有一个 SPPRCLIB 文件时的非科研 smoke 测试

这只能验证工程正确性，**不得报告为模型泛化或论文有效性**。复用现有实例作训练和测试：

```bash
cd ~/code/dass/voc_ls_e1
printf '%s\n' 'instances/spprclib/A-n54-k7-149.sppcc' > smoke_same_instance.txt

python3 e1_run.py collect \
  --repo ~/code/dass/pathwyse \
  --instances-file smoke_same_instance.txt \
  --profiles p0_r,p1_s \
  --out ~/code/dass/e1_smoke_train \
  --skip-build --timeout 300

python3 e1_train.py \
  --manifest ~/code/dass/e1_smoke_train/train_manifest.json \
  --model ~/code/dass/e1_smoke_model/rank_linear.v1

python3 e1_run.py eval \
  --repo ~/code/dass/pathwyse \
  --instances-file smoke_same_instance.txt \
  --out ~/code/dass/e1_smoke_eval \
  --model ~/code/dass/e1_smoke_model/rank_linear.v1 \
  --profiles p0_r,p1_s --repeats 3 --warmups 1 --skip-build \
  --allow-overlap-smoke

python3 e1_analyze.py --out ~/code/dass/e1_smoke_eval
```

没有 `--allow-overlap-smoke` 时程序会明确拒绝训练和测试实例内容相同（即使文件名不同）。即便允许 smoke，分析结果也会标注 **NON-PUBLISHABLE**。

## 7. Go/No-Go 判据与下一步实验

1. **Correctness（强制）**：所有 B0/B1 配对返回 Optimal 且每实例最优目标值一致；若 B1 返回不同目标值，先排查候选链表和关闭标签的不变量，不继续 VOC。
2. **Model actually used（强制）**：B1 的 `scored_labels >= candidate_calls`，并统计 `reordered_choices > 0` 的查询比例；没有实际改变顺序不能称为学习型搜索收益。
3. **Search work（研究信号）**：比较 B0/B1 跨 DSSR 迭代的总候选扩展数。仅减少最终一轮扩展数不足以认定完整搜索更有效。
4. **Net end-to-end cost（研究信号）**：以进程 `wall_s` 为主，`solver_s` 和 `selector_ns` 为成本分解；B1 少扩展标签但模型评分开销较大仍可能更慢。
5. **Generalization（论文必要）**：按独立实例等权汇总，报告性能退化实例比例、跨实例 Bootstrap 区间；单个实例的 10 轮重复不能代表其他规模或约束强度。
6. **Next move**：只有发现模型净收益具有实例/阶段依赖性，才为后续 VOC 反事实动作价值学习提供直接实验证据；如果这个 7 维弱监督排序不产生收益，应先改良 B1 监督目标或更接近原论文策略，而非立刻否决 VOC 机制。

### 重要安全边界

- 代码不会替换 dominance/join/DSSR 的数学规则，也没有额外跳过标签；但改变扩展顺序可能暴露已有算法中依赖排序的实现细节，**必须以真实 PathWyse 的结果一致性验收**。
- 训练数据中的 `accepted` 是该扩展即时成功插入的后验标签，只作为模型监督；它不在推理输入特征中。
- `top-k` 控制的是每次候选决策评估多少 open 标签，**不是 VOC 的动作窗口 K**；两者研究含义不同。
- E1 是 *weakly supervised Always Learned prototype*，不能将这份实现宣称为 2026 年论文分类器的逐项复现。
- C++ 选择器已经通过独立小程序的 C++20 编译与行为检查，Python 端通过模拟求解器全流程及防泄漏测试；由于这里无法访问用户服务器，尚未在真实 PathWyse 完成整库编译与实际实例验收，用户需要按第 1 节在服务器执行一次真实编译和 smoke。
