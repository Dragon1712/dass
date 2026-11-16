# VOC-LS E1.1：独立实例训练／验证／测试与严格配对评估

基于你在 GitHub `Dragon1712/dass` 的 E1 结果（2026-11-15，commit `26e1f811...`）开发。运行平台是**离线 Linux 服务器**，所有 Python 脚本使用标准库；**不需服务器连接 GitHub，不需要再次修改 C++ 源码。** 仍使用现有的 `~/code/dass/voc_ls_e1/{e1_run.py,e1_train.py}` 与已编译的 E1 PathWyse。

## 为什么现在做 E1.1，而不是直接上 VOC？

已上传的 E1 Smoke 仅一个实例、3 次重复，同一实例训练与评估：

| 条件 | B0 mean | B1 mean | B1 重排数 | B0/B1 扩展差 | B0/B1 DSSR 轮数 | 95% Bootstrap 差异区间 |
|---|---:|---:|---:|---:|---:|---|
| `p0_r` 串行 restricted | 1.5016 s | 1.3004 s | 83,361 | 0 | 15/15 | [−0.183, +0.422] s |
| `p1_s` 并行 standard | 0.7823 s | 0.7827 s | 67,917 | 0 | 9/9 | [−0.104, +0.109] s |

排序在执行，但没有减少搜索量，也没有稳健净加速的证据。训练代理标签为 `accepted/(attempted+1)`，并非反事实计算价值；其中 `node_frac/degree` 在同一节点内固定，`direction` 在方向独立的线程内固定。**需要用未见过的实例做一次严格 B0/B1 对比。**

此外，E1 目录中的 `e1_split` 已有 **31 训练候选 + 14 保留测试实例**，但没有独立验证集；E1.1 将这 31 个再分为 **25 真训练 + 6 验证**，不碰原来的 14 个测试文件。

注意：smoke 轨迹对 `A-n54-k7-149` 记录的 SHA256 为 `29384774...`，新 split manifest 对同名文件记录 `4f175f96...`，说明它们是**不同内容版本**。E1.1 严格核对目前文件 SHA 是否仍与 split manifest 一致，避免静默数据漂移。

## 0. 安装

把 zip 上传到服务器的 `~/code/dass/`，解压成：

```text
~/code/dass/
├── pathwyse/                 # 现有 E1 补丁已编译的源码和 bin/pathwyse
├── voc_ls_e1/               # 已有 e1_run.py, e1_train.py
├── e1_split/                # 已有 31/14, split_manifest.json
└── voc_ls_e1_heldout/       # 本包，3 个脚本和 README
```

```bash
cd ~/code/dass/voc_ls_e1_heldout
python3 -m py_compile e1_heldout.py e1_heldout_analyze.py
# 测试阶段不用 PathWyse 真实数据；需要已有 ~/code/dass/voc_ls_e1
python3 selftest_heldout.py
```

脚本默认路径就是你当前服务器的路径，所以下列命令不必重复指定 `--repo`、`--e1-dir`、`--split-dir`。所有输出写入**新的** `~/code/dass/e1_heldout_v1`，不会覆盖旧的 E1 Smoke。

## 1. Freeze：冻结 25/6/14 与当前文件内容

```bash
python3 e1_heldout.py prepare --work ~/code/dass/e1_heldout_v1
```

成功将生成 `dataset.json` 与 `train.txt`、`validation.txt`、`test.txt`。如果报 `FROZEN SPLIT MISMATCH`，说明原 `e1_split` 文件与现有 `.sppcc` 内容不同，**不要跳过**，应先确认文件内容。确实需要更新数据时，用新的路径重新划分：

```bash
cd ~/code/dass/voc_ls_e1
python3 e1_split.py \
  --repo ~/code/dass/pathwyse \
  --out ~/code/dass/e1_split_v2 \
  --train-fraction 0.7 --seed 20261115

cd ~/code/dass/voc_ls_e1_heldout
python3 e1_heldout.py prepare \
  --split-dir ~/code/dass/e1_split_v2 \
  --work ~/code/dass/e1_heldout_v2
```

新一次实验的后续命令必须一直使用相同 `--work`。不能把 v1/v2 结果合并为一个实验。

## 2. Collect：只在 25 个训练实例上采集 B0 轨迹

为了先确认大型实例是否可处理，可以每次执行几个实例并多次重跑命令：

```bash
cd ~/code/dass/voc_ls_e1_heldout
python3 -u e1_heldout.py collect \
  --work ~/code/dass/e1_heldout_v1 \
  --timeout 600 --max-new 5 \
  2>&1 | tee ~/code/dass/e1_heldout_collect.log
```

此命令每次最多**新启动 5 个实例**；已成功的自动跳过。需要跑完所有剩余实例时：

```bash
python3 -u e1_heldout.py collect \
  --work ~/code/dass/e1_heldout_v1 --timeout 600 \
  2>&1 | tee ~/code/dass/e1_heldout_collect_all.log
```

如果某实例中途超时/错误，程序会记录失败并保留目录；调整运行条件后再用 `--retry-failed`，旧失败目录会转入 `failed_attempts/`，**不会静默覆盖**。

如果困难实例确实需要更长的时间，可例如将 `--timeout 600` 改为 `--timeout 1800`，但应在完整实验方法中说明时间上限、超时计数及处理规则。不能将失败实例静默剔除。

## 3. Train：训练独立实例的 B1 排序模型

