# DataHub-G1 Auto Pipeline

自动化数据处理: Azure Blob 轮询 → Docker 内处理 → 结果直写 Blob。

---

## 快速开始

### 1. 启动 Docker 容器 (首次或重启后)

```bash
cd /home/eai/DataHub-G1
sg docker -c "./dk.sh daemon"
```

容器名 `ros_data_processor`，设为 `--restart unless-stopped`，VM 重启后自动恢复。

### 2. 启动调度器

```bash
cd /home/eai/DataHub-G1/src

# 后台持续运行 (推荐)
nohup python3 auto_scheduler.py run > /dev/null 2>&1 &

# 或: 只跑一轮就退出 (调试用)
python3 auto_scheduler.py run --once

# 或: 指定结果目录日期
python3 auto_scheduler.py run --result-date 20260413
```

调度器会每 5 分钟轮询一次, 自动发现新 episode 并处理。  
日志写入 `logs/auto_scheduler.log`。

### 3. 查看状态

```bash
python3 auto_scheduler.py status
```

输出各组合的源数据总数、已处理、已跳过、待处理。

### 4. 查看日志

```bash
# 实时跟踪
tail -f ../logs/auto_scheduler.log

# 看最后 50 行
tail -50 ../logs/auto_scheduler.log
```

### 5. 停止调度器

```bash
# 找到 PID
ps aux | grep auto_scheduler

# 停止
kill <PID>
```

---

## 跳过列表管理

对于不需要处理的 episode (历史遗留、数据错误等), 可以永久标记跳过:

```bash
# 标记跳过 (支持范围和逗号)
python3 auto_scheduler.py skip ruicheng_force '45-63,82,97,124' --reason '历史已处理'

# 查看跳过列表
python3 auto_scheduler.py skiplist                    # 所有组合
python3 auto_scheduler.py skiplist ruicheng_force     # 指定组合

# 从跳过列表移除 (允许重新处理)
python3 auto_scheduler.py unskip ruicheng_force '132,133'
```

连续失败 3 次的 episode 会被自动加入跳过列表, 附带失败原因。

---

## 三种任务组合

| 组合 | task/job | combo_name | force_visualization |
|------|----------|------------|---------------------|
| A | 11/14 | `ruicheng_force` | true |
| B | 14/18 | `cogact_supermarket` | false |
| C | 15/19 | `cogact_pickplace` | false |

配置在 `src/combo_config.py`。