```bash
python3 -u e1_heldout.py train \
  --work ~/code/dass/e1_heldout_v1 \
  --ridge 0.01 --max-rows 8000 \
  2>&1 | tee ~/code/dass/e1_heldout_train.log
```

该命令必须等全部 25 个训练实例完成（或明确以 `--allow-incomplete-train` 仅做**不可发表的 pilot**）。每个实例的 `p0_r` 和 `p1_s` 轨迹分别从原有 PathWyse B0 收集，训练会根据实例进行等权加权，而不是把大实例的所有标签当作独立实例。

生成：

```text
~/code/dass/e1_heldout_v1/model/rank_linear.v1
~/code/dass/e1_heldout_v1/model/rank_linear.v1.json
```

训练阶段会检查每个 trace 的 SHA，模型 metadata 记录训练实例 SHA，正式评估拒绝训练与验证/测试内容重叠。**不得复用先前单实例 Smoke 模型**作正式泛化评估。

## 4. Validation：先跑 6 个验证实例

```bash
python3 -u e1_heldout.py eval \
  --work ~/code/dass/e1_heldout_v1 \
  --subset validation \
  --timeout 600 --repeats 10 --warmups 2 --top-k 8 \
  2>&1 | tee ~/code/dass/e1_heldout_validation.log

python3 e1_heldout_analyze.py \
  --work ~/code/dass/e1_heldout_v1 \
  --subset validation --repeats 10
```

验证报告检查 B1 是否缩小累计扩展数、迭代数和完整运行时间；若没有改善，应如实记录再决定是否跑最终测试、或在一个**新实验版本**中更换监督/策略。**不能用最终的 test 14 反复挑 K、模型或训练超参。** 当前 K 在 prepare 阶段预注册为 8。

## 5. Test：最后固定模型，在原 14 个保留测试实例上评估

只有验证分析覆盖全部 6 个实例、且未发生正确性错误，脚本才允许执行 test：

```bash
python3 -u e1_heldout.py eval \
  --work ~/code/dass/e1_heldout_v1 \
  --subset test --timeout 600 --repeats 10 --warmups 2 --top-k 8 \
  2>&1 | tee ~/code/dass/e1_heldout_test.log

python3 e1_heldout_analyze.py \
  --work ~/code/dass/e1_heldout_v1 \
  --subset test --repeats 10
```

测试阶段每实例 2 profiles × 2 policies × 10 repeats = **40 次正式测量**，14 个实例共 **560 次正式测量**，另含预热。若实例很难，整体计算量可能较大；建议先在验证阶段检查求解成本。脚本按实例运行、自动保存阶段日志、支持从失败后继续。

## 6. 产出与解释

```text
e1_heldout_v1/
├── dataset.json                   # 25/6/14 内容 SHA 冻结
├── code_attestation.json          # 源码 + 可执行文件 + SHARED LIB SHA256
├── train.txt, validation.txt, test.txt
├── train_manifest.json
├── training_coverage.json
├── model/rank_linear.v1(.json)
├── collect/<instance_sha>.../     # B0 训练轨迹
├── eval/validation/<instance...>/ # 配对结果和日志
├── eval/test/<instance...>/
├── failed_attempts/              # 重试时保存失败尝试
├── phase_status/
├── driver_logs/
├── validation_report.json, validation_per_instance.csv
└── test_report.json, test_per_instance.csv
```

`e1_heldout_analyze.py` 不会把重复轮次当成独立测试实例；主汇总给**每个测试实例同等权重**。输出指标：平均/中位 B0−B1 墙钟时间差、实例级 bootstrap 区间、几何平均加速比、实例胜负数、扩展数与 DSSR 轮数差异、B1 额外候选选择器开销、真实重排数量。

**边界：** 若测试实例失败、超时或缺失，将输出 `INCOMPLETE_DO_NOT_CLAIM_GENERALIZATION` 并逐一列出缺失实例，分析命令返回代码 3。不能用其余容易实例的结果直接声称整体加速。若基于测试集决定改模型，下一版本必须使用新的冻结测试集做最终外部验证。

## 7. E1.1 Go/No-Go 建议

- **Go（继续研究动态 VOC）**：所有实例保持 Optimal，部分实例有可靠的**净时间收益**，且收益在实例/搜索阶段之间存在可预测差异。这才是“何时值得调用模型”的有效起点。
- **Revise B1（改排序监督）**：重排显著，但扩展数/DSSR 轮次几乎不变，B1 的选择器成本又增加时，应首先改为预测更接近**后续支配工作量、搜索树收缩或剩余求解成本**的目标；考虑更有信息量的候选状态特征，而不是直接上 VOC。
- **Stop/redirect**：如果改进后的 B1 在足够多的独立实例上仍始终无净收益，则需要重新审视“学习标签排序是有效动作空间”的前提，尝试标签池策略、DSSR轮次调度或其它动作，而不是继续堆模型。

## 8. 验证状态

该工具已使用**模拟求解器**运行完整闭环：冻结数据、训练/验证/测试隔离、断点续跑、模型训练、完整 B0/B1 配对、实例级分析；并测试了未先验证便访问测试集会被拒绝、数据文件被修改会被拒绝。**没有在你真实 Linux PathWyse 上运行。** 请按以上命令真实执行后将 `validation_report.json` 和 `test_report.json` 等上传 GitHub，再做科研判断。
